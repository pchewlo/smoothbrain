"""Readers for every source. Each turns its native format into one scrubbed markdown
file per conversation/document under raw/, and records it in state/items.json.

Sources: Claude Code sessions (+ subagents), Codex sessions, Claude.ai exports
(conversations, Claude Design chats, Projects, memory), ChatGPT exports, Wispr Flow
meetings + notes (via the claude.ai connector), Notion (API token), Claude Code
memory files, and notes saved into inbox/ through the MCP server.
"""

from __future__ import annotations

import glob
import json
import os
import re
import zipfile
from pathlib import Path

from .common import (CFG, HOME, INBOX, OWNER, RAW, ROOT, SOURCES_FILE, Doc, Items, iso_date, iso_time,
                     load_json, now_iso, one_line, save_json, sha, slug)
from .scrub import scrub

SOURCE_DIRS = {
    "Claude Code": "claude-code", "Codex": "codex", "Claude": "claude-ai", "Claude Design": "claude-design",
    "Claude Project": "claude-projects", "Claude Memory": "claude-ai", "ChatGPT": "chatgpt",
    "Wispr Meeting": "wispr", "Wispr Note": "wispr", "Notion": "notion", "Memory": "memory", "Note": "notes",
}
SYSTEM_TAGS = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|ide_selection|ide_opened_file)>[\s\S]*?</\1>")
MAX_PASTE = 6000


class Sources:
    """state/sources.json: cursors (file mtimes, processed zips, last Wispr/Notion sync)."""

    def __init__(self):
        self.data = load_json(SOURCES_FILE, {})

    def section(self, name):
        return self.data.setdefault(name, {})

    def save(self):
        save_json(SOURCES_FILE, self.data)


def put(items: Items, *, item_id: str, source: str, title: str, date: str, updated: str,
        markdown: str, hint: str = "", counts: dict | None = None) -> bool:
    """Scrub, write raw file if changed, update the index. Returns True if new/changed."""
    clean, n = scrub(markdown)
    title, _ = scrub(title or "")
    h = sha(clean)
    rec = items.get(item_id)
    if rec and rec.get("hash") == h:
        return False
    date = date or iso_date(updated) or "0000-00-00"
    if rec and rec.get("path"):
        rel = rec["path"]
    else:
        short = sha(item_id)[:8]
        rel = f"raw/{SOURCE_DIRS.get(source, 'other')}/{date[:7]}/{date} {slug(title)} [{short}].md"
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(clean)
    rec = rec or {"id": item_id, "first_seen": now_iso()}
    rec.update({
        "source": source, "title": title.strip() or "Untitled", "date": date, "updated": updated or date,
        "path": rel, "hash": h, "chars": len(clean), "hint": hint, "redactions": n,
        "counts": counts or {},
    })
    items.data[item_id] = rec
    return True


# ---------------------------------------------------------------- Claude Code

