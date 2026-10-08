"""Notion through the claude.ai Notion connector, driven via `claude -p`
(see llm.mcp_calls). No Notion integration token needed: it sees whatever the
connector sees in your claude.ai account.

Crawl: every joined teamspace's top pages + private + shared pages, walked down
through child pages and databases. A database becomes one item listing all its
rows (properties included); a row's own page is fetched when the row is new or
its properties changed. Nightly runs refetch pages whose edit time moved (from
search/recent listings) and re-list every database; a full re-walk runs weekly.

Fetched pages are cached in .local/notion/ (gitignored); ingest_notion() turns the
cache into raw/ items.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .common import CFG, LOCAL, load_json, save_json, sha
from .llm import mcp_calls

N = "mcp__claude_ai_Notion__notion-"
CACHE = LOCAL / "notion"
STATE = CACHE / "state.json"
SKIP = {s.replace("-", "") for s in CFG["notion"]["skip"]}  # page/database ids to leave out
FULL_EVERY_DAYS = int(CFG["notion"]["full_every_days"])
BATCH, WORKERS = 6, 4

PAGE_RE = re.compile(r'<page url="(?:\{\{)?https://app\.notion\.com/p/([0-9a-f]{32})')
DB_RE = re.compile(r'<database url="(?:\{\{)?https://app\.notion\.com/p/([0-9a-f]{32})')
VIEW_RE = re.compile(r'<view url="(?:\{\{)?(view://[0-9a-f-]{36})(?:\}\})?">\s*(\{.*?\})\s*</view>', re.S)


def nid(s: str) -> str:
    m = re.search(r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})", s or "")
    return m.group(1).replace("-", "") if m else ""


def _calls(calls: list[dict]) -> list:
    """Run proxied MCP calls in parallel batches; returns parsed JSON (or None) per call."""
    batches = [calls[i:i + BATCH] for i in range(0, len(calls), BATCH)]
    out: list = []
    with ThreadPoolExecutor(WORKERS) as pool:
        for res in pool.map(mcp_calls, batches):
            for r in res:
                try:
                    out.append(json.loads(r) if r else None)
                except ValueError:
                    out.append(None)
    return out


def _listing(log) -> dict[str, dict]:
    """Every page/database the cheap listings expose → {id: {edited, title, team}}."""
    [teams] = _calls([{"tool": N + "get-teams", "args": {}}])
    joined = [t for t in (teams or {}).get("joinedTeams", []) if not t.get("in_trash")]
    calls = [{"tool": N + "search", "args": {"query": "", "teamspace_id": t["id"], "page_size": 50, "max_highlight_length": 0}} for t in joined]
    calls += [{"tool": N + "list-private-pages", "args": {"limit": 200}},
              {"tool": N + "list-shared-pages", "args": {"limit": 200}},
              {"tool": N + "list-recent-pages", "args": {"limit": 200}}]
    res = _calls(calls)
    seen: dict[str, dict] = {}
    for i, r in enumerate(res):
        team = joined[i]["name"].strip() if i < len(joined) else ("Private" if i == len(joined) else "")
        for x in (r or {}).get("results", []):
            pid = nid(x.get("id") or x.get("url") or "")
            if not pid or pid in SKIP:
                continue
            rec = seen.setdefault(pid, {})
            rec.update({k: v for k, v in {"edited": x.get("timestamp"), "title": x.get("title"), "kind": x.get("type"),
                                          "top": not x.get("path"), "team": team or rec.get("team")}.items() if v})
    log(f"  notion: {len(joined)} teamspaces, {len(seen)} pages in listings")
    return seen


def _parse_page(d: dict) -> dict:
    text = d.get("text") or ""
    kind = (d.get("metadata") or {}).get("type") or "page"
    anc = re.findall(r'<parent-(?:page|database|data-source)[^>]*title="([^"]*)"', text.split("</ancestor-path>")[0]) if "<ancestor-path>" in text else []
    out = {"kind": kind, "title": d.get("title") or "", "url": d.get("url") or "", "edited": d.get("page_last_edited_at") or "",
           "path": " / ".join(anc), "children": [], "dbs": [], "views": []}
    if kind == "database":
        for url, js in VIEW_RE.findall(text):
            try:
                v = json.loads(js.replace("{{", "").replace("}}", ""))
            except ValueError:
                v = {}
            out["views"].append({"url": url, "filtered": bool(v.get("advancedFilter") or v.get("filter")), "name": v.get("name", "")})
        out["body"] = ""
    else:
        content = text.split("<content>", 1)[-1].rsplit("</content>", 1)[0] if "<content>" in text else text
        out["children"] = PAGE_RE.findall(content)
        out["dbs"] = DB_RE.findall(content)
        props = re.search(r"<properties>\s*(.*?)\s*</properties>", text, re.S)
        out["props"] = props.group(1) if props else ""
        body = re.sub(r"<(iconMetadata|ancestor-path)>[\s\S]*?</\1>", "", content)
        out["body"] = body.strip()
    return out


def _db_rows(view_url: str) -> list[dict]:
    rows, cursor = [], None
    for _ in range(100):
        data = {"mode": "view", "view_url": view_url, "page_size": 100}
        if cursor:
            data["start_cursor"] = cursor
        [r] = _calls([{"tool": N + "query-data-sources", "args": {"data": data}}])
        if not r:
            break
        rows += r.get("results") or []
        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")
    return rows


def crawl(log, full: bool | None = None) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    st = load_json(STATE, {"pages": {}, "last_full": ""})
    pages: dict = st["pages"]
    if full is None:
        last = st.get("last_full") or "1970-01-01T00:00:00+00:00"
        full = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days >= FULL_EVERY_DAYS
    listing = _listing(log)
    queue: list[str] = []
    for pid, info in listing.items():
        known = pages.get(pid)
        if full or not known or (info.get("edited") and info["edited"][:16] > (known.get("edited") or "")[:16]):
            queue.append(pid)
        if known and info.get("team") and not known.get("team"):
            known["team"] = info["team"]
    if not full:  # databases are always re-listed (rows carry no edit time)
        queue += [p for p, v in pages.items() if v.get("kind") == "database" and p not in queue]
    done: set[str] = set()
    fetched = 0
    while queue:
        batch = [p for p in dict.fromkeys(queue) if p not in done and p not in SKIP][:48]
        queue = [p for p in queue if p not in done and p not in batch]
        if not batch:
            break
        res = _calls([{"tool": N + "fetch", "args": {"id": p}} for p in batch])
        for pid, d in zip(batch, res):
            done.add(pid)
            if not d:
                continue
            fetched += 1
            p = _parse_page(d)
            prev = pages.get(pid, {})
            team = prev.get("team") or (listing.get(pid) or {}).get("team") or ""
            rec = {"kind": p["kind"], "title": p["title"], "url": p["url"], "edited": p["edited"] or prev.get("edited", ""),
                   "path": p["path"], "team": team, "created": prev.get("created", "")}
            if p["kind"] == "database":
                views = [v for v in p["views"] if not v["filtered"]] or p["views"]
                rows = _db_rows(views[0]["url"]) if views else []
                old_rows = prev.get("row_hashes") or {}
                rec["row_hashes"] = {}
                changed_rows = False
                for row in rows:
                    rid = nid(row.get("url", ""))
                    if not rid:
                        continue
                    h = sha(json.dumps(row, sort_keys=True))
                    rec["row_hashes"][rid] = h
                    pages.setdefault(rid, {}).setdefault("team", team)
                    if old_rows.get(rid) != h:
                        changed_rows = True
                    if full or old_rows.get(rid) != h:
                        queue.append(rid)
                if changed_rows or not rec["edited"]:
                    rec["edited"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                save_json(CACHE / f"{pid}.json", {**rec, "rows": rows})
            else:
                save_json(CACHE / f"{pid}.json", {**rec, "props": p.get("props", ""), "body": p["body"]})
                for c in p["children"] + p["dbs"]:
                    pages.setdefault(c, {}).setdefault("team", team)
                    if c not in done and (full or c not in st["pages"] or not (CACHE / f"{c}.json").exists()):
                        queue.append(c)
            pages[pid] = {**prev, **rec}
        save_json(STATE, st)
        log(f"  notion: fetched {fetched}, {len(queue)} queued")
    if full:
        st["last_full"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(STATE, st)
    return {"fetched": fetched, "known": len(pages), "full": full}


def cached_pages():
    for f in CACHE.glob("*.json"):
        if f.name == "state.json":
            continue
        d = load_json(f, None)
        if d:
            yield f.stem, d
