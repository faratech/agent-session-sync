# Adapter contract

The engine converts sources into `Session` and `Event` objects from
`agent_session_sync.model`. Serialized sessions have `schema_version: 1`;
unknown versions are rejected. Event kinds are `user`, `assistant`, `tool_call`,
`tool_result`, and `attachment`. Put structured tool data in `event.data` and
retain source event identity and timestamps.

## Portable files

The file adapter demonstrates a sixth harness with no engine changes:

```json
{
  "adapters": {
    "mycli": {
      "home": "/work/exports",
      "launch": ["mycli", "--read", "{context}"]
    }
  }
}
```

`home/*.json` files contain portable sessions. `{context}` is replaced in a launch
argument with the generated context file's absolute path. Launches use argument
lists, without shell evaluation.

Watched exports remain live inputs even when their provenance came from a manual
import. Removing a watched file removes it from discovery. Explicit manual imports
use an independent `imported-...` namespace and preserve their original source key.

## Executable adapters

A configured executable receives one JSON request on stdin and returns one JSON
response on stdout. Both include `protocol_version: 1`. Nonzero exit, invalid
output, and timeout are errors. Diagnostics belong on stderr.

```json
{
  "adapters": {
    "custom": {
      "command": ["python3", "/tools/portable_adapter.py", "/data/exports"],
      "timeout": 30,
      "launch": ["custom-cli", "--context", "{context}"]
    }
  }
}
```

| Request | Response |
|---|---|
| `{"protocol_version":1,"operation":"list"}` | `{"protocol_version":1,"sessions":[{"native_id":"id","path":"/export.json"}]}` |
| `{"protocol_version":1,"operation":"read","native_id":"id","path":"/export.json"}` | `{"protocol_version":1,"session":{...portable session...}}` |

`path` enables filesystem fingerprinting. Use a nonexistent identifier path for
remote sources that must be fetched each run. Executable adapters are read-only;
the core writes archives and contexts. They run only when explicitly configured,
including during previews. See [the example](../examples/portable_adapter.py).

## Python entry points

Declare an `agent_session_sync.adapters` entry point. Enable its name explicitly
with `"plugins": ["adapter-name"]` in user configuration. The entry point is a
factory called with configuration and returning an adapter providing:

- `installation`: `Installation` with harness/profile/home/executable metadata;
- `endpoint`: unique harness or `harness@profile` name;
- `options`, `home`, and `native_import` (false for external adapters);
- `discover()`: iterable of `SessionRef(native_id, path)`;
- `read(ref)`: validated `Session`;
- `launch(context_path)`: argument list for loading context.

Subclass `FileAdapter` or `Adapter` for helpers. External adapters use context
handoffs; built-in native renderers are reserved for Codex and Claude. Profiles
namespace native identities independently.

Profile names beginning `imported-` are reserved. Plugins should send diagnostics
to stderr and keep stdout available for the CLI's JSON and hook protocols.

## Format references

Fixtures are invented; no user transcripts are committed. Codex/Claude retain
the v1 utility's supported event families, adding strict JSONL parsing and Claude
parent-chain selection. Record shapes handled beyond those families, each with a
regression test in `tests/test_issue_regressions.py`:

- Codex: prompts from `event_msg` `user_message` or `item_completed` `UserMessage`
  items; empty rollouts are not sessions yet.
- Claude Code: `compact_boundary` records are crossed through `logicalParentUuid`,
  or the last conversation record before the boundary, and bridged
  `isCompactSummary` records are dropped; parallel tool calls and results and
  `/goal` side branches hang off the active chain; mid-turn prompts are
  `queued_command` attachments (`origin.kind == "human"`, or `Goal set:`
  auto-continuations); `<command-args>` of slash commands, including
  `system`/`local_command` records, are user text; `origin.kind ==
  "task-notification"` turns are skipped; `custom-title`, then `ai-title`, names
  the session.
- Muse: an accepted intent links to its run by `intent_id`; `[Pasted Content N
  chars]` display text is replaced by the run's started prompt; `goal_run_binding`
  continuations and nudges are harness turns, and the `goal_control` objective is
  the user's `/goal`; tool-task `output` events are not assistant replies.
- Antigravity: workspaces come from `history.jsonl`,
  `cache/conversation_metadata.json`, and `cache/last_conversations.json`.
- All JSONL: a record torn by an interleaved writer, or cut short by a crash
  before the writer continued (including NUL padding), is dropped with a session
  issue; lone UTF-16 surrogates are replaced with U+FFFD.

Changing what a built-in parser produces requires bumping `PARSER_VERSION` in
`agent_session_sync/adapters.py`, so cached parses are refreshed.

- [Muse journal and export](https://dev.meta.ai/docs/cookbook/audit-agent-sessions): schema 1, committed messages/tools, and transaction frames observed locally.
- [Muse settings hooks](https://meta-models.github.io/muse-code-sdk/next/guides/extend/hooks/): matcher groups and `hookSpecificOutput.additionalContext`.
- [Antigravity hooks](https://antigravity.google/docs/hooks/): transcript paths and `SessionStart.injectSteps`; readers handle user, planner, and historical tool-output steps.
- [Copilot events](https://github.com/github/copilot-sdk/blob/main/docs/features/streaming-events.md), [persistence](https://docs.github.com/en/copilot/concepts/security-governance-and-network-settings/session-data), and [user hooks](https://docs.github.com/en/copilot/reference/hooks-reference).

Live smoke checks must use isolated configuration/data roots and an offline echo
provider or local mock server. Tests must not use active user sessions or call a
model provider implicitly.
