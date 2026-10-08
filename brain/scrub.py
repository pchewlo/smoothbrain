"""Secret scrubber. Every raw file is scrubbed before it is written, and the commit
step re-scans the staged tree and refuses to commit if anything still matches.

Two layers:
1. Literal values pulled from every .env file under ~ (plus ~/.config, any
   [scrub] extra_secret_files in config.toml, and ~/.codex/auth.json). Values under secret-sounding names are redacted
   at 6+ chars; other values only if they look like random keys.
2. Patterns for well-known key formats, connection strings with passwords,
   private-key blocks, JWTs and NAME=value assignments with secret-sounding names.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

HOME = Path.home()
SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASS|DSN|DATABASE_URL|DB_URL|PRIVATE|CREDENTIAL|AUTH|WEBHOOK|SALT|COOKIE|SESSION)",
    re.I,
)
PRUNE = {"node_modules", ".git", "Library", ".Trash", ".cache", ".npm", ".nvm", ".venv", "venv", "site-packages", "raw", "tree"}

# Defaults that appear in env files but also everywhere in ordinary text.
COMMON = {
    "true", "false", "null", "none", "production", "development", "test", "postgres", "password", "admin",
    "root", "localhost", "user", "supabase", "default", "public", "secret", "changeme", "example", "neondb",
    "neondb_owner", "service_role", "anon", "github", "vercel",
}

PATTERNS = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)")),
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abposre]-[A-Za-z0-9\-]{10,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GOOGLE_OAUTH", re.compile(r"\b(?:ya29\.[0-9A-Za-z_\-]{20,}|GOCSPX-[0-9A-Za-z_\-]{20,}|1//0[0-9A-Za-z_\-]{30,})")),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}|\bwhsec_[0-9A-Za-z]{20,}")),
    ("TELEGRAM_BOT_TOKEN", re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_\-]{30,}")),
    ("NOTION_TOKEN", re.compile(r"\b(?:secret_|ntn_)[A-Za-z0-9]{30,}")),
    ("RESEND_KEY", re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{16,}")),
    ("SUPABASE_KEY", re.compile(r"\b(?:sbp_[a-f0-9]{40}|sb_secret_[A-Za-z0-9_\-]{20,})")),
    ("VERCEL_TOKEN", re.compile(r"\b(?:vcp|vci|vca|vcr|vck)_[A-Za-z0-9]{20,}")),
    ("HF_TOKEN", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("NPM_TOKEN", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("SENDGRID_KEY", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]
# Connection strings: keep scheme, user and host, drop the password.
URL_CREDS = re.compile(
    r"\b((?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|https?|ftp|smtp)://[^:\s/@'\"]+:)([^@\s'\"]{3,})(@)"
)
BEARER = re.compile(r"(\b(?:Bearer|Basic|token)\s+)([A-Za-z0-9._~+/=\-]{20,})", re.I)
ASSIGN = re.compile(
    r"""(["']?\b[A-Za-z0-9_\-]*(?:api[_-]?key|secret|token|password|passwd|private[_-]?key|access[_-]?key|service[_-]?role|client[_-]?secret|auth[_-]?key)[A-Za-z0-9_\-]*["']?\s*[:=]\s*["']?)([A-Za-z0-9._\-/+=!@#$%^&*~]{12,})""",
    re.I,
)


def _looks_random(v: str) -> bool:
    return (
        len(v) >= 20
        and " " not in v
        and re.search(r"[A-Za-z]", v) is not None
        and re.search(r"\d", v) is not None
        and not v.startswith(("http://", "https://", "/"))
    )


def _env_files():
    roots = [HOME]
    seen = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = Path(dirpath).relative_to(HOME).parts
            dirnames[:] = [d for d in dirnames if d not in PRUNE and len(depth) < 5]
            for f in filenames:
                if (f == ".env" or f.startswith(".env.") or f.endswith(".env")) and "example" not in f and "sample" not in f:
                    p = Path(dirpath) / f
                    if p not in seen:
                        seen.add(p)
                        yield p
    from .common import CFG
    for extra in CFG["scrub"]["extra_secret_files"]:
        p = Path(os.path.expanduser(extra))
        if p.exists():
            yield p


def _parse_env(path: Path):
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return
    if "=" not in text and len(text.strip()) < 200:
        yield "TOKEN", text.strip()  # bare token file
        return
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        yield name, val


def _json_secrets(path: Path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    stack = [("", data)]
    while stack:
        k, v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.items())
        elif isinstance(v, list):
            stack.extend((k, x) for x in v)
        elif isinstance(v, str):
            yield k, v


@lru_cache(maxsize=1)
def literal_secrets() -> tuple[tuple[str, str], ...]:
    found: dict[str, str] = {}
    sources = [(p, _parse_env(p)) for p in _env_files()]
    codex_auth = HOME / ".codex/auth.json"
    if codex_auth.exists():
        sources.append((codex_auth, _json_secrets(codex_auth)))
    for _, pairs in sources:
        for name, val in pairs:
            val = val.strip()
            if not val or val.lower() in COMMON or re.search(r"(_MODE|_HOST|HOST_|_REGION|LAST_REFRESH|_EMAIL)$", name.upper()):
                continue
            if (SECRET_NAME.search(name) and len(val) >= 6 and not val.startswith(("http://", "https://"))) or _looks_random(val):
                found[val] = re.sub(r"[^A-Z0-9_]", "_", name.upper())[:40] or "SECRET"
            # Credentials inside URL values (DATABASE_URL etc.)
            m = URL_CREDS.search(val)
            if m and m.group(2).lower() not in COMMON and len(m.group(2)) >= 6:
                found[m.group(2)] = re.sub(r"[^A-Z0-9_]", "_", name.upper())[:40]
    return tuple(sorted(found.items(), key=lambda kv: -len(kv[0])))


@lru_cache(maxsize=1)
def _literal_re():
    lits = [re.escape(v) for v, _ in literal_secrets()]
    if not lits:
        return None, {}
    names = dict(literal_secrets())
    return re.compile("|".join(lits)), names


def scrub(text: str) -> tuple[str, int]:
    """Returns (clean_text, number_of_redactions)."""
    if not text:
        return text, 0
    count = 0

    def sub(pattern, repl, s):
        nonlocal count
        s, n = pattern.subn(repl, s)
        count += n
        return s

    for name, pat in PATTERNS:
        text = sub(pat, f"[REDACTED:{name}]", text)
    text = sub(URL_CREDS, lambda m: m.group(1) + "[REDACTED:PASSWORD]" + m.group(3), text)
    lit_re, names = _literal_re()
    if lit_re is not None:
        text = sub(lit_re, lambda m: f"[REDACTED:{names.get(m.group(0), 'SECRET')}]", text)
    text = sub(BEARER, lambda m: m.group(1) + "[REDACTED:TOKEN]", text)
    text = sub(ASSIGN, lambda m: m.group(1) + "[REDACTED:SECRET]", text)
    return text, count


def find_leaks(text: str) -> list[str]:
    """Names of anything that still matches. Used as a gate before commit."""
    hits = []
    lit_re, names = _literal_re()
    if lit_re is not None:
        for m in lit_re.finditer(text):
            hits.append(names.get(m.group(0), "SECRET"))
    for name, pat in PATTERNS:
        if pat.search(text):
            hits.append(name)
    for m in URL_CREDS.finditer(text):
        if not m.group(2).startswith("[REDACTED"):
            hits.append("URL_PASSWORD")
    return hits
