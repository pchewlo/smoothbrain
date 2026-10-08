# smoothbrain

A personal brain built from your AI conversations, meetings and notes. It turns Claude Code, Codex, Claude.ai, ChatGPT, Wispr Flow and Notion into one markdown archive and adds a summary tree on top. A local MCP server lets any MCP client look things up in it, in a few tool calls.

It runs on your Mac, summarises with your Claude subscription through `claude -p` (no API key), and refreshes every night.

The summary tree follows Victor Taelin's [OptChat](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449).

## Pieces you might want on their own

**Wispr Flow meetings without an API.** Wispr Flow has no public API for its meeting notetaker, but the desktop app keeps everything locally:

- `~/Library/Application Support/Wispr Flow/flow.sqlite`:
  - `Meetings`: titles, summaries, notes, participant names, and a `speakerMap` that ties speaker labels to people
  - `Notes`: scratchpad notes
- `.../Wispr Flow/meetings/<id>/refined.ndjson` (falling back to `live.ndjson`): the transcript, one `{timestamp, text, speaker}` line per utterance.

`ingest_wispr()` in [`brain/sources.py`](brain/sources.py) turns these into markdown transcripts with named speakers, opening the database read-only. These are internal formats and may change when Wispr updates the app.

**claude.ai connectors from a script, with no API key.** Any connector on your claude.ai account (Notion, Gmail, Drive, Calendar…) can be called from a script by using a small model as a relay. `mcp_calls()` in [`brain/llm.py`](brain/llm.py) works like this:

1. It runs `claude -p --output-format stream-json --verbose` with Haiku, a system prompt that says "make exactly these tool calls, then reply DONE", and `--allowedTools` limited to those tools.
2. It reads the raw `tool_result` blocks straight off the stream. The model never retypes the content, so the cost is one cheap turn per batch of calls.
3. It matches each result to the call you asked for and retries any that are missing.

Two things are needed to make it reliable:

- `MCP_CONNECTION_NONBLOCKING=false`. Without it, the claude.ai connectors may still be loading when the first turn runs, and the model gets "No such tool available".
- `MAX_MCP_OUTPUT_TOKENS` set high (it's 120000 here), so large results come back inline instead of as a file reference.

Some connectors report `needs-auth` in headless runs even when they work interactively. Check the `mcp_servers` list in the stream's `init` event.

**Every chat log format to one markdown format.**

- Claude Code sessions, with subagent tasks and reports folded in and messages typed mid-turn kept
- Codex rollouts, one item per rollout file, because resumed sessions repeat the session id
- Claude.ai exports: conversations, Claude Design chats, Projects with their knowledge files, and memory
- ChatGPT exports: the `mapping` tree is followed from `current_node` back to the root, so you get the branch you ended on

Tool calls become one line each.

## How it fits together

```
sources ──ingest──▶ raw/            one scrubbed markdown file per conversation, meeting or page
                    state/items.json   index: date, source, project, summary, section map
        ──summarise (Haiku)──▶ a ~500-character line per item; long items get section notes with line ranges
        ──tree (Sonnet, Opus)──▶ tree/overview → projects → project-months
                                 tree/overview → months → weeks → days
mcp/server.mjs ── overview · search · zoom · read · save ──▶ any MCP client
```

To answer a question, the model works down from the top instead of loading the archive:

1. **`search`** first matches the words against item summaries and section notes in memory. It then runs ripgrep over the raw text: files must contain every word, and they're ranked by hit count with a bonus for lines where the words appear together. It returns item ids, raw paths and the best lines with their line numbers.
2. **`zoom`** opens a tree node, or an item's section map.
3. **`read`** pages through a raw file by line range.

Every call returns a bounded amount of text, and any fact is about five hops from the overview however big the archive gets.

## Setup (macOS)

Needs:

- Python 3.11+
- Node 18+
- [Claude Code](https://claude.com/claude-code), logged in with a Claude subscription

macOS ships Python 3.9. If `python3 --version` is older than 3.11, run the commands below with `uv run python` or a newer `python3.x` instead.

```sh
git clone https://github.com/pchewlo/smoothbrain && cd smoothbrain
(cd mcp && npm install)

python3 -m brain init            # creates ~/brain (or $BRAIN_ROOT) and ~/brain/config.toml
# edit ~/brain/config.toml: your name (`owner`), which sources to read, and your [projects]

python3 -m brain ingest          # converts everything into ~/brain/raw (fast, no model calls)
python3 -m brain summarise 5     # try 5 items first and check the summaries look right
python3 -m brain summarise       # the rest (this is the long, usage-heavy step)
python3 -m brain tree            # roll-ups
python3 -m brain install-launchd 02:30   # nightly refresh
```

Connect the MCP server:

```sh
claude mcp add --scope user brain -- "$PWD/mcp/start.sh"
```

For the Claude desktop app, Cursor and others, add this to the app's MCP config:

```json
{ "mcpServers": { "brain": { "command": "/path/to/smoothbrain/mcp/start.sh", "args": [], "env": { "BRAIN_ROOT": "/Users/you/brain" } } } }
```

To back the brain up, make `~/brain` a git repo with a **private** remote. The nightly job then commits and pushes it after the secret check passes.

## Sources

| Source | Read from | Config key |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` and `*/subagents/` | `claude_code` |
| Codex | `~/.codex/sessions`, `~/.codex/archived_sessions` | `codex` |
| Claude.ai, Claude Design, Projects, memory | export zips in `[exports] dirs` | `exports` |
| ChatGPT | export zips in `[exports] dirs` | `exports` |
| Wispr Flow | the app's local data (see above) | `wispr` |
| Notion | the claude.ai Notion connector, through the relay | `notion` (off by default) |
| Claude Code memory | `~/.claude/projects/*/memory/*.md` | `memory` |
| Notes | `inbox/`, written by the MCP `save` tool | `inbox` |

Export zips are recognised by their contents, so drop them in unopened. For ChatGPT: Settings → Data controls → Export data. For Claude.ai: Settings → Privacy → Export data.

Notion: the crawl starts at each teamspace's top pages, plus your private and shared pages, and walks down. Each database becomes one item listing all its rows. A row's own page is fetched when the row is new or changed. Nightly runs refetch edited pages and re-list databases; a full re-walk runs weekly.

Claude Code deletes session logs after 30 days by default. Set `"cleanupPeriodDays": 3650` in `~/.claude/settings.json` to keep them.

## Cost

All model calls go through `claude -p`, so they count against your subscription's usage limits. There is no API bill. The first `summarise` is the heavy step: one Haiku call per item (more for long items), so a few thousand items takes hours and a noticeable share of your weekly usage. After that, nightly runs only summarise what changed. Notion adds a fixed nightly cost, because databases are re-listed every run.

If a run hits the usage limit, it stops cleanly and carries on at the next scheduled slot. Lower `[workers]` to go easier on your limits.

## Secrets

Logs contain whatever you pasted into them. Before any file is written, [`brain/scrub.py`](brain/scrub.py) redacts the following, replacing each value with `[REDACTED:NAME]`:

- every value from `.env` files under your home folder (and any files listed in `[scrub] extra_secret_files`)
- common key formats: Anthropic, OpenAI, GitHub, Stripe, Slack, AWS, Google, Telegram bots, Notion, Supabase and others
- passwords in connection strings
- private-key blocks, JWTs and bearer tokens
- `NAME=value` lines with secret-sounding names

`python -m brain check` re-scans the data folder, and the nightly job won't commit if anything matches. The scrubber works by pattern matching and can miss a secret in an unusual format, so keep any remote private.

## Commands

```
python -m brain init | ingest [source…] | notion-crawl [full] | summarise [N] | tree | check | run | install-launchd [HH:MM]
```

Logs are in `~/Library/Logs/smoothbrain/`. To re-run tonight's job now: `scripts/nightly.sh --force`.

## Limits

- **Search matches exact words.** If you search "cost" and the conversation only says "price", it may be missed. The summaries narrow the gap and the model can try other words, but there are no embeddings yet.
- **The archive is up to a day behind.** Notes made with `save` are searchable at once.
- **The MCP server is local (stdio).** claude.ai on the web or phone and ChatGPT can't reach it without a hosted copy, which this project doesn't include.
- **macOS only for now:** launchd, the Wispr path and notifications.

MIT licence.
