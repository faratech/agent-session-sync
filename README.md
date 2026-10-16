# agent-session-sync

Automatic conversation handoffs across **Codex CLI, Claude Code, Meta Muse Code,
Google Antigravity CLI (`agy`), and GitHub Copilot CLI**.

Start work in one tool and continue in another without explaining the whole task
again. Detects installed harnesses on Linux, macOS, and Windows, caches their
locations and versions, and keeps portable conversation archives. Python 3.8+;
no third-party dependencies in the core.

## Install and enable automatic handoffs

```sh
curl -fsSL https://raw.githubusercontent.com/faratech/agent-session-sync/main/install.py | python3 -
agent-session-sync doctor
agent-session-sync --install --dry-run
agent-session-sync --install
```

The installer needs only Python 3.8+. It installs a managed copy under
`~/.local/share/agent-session-sync` with launchers in `~/.local/bin` (on Windows,
`%LOCALAPPDATA%\agent-session-sync` with `.cmd` launchers in its `bin`), pinned to
one commit (`--ref TAG|BRANCH|SHA`, default `main`).
When agent-session-sync is already a uv or pipx tool, it upgrades that tool instead.
Alternatively `uv tool install .` or `pipx install .` from a checkout, or run the
checkout directly: `python3 agent-session-sync.py doctor`. Keep the package
directory beside the script; downloading only the script is no longer sufficient.

### Update an existing installation

Run the same installer, or `agent-session-sync update`, on any machine where the
tool is installed, including v1 single-script installs:

```sh
curl -fsSL https://raw.githubusercontent.com/faratech/agent-session-sync/main/install.py | python3 - --dry-run
curl -fsSL https://raw.githubusercontent.com/faratech/agent-session-sync/main/install.py | python3 -
agent-session-sync update                # v2 installs; --ref and --dry-run work too
```

The installer detects the automation already in place (v1 or v2 cron lines,
scheduled tasks, harness hooks, the memory companion) and re-points exactly that
automation at the new code, keeping its state directory and captured
configuration. Machines that only use the CLI get new code and no automation.
Automation that runs from a git checkout of this repository is left alone (update
it with `git pull`); `--managed` moves it to the managed install. v1's own
`--update` cannot install v2; run the installer once on those machines.

Installation adds a two-minute scheduler (cron on Linux/macOS, Task Scheduler on
Windows) and hooks for detected Claude, Muse, Antigravity, and Copilot
installations. Start a new harness session to load its hooks. Codex history is
picked up by the scheduler and by the other harnesses' hooks.

No per-switch handoff command is required:

| Harness | Incoming conversations |
|---|---|
| Codex CLI | Imported native session in `codex resume` |
| Claude Code | Imported native session in `claude --resume` |
| Meta Muse Code (`muse`) | Latest foreign conversation for the project injected by a new-session hook |
| Antigravity (`agy`) | Latest foreign conversation for the workspace injected by a new-session hook |
| GitHub Copilot CLI (`copilot`) | Latest foreign conversation for the project injected by a new-session hook |

Context hooks leave explicit resumes and existing conversations alone. They
don't call a model or change permissions. Selection stays within the same
workspace; a newer conversation in the target harness takes precedence over an
older foreign one. You can also open a harness with the latest foreign context:

```sh
agent-session-sync open muse --project .
agent-session-sync open agy --project . --dry-run
```

## Discovery and caching

`doctor` reports executable paths, versions, data roots, and transfer modes.
Discovery searches `PATH`, common user installations, npm/pnpm and Cargo
locations, Homebrew, and Windows npm, Scoop, WinGet, and App Paths. Windows
`.exe`, `.cmd`, and `.bat` launchers are recognized.

The private `discovery.json` cache lives in the state directory. Version probes
are reused for 24 hours. Executable, `PATH`/`PATHEXT`, and profile changes
invalidate it. Missing and newly installed binaries are checked every run.
Dry runs don't run version probes or write the cache.

```sh
agent-session-sync doctor --refresh
agent-session-sync doctor --json
```

| Harness | Default data root | Override |
|---|---|---|
| Codex | `~/.codex` | `CODEX_HOME` |
| Claude | `~/.claude` | `CLAUDE_CONFIG_DIR` |
| Muse | `$XDG_DATA_HOME/muse`, otherwise `~/.local/share/muse` | `MUSE_HOME` |
| Antigravity | `~/.gemini/antigravity-cli` | `AGY_HOME` |
| Copilot | `~/.copilot` | `COPILOT_HOME` |