def _tool_line(name: str, inp: dict) -> str:
    inp = inp or {}
    for key in ("description", "command", "file_path", "path", "pattern", "url", "query", "prompt", "skill", "action"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return f"{name}: {v}"
    short = name.replace("mcp__claude_ai_", "").replace("mcp__", "")
    return f"{short}: {one_line(json.dumps(inp, ensure_ascii=False), 140)}" if inp else short


def _cc_user_text(content) -> str:
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(parts)
    text = SYSTEM_TAGS.sub("", text)
    m = re.search(r"<command-name>(.*?)</command-name>[\s\S]*?(?:<command-args>(.*?)</command-args>)?", text)
    if m:
        return f"{m.group(1).strip()} {(m.group(2) or '').strip()}".strip()
    return text.strip()


def _cc_records(path: str):
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _cc_subagent(path: str) -> str:
    meta = load_json(Path(path[: -len(".jsonl")] + ".meta.json"), {})
    first, last = "", ""
    for o in _cc_records(path):
        if o.get("type") == "user" and not first:
            first = _cc_user_text(o["message"].get("content"))
        elif o.get("type") == "assistant":
            t = "\n".join(b.get("text", "") for b in o["message"].get("content", []) if b.get("type") == "text").strip()
            if t:
                last = t
    head = f"{meta.get('agentType') or 'agent'}: {meta.get('description') or one_line(first, 80)}"
    body = ""
    if first:
        body += "**Task:** " + (first[:1500] + ("…" if len(first) > 1500 else "")) + "\n\n"
    if last:
        body += "**Report:** " + (last[:4000] + ("…" if len(last) > 4000 else ""))
    return f"#### Subagent · {head}\n\n{body}\n"


def ingest_claude_code(items: Items, cursors: dict, log) -> int:
    changed = 0
    for path in sorted(glob.glob(str(HOME / ".claude/projects/*/*.jsonl"))):
        sid = Path(path).stem
        subs = sorted(glob.glob(str(Path(path).with_suffix("")) + "/subagents/**/*.jsonl", recursive=True))
        stamp = [os.path.getmtime(p) for p in [path] + subs] + [os.path.getsize(path)]
        key = path
        if cursors.get(key) == stamp and items.get(f"claude-code:{sid}"):
            continue
        title, cwd, first_ts, last_ts = "", "", "", ""
        doc_parts = []
        doc = Doc("", {})
        for o in _cc_records(path):
            t = o.get("type")
            ts = o.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if t == "ai-title":
                title = o.get("aiTitle") or title
            elif t == "custom-title":
                title = o.get("customTitle") or title
            if o.get("isSidechain"):
                continue
            cwd = cwd or o.get("cwd") or ""
            if t == "user":
                msg = o.get("message") or {}
                if o.get("isMeta") or o.get("isCompactSummary"):
                    if o.get("isCompactSummary"):
                        doc.tool("[context compacted]")
                    continue
                text = _cc_user_text(msg.get("content"))
                if text:
                    doc.say(OWNER, text, iso_time(ts))
            elif t == "attachment" and (o.get("attachment") or {}).get("type") == "queued_command":
                p = o["attachment"].get("prompt")
                text = _cc_user_text(p if isinstance(p, (str, list)) else "")
                if text:
                    doc.say(OWNER, text, iso_time(ts) + " (mid-turn)")
            elif t == "assistant":
                for b in (o.get("message") or {}).get("content", []) or []:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        doc.say("Claude", b["text"])
                    elif b.get("type") == "tool_use":
                        doc.tool(_tool_line(b.get("name", "tool"), b.get("input")))
        for sp in subs:
            doc_parts.append(_cc_subagent(sp))
        cursors[key] = stamp
        if doc.speaker_turns["user"] == 0 and doc.speaker_turns["other"] == 0:
            continue
        if not title:
            first_user = next((p for p in doc.parts if p.startswith(f"### {OWNER}")), "")
            title = one_line(first_user.split("\n", 2)[-1], 70)
        doc.title = title
        doc.meta = {"source": "Claude Code", "session": sid, "cwd": cwd, "started": iso_time(first_ts),
                    "last activity": iso_time(last_ts), "subagents": len(subs) or ""}
        if doc_parts:
            doc.section("Subagents", "\n".join(doc_parts))
        if put(items, item_id=f"claude-code:{sid}", source="Claude Code", title=title, date=iso_date(first_ts),
               updated=last_ts, markdown=doc.render(), hint=cwd, counts=dict(doc.speaker_turns)):
            changed += 1
    log(f"claude code: {changed} new/changed")
    return changed


# ---------------------------------------------------------------- Codex

def ingest_codex(items: Items, cursors: dict, log) -> int:
    changed = 0
    names = {}
    idx = HOME / ".codex/session_index.jsonl"
    if idx.exists():
        for line in idx.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
                names[r["id"]] = r
            except (ValueError, KeyError):
                continue
    files = sorted(glob.glob(str(HOME / ".codex/sessions/**/*.jsonl"), recursive=True)
                   + glob.glob(str(HOME / ".codex/archived_sessions/**/*.jsonl"), recursive=True))
    for path in files:
        stamp = [os.path.getmtime(path), os.path.getsize(path)]
        if cursors.get(path) == stamp:
            continue
        meta, doc = None, Doc("", {})
        first_ts = last_ts = ""
        for o in _cc_records(path):
            p = o.get("payload") or {}
            ts = o.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if o.get("type") == "session_meta":
                meta = p
            elif o.get("type") == "response_item":
                if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                    text = "\n".join(b.get("text", "") for b in (p.get("content") or []) if isinstance(b, dict)).strip()
                    if not text:
                        continue
                    if p["role"] == "user":
                        if text.startswith("<") or text.startswith("# AGENTS.md"):
                            continue
                        doc.say(OWNER, text, iso_time(ts))
                    else:
                        doc.say("Codex", text)
                elif p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                    args = p.get("arguments") or p.get("input") or p.get("action") or ""
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"input": args}
                    if isinstance(args, dict) and isinstance(args.get("command"), list):
                        args["command"] = " ".join(map(str, args["command"]))
                    doc.tool(_tool_line(p.get("name") or "shell", args if isinstance(args, dict) else {}))
        cursors[path] = stamp
        if not meta or doc.speaker_turns["user"] == 0:
            continue
        sid = Path(path).stem[-36:]  # one item per rollout file; resumed sessions share meta ids
        r = names.get(meta.get("id"), {}) or names.get(sid, {})
        title = (r.get("thread_name") or "").strip()
        if not title:
            first_user = next((p for p in doc.parts if p.startswith(f"### {OWNER}")), "")
            title = one_line(first_user.split("\n", 2)[-1], 70)
        doc.title = title
        doc.meta = {"source": "Codex", "session": sid, "cwd": meta.get("cwd", ""), "started": iso_time(first_ts),
                    "last activity": iso_time(last_ts)}
        if put(items, item_id=f"codex:{sid}", source="Codex", title=title, date=iso_date(first_ts),
               updated=last_ts, markdown=doc.render(), hint=meta.get("cwd", ""), counts=dict(doc.speaker_turns)):
            changed += 1
    log(f"codex: {changed} new/changed")
    return changed


