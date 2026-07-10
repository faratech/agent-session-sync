# agent-session-sync

Bidirectional conversation sync between [OpenAI Codex CLI](https://github.com/openai/codex) and [Claude Code](https://claude.com/claude-code).

Start a conversation in one, resume it in the other. A single Python file, no dependencies.

```
~/.codex/sessions/2026/07/09/rollout-*.jsonl   <->   ~/.claude/projects/<munged-cwd>/<uuid>.jsonl
```

Both CLIs keep their history as JSONL transcripts, and both can resume a session from disk. They just can't read each other's format. This script translates between them, in place, on a timer — so `codex resume` lists your Claude conversations and `claude --resume` lists your Codex ones, natively, with no plugin or wrapper.

## Why

You hit a usage limit in one tool. Or you want a second model's read on a thread you've been pulling on for an hour. Or you just prefer one TUI for some kinds of work. Today that means re-explaining an hour of context by hand. This removes that step.

## Install

Requires Python 3.8+. No packages to install.

```sh
git clone https://github.com/faratech/agent-session-sync
cd agent-session-sync
./agent-session-sync.py --dry-run -v      # preview, writes nothing
./agent-session-sync.py                   # sync both directions, last 30 days
```

An idle run costs about 100 ms — it stats source files and skips anything unchanged — so it's cheap to run on a short timer.

```
--to-claude       only sync codex -> claude
--to-codex        only sync claude -> codex
--days N          only sources modified in the last N days (default 30)
--session <id>    force one source by id, ignoring age and state
--dry-run -v      show what would happen
--quiet           no summary line (for cron)
```

## Run it automatically

**cron** (every 2 minutes):

```
*/2 * * * * /usr/bin/python3 /path/to/agent-session-sync.py --quiet >> /var/log/agent-session-sync.log 2>&1
```

**Claude Code hooks** — refresh the mirrors the moment a session starts or a turn ends. In `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "/usr/bin/python3 /path/to/agent-session-sync.py --quiet",
      "timeout": 120, "async": true,
      "statusMessage": "Syncing Codex/Claude sessions"
    }]}],
    "Stop": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "/usr/bin/python3 /path/to/agent-session-sync.py --quiet",
      "timeout": 120, "async": true
    }]}]
  }
}
```

**Windows Task Scheduler**:

```powershell
schtasks /create /tn agent-session-sync /sc minute /mo 2 /ru SYSTEM ^
  /tr "pythonw C:\path\to\agent-session-sync.py --quiet"
```

## How the translation works

Tool calls are the hard part: the two formats pair calls with results differently, and a resumed session with a dangling or mismatched tool call is rejected by the API. So tool activity is **flattened into readable text blocks** rather than translated structurally:

| Direction | Tool activity becomes |
|---|---|
| Codex → Claude | `[codex_tool_call]` / `[codex_tool_result]` |
| Claude → Codex | `[external_agent_tool_call]` / `[external_agent_tool_result]` |

This is the same strategy Codex's own (since-removed) `external_migration` importer used, which is why both TUIs render imports natively and resume works without tool-pairing constraints. Each block is capped at 3000 characters.

Real typed prompts stay as user messages. Injected context (AGENTS.md, environment preambles), sub-agent threads, and encrypted reasoning are skipped.

## Safety properties

The script is designed to be run repeatedly, unattended, against files you care about.

- **Idempotent.** Target IDs are derived deterministically (uuid5 / a seeded uuid7) from the source session, so re-running never produces a second copy. A registry under the state dir records what was written.
- **Never clobbers a continuation.** If you resume an imported session in the other tool, that mirror now diverges from what the script wrote. It detects this by hash and stops overwriting that file, permanently. Your continuation wins.
- **No import loops.** A mirror is never re-imported back across the boundary. Both directions carry a marker and are checked against the registry.
- **Atomic writes.** Every file is written to a tempfile and `os.replace`d into position.
- **Single-flight.** A non-blocking lock means overlapping cron ticks are a no-op, not a race.

Nothing is ever deleted, and the source transcript is only ever read.

## State

A registry of what was written, and the hashes it was written with:

| | |
|---|---|
| `$AGENT_SESSION_SYNC_STATE_DIR` | if set |
| root, POSIX | `/var/lib/agent-session-sync` |
| non-root, POSIX | `$XDG_STATE_HOME/agent-session-sync`, else `~/.local/state/...` |
| Windows | `%LOCALAPPDATA%\agent-session-sync` |

`$CODEX_HOME` and `$CLAUDE_CONFIG_DIR` are honored if set. Deleting the state dir is safe: the next run rebuilds it, and because IDs are deterministic it re-recognizes its own past output — but it will also forget which mirrors you had continued, so re-mirror on a clean state dir only if you haven't.

## Caveats

- **Claude resume is per-project.** A session imports into the project directory matching its original `cwd`, so `claude --resume` only lists it when run from that directory.
- **An in-progress conversation lags** by up to your sync interval.
- **Assistant text only.** Thinking blocks, images, and structured tool arguments beyond the text cap don't survive the round trip. You get a faithful transcript, not a byte-exact one.
- The `version` fields stamped into generated records are cosmetic.

## License

MIT