Installation captures data-root configuration for scheduler and hook processes,
whose environments can differ from your terminal. Reinstall automation after
changing that captured configuration.

## Inspect, export, or choose a conversation

```sh
agent-session-sync                      # sync detected harnesses once
agent-session-sync --dry-run -v          # preview without persistent writes
agent-session-sync list --project .
agent-session-sync list --harness muse --days 90
agent-session-sync show <session-id>
agent-session-sync handoff <session-id> --to copilot
agent-session-sync handoff <session-id> --to claude --launch
agent-session-sync export <session-id> --output conversation.json
agent-session-sync export <session-id> --format markdown
agent-session-sync import conversation.json
agent-session-sync import trajectory.json --harness muse --project .
```

Use a native ID, an unambiguous prefix, or `harness:profile:native-id`.
Ambiguities fail before any handoff is written. `--json` produces machine-readable
output; `--quiet` suppresses normal output. Global options work on either side
of a command. Explicit handoffs accept `--mode auto|native|context`, `--note TEXT`,
and `--budget BYTES` (default 32768, minimum 1024).

Manual imports receive a stable `imported-...` profile, preserving their origin
identity while keeping them separate from live native caches. Use the full key
printed by `import` when the original native ID also exists. Reimporting the same
snapshot is idempotent; the `imported-` profile prefix is reserved.

## Conversation fidelity and safety

Each handoff contains `context.md` (bounded recent context), `transcript.md`
(complete portable conversation), `manifest.json` (provenance and fidelity
notices), and a pinned version 1 JSON archive. Context includes the original
request excerpt, workspace metadata, recent turns, and an optional note.
Destinations share the archived readable transcript through hard links where
supported, with a guarded copy fallback on other filesystems.

Tool arguments and results stay structured in the archive. Targets receive
historical text blocks, avoiding mismatched tool-call protocols. Large tool
blocks and oversized turns can be shortened in context. Attachments remain
references; binary assets are not copied. Hidden reasoning, injected workspace
instructions, and harness-generated turns (task notifications, goal
continuations, tool-task output) are excluded. Prompts typed while an agent is
working and slash-command arguments such as `/goal <objective>` are kept, and
Claude conversations are followed across `/compact` boundaries and parallel tool
calls. No model-generated summary is required.

Bounded context prioritizes the latest user request and assistant conclusion over
older tool output. The complete handoff note is kept in the manifest and survives
continuation recovery. Renderer identities are versioned so upgrades can create
new artifacts while preserving older snapshots.

Sources are read-only. A → B → A creates a new linked conversation; earlier
sessions remain available. Unchanged imports aren't imported again, while
continuations are combined with their archived parents. Native handoffs are
snapshots: a changed source revision can create another imported session.
Earlier snapshots aren't automatically deleted.

Writes are atomic and deterministic, and sync runs share a lock. Continued or
unfamiliar destinations are never overwritten. An unfinished final JSONL record
is deferred. A record torn by an interleaved writer, or cut short by a crash
before the writer continued, is dropped and reported as a session issue; other
malformed transcripts are refused. Dry runs create no state, locks, archives, or
hooks.

Antigravity's full transcript is preferred over its compact transcript. Its
workspace comes from `history.jsonl`, `cache/conversation_metadata.json`, and
`cache/last_conversations.json`, and hooks capture associations for new sessions.
Conversations still without a workspace are skipped during automatic sync; use
`workspace_map` or an explicit `--project` handoff for them.

## Configuration and additional harnesses

Configuration defaults to `$XDG_CONFIG_HOME/agent-session-sync/config.json`
(normally `~/.config/...`), or `%LOCALAPPDATA%\agent-session-sync\config.json` on
Windows. Override with `--config PATH` or `AGENT_SESSION_SYNC_CONFIG`.

Relative data roots, settings paths, and explicit executable paths are resolved
from the configuration file's directory. A missing explicitly selected file is
an error. UTF-8 input files may include a BOM; protocol and JSON output use UTF-8
independently of the terminal's locale.
Local `file:` workspace URIs are decoded before launching or writing native
sessions. Windows network URIs retain their UNC host; nonlocal file URIs on
Linux and macOS are reported rather than treated as local directories.