# ---------------------------------------------------------------- Exports (Claude.ai, ChatGPT)

def export_zips() -> list[str]:
    out = []
    for d in CFG["exports"]["dirs"]:
        d = d.replace("{root}", str(ROOT))
        out += glob.glob(os.path.join(os.path.expanduser(d), "*.zip"))
    return sorted(set(out), key=os.path.getmtime)


def _newer(items: Items, item_id: str, updated: str) -> bool:
    rec = items.get(item_id)
    return not rec or (updated or "") > (rec.get("updated") or "")


def _claude_message(m: dict, doc: Doc):
    speaker = OWNER if m.get("sender") == "human" else "Claude"
    when = iso_time(m.get("created_at"))
    blocks = m.get("content") or []
    texts = []
    if blocks:
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                texts.append(b["text"])
            elif b.get("type") == "tool_use":
                if texts:
                    doc.say(speaker, "\n\n".join(texts), when)
                    texts, when = [], ""
                doc.tool(_tool_line(b.get("name", "tool"), b.get("input") or {}))
    elif (m.get("text") or "").strip():
        texts.append(m["text"])
    for a in (m.get("attachments") or []) + (m.get("files") or []):
        name = a.get("file_name") or "file"
        ext = (a.get("extracted_content") or "").strip()
        if ext:
            cut = ext[:MAX_PASTE] + (f"\n…[{len(ext) - MAX_PASTE} more chars]" if len(ext) > MAX_PASTE else "")
            texts.append(f"[Attached: {name}]\n\n```\n{cut}\n```")
        else:
            texts.append(f"[Attached: {name}]")
    if texts:
        doc.say(speaker, "\n\n".join(texts), when)


