"""Headless Claude Code (`claude -p`) on your Claude subscription, no API key.

`ask()` runs one prompt with optional structured output. `mcp_calls()` uses a
model as a thin proxy to the claude.ai connectors (Wispr Flow, Notion): the model
makes exactly the listed tool calls and the raw tool results are read straight
off the stream-json output, so the model never retypes the content.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading

from .common import CFG

LINE_MODEL = CFG["models"]["line"]
ROLLUP_MODEL = CFG["models"]["rollup"]
TOP_MODEL = CFG["models"]["top"]

_LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "out of extra usage", "hit your limit", "5-hour limit", "weekly limit")
_AUTH_MARKERS = ("not logged in", "invalid api key", "please run /login", "authentication", "oauth token")


class UsageLimitError(RuntimeError):
    pass


class SetupError(RuntimeError):
    pass


_claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
# Without this the CLI would bill an API key from the environment instead of the subscription.
_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
_env["MCP_CONNECTION_NONBLOCKING"] = "false"  # wait for claude.ai connectors before the first turn
_env["MAX_MCP_OUTPUT_TOKENS"] = "120000"
_workdir = tempfile.mkdtemp(prefix="brain-llm-")  # outside any repo: no CLAUDE.md leaks in

_cost_lock = threading.Lock()
COST = {"usd": 0.0, "calls": 0, "ms": 0}


def _track(out: dict):
    with _cost_lock:
        COST["usd"] += float(out.get("total_cost_usd") or 0)
        COST["calls"] += 1
        COST["ms"] += int(out.get("duration_ms") or 0)


def _classify(message: str):
    low = message.lower()
    if any(m in low for m in _LIMIT_MARKERS):
        raise UsageLimitError(message[:300])
    if any(m in low for m in _AUTH_MARKERS):
        raise SetupError(message[:300])
    raise RuntimeError(message[:300])


def ask(prompt: str, *, system: str, model: str = LINE_MODEL, schema: dict | None = None, timeout: int = 600):
    """Returns structured_output (dict) when schema is given, else the text result."""
    cmd = [
        _claude, "-p", "--model", model, "--output-format", "json",
        "--system-prompt", system, "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
    ]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    last = None
    for _ in range(2):
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=_env, cwd=_workdir)
        except subprocess.TimeoutExpired as exc:
            last = exc
            continue
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {"is_error": True, "result": (proc.stdout + proc.stderr).strip()[-500:]}
        _track(out)
        if out.get("is_error") or proc.returncode != 0:
            try:
                _classify(str(out.get("result") or proc.stderr.strip()[-300:] or f"exit {proc.returncode}"))
            except RuntimeError as exc:
                if isinstance(exc, (UsageLimitError, SetupError)):
                    raise
                last = exc
                continue
        if schema:
            if out.get("structured_output") is None:
                last = RuntimeError(f"no structured output ({out.get('subtype')})")
                continue
            return out["structured_output"]
        return (out.get("result") or "").strip()
    raise RuntimeError(str(last))


PROXY_SYSTEM = (
    "You are a tool-calling relay. Make exactly the tool calls listed by the user, with exactly the "
    "arguments given, all in parallel in a single turn. Do not make any other calls, do not retry, "
    "do not summarise results. When the calls have returned, reply with the single word DONE."
)


def mcp_calls(calls: list[dict], *, timeout: int = 600) -> list[str | None]:
    """calls: [{"tool": full_tool_name, "args": {...}}]. Returns result text per call (None if missing)."""
    if not calls:
        return []
    tools = sorted({c["tool"] for c in calls})
    prompt = "Make these tool calls:\n" + "\n".join(json.dumps({"tool": c["tool"], "arguments": c["args"]}) for c in calls)
    cmd = [
        _claude, "-p", "--model", LINE_MODEL, "--output-format", "stream-json", "--verbose",
        "--system-prompt", PROXY_SYSTEM, "--tools", "", "--allowedTools", " ".join(tools),
        "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
    ]
    for attempt in range(3):
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=_env, cwd=_workdir)
        uses, results, final = {}, {}, None
        for line in proc.stdout.splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t == "assistant":
                for b in o["message"].get("content", []):
                    if b.get("type") == "tool_use":
                        uses[b["id"]] = (b["name"], b.get("input") or {})
            elif t == "user":
                for b in o["message"].get("content", []):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        text = c if isinstance(c, str) else "".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))
                        results[b["tool_use_id"]] = (text, bool(b.get("is_error")))
            elif t == "result":
                final = o
                _track(o)
        if final and final.get("is_error"):
            _classify(str(final.get("result")))
        out: list[str | None] = [None] * len(calls)
        for uid, (name, inp) in uses.items():
            if uid not in results:
                continue
            text, is_err = results[uid]
            if is_err or "No such tool available" in text:
                continue
            for i, c in enumerate(calls):
                if out[i] is None and c["tool"] == name and _same(c["args"], inp):
                    out[i] = text
                    break
        missing = [c for c, r in zip(calls, out) if r is None]
        if not missing or attempt == 2:
            return out
        # Retry only what's missing.
        sub = mcp_calls(missing, timeout=timeout) if len(missing) < len(calls) else None
        if sub is not None:
            it = iter(sub)
            return [r if r is not None else next(it) for r in out]
    return out


def _same(want, got) -> bool:
    if isinstance(want, dict) and isinstance(got, dict):
        return all(k in got and _same(v, got[k]) for k, v in want.items())
    if isinstance(got, str) and not isinstance(want, str):
        try:
            return _same(want, json.loads(got))
        except ValueError:
            return False
    return want == got