```json
{
  "harnesses": {
    "muse": {"home": "/custom/muse", "executable": "/custom/bin/muse"},
    "agy": {"workspace_map": {"conversation-uuid": "/work/my-project"}}
  },
  "profiles": [
    {"harness": "codex", "name": "work", "home": "/work/codex-home"}
  ]
}
```

Without `routes`, sync connects detected harnesses. To restrict it:

```json
{
  "routes": [
    {"from": "codex", "to": "muse", "project": "/work/my-project"},
    {"from": "muse", "to": "codex"}
  ]
}
```

An empty route list disables automatic transfers. Explicit handoffs still work.
`auto_handoff: false` globally or per harness disables startup context injection;
`enabled: false` per harness excludes it from discovery.

Profiles sharing a startup-hook settings file are identified by their transcript
paths. If a harness omits that path at startup, automatic context injection is
skipped rather than assuming the default profile. Use separate profile settings
files or `open harness@profile` in that case.

Additional CLIs can use portable files, executable adapters, or installed Python
entry points. See [the adapter contract](docs/adapters.md) and
[the executable example](examples/portable_adapter.py). Private formats need an
adapter or an export; TUIs aren't scraped.

## State, upgrades, and removal

Override state with `AGENT_SESSION_SYNC_STATE_DIR` or `--state-dir`. Defaults:
`/var/lib/agent-session-sync` for POSIX root; `$XDG_STATE_HOME/agent-session-sync`
(otherwise `~/.local/state/...`) for other POSIX users;
`%LOCALAPPDATA%\agent-session-sync` for Windows.

`sessions.sqlite3` holds the index and transfer ledger. Archives and handoffs are
ordinary local files. The v1 `state.json` is backed up as `state-v1.json.bak`
before migration. Registered legacy files and IDs remain intact; new native
handoffs use independent snapshots, so a live legacy continuation is never
replaced during migration. Keep archives and the ledger together to preserve
lineage. Project-scoped scans use workspace hints to avoid decoding unrelated
cached histories; completed transfers repair missing generated artifacts.

```sh
agent-session-sync update               # managed, uv, or pipx installs
git pull                               # checkout
agent-session-sync --uninstall --dry-run
agent-session-sync --uninstall
agent-session-sync --uninstall --purge   # also remove state and archives
```

Uninstall removes this utility's automation; native session histories remain.
Settings are backed up before modification and unrelated entries are preserved.
`--no-cron` and `--no-hooks` select one part. `update` (or `--update`) runs the
published installer at a pinned commit; see "Update an existing installation".

Recorded hook destinations support cleanup after disabling or removing profiles.
Purge coordinates with running syncs and retains lock sentinels. When automation
is intentionally retained with `--no-cron` or `--no-hooks`, its captured config is
also retained.

## Memory companion

Memory sync stays separate and opt-in:

```sh
python3 memory-sync.py --dry-run -v
memory-sync --dry-run -v                 # installed package
agent-session-sync --install --memories
agent-session-sync --uninstall --memories
```

It owns two generated memory files and pointers, with a separate lock and registry.
Missing outputs and pointers are repaired while unchanged bundles keep their
mtime. Index mode reads each source once; auto mode accounts for the complete
bundle's size. Missing source exports leave the last snapshot unchanged.
Scheduled memory jobs capture their default-profile data roots and state path.
Codex SQLite memories aren't read. Plain session uninstall leaves memory
automation running. Earlier options are documented in
the [v1 reference](docs/v1-reference.md#memory-sync-opt-in-companion).

## Development

```sh
python3 -m unittest discover -s tests -v
python3 tests/smoke_harnesses.py -v       # optional installed CLIs; offline mocks/echo
```

Fixtures contain invented conversations shaped after documented formats and local
record-shape inspection. Tests cover all 20 directed handoffs, parsing,
continuations, ownership, discovery caching, dry runs, hook protocols, the
installer's in-place updates, and a regression test for every fixed issue
(`tests/test_issue_regressions.py`). CI runs on Linux, macOS, and Windows with
Python 3.8 and 3.13. Harness formats can change; detected
versions aren't a claim of verified compatibility with every release.
See [format references](docs/adapters.md#format-references).

MIT licensed.