def _ingest_claude_conversations(items, convs, log) -> int:
    changed = 0
    for c in convs:
        cid = f"claude:{c['uuid']}"
        if not _newer(items, cid, c.get("updated_at")):
            continue
        doc = Doc((c.get("name") or "").strip(), {})
        for m in c.get("chat_messages") or []:
            _claude_message(m, doc)
        if doc.empty:
            continue
        summary = re.sub(r"^\W*conversation overview\W*", "", c.get("summary") or "", flags=re.I).strip()
        if summary:
            doc.parts.insert(0, f"## Claude's own summary\n\n{summary}\n\n## Conversation\n")
        doc.meta = {"source": "Claude.ai", "conversation": c["uuid"], "created": iso_time(c.get("created_at")),
                    "updated": iso_time(c.get("updated_at")), "link": f"https://claude.ai/chat/{c['uuid']}"}
        title = doc.title or one_line(summary, 70)
        doc.title = title
        if put(items, item_id=cid, source="Claude", title=title, date=iso_date(c.get("created_at")),
               updated=c.get("updated_at") or "", markdown=doc.render(), counts=dict(doc.speaker_turns)):
            changed += 1
    return changed


def _ingest_design(items, chats) -> int:
    changed = 0
    for d in chats:
        cid = f"design:{d['uuid']}"
        if not _newer(items, cid, d.get("updated_at")):
            continue
        doc = Doc("", {})
        for m in d.get("messages") or []:
            body = m.get("content") or {}
            text = body.get("content") if isinstance(body, dict) else body
            if isinstance(text, str):
                doc.say(OWNER if m.get("role") == "user" else "Claude", text, iso_time(m.get("created_at")))
        if doc.empty:
            continue
        proj = d.get("project")
        proj = proj.get("name") if isinstance(proj, dict) else proj
        title = (d.get("title") or "").strip()
        doc.title = f"{title} ({proj})" if proj else title
        doc.meta = {"source": "Claude Design", "chat": d["uuid"], "project": proj or "",
                    "created": iso_time(d.get("created_at")), "updated": iso_time(d.get("updated_at"))}
        if put(items, item_id=cid, source="Claude Design", title=doc.title, date=iso_date(d.get("created_at")),
               updated=d.get("updated_at") or "", markdown=doc.render(), counts=dict(doc.speaker_turns)):
            changed += 1
    return changed


def _ingest_projects(items, projects) -> int:
    changed = 0
    for p in projects:
        if p.get("is_starter_project"):
            continue
        pid = f"claude-project:{p['uuid']}"
        if not _newer(items, pid, p.get("updated_at")):
            continue
        doc = Doc(f"Claude Project: {p.get('name') or 'Untitled'}", {
            "source": "Claude.ai Project", "project": p["uuid"], "created": iso_time(p.get("created_at")),
            "updated": iso_time(p.get("updated_at")), "knowledge files": len(p.get("docs") or [])})
        if (p.get("description") or "").strip():
            doc.section("Description", p["description"])
        if (p.get("prompt_template") or "").strip():
            doc.section("Project instructions", p["prompt_template"])
        for d in p.get("docs") or []:
            doc.section(f"Knowledge: {d.get('filename') or 'doc'}", d.get("content") or "")
        if put(items, item_id=pid, source="Claude Project", title=doc.title, date=iso_date(p.get("created_at")),
               updated=p.get("updated_at") or "", markdown=doc.render()):
            changed += 1
    return changed


def _ingest_claude_memory(items, mem, zip_mtime) -> int:
    if isinstance(mem, list):
        mem = mem[0] if mem else {}
    doc = Doc("Claude.ai memory", {"source": "Claude.ai memory export"})
    if mem.get("conversations_memory"):
        doc.section("Memory from chats", str(mem["conversations_memory"]))
    for pid, text in (mem.get("project_memories") or {}).items():
        doc.section(f"Project memory {pid}", str(text))
    if doc.empty:
        return 0
    updated = iso_time(zip_mtime)
    if not _newer(items, "claude-memory:account", updated):
        return 0
    return int(put(items, item_id="claude-memory:account", source="Claude Memory", title="Claude.ai memory",
                   date=iso_date(zip_mtime), updated=updated, markdown=doc.render()))


def _chatgpt_text(msg: dict) -> str:
    c = msg.get("content") or {}
    parts = c.get("parts") or []
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            if p.get("content_type") == "image_asset_pointer":
                out.append("[image]")
            elif isinstance(p.get("text"), str):
                out.append(p["text"])
    if not out and isinstance(c.get("text"), str):
        out.append(c["text"])
    return "\n".join(out).strip()


