"""Line summaries (~500 bytes) per item, with Haiku. Long items are first cut into
sections, each with its own note and line range, so an agent can jump straight to
the right part of a long raw file. Only items whose raw hash changed are redone;
sections whose text is unchanged keep their old note.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import CFG, OWNER, ROOT, Items, sha
from .llm import COST, LINE_MODEL, SetupError, UsageLimitError, ask
from .scrub import scrub


def _clean(s: str) -> str:
    return scrub((s or "").strip())[0]

CHUNK = 80_000      # chars per section of a long item
SINGLE = 90_000     # items up to this size are summarised in one call
WORKERS = int(CFG["workers"]["summarise"])

PROJECTS: dict = CFG["projects"]
FALLBACK = list(PROJECTS)[-1]  # the last project is the catch-all

LINE_SYSTEM = f"""You write one index line for a personal archive of {OWNER}'s AI conversations, meetings and notes. The line lets {OWNER} or an AI agent decide, without opening it, whether this item holds what they are looking for.

Write in plain English, past tense, no filler, no adjectives about quality. Pack in the specifics a search would need: what {OWNER} was working on, what was produced, decisions made and their reasons, numbers (prices, dates, amounts, counts), names of people, companies, tools and files, and anything left open. Refer to {OWNER} as "{OWNER}". 350-550 characters for the summary.

Pick the project from this list (use "{FALLBACK}" when nothing else fits):
{json.dumps(PROJECTS, indent=1, ensure_ascii=False)}
"""

SECTION_SYSTEM = f"""You are summarising one section of a long conversation from {OWNER}'s personal AI archive. Write 250-450 characters: what was discussed or done in this section, decisions, numbers, names, outputs. Plain English, past tense, no filler."""

LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short descriptive title, max 80 chars"},
        "project": {"type": "string", "enum": list(PROJECTS)},
        "summary": {"type": "string"},
    },
    "required": ["title", "project", "summary"],
}
SECTION_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def _chunks(text: str) -> list[tuple[int, int, str]]:
    """Split on line boundaries into ≤CHUNK-char pieces → (first_line, last_line, text), 1-based."""
    out, buf, size, start = [], [], 0, 1
    for i, line in enumerate(text.split("\n"), 1):
        if size + len(line) > CHUNK and buf:
            out.append((start, i - 1, "\n".join(buf)))
            buf, size, start = [], 0, i
        buf.append(line[:CHUNK])
        size += len(line) + 1
    if buf:
        out.append((start, start + len(buf) - 1, "\n".join(buf)))
    return out


def _header(rec: dict) -> str:
    return (f"Source: {rec['source']}\nDate: {rec['date']}\nOriginal title: {rec.get('title', '')}\n"
            f"Location hint: {rec.get('hint', '')}\n")


def summarise_one(rec: dict) -> dict:
    text = (ROOT / rec["path"]).read_text(errors="ignore")
    if len(text) <= SINGLE:
        out = ask(_header(rec) + "\n---\n" + text, system=LINE_SYSTEM, model=LINE_MODEL, schema=LINE_SCHEMA)
        return {"line": out, "sections": []}
    old = {s["hash"]: s for s in rec.get("sections") or []}
    sections = []
    pieces = _chunks(text)
    for n, (a, b, chunk) in enumerate(pieces, 1):
        h = sha(chunk)
        if h in old:
            sections.append({**old[h], "lines": [a, b]})
            continue
        out = ask(f"{_header(rec)}Section {n} of {len(pieces)} (lines {a}-{b})\n---\n{chunk}",
                  system=SECTION_SYSTEM, model=LINE_MODEL, schema=SECTION_SCHEMA)
        sections.append({"hash": h, "lines": [a, b], "note": out["summary"].strip()})
    notes = "\n".join(f"[lines {s['lines'][0]}-{s['lines'][1]}] {s['note']}" for s in sections)
    prompt = (_header(rec) + f"Length: {len(text):,} chars in {len(pieces)} sections.\n\n"
              f"Opening of the conversation:\n---\n{text[:12000]}\n---\n\nSection notes, in order:\n{notes}")
    out = ask(prompt, system=LINE_SYSTEM, model=LINE_MODEL, schema=LINE_SCHEMA)
    return {"line": out, "sections": sections}


def summarise_items(log, limit: int | None = None) -> dict:
    items = Items()
    todo = [r for r in items if r.get("sum_hash") != r["hash"]]
    todo.sort(key=lambda r: r.get("date") or "", reverse=True)  # newest first
    if limit:
        todo = todo[:limit]
    log(f"summarise: {len(todo)} items to do")
    lock = threading.Lock()
    stop = threading.Event()
    done = failed = 0
    fatal: list[Exception] = []

    def work(rec):
        if stop.is_set():
            return rec, None, None
        try:
            return rec, summarise_one(rec), None
        except (UsageLimitError, SetupError) as exc:
            stop.set()
            return rec, None, exc
        except Exception as exc:  # noqa: BLE001
            return rec, None, exc

    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(work, r) for r in todo]
        for fut in as_completed(futures):
            rec, res, exc = fut.result()
            with lock:
                if res:
                    line = res["line"]
                    rec.update({
                        "summary": _clean(line["summary"]), "project": line["project"],
                        "ai_title": _clean(line["title"]), "sum_hash": rec["hash"],
                        "sections": [{**x, "note": _clean(x["note"])} for x in res["sections"]],
                    })
                    done += 1
                elif exc is not None:
                    failed += 1
                    if isinstance(exc, (UsageLimitError, SetupError)):
                        fatal.append(exc)
                    else:
                        log(f"  ! {rec['id']}: {str(exc)[:160]}")
                if (done + failed) % 25 == 0:
                    items.save()
                    log(f"  {done} done, {failed} failed, ~${COST['usd']:.2f} notional")
    items.save()
    log(f"summarise: {done} done, {failed} failed; {COST['calls']} CLI calls, ~${COST['usd']:.2f} notional")
    if fatal:
        raise fatal[0]
    return {"done": done, "failed": failed}
