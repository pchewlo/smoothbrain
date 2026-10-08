# smoothbrain

Turns your Claude Code, Codex, Claude.ai, ChatGPT, Wispr Flow and Notion history into one markdown archive with a summary tree, and serves it to Claude (or any MCP client) through a local MCP server. Runs on macOS and uses your Claude subscription through `claude -p`. The summary tree follows [OptChat](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449).

## Requirements

- macOS
- Python 3.11 or newer (macOS ships 3.9; use `uv run python` or a newer `python3.x` in the commands below if needed)
- Node 18 or newer
- [Claude Code](https://claude.com/claude-code), logged in with a Claude subscription

## Install

```sh
git clone https://github.com/pchewlo/smoothbrain && cd smoothbrain
(cd mcp && npm install)
python3 -m brain init
```

`init` creates the data folder `~/brain` (set `BRAIN_ROOT` to put it elsewhere) and `~/brain/config.toml`.

Edit `~/brain/config.toml`:

- `owner`: your name, as it should appear in transcripts and summaries
- `[sources]`: turn sources on or off
- `[projects]`: the projects items get filed under, each with a one-line description. The last one is the catch-all.

Stop Claude Code deleting its session logs after 30 days by adding this to `~/.claude/settings.json`:

```json
"cleanupPeriodDays": 3650
```

## First run

```sh
python3 -m brain ingest          # convert everything into ~/brain/raw (no model calls)
python3 -m brain summarise 5     # summarise 5 items and check they look right
python3 -m brain summarise       # summarise the rest
python3 -m brain tree            # build the summary tree
```

The full `summarise` is the slow step. It makes one Haiku call per item, so a few thousand items take hours and use a noticeable share of your weekly usage. If it hits your usage limit, run it again later and it carries on where it stopped.

## Run it every night

```sh
python3 -m brain install-launchd 02:30
```

The job ingests whatever is new, summarises only what changed, rebuilds the tree and runs the secret check. If it misses its slot, it retries at 09:00, 13:00 and 19:00. Logs are in `~/Library/Logs/smoothbrain/`.

To run it now: `scripts/nightly.sh --force`.

To back the archive up, make `~/brain` a git repo with a private remote. The nightly job will then commit and push after the secret check passes.

## Connect it to Claude

Claude Code:

```sh
claude mcp add --scope user brain -- /path/to/smoothbrain/mcp/start.sh
```

Claude desktop app: add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart the app:

```json
{ "mcpServers": { "brain": { "command": "/path/to/smoothbrain/mcp/start.sh", "args": [], "env": { "BRAIN_ROOT": "/Users/you/brain" } } } }
```

Cursor: the same block in `~/.cursor/mcp.json`. Codex: `codex mcp add brain -- /path/to/smoothbrain/mcp/start.sh`.

The server has five tools: `overview`, `search`, `zoom`, `read` and `save`. In a chat, ask about past work ("what did I decide about pricing?"). To save a note, say "save this to my brain"; it is searchable at once and summarised on the next nightly run.

Claude on the web, the phone apps and ChatGPT can't reach a local MCP server.

## Adding exports

Claude.ai: Settings → Privacy → Export data. ChatGPT: Settings → Data controls → Export data.

Download the zip and leave it in `~/Downloads` (or move it to `~/brain/exports/`) without unzipping it. The next run picks it up and detects the format. Conversations that appear in several exports are kept once, at their latest version.

## Sources

| Source | Read from | Config key |
|---|---|---|
| Claude Code | `~/.claude/projects/` | `claude_code` |
| Codex | `~/.codex/sessions`, `~/.codex/archived_sessions` | `codex` |
| Claude.ai, Claude Design, Claude Projects, Claude memory | export zips | `exports` |
| ChatGPT | export zips | `exports` |
| Wispr Flow meetings and notes | `~/Library/Application Support/Wispr Flow/` | `wispr` |
| Notion | your claude.ai Notion connector | `notion` (off by default) |
| Claude Code memory | `~/.claude/projects/*/memory/` | `memory` |
| Saved notes | `~/brain/inbox/` | `inbox` |

Notion needs the Notion connector connected in your claude.ai account. It reads every teamspace, private page and shared page the connector can see. To leave a page or database out, add its id to `[notion] skip`.

## Secrets

Before anything is written, values from every `.env` file in your home folder, common API key formats, passwords in connection strings, private keys and tokens are replaced with `[REDACTED:NAME]`. Add files holding bare tokens to `[scrub] extra_secret_files`. `python3 -m brain check` re-scans the archive, and the nightly job won't commit if it finds anything. It works by pattern matching, so keep any remote private.

## Commands

```
python3 -m brain init                     create the data folder and config
python3 -m brain ingest [source ...]      import new and changed items
python3 -m brain summarise [N]            summarise new and changed items
python3 -m brain tree                     rebuild the summary tree
python3 -m brain check                    scan the archive for secrets
python3 -m brain run                      ingest, summarise and tree
python3 -m brain notion-crawl [full]      refresh the Notion cache
python3 -m brain install-launchd [HH:MM]  schedule the nightly run
```

MIT licence.
