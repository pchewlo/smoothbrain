"""Gate before every commit: re-scan everything git would pick up for secrets.
Prints file + secret type only, never the value."""

from __future__ import annotations

import subprocess

from .common import ROOT
from .scrub import find_leaks, scrub


def check_tree(log, fix: bool = True) -> bool:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode == 0:
        files = [f for f in out.stdout.split("\0") if f]
    else:  # data folder isn't a git repo: scan what would be shared
        files = [str(p.relative_to(ROOT)) for d in ("raw", "tree", "state", "inbox") for p in (ROOT / d).rglob("*") if p.is_file()]
    bad = []
    for rel in files:
        p = ROOT / rel
        if p.suffix not in (".md", ".json", ".txt", "") or not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        hits = find_leaks(text)
        if hits and fix and (rel.startswith(("raw/", "tree/", "inbox/", "state/"))):
            clean, _ = scrub(text)
            p.write_text(clean)
            hits = find_leaks(clean)
        if hits:
            bad.append((rel, sorted(set(hits))))
    for rel, hits in bad[:50]:
        log(f"  LEAK {rel}: {', '.join(hits)}")
    log(f"check: {len(files)} files scanned, {len(bad)} with secrets")
    return not bad