def _ingest_chatgpt(items, convs) -> int:
    changed = 0
    for c in convs:
        cid_native = c.get("conversation_id") or c.get("id")
        cid = f"chatgpt:{cid_native}"
        updated = iso_time(c.get("update_time"))
        if not _newer(items, cid, updated):
            continue
        mapping = c.get("mapping") or {}
        node = c.get("current_node")
        chain = []
        while node and node in mapping:
            chain.append(mapping[node])
            node = mapping[node].get("parent")
        doc = Doc((c.get("title") or "").strip(), {})
        for n in reversed(chain):
            m = n.get("message")
            if not m:
                continue
            role = (m.get("author") or {}).get("role")
            if (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                continue
            text = _chatgpt_text(m)
            if role == "user":
                doc.say(OWNER, text, iso_time(m.get("create_time")))
            elif role == "assistant":
                if (m.get("content") or {}).get("content_type") in ("code", "execution_output"):
                    doc.tool(f"code: {one_line(text, 140)}")
                else:
                    doc.say("ChatGPT", text)
            elif role == "tool":
                doc.tool(f"{(m.get('author') or {}).get('name') or 'tool'}")
        if doc.empty:
            continue
        doc.meta = {"source": "ChatGPT", "conversation": cid_native, "created": iso_time(c.get("create_time")),
                    "updated": updated, "model": c.get("default_model_slug") or "",
                    "link": f"https://chatgpt.com/c/{cid_native}"}
        if put(items, item_id=cid, source="ChatGPT", title=doc.title, date=iso_date(c.get("create_time")),
               updated=updated, markdown=doc.render(), counts=dict(doc.speaker_turns)):
            changed += 1
    return changed


def ingest_exports(items: Items, cursors: dict, log, force: bool = False) -> int:
    changed = 0
    for zpath in export_zips():
        stamp = [os.path.getmtime(zpath), os.path.getsize(zpath)]
        if cursors.get(zpath) == stamp and not force:
            continue
        try:
            z = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile:
            cursors[zpath] = stamp
            continue
        names = z.namelist()
        conv_name = next((n for n in names if n.endswith("conversations.json")), None)
        design = [n for n in names if re.search(r"(^|/)design_chats/[^/]+\.json$", n)]
        projects = [n for n in names if re.search(r"(^|/)projects/[^/]+\.json$", n)]
        mem = next((n for n in names if n.endswith("memories.json")), None)
        if not (conv_name or design or projects):
            cursors[zpath] = stamp  # not an AI export
            continue
        n0 = changed
        kind = "?"
        if conv_name:
            with z.open(conv_name) as f:
                convs = json.load(f)
            if convs and isinstance(convs, list) and "mapping" in convs[0]:
                kind = "ChatGPT"
                changed += _ingest_chatgpt(items, convs)
            elif convs and isinstance(convs, list) and "chat_messages" in convs[0]:
                kind = "Claude"
                changed += _ingest_claude_conversations(items, convs, log)
            del convs
        if design:
            kind += "+design"
            changed += _ingest_design(items, [json.loads(z.read(n)) for n in design])
        if projects:
            kind += "+projects"
            changed += _ingest_projects(items, [json.loads(z.read(n)) for n in projects])
        if mem:
            try:
                changed += _ingest_claude_memory(items, json.loads(z.read(mem)), stamp[0])
            except (ValueError, AttributeError):
                pass
        cursors[zpath] = stamp
        log(f"export {Path(zpath).name} ({kind}): {changed - n0} new/changed")
    return changed


# ---------------------------------------------------------------- Wispr Flow (local app data)
# Wispr Flow has no public API for meetings, but the desktop app keeps everything
# locally. These are internal, undocumented formats and may change with app updates.

WISPR_DIR = HOME / "Library/Application Support/Wispr Flow"


def _wispr_speakers(raw: str | None) -> dict[str, str]:
    """speaker label → name, from Meetings.speakerMap."""
    try:
        m = json.loads(raw or "{}")
    except ValueError:
        return {}
    people = {k: (v or {}).get("name") for k, v in (m.get("people") or {}).items()}
    out = {}
    for label, a in (m.get("assignments") or {}).items():
        a = a or {}
        who = a.get("user") or a.get("consensus") or a.get("dom") or a.get("llm")
        if who and people.get(who):
            out[str(label)] = people[who]
    return out


def ingest_wispr(items: Items, cursors: dict, log) -> int:
    """Reads Wispr Flow's own local store: flow.sqlite (titles, summaries, notes,
    speaker names) and meetings/<id>/refined.ndjson (or live.ndjson) transcripts."""
    import sqlite3
    db = WISPR_DIR / "flow.sqlite"
    if not db.exists():
        log("wispr: app data not found, skipped")
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    changed = 0
    rows = con.execute("select * from Meetings where isDeleted = 0 and coalesce(isTourDemo, 0) = 0").fetchall()
    for m in rows:
        mid = m["id"]
        folder = WISPR_DIR / "meetings" / mid
        tfile = next((folder / n for n in ("refined.ndjson", "live.ndjson") if (folder / n).exists()), None)
        stamp = [m["modifiedAt"], os.path.getmtime(tfile) if tfile else 0]
        if cursors.get(mid) == stamp and items.get(f"wispr:{mid}"):
            continue
        names = _wispr_speakers(m["speakerMap"])
        lines, last = [], None
        if tfile:
            for raw in tfile.read_text(errors="ignore").splitlines():
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                sp = o.get("speaker") or {}
                who = sp.get("name") or names.get(str(sp.get("id"))) or f"Speaker {sp.get('id', '?')}"
                text = (o.get("text") or "").strip()
                if not text:
                    continue
                if who == last and lines:
                    lines[-1] += " " + text
                else:
                    lines.append(f"**{who}** [{o.get('timestamp', '')}]: {text}")
                    last = who
        fix = lambda s: re.sub(r"<@speaker:(\d+)>", lambda x: names.get(x.group(1), f"Speaker {x.group(1)}"), s or "")
        try:
            participants = ", ".join(json.loads(m["participantNames"] or "[]"))
        except ValueError:
            participants = ""
        participants = participants or ", ".join(sorted(set(names.values())))
        start = m["calendarOccurrenceStartAtUtc"] or m["createdAt"]
        title = (m["title"] or "").strip() or "Untitled meeting"
        doc = Doc(title, {"source": "Wispr Flow meeting", "meeting": mid, "start": iso_time(start),
                          "attendees": participants,
                          "link": f"https://notes.wisprflow.ai/shared/{m['shareSlug']}" if m["shareSlug"] else ""})
        if (m["summary"] or "").strip():
            doc.section("Summary (Wispr)", fix(m["summary"]))
        if (m["notes"] or "").strip():
            doc.section("Notes", fix(m["notes"]))
        if lines:
            doc.section("Transcript", "\n\n".join(lines))
        cursors[mid] = stamp
        if doc.empty:
            continue
        if put(items, item_id=f"wispr:{mid}", source="Wispr Meeting", title=title, date=iso_date(start),
               updated=str(m["modifiedAt"]), markdown=doc.render(), hint=participants, counts={"user": 1, "other": 1}):
            changed += 1
    notes = con.execute("select * from Notes where isDeleted = 0").fetchall()
    for n in notes:
        doc = Doc((n["title"] or "").strip() or "Wispr note", {"source": "Wispr Flow note", "modified": iso_time(n["modifiedAt"])})
        doc.raw(n["content"] or n["contentPreview"] or "")
        if put(items, item_id=f"wispr-note:{n['id']}", source="Wispr Note", title=doc.title,
               date=iso_date(n["createdAt"]), updated=str(n["modifiedAt"]), markdown=doc.render()):
            changed += 1
    con.close()
    log(f"wispr: {len(rows)} meetings, {len(notes)} notes; {changed} new/changed")
    return changed


# ---------------------------------------------------------------- Notion (claude.ai connector)

def _row_md(row: dict) -> str:
    name = row.get("Name") or row.get("Title") or next((v for k, v in row.items() if k not in ("url",) and isinstance(v, str) and v), "Untitled")
    props = [f"- {k}: {v}" for k, v in row.items()
             if k not in ("url",) and not k.endswith(":is_datetime") and v not in ("", None, "[]", 0) and k != "Name"]
    return f"### {name}\n\n" + "\n".join(props) + f"\n- page: {row.get('url', '')}\n"


def ingest_notion(items: Items, cursors: dict, log, crawl_first: bool = True) -> int:
    from .notion_mcp import cached_pages, crawl
    if crawl_first:
        crawl(log)
    changed = 0
    db_rows = set()
    pages = list(cached_pages())
    for pid, d in pages:
        if d.get("kind") == "database":
            db_rows |= {nid for nid in (d.get("row_hashes") or {})}
    for pid, d in pages:
        title = re.sub(r"\s+", " ", d.get("title") or "").strip() or "Untitled"
        loc = " / ".join(x for x in (d.get("team"), d.get("path")) if x)
        edited = d.get("edited") or ""
        if d.get("kind") == "database":
            rows = d.get("rows") or []
            doc = Doc(f"{title} (Notion database)", {"source": "Notion database", "location": loc, "link": d.get("url", ""),
                                                     "rows": len(rows), "as of": iso_time(edited)})
            for r in rows:
                doc.raw(_row_md(r))
            item_id = f"notion-db:{pid}"
        else:
            body = (d.get("body") or "").strip()
            if pid in db_rows and len(body) < 40:
                continue  # an empty database row: already in its database's item
            doc = Doc(title, {"source": "Notion", "location": loc, "link": d.get("url", ""), "edited": iso_time(edited)})
            if d.get("props") and d["props"] not in ("{}", ""):
                doc.section("Properties", d["props"])
            doc.raw(body)
            item_id = f"notion:{pid}"
        if put(items, item_id=item_id, source="Notion", title=doc.title, date=iso_date(edited), updated=edited,
               markdown=doc.render(), hint=loc):
            changed += 1
    log(f"notion: {len(pages)} cached pages, {changed} new/changed")
    return changed


# ---------------------------------------------------------------- Claude Code memory + inbox

def ingest_memory(items: Items, cursors: dict, log) -> int:
    changed = 0
    for path in sorted(glob.glob(str(HOME / ".claude/projects/*/memory/*.md"))):
        if path.endswith("MEMORY.md"):
            continue
        text = Path(path).read_text(errors="ignore")
        m = re.search(r"^name:\s*(.+)$", text, re.M)
        d = re.search(r"^description:\s*(.+)$", text, re.M)
        scope = Path(path).parent.parent.name
        name = (m.group(1).strip() if m else Path(path).stem).strip("\"'")
        mtime = os.path.getmtime(path)
        doc = Doc(f"Memory: {name}", {"source": "Claude Code memory (read-only copy)", "file": path.replace(str(HOME), "~"),
                                      "description": d.group(1).strip().strip("\"'") if d else ""})
        doc.raw(text)
        if put(items, item_id=f"memory:{scope}/{Path(path).stem}", source="Memory", title=f"Memory: {name}",
               date=iso_date(mtime), updated=iso_time(mtime), markdown=doc.render(), hint=scope):
            changed += 1
    log(f"memory: {changed} new/changed")
    return changed


def ingest_inbox(items: Items, cursors: dict, log) -> int:
    changed = 0
    INBOX.mkdir(exist_ok=True)
    for path in sorted(INBOX.glob("*.md")):
        text = path.read_text(errors="ignore")
        m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
        date = m.group(1) if m else iso_date(os.path.getmtime(path))
        t = re.search(r"^# (.+)$", text, re.M)
        title = t.group(1).strip() if t else path.stem
        if put(items, item_id=f"note:{path.stem}", source="Note", title=title, date=date,
               updated=iso_time(os.path.getmtime(path)), markdown=text, counts={"user": 1, "other": 0}):
            changed += 1
        done = INBOX / "processed"
        done.mkdir(exist_ok=True)
        path.rename(done / path.name)
    log(f"inbox: {changed} notes")
    return changed


ALL = {
    "claude-code": ingest_claude_code,
    "codex": ingest_codex,
    "exports": ingest_exports,
    "wispr": ingest_wispr,
    "notion": ingest_notion,
    "memory": ingest_memory,
    "inbox": ingest_inbox,
}
