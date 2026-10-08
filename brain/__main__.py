"""python -m brain <command>

  init                  create the data folder ($BRAIN_ROOT, default ~/brain) and config.toml
  ingest [source ...]   pull new/changed items into raw/ (default: sources enabled in config.toml)
  notion-crawl [full]   refresh the Notion cache only (no index writes)
  summarise [N]         line summaries for new/changed items
  tree                  rebuild the day/week/month/project/overview nodes
  check                 scan everything in the data folder for secrets
  run                   ingest + summarise + tree (what the nightly job does)
  install-launchd [HH:MM]   schedule the nightly job on macOS (default 02:30)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

from .common import CFG, CODE, CONFIG_FILE, HOME, LOCAL, ROOT, Items
from .sources import ALL, Sources


def log(msg: str):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def enabled() -> list[str]:
    return [n for n in ALL if CFG["sources"].get(n.replace("-", "_"), True)]


def ingest(names: list[str]) -> dict:
    items, sources = Items(), Sources()
    report = {}
    for name in names or enabled():
        t0 = time.time()
        try:
            report[name] = ALL[name](items, sources.section(name), log)
        except Exception as exc:  # noqa: BLE001 - one source failing shouldn't stop the others
            log(f"! {name} failed: {type(exc).__name__}: {exc}")
            report[name] = f"failed: {exc}"
        finally:
            items.save()
            sources.save()
        log(f"  {name} took {time.time() - t0:.0f}s")
    return report


def init():
    for d in ("raw", "tree", "state", "inbox", "exports", ".local"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        shutil.copy(CODE / "config.example.toml", CONFIG_FILE)
        print(f"wrote {CONFIG_FILE}: set `owner` and your [projects] before the first run")
    gi = ROOT / ".gitignore"
    if not gi.exists():
        gi.write_text(".local/\ninbox/processed/\nexports/\n.DS_Store\n")
    print(f"data folder ready: {ROOT}")


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>{script}</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BRAIN_ROOT</key><string>{root}</string>
    <key>BRAIN_PYTHON</key><string>{python}</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>{slots}</array>
  <key>StandardOutPath</key><string>{logs}/launchd.log</string>
  <key>StandardErrorPath</key><string>{logs}/launchd.log</string>
</dict>
</plist>
"""


def install_launchd(at: str = "02:30"):
    label = "com.brain-kit.nightly"
    h, m = (int(x) for x in at.split(":"))
    # The main slot, plus retries that exit at once after a successful run.
    hours = [h] + [x for x in (9, 13, 19) if x != h]
    slots = "".join(f"<dict><key>Hour</key><integer>{hh}</integer><key>Minute</key><integer>{m if hh == h else 0}</integer></dict>"
                    for hh in hours)
    logs = HOME / "Library/Logs/brain-kit"
    logs.mkdir(parents=True, exist_ok=True)
    plist = HOME / f"Library/LaunchAgents/{label}.plist"
    plist.write_text(PLIST.format(label=label, script=CODE / "scripts/nightly.sh", root=ROOT,
                                  python=sys.executable, slots=slots, logs=logs))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=True)
    print(f"installed {plist}; runs at {at} (retries {', '.join(f'{x}:00' for x in hours[1:])}). "
          f"Run now: launchctl kickstart gui/{uid}/{label}")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"
    rest = argv[1:]
    if cmd == "init":
        init()
        return 0
    if cmd == "install-launchd":
        install_launchd(*(rest[:1] or ["02:30"]))
        return 0
    LOCAL.mkdir(parents=True, exist_ok=True)
    if cmd == "ingest":
        ingest(rest)
    elif cmd == "notion-crawl":
        from .notion_mcp import crawl
        log(str(crawl(log, full=True if rest[:1] == ["full"] else None)))
    elif cmd == "summarise":
        from .summarise import summarise_items
        summarise_items(log, limit=int(rest[0]) if rest else None)
    elif cmd == "tree":
        from .tree import build_tree
        build_tree(log)
    elif cmd == "check":
        from .check import check_tree
        return 0 if check_tree(log) else 1
    elif cmd == "run":
        from .llm import UsageLimitError
        from .summarise import summarise_items
        from .tree import build_tree
        ingest([])
        try:
            summarise_items(log)
            build_tree(log)
        except UsageLimitError as exc:
            log(f"! usage limit hit, will resume next run: {exc}")
            return 3
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
