"""Paths, config, the item index, and helpers shared by every source and stage.

Code lives in this repo; data lives in BRAIN_ROOT (default ~/brain):
raw/, tree/, state/, inbox/, config.toml, and .local/ for caches and logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CODE = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("BRAIN_ROOT", HOME / "brain")).expanduser()
RAW = ROOT / "raw"
TREE = ROOT / "tree"
STATE = ROOT / "state"
INBOX = ROOT / "inbox"
LOCAL = ROOT / ".local"  # keep out of git: caches, logs
CONFIG_FILE = ROOT / "config.toml"

DEFAULT_CONFIG = (CODE / "config.example.toml").read_text()


def _load_config() -> dict:
    base = tomllib.loads(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        user = tomllib.loads(CONFIG_FILE.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict) and k != "projects":
                base[k].update(v)
            else:
                base[k] = v
    return base


CFG = _load_config()
OWNER = CFG.get("owner") or "Me"  # how you appear in transcripts and summaries

ITEMS_FILE = STATE / "items.json"
SOURCES_FILE = STATE / "sources.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def slug(text: str, n: int = 70) -> str:
    text = re.sub(r"[\x00-\x1f/\\:*?\"<>|#\[\]]+", " ", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return (text[:n].rstrip(" .") or "untitled")


def iso_date(value) -> str:
    """Anything timestamp-like → YYYY-MM-DD (local time)."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        if value > 1e12:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    s = str(value)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return s[:10]


def iso_time(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        if value > 1e12:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Items:
    """state/items.json: one record per conversation/document, keyed by stable id."""

    def __init__(self):
        self.data: dict[str, dict] = load_json(ITEMS_FILE, {})

    def save(self):
        save_json(ITEMS_FILE, self.data)

    def get(self, item_id):
        return self.data.get(item_id)

    def __iter__(self):
        return iter(self.data.values())


def one_line(text: str, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


class Doc:
    """Builds the markdown for one raw file."""

    def __init__(self, title: str, meta: dict):
        self.title = title
        self.meta = meta
        self.parts: list[str] = []
        self.speaker_turns = {"user": 0, "other": 0}

    def say(self, speaker: str, text: str, when: str = ""):
        text = (text or "").strip()
        if not text:
            return
        self.speaker_turns["user" if speaker == OWNER else "other"] += 1
        head = f"### {speaker}" + (f" · {when}" if when else "")
        self.parts.append(f"{head}\n\n{text}\n")

    def tool(self, line: str):
        self.parts.append(f"> → {one_line(line, 200)}\n")

    def section(self, heading: str, body: str = ""):
        self.parts.append(f"## {heading}\n\n{body.strip()}\n" if body.strip() else f"## {heading}\n")

    def raw(self, text: str):
        if text and text.strip():
            self.parts.append(text.strip() + "\n")

    def render(self) -> str:
        meta = "\n".join(f"- {k}: {v}" for k, v in self.meta.items() if v not in (None, ""))
        return f"# {self.title or 'Untitled'}\n\n{meta}\n\n---\n\n" + "\n".join(self.parts)

    @property
    def empty(self) -> bool:
        return not self.parts
