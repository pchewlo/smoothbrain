"""The summary tree (after Victor Taelin's OptChat): item lines roll up into days,
weeks, months and a top overview, and separately into project-months and projects.
Every node is a markdown file under tree/ that links up, and down to its children,
ending at the raw files. A node's summary is only regenerated when its inputs change.

Node ids (used by the MCP zoom tool) are paths under tree/ without .md:
  overview · months/2026-09 · weeks/2026-W39 · days/2026-09-28 · projects/work · projects/work/2026-09
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from .common import CFG, OWNER, STATE, TREE, Items, load_json, save_json, sha
from .llm import COST, ROLLUP_MODEL, TOP_MODEL, UsageLimitError, ask
from .scrub import scrub

NODES_FILE = STATE / "tree.json"
TIMELESS = {"Memory"}  # reference copies, not activity: projects only
WORKERS = int(CFG["workers"]["rollup"])

ROLLUP_SYSTEM = f"""You maintain a hierarchical summary of {OWNER}'s personal archive of AI conversations, meetings and notes. You are given the entries under one node of the tree (for example all conversation lines in a week). Write that node's summary so {OWNER} or an AI agent can tell what happened there and decide whether to look deeper.

Rules: plain English, no filler, no praise, no headings. Group by project: one short paragraph per project, starting with the project name and a colon, most significant first. Keep the specifics: decisions and why, numbers, prices, dates, names of people, companies and tools, outputs produced, open questions. Drop trivia. Do not invent anything not in the entries."""


def _iso_week(d: str) -> str:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def _week_range(wk: str) -> str:
    y, w = wk.split("-W")
    mon = date.fromisocalendar(int(y), int(w), 1)
    sun = mon + timedelta(days=6)
    return f"{mon:%-d %b} – {sun:%-d %b %Y}"


def pslug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "other"


def _month_name(m: str) -> str:
    return date.fromisoformat(m + "-01").strftime("%B %Y")


def _line(rec: dict, rel_prefix: str, with_project=True) -> str:
    title = rec.get("ai_title") or rec.get("title") or "Untitled"
    proj = f"{rec.get('project') or '?'} · " if with_project else ""
    summary = rec.get("summary") or "(not summarised yet)"
    return (f"- {proj}[{title}]({rel_prefix}{rec['path'].replace(' ', '%20')}) · {rec['source']} · {rec['date']}\n"
            f"  {summary}\n  `id: {rec['id']}`")


def _plain(rec: dict) -> str:
    return f"[{rec['date']} · {rec.get('project') or '?'} · {rec['source']}] {rec.get('ai_title') or rec.get('title')}: {rec.get('summary') or ''}"


class Builder:
    def __init__(self, log):
        self.log = log
        self.cache = load_json(NODES_FILE, {})
        self.used: set[str] = set()
        self.calls = 0

    def summary(self, node: str, kind: str, inputs: str, max_chars: int, model=ROLLUP_MODEL) -> str:
        self.used.add(node)
        h = sha(f"{model}|{max_chars}|{inputs}")
        cached = self.cache.get(node)
        if cached and cached.get("hash") == h:
            return cached["summary"]
        prompt = (f"Node: {kind}\nWrite the summary in at most {max_chars} characters.\n\nEntries:\n{inputs[:350_000]}")
        text = scrub(ask(prompt, system=ROLLUP_SYSTEM, model=model, timeout=900))[0]
        self.cache[node] = {"hash": h, "summary": text}
        self.calls += 1
        return text

    def parallel(self, jobs: list[tuple]):
        """jobs: (node, kind, inputs, max_chars, model). Returns {node: summary}."""
        out = {}
        with ThreadPoolExecutor(WORKERS) as pool:
            futs = {pool.submit(self.summary, *j): j[0] for j in jobs}
            for f, node in futs.items():
                try:
                    out[node] = f.result()
                except UsageLimitError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  ! {node}: {str(exc)[:160]}")
                    out[node] = (self.cache.get(node) or {}).get("summary", "")
        save_json(NODES_FILE, self.cache)
        return out


def write(rel: str, text: str):
    path = TREE / (rel + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def build_tree(log):
    items = [r for r in Items() if r.get("date") and r["date"][:4].isdigit()]
    b = Builder(log)
    by_day, by_week, by_month = defaultdict(list), defaultdict(list), defaultdict(list)
    by_proj_month = defaultdict(list)
    for r in items:
        proj = r.get("project") or "Unsorted"
        by_proj_month[(proj, r["date"][:7])].append(r)
        if r["source"] in TIMELESS:
            continue
        by_day[r["date"]].append(r)
        by_week[_iso_week(r["date"])].append(r)
        by_month[r["date"][:7]].append(r)
    for groups in (by_day, by_week, by_month, by_proj_month):
        for v in groups.values():
            v.sort(key=lambda r: (r["date"], r.get("updated") or ""))

    # ---- level 1: busy days, weeks, project-months (independent, run in parallel)
    jobs = []
    for d, rs in by_day.items():
        if len(rs) >= 6:
            jobs.append((f"days/{d}", f"day {d}", "\n".join(map(_plain, rs)), 900, ROLLUP_MODEL))
    for wk, rs in by_week.items():
        jobs.append((f"weeks/{wk}", f"week {wk} ({_week_range(wk)})", "\n".join(map(_plain, rs)), 1600, ROLLUP_MODEL))
    for (p, m), rs in by_proj_month.items():
        if len(rs) >= 3:
            jobs.append((f"projects/{pslug(p)}/{m}", f"project {p}, {_month_name(m)}", "\n".join(map(_plain, rs)), 1400, ROLLUP_MODEL))
    log(f"tree: level 1, {len(jobs)} nodes")
    s = b.parallel(jobs)

    # ---- level 2: months (from weeks), projects (from project-months)
    weeks_of_month = defaultdict(set)
    for wk, rs in by_week.items():
        for r in rs:
            weeks_of_month[r["date"][:7]].add(wk)
    jobs = []
    for m in by_month:
        inputs = "\n\n".join(f"Week {wk} ({_week_range(wk)}):\n{s.get(f'weeks/{wk}', '')}" for wk in sorted(weeks_of_month[m]))
        jobs.append((f"months/{m}", f"month {_month_name(m)}", inputs, 2500, ROLLUP_MODEL))
    projects = defaultdict(list)
    for (p, m) in by_proj_month:
        projects[p].append(m)
    for p, months in projects.items():
        parts = []
        for m in sorted(months):
            node = f"projects/{pslug(p)}/{m}"
            body = s.get(node) or "\n".join(map(_plain, by_proj_month[(p, m)]))
            parts.append(f"{_month_name(m)} ({len(by_proj_month[(p, m)])} items):\n{body}")
        jobs.append((f"projects/{pslug(p)}", f"project {p}: its whole history, oldest to newest. Summarise what it is, "
                     "what has been done and decided over time, the current state, and open threads.", "\n\n".join(parts), 3500, TOP_MODEL))
    log(f"tree: level 2, {len(jobs)} nodes")
    s.update(b.parallel(jobs))

    # ---- level 3: overview
    recent = sorted(by_month)[-3:]
    inputs = "\n\n".join(f"PROJECT {p} ({sum(len(by_proj_month[(p, m)]) for m in ms)} items, {min(ms)} to {max(ms)}):\n{s.get(f'projects/{pslug(p)}', '')}"
                         for p, ms in sorted(projects.items(), key=lambda kv: -sum(len(by_proj_month[(kv[0], m)]) for m in kv[1])))
    inputs += "\n\n" + "\n\n".join(f"RECENT MONTH {_month_name(m)}:\n{s.get(f'months/{m}', '')}" for m in recent)
    overview = b.parallel([("overview", f"the top of the tree: who {OWNER} is as seen through this archive, every project and its "
                            f"current state, what they have been doing recently. Start with a 3-sentence orientation.", inputs, 6000, TOP_MODEL)])["overview"]

    # ---- write files
    month_weeks = {m: sorted(weeks_of_month[m]) for m in by_month}
    for d, rs in by_day.items():
        wk, m = _iso_week(d), d[:7]
        head = f"# {date.fromisoformat(d):%A %-d %B %Y}\n\nUp: [week {wk}](../weeks/{wk}.md) · [{_month_name(m)}](../months/{m}.md) · [overview](../overview.md)\n\n"
        summ = s.get(f"days/{d}", "")
        write(f"days/{d}", head + (summ + "\n\n" if summ else "") + f"## {len(rs)} items\n\n" + "\n".join(_line(r, "../../") for r in rs) + "\n")
    for wk, rs in by_week.items():
        days = sorted({r["date"] for r in rs})
        m = days[0][:7]
        body = "\n".join(f"- [{date.fromisoformat(d):%a %-d %b}](../days/{d}.md) · {len(by_day[d])} items: "
                         + "; ".join((r.get("ai_title") or r.get("title") or "")[:60] for r in by_day[d][:6])
                         + (" …" if len(by_day[d]) > 6 else "") for d in days)
        write(f"weeks/{wk}", f"# Week {wk} ({_week_range(wk)})\n\nUp: [{_month_name(m)}](../months/{m}.md) · [overview](../overview.md)\n\n"
              f"{s.get(f'weeks/{wk}', '')}\n\n## Days\n\n{body}\n")
    for m, rs in by_month.items():
        wks = "\n".join(f"- [Week {wk}](../weeks/{wk}.md) ({_week_range(wk)}) · {len(by_week[wk])} items" for wk in month_weeks[m])
        pc = defaultdict(int)
        for r in rs:
            pc[r.get("project") or "Unsorted"] += 1
        projs = "\n".join(f"- [{p}](../projects/{pslug(p)}/{m}.md) · {n}" for p, n in sorted(pc.items(), key=lambda kv: -kv[1]))
        write(f"months/{m}", f"# {_month_name(m)}\n\nUp: [overview](../overview.md)\n\n{s.get(f'months/{m}', '')}\n\n"
              f"## Weeks\n\n{wks}\n\n## By project\n\n{projs}\n")
    for (p, m), rs in by_proj_month.items():
        ps = pslug(p)
        write(f"projects/{ps}/{m}", f"# {p} · {_month_name(m)}\n\nUp: [{p}](../{ps}.md) · [{_month_name(m)}](../../months/{m}.md)\n\n"
              + (s.get(f"projects/{ps}/{m}", "") + "\n\n" if s.get(f"projects/{ps}/{m}") else "")
              + f"## {len(rs)} items\n\n" + "\n".join(_line(r, "../../../", with_project=False) for r in rs) + "\n")
    for p, months in projects.items():
        ps = pslug(p)
        rows = "\n".join(f"- [{_month_name(m)}]({ps}/{m}.md) · {len(by_proj_month[(p, m)])} items" for m in sorted(months, reverse=True))
        write(f"projects/{ps}", f"# {p}\n\nUp: [overview](../overview.md)\n\n{s.get(f'projects/{ps}', '')}\n\n## Months\n\n{rows}\n")
    prow = "\n".join(f"- [{p}](projects/{pslug(p)}.md) · {sum(len(by_proj_month[(p, m)]) for m in ms)} items · {min(ms)} → {max(ms)}"
                     for p, ms in sorted(projects.items(), key=lambda kv: max(kv[1]), reverse=True))
    mrow = "\n".join(f"- [{_month_name(m)}](months/{m}.md) · {len(by_month[m])} items" for m in sorted(by_month, reverse=True))
    total = len(items)
    write("overview", f"# {OWNER}'s brain: overview\n\n{total:,} items. Drill down: overview → project or month → week → day → raw file.\n\n"
          f"{overview}\n\n## Projects\n\n{prow}\n\n## Months\n\n{mrow}\n")
    # Drop nodes that no longer exist (e.g. an item re-filed to another project)
    keep = {f"days/{d}" for d in by_day} | {f"weeks/{w}" for w in by_week} | {f"months/{m}" for m in by_month} \
        | {f"projects/{pslug(p)}/{m}" for p, m in by_proj_month} | {f"projects/{pslug(p)}" for p in projects} | {"overview"}
    for f in TREE.rglob("*.md"):
        rel = str(f.relative_to(TREE))[:-3]
        if rel not in keep:
            f.unlink()
    b.cache = {k: v for k, v in b.cache.items() if k in keep}
    save_json(NODES_FILE, b.cache)
    log(f"tree: {len(keep)} nodes, {b.calls} regenerated, ~${COST['usd']:.2f} notional this run")
