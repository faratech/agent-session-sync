# agent-session-sync

Bidirectional conversation sync between [OpenAI Codex CLI](https://github.com/openai/codex) and [Claude Code](https://claude.com/claude-code).

Start a conversation in one, resume it in the other. One Python file, standard library only. An opt-in companion, [`memory-sync.py`](#memory-sync-opt-in-companion), does the same for each agent's long-term memory.

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

```sh
./agent-session-sync.py --install --dry-run   # show exactly what would change
./agent-session-sync.py --install
```

That adds two things, each only if it isn't already there:

- a **scheduler entry** every 2 minutes — cron on POSIX, Task Scheduler on Windows
- **Claude Code hooks** (`SessionStart` + `Stop`, async) so the mirrors refresh the moment you start a session or finish a turn

Claude Code reads hooks at startup, so open `/hooks` once or start a new session to pick them up. Cron covers the gap either way.

To remove them again:

```sh
./agent-session-sync.py --uninstall             # scheduler + hooks
./agent-session-sync.py --uninstall --memories  # ...and the memory companion, if installed
./agent-session-sync.py --uninstall --purge     # ...and the state dir + logs
```

Use `--no-cron` or `--no-hooks` to do just one half. `--memories` is symmetric: it applies to whichever of `--install` / `--uninstall` you pass, so a plain `--uninstall` leaves the memory companion running.

Both are **idempotent and surgical**. Entries are matched on the *script filename* in the command (and, for crontab comments, on the tool name) — never on a bare substring, since this repo's own directory is called `agent-session-sync` and every path to `memory-sync.py` therefore contains it. So `--install` twice is a no-op, an entry you wrote by hand is recognised rather than duplicated, uninstalling one script never disturbs the other, and your unrelated cron jobs and hooks are left byte-for-byte alone.

`settings.json` is backed up before it's rewritten, and if it isn't valid JSON the installer refuses to touch it. `--uninstall` never deletes synced sessions or memory files — it only removes the automation.

If you'd rather wire it up yourself, the equivalents are:

```
*/2 * * * * /usr/bin/python3 /path/to/agent-session-sync.py --quiet >> /var/log/agent-session-sync.log 2>&1
```

```powershell
schtasks /create /tn agent-session-sync /sc minute /mo 2 ^
  /tr "pythonw C:\path\to\agent-session-sync.py --quiet"
```

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

## Memory sync (opt-in companion)

`memory-sync.py` syncs the notes each agent keeps *about you*, rather than conversations:

```
$CODEX_HOME/memories/memory_summary.md  <->  <claude projects>/*/memory/*.md
```

It's a separate script on purpose. Session transcripts are inert until you resume one; memory files get injected into **every** future session. That's a higher-trust write, so it stays off unless you ask for it, and it runs behind its own lock and state:

```sh
./memory-sync.py --dry-run -v                  # preview, writes nothing
./agent-session-sync.py --install --memories   # add its cron entry + Stop hook
```

```
--to-claude       only sync codex -> claude
--to-codex        only sync claude -> codex
--project <name>  Claude project dir to receive Codex memory
                  (default: the one matching your home dir)
--warn-bytes N    warn when the bundle Codex must read exceeds N (default 65536)
--dry-run -v      show what would happen
--quiet           silent when idle (for cron)
```

It writes exactly two files, both of which it owns and stamps with a generated-by marker:

- `<claude projects>/<your home project>/memory/codex_memory_sync.md`
- `$CODEX_HOME/memories/claude_code_sync.md`

plus one idempotent pointer line in each side's index — Claude's `MEMORY.md` and Codex's global `AGENTS.md`. Everything else is read-only. Codex's own `MEMORY.md`, `memory_summary.md` and `raw_memories.md` are never written, and if something *without* the marker is sitting at an owned path, it is reported and left alone rather than overwritten.

Two things to know before you turn it on:

- **Your Claude memories become Codex context.** The `AGENTS.md` pointer tells Codex to read the bundle at session start, so anything in your Claude memory files goes to your Codex model provider. Read the bundle once before you trust it with secrets.
- **It costs context.** The bundle is every memory file from every project, concatenated. `memory-sync.py` warns above 64 KB (`--warn-bytes`); a 160 KB bundle is roughly 40k tokens on every Codex session.

Codex → Claude no-ops unless `memory_summary.md` exists. Recent Codex keeps memory in `memories_*.sqlite`, which this script does not read.

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

A registry of what was written, and the hashes it was written with. Both scripts share the directory but nothing inside it — `state.json` / `lock` for sessions, `memory-state.json` / `memory.lock` for memories — so one cannot stall or corrupt the other.

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
