"""Harness-specific readers behind a shared, versioned adapter contract."""
import importlib.metadata
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .discovery import Installation
from .io import SyncError, read_records, digest, loads, workspace_path
from .model import Event, Session, MARKER, identity, timestamp

WORKSPACE_UNKNOWN = "workspace unknown; supply workspace_map or capture a hook notification"
# Bump when a built-in parser's output changes so cached parses are refreshed.
PARSER_VERSION = 2


@dataclass
class SessionRef:
    native_id: str
    path: str

    def __post_init__(self):
        if not isinstance(self.native_id, str) or not self.native_id or not isinstance(self.path, str) or not self.path:
            raise SyncError("session references require nonempty native_id and path strings")


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (text(item) for item in value if isinstance(item, (str, dict))) if part)
    if isinstance(value, dict):
        if value.get("type", value.get("kind")) in ("text", "input_text", "output_text"):
            return value.get("text", "")
        return text(value.get("content", ""))
    return ""


def slash_command(value):
    """'/name args' for a recorded slash command whose arguments are the user's own words."""
    args = re.search(r"<command-args>(.*?)</command-args>", value, re.S)
    name = re.search(r"<command-name>(.*?)</command-name>", value, re.S)
    if args and args.group(1).strip() and name:
        return name.group(1).strip() + " " + args.group(1).strip()
    return ""


def choose_title(prompts, fallback):
    """The first prompt that states a task; a slash command with at most one
    argument (/effort medium) names a setting, while /goal <objective> is a task."""
    title = next((t for t in prompts if not re.fullmatch(r"/\S+(\s+\S+)?", t.strip())), prompts[0] if prompts else fallback)
    return title[:100].replace("\n", " ")


class Adapter:
    """Plugins implement discover/read/launch. Native rendering is optional."""
    native_import = False
    format_name = "unknown"

    def __init__(self, installation, options=None):
        self.installation = installation
        self.options = options or {}
        self.home = Path(installation.home)

    @property
    def endpoint(self):
        return self.installation.endpoint

    def discover(self):
        raise NotImplementedError

    def read(self, ref):
        records, issues, source_sha = read_records(ref.path)
        session = self.parse(records, ref)
        session.source_sha = source_sha
        session.source_path = str(Path(ref.path).absolute())
        session.issues.extend(issues)
        if session.title == session.native_id and not getattr(session, "titled", False):
            session.title = choose_title([e.text for e in session.events if e.kind == "user"], session.native_id)
        for record in records:
            if isinstance(record.get("agent_session_sync"), dict):
                session.provenance = record["agent_session_sync"]
                break
        # Context prompts identify their transfer, including when recorded by another harness.
        if not session.provenance:
            for event in session.events:
                match = re.search(re.escape(MARKER) + r"([a-f0-9-]{36})\]", event.text)
                if match:
                    session.provenance = {"transfer_id": match.group(1), "context": True}
                    break
        session.validate()
        return session

    def session(self, ref, workspace="", **kwargs):
        return Session(self.installation.harness, ref.native_id, workspace, [],
                       profile=self.installation.profile, **kwargs)

    def add(self, session, record, kind, value="", name="", call_id="", data=None):
        if not value and not data and kind != "attachment":
            return
        event_id = str(record.get("uuid") or record.get("id") or record.get("sequence") or record.get("step_index") or len(session.events))
        session.events.append(Event(identity(session.key, event_id, str(len(session.events))), kind, value,
                                    timestamp(record.get("timestamp") or record.get("recorded_at") or record.get("created_at")),
                                    str(name or ""), str(call_id or ""), data or {}))

    def launch(self, context_path):
        prompt = "Read {} and continue the task described there. {}{}]".format(
            context_path, MARKER, Path(context_path).parent.name)
        exe = self.installation.executable or self.installation.harness
        name = self.installation.harness
        if name == "agy":
            return [exe, "--prompt-interactive", prompt]
        if name == "copilot":
            return [exe, "-i", prompt]
        return [exe, prompt]

    def resume(self, session_id):
        exe = self.installation.executable or self.installation.harness
        return {"codex": [exe, "resume", session_id], "claude": [exe, "--resume", session_id],
                "muse": [exe, "resume", session_id], "agy": [exe, "--conversation", session_id],
                "copilot": [exe, "--resume", session_id]}[self.installation.harness]


class Codex(Adapter):
    native_import = True
    format_name = "rollout JSONL (session_meta / response_item)"

    def discover(self):
        for path in sorted((self.home / "sessions").glob("**/rollout-*.jsonl")):
            match = re.search(r"([a-f0-9-]{36})\.jsonl$", path.name)
            if match:
                yield SessionRef(match.group(1), str(path))

    def parse(self, records, ref):
        if not records:
            # A rollout is created before its first record; it holds no session yet.
            return self.session(ref)
        meta = next((r.get("payload", {}) for r in records if r.get("type") == "session_meta"), None)
        if meta is None:
            raise SyncError("Codex transcript lacks session_meta: " + ref.path)
        session = self.session(ref, meta.get("cwd", ""), branch=(meta.get("git") or {}).get("branch", ""))
        if (meta.get("base_instructions") or {}).get("text", "").startswith("External import of Claude Code session"):
            session.provenance = {"legacy_import": True}
        if meta.get("thread_source") == "subagent" or isinstance(meta.get("source"), dict) and "subagent" in meta["source"]:
            return session
        prompts = [r["payload"].get("message", "") for r in records
                   if r.get("type") == "event_msg" and (r.get("payload") or {}).get("type") == "user_message"]
        # Newer builds record typed prompts as completed UserMessage items instead.
        prompts += [text(r["payload"]["item"].get("content")) for r in records
                    if r.get("type") == "event_msg" and (r.get("payload") or {}).get("type") == "item_completed"
                    and isinstance(r["payload"].get("item"), dict) and r["payload"]["item"].get("type") == "UserMessage"]
        for record in records:
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload") or {}
            kind = payload.get("type")
            if kind == "message":
                role = payload.get("role")
                value = text(payload.get("content"))
                if role == "user":
                    if value not in prompts:
                        continue
                    prompts.remove(value)
                if role in ("user", "assistant"):
                    self.add(session, record, role, value)
                    for block in payload.get("content", []):
                        if isinstance(block, dict) and block.get("type") in ("input_image", "image"):
                            self.add(session, record, "attachment", "image", data={"reference": block.get("image_url", "")})
            elif kind in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                self.add(session, record, "tool_call", name=payload.get("name", kind), call_id=payload.get("call_id"),
                         data={"arguments": payload.get("arguments", payload.get("input", payload.get("action", {})))})
            elif kind in ("function_call_output", "custom_tool_call_output"):
                value = payload.get("output", "")
                self.add(session, record, "tool_result", value if isinstance(value, str) else text(value),
                         call_id=payload.get("call_id"), data={"output": value})
        return session


class Claude(Adapter):
    native_import = True
    format_name = "project JSONL (user / assistant)"

    def discover(self):
        for path in sorted((self.home / "projects").glob("*/*.jsonl")):
            if re.fullmatch(r"[a-f0-9-]{36}", path.stem):
                yield SessionRef(path.stem, str(path))

    def parse(self, records, ref):
        session = self.session(ref)
        # Claude Code names sessions itself; a user rename wins over its generated title.
        named = [t for t in (r.get("customTitle") for r in records if r.get("type") == "custom-title") if isinstance(t, str) and t.strip()] or \
                [t for t in (r.get("aiTitle") for r in records if r.get("type") == "ai-title") if isinstance(t, str) and t.strip()]
        summaries = set()
        if any(r.get("isMeta") and text((r.get("message") or {}).get("content", "")).startswith("[Imported from OpenAI Codex CLI session") for r in records[:5]):
            session.provenance = {"legacy_import": True}
        # Follow the active leaf's parent chain rather than concatenating branches.
        indexed = {r["uuid"]: r for r in records if r.get("uuid")}
        position = {id(r): i for i, r in enumerate(records)}
        leaf = next((r for r in reversed(records) if r.get("type") in ("user", "assistant")
                     and not r.get("isSidechain") and r.get("uuid")), None)
        # A leaf with an explicit null parent is a new root (an edited first prompt).
        if leaf and (leaf.get("parentUuid") in indexed or "parentUuid" in leaf and leaf["parentUuid"] is None):
            every = records
            chain, visited, bridged = [], set(), set()
            while leaf:
                if leaf.get("uuid") in visited:
                    raise SyncError("cycle in Claude parent chain: " + ref.path)
                visited.add(leaf.get("uuid"))
                chain.append(leaf)
                parent = leaf.get("parentUuid")
                # /compact boundaries restart the chain but keep a logical link to prior history.
                if parent is None and leaf.get("subtype") == "compact_boundary":
                    if leaf.get("logicalParentUuid") in indexed and leaf["logicalParentUuid"] not in visited:
                        parent = leaf["logicalParentUuid"]
                    else:
                        # The logical link often names a pending record written after the summary;
                        # the history then ends at the last conversation record before the boundary.
                        parent = next((r["uuid"] for r in reversed(records[:position[id(leaf)]]) if r.get("uuid")
                                       and r["uuid"] not in visited and not r.get("isSidechain")
                                       and r.get("type") in ("user", "assistant", "attachment", "system")), None)
                    if parent is not None:
                        bridged.add(leaf.get("uuid"))
                leaf = indexed.get(parent)
            records = list(reversed(chain))
            if bridged:
                # A bridged boundary's synthetic summary restates history now present in full;
                # an unbridged one's summary is the only account of what came before.
                kept, crossed = [], False
                for r in records:
                    if r.get("subtype") == "compact_boundary":
                        crossed = r.get("uuid") in bridged
                    if not (crossed and r.get("isCompactSummary")):
                        kept.append(r)
                records = kept
            records = with_side_records(records, every)
        for record in records:
            session.workspace = session.workspace or record.get("cwd", "")
            session.branch = session.branch or record.get("gitBranch", "")
            queued = record.get("attachment") if record.get("type") == "attachment" else None
            if isinstance(queued, dict) and queued.get("type") == "queued_command" and not record.get("isSidechain") \
                    and isinstance(queued.get("prompt"), str):
                origin = (queued.get("origin") or {}).get("kind")
                if origin == "human":
                    # Messages typed mid-turn exist only as queued-command attachments.
                    self.add(session, record, "user", queued["prompt"])
                elif origin == "auto-continuation" and queued["prompt"].startswith("Goal set: "):
                    # A /goal typed mid-turn is queued as its confirmation.
                    self.add(session, record, "user", "/goal " + queued["prompt"][len("Goal set: "):].strip())
                continue
            if record.get("type") == "system" and record.get("subtype") == "local_command" and not record.get("isSidechain"):
                command = slash_command(text(record.get("content", "")))
                if command:
                    self.add(session, record, "user", command)
                continue
            if record.get("type") not in ("user", "assistant") or record.get("isSidechain") or record.get("isMeta"):
                continue
            if record["type"] == "user" and (record.get("origin") or {}).get("kind") == "task-notification":
                # Harness-injected background task notices are not things the user said.
                continue
            message = record.get("message") or {}
            content = message.get("content", "")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            if not isinstance(blocks, list):
                raise SyncError("invalid Claude message content: " + ref.path)
            # Coalesce adjacent text without moving it across tools or attachments.
            pending = []
            def flush_text():
                value = text(pending)
                if record["type"] == "user" and value.startswith("<command-name>"):
                    value = slash_command(value) or value
                if value and not (record["type"] == "user" and value.startswith(("<command-name>", "<local-command", "<system-reminder>", "<task-notification>"))):
                    self.add(session, record, record["type"], value)
                    if record.get("isCompactSummary"):
                        summaries.add(value)
                pending.clear()
            for block in blocks:
                if isinstance(block, str) or isinstance(block, dict) and block.get("type") == "text":
                    pending.append(block)
                    continue
                flush_text()
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    self.add(session, record, "tool_call", name=block.get("name"), call_id=block.get("id"), data={"arguments": block.get("input", {})})
                elif kind == "tool_result":
                    self.add(session, record, "tool_result", text(block.get("content")), call_id=block.get("tool_use_id"), data={"output": block.get("content")})
                elif kind in ("image", "document"):
                    self.add(session, record, "attachment", kind, data=block)
            flush_text()
        if named:
            session.title = named[-1].strip()[:100]
        elif summaries:
            # A compaction summary restates earlier work; it never names this session.
            session.title = choose_title([e.text for e in session.events if e.kind == "user" and e.text not in summaries], session.native_id)
            session.titled = True  # transient: read() must not fall back to the summary
        return session


def with_side_records(chain, every):
    """Records Claude Code writes beside the active chain without abandoning it.

    Parallel tool calls from one API message are chained call to call, and each
    result hangs off its own call, so results (and, when they return out of order,
    whole calls) sit on side branches. A /goal typed during a tool run is likewise
    written as a goal_status/local_command branch off that call. Abandoned branches
    (rewinds and retries) are new prompts or new API messages and stay excluded.
    """
    def blocks(r):
        content = (r.get("message") or {}).get("content")
        return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    included = {r.get("uuid") for r in chain}
    by_uuid = {r.get("uuid"): r for r in every if r.get("uuid")}
    messages = {(r.get("message") or {}).get("id") for r in chain if r.get("type") == "assistant"} - {None}
    side, changed = [], True
    while changed:
        changed = False
        members = chain + side
        calls = {b.get("id") for r in members if r.get("type") == "assistant" for b in blocks(r) if b.get("type") == "tool_use"}
        calls -= {b.get("tool_use_id") for r in members if r.get("type") == "user" for b in blocks(r) if b.get("type") == "tool_result"}
        for r in every:
            if r.get("uuid") in included or r.get("parentUuid") not in included or r.get("isSidechain") or not r.get("uuid"):
                continue
            found = blocks(r)
            if r.get("type") == "assistant":
                # Split from one API message: same message id, or chained directly to an assistant record
                # (a retry would branch from a user or tool-result record instead).
                sibling = (r.get("message") or {}).get("id") in messages or (by_uuid.get(r["parentUuid"]) or {}).get("type") == "assistant"
                keep = sibling and found and all(b.get("type") == "tool_use" for b in found)
            elif r.get("type") == "user":
                keep = found and all(b.get("type") == "tool_result" and b.get("tool_use_id") in calls for b in found)
            else:
                keep = r.get("type") == "system" and r.get("subtype") == "local_command" or \
                    r.get("type") == "attachment" and (r.get("attachment") or {}).get("type") == "goal_status"
            if keep:
                side.append(r)
                included.add(r["uuid"])
                changed = True
    if not side:
        return chain
    position = {id(r): i for i, r in enumerate(every)}
    children = {}
    for r in sorted(side, key=lambda r: position[id(r)]):
        children.setdefault(r["parentUuid"], []).append(r)
    result = []
    def emit(r):
        result.append(r)
        for child in children.get(r.get("uuid"), []):
            emit(child)
    for r in chain:
        emit(r)
    return result


class Muse(Adapter):
    format_name = "journal schema_version=1 / export_schema_version=1"

    def discover(self):
        for path in sorted((self.home / "sessions").glob("*/*/*/*/session.jsonl")):
            yield SessionRef(path.parent.name, str(path))

    def parse(self, records, ref):
        session = self.session(ref)
        expanded = []
        for exported in records:
            record = exported.get("envelope", exported)
            if not isinstance(record, dict):
                raise SyncError("invalid Muse export envelope: " + ref.path)
            if "retained_frame" in record:
                try:
                    expanded.extend(loads(c["record_json"]) for c in record["children"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise SyncError("invalid Muse transaction: {}: {}".format(ref.path, exc))
            else:
                expanded.append(record)
        if Path(ref.path).suffix.lower() == ".json":
            native_id = next(((r.get("stream") or {}).get("id") for r in expanded if (r.get("stream") or {}).get("kind") == "session" and (r.get("stream") or {}).get("id")), None)
            if native_id:
                if session.title == session.native_id:
                    session.title = native_id
                session.native_id = native_id
        # Goal continuations and nudges are runs whose prompts the harness wrote, not the user.
        harness_runs = {(r.get("payload", {}).get("event") or {}).get("record", {}).get("run_id") for r in expanded
                        if (r.get("payload", {}).get("event") or {}).get("kind") == "goal_run_binding"
                        and (r["payload"]["event"].get("record") or {}).get("authority_kind") in ("continuation", "progress_nudge")} - {None}
        displayed_runs = {r.get("payload", {}).get("run_id") for r in expanded if (r.get("payload", {}).get("event") or {}).get("kind") == "user_prompt_display"}
        started_runs = {r.get("payload", {}).get("run_id") for r in expanded if (r.get("payload", {}).get("event") or {}).get("kind") == "started" and "prompt" in r.get("payload", {}).get("event", {})}
        full_prompts = {r.get("payload", {}).get("run_id"): r["payload"]["event"]["prompt"] for r in expanded
                        if (r.get("payload", {}).get("event") or {}).get("kind") == "started"
                        and isinstance(r["payload"]["event"].get("prompt"), str) and r["payload"]["event"]["prompt"]}
        committed_runs = {r.get("payload", {}).get("run_id") for r in expanded if (r.get("payload", {}).get("event") or {}).get("kind") == "assistant_message_committed"}
        for record in expanded:
            if record.get("schema_version", 1) != 1:
                raise SyncError("unsupported Muse journal schema: " + ref.path)
            payload = record.get("payload") or {}
            event = payload.get("event") or {}
            fact = payload.get("record") or {}
            kind = event.get("kind") or record.get("payload_type") or payload.get("kind")
            if kind in ("runtime.session.metadata", "session.workspace_branch.observed"):
                session.workspace = session.workspace or fact.get("workspace_root", "")
                session.branch = session.branch or (fact.get("reference") or {}).get("name", "")
            elif kind in ("user_prompt_display", "started", "runtime.user_intent.accepted") \
                    and (payload.get("run_id") or payload.get("intent_id")) in harness_runs:
                continue
            elif kind == "runtime.command_intake.received" and fact.get("client_id") == "muse-runtime-goal":
                continue
            elif kind == "inbox_item_queued" and (event.get("source") or {}).get("source") == "goal_control" \
                    and (event.get("source") or {}).get("action") == "set":
                # The objective typed with /goal is recorded only in its goal-control item.
                messages = (event.get("payload") or {}).get("model_user_messages") or []
                objective = next((m.group(1).strip() for m in (re.search(r"<objective>(.*?)</objective>", v, re.S)
                                  for v in messages if isinstance(v, str)) if m), "")
                if objective:
                    self.add(session, record, "user", "/goal " + objective)
            elif kind == "user_prompt_display":
                shown = event.get("prompt", "")
                # The display form collapses pastes to a placeholder; the run's started prompt is complete.
                if re.search(r"\[Pasted Content \d+ chars\]", shown) and full_prompts.get(payload.get("run_id")):
                    shown = full_prompts[payload["run_id"]]
                self.add(session, record, "user", shown)
            elif kind == "started" and event.get("prompt") and payload.get("run_id") not in displayed_runs:
                self.add(session, record, "user", event["prompt"])
            # Current builds link an accepted intent to its run by intent_id rather than run_id.
            elif kind == "runtime.user_intent.accepted" and (payload.get("run_id") or payload.get("intent_id")) not in displayed_runs | started_runs:
                value = "\n".join(text(m.get("content", [])) for m in payload.get("model_messages", []))
                self.add(session, record, "user", value)
            elif kind == "runtime.command_intake.received" and not displayed_runs and not started_runs:
                command = fact.get("command") or {}
                self.add(session, record, "user", (command.get("payload") or {}).get("prompt", ""))
            elif kind == "assistant_message_committed":
                self.add(session, record, "assistant", event.get("text", ""))
            # Tool tasks stream output too; only a run's own final output is an assistant reply.
            elif kind == "output" and event.get("final_result") and payload.get("run_id") not in committed_runs \
                    and payload.get("kind") != "task" and not payload.get("task_id"):
                self.add(session, record, "assistant", event.get("chunk", ""))
            elif kind == "assistant_tool_calls_committed":
                for call in event.get("tool_calls", []):
                    self.add(session, record, "tool_call", name=call.get("name"), call_id=call.get("call_id"), data={"arguments": call.get("args", {})})
            elif kind == "tool_result_batch_committed":
                for result in event.get("results", []):
                    self.add(session, record, "tool_result", result.get("text", ""), call_id=result.get("tool_call_id"))
            elif kind == "tool_output_ref":
                ref_data = event.get("output_ref") or {}
                self.add(session, record, "attachment", ref_data.get("path") or ref_data.get("uri", "tool output"), data=ref_data)
        return session


class Antigravity(Adapter):
    format_name = "step transcripts (USER_INPUT / PLANNER_RESPONSE)"

    def discover(self):
        for directory in sorted((self.home / "brain").glob("*/.system_generated/logs")):
            path = directory / "transcript_full.jsonl"
            if not path.exists():
                path = directory / "transcript.jsonl"
            if path.is_file():
                yield SessionRef(directory.parent.parent.name, str(path))

    def parse(self, records, ref):
        session = self.session(ref)
        mapping = {}
        try:
            # The prompt history names each conversation's workspace, including ones the caches dropped.
            with open(str(self.home / "history.jsonl"), encoding="utf-8-sig") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and isinstance(entry.get("conversationId"), str) \
                            and isinstance(entry.get("workspace"), str) and entry["workspace"]:
                        mapping.setdefault(entry["conversationId"], entry["workspace"])
        except OSError:
            pass
        try:
            # Every conversation records its own workspace; the latest-per-directory cache refines it.
            metadata = json.loads((self.home / "cache/conversation_metadata.json").read_text(encoding="utf-8-sig"))
            for sid, item in ((metadata or {}).get("conversations") or {}).items():
                uris = ((item or {}).get("summary") or {}).get("WorkspaceURIs") or []
                if isinstance(sid, str) and uris and isinstance(uris[0], str) and uris[0]:
                    mapping[sid] = workspace_path(uris[0])
        except (OSError, ValueError, AttributeError, TypeError, SyncError):
            pass
        try:
            latest = json.loads((self.home / "cache/last_conversations.json").read_text(encoding="utf-8-sig"))
            if isinstance(latest, dict):
                mapping.update({sid: cwd for cwd, sid in latest.items() if isinstance(sid, str) and isinstance(cwd, str)})
        except (OSError, ValueError, AttributeError):
            pass
        explicit = self.options.get("workspace_map", {})
        mapping.update(explicit)
        session.workspace = mapping.get(ref.native_id, "")
        for record in records:
            if ref.native_id not in explicit:
                session.workspace = session.workspace or record.get("cwd", "") or next(iter(record.get("workspacePaths", [])), "")
            kind = record.get("type")
            if record.get("status") not in (None, "DONE", "ERROR", "CANCELLED"):
                continue
            if record.get("truncated_fields"):
                session.issues.append("source truncated fields at step {}".format(record.get("step_index")))
            value = record.get("content", "")
            if kind == "USER_INPUT" and record.get("source", "USER_EXPLICIT") == "USER_EXPLICIT":
                match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", value, re.S)
                self.add(session, record, "user", match.group(1).strip() if match else value)
            elif kind == "PLANNER_RESPONSE":
                self.add(session, record, "assistant", value)
            elif kind in ("RUN_COMMAND", "CODE_ACTION", "TOOL_RESULT") or record.get("source") == "TOOL" or kind == "GENERIC" and record.get("source") == "MODEL":
                self.add(session, record, "tool_result", value, name=kind)
            for call in record.get("tool_calls", []):
                self.add(session, record, "tool_call", name=call.get("name"), call_id=call.get("id"), data={"arguments": call.get("args", {})})
        if not session.workspace:
            session.issues.append(WORKSPACE_UNKNOWN)
        return session


class Copilot(Adapter):
    format_name = "persisted session events (user.message / assistant.message)"

    def discover(self):
        for path in sorted((self.home / "session-state").glob("*/events.jsonl")):
            yield SessionRef(path.parent.name, str(path))

    def parse(self, records, ref):
        session = self.session(ref)
        calls = set()
        for record in records:
            if record.get("agentId") or record.get("ephemeral"):
                continue
            data = record.get("data") or {}
            kind = record.get("type")
            if kind == "session.start":
                context = data.get("context") or {}
                session.workspace = context.get("cwd") or data.get("cwd", "")
                session.branch = context.get("branch", "")
            elif kind in ("user.message", "assistant.message"):
                value = text(data.get("content", ""))
                if kind == "user.message" and value.startswith(("<skill-context", "<system-reminder>")):
                    continue
                self.add(session, record, kind.split(".")[0], value)
                for attachment in data.get("attachments", []):
                    self.add(session, record, "attachment", attachment.get("path", "attachment"), data=attachment)
                for call in data.get("toolRequests", []):
                    calls.add(call.get("toolCallId"))
                    self.add(session, record, "tool_call", name=call.get("name"), call_id=call.get("toolCallId"), data={"arguments": call.get("arguments", {})})
            elif kind == "tool.execution_start" and data.get("toolCallId") not in calls:
                calls.add(data.get("toolCallId"))
                self.add(session, record, "tool_call", name=data.get("toolName"), call_id=data.get("toolCallId"), data={"arguments": data.get("arguments", {})})
            elif kind == "tool.execution_complete":
                result = data.get("result", data.get("error", ""))
                value = result if isinstance(result, str) else text(result)
                self.add(session, record, "tool_result", value, call_id=data.get("toolCallId"), data={"output": result, "success": data.get("success")})
        return session


class FileAdapter(Adapter):
    """A sixth adapter, implemented entirely through the public contract."""
    format_name = "portable session JSON"

    def discover(self):
        for path in sorted(self.home.glob("*")):
            if path.suffix.lower() == ".json" and path.is_file():
                yield SessionRef(path.stem, str(path))

    def read(self, ref):
        try:
            raw = Path(ref.path).read_bytes()
            session = Session.from_dict(loads(raw))
        except (OSError, ValueError) as exc:
            raise SyncError("invalid portable session: {}".format(exc))
        session.harness = self.installation.harness
        session.profile = self.installation.profile
        session.native_id = ref.native_id
        session.source_path = str(Path(ref.path).absolute())
        session.source_sha = digest(raw)
        return session

    def launch(self, context_path):
        command = self.options.get("launch", [])
        return [arg.replace("{context}", str(context_path)) for arg in command]


class CommandAdapter(FileAdapter):
    format_name = "executable adapter protocol v1"

    def request(self, operation, **kwargs):
        command = self.options.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(a, str) for a in command):
            raise SyncError("executable adapters require a command argument list")
        try:
            result = subprocess.run(command, input=json.dumps(dict(protocol_version=1, operation=operation, **kwargs)).encode("utf-8"),
                                    capture_output=True, timeout=self.options.get("timeout", 30), check=True)
            value = loads(result.stdout)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise SyncError("adapter {} failed: {}".format(self.endpoint, exc))
        if not isinstance(value, dict) or type(value.get("protocol_version")) is not int or value["protocol_version"] != 1:
            raise SyncError("adapter must return protocol_version=1")
        return value

    def discover(self):
        refs = self.request("list").get("sessions")
        if not isinstance(refs, list):
            raise SyncError("adapter list response must contain a sessions array")
        for ref in refs:
            try:
                yield SessionRef(**ref)
            except TypeError as exc:
                raise SyncError("invalid adapter session reference: {}".format(exc))

    def read(self, ref):
        session = Session.from_dict(self.request("read", native_id=ref.native_id, path=ref.path).get("session"))
        session.harness, session.profile = self.installation.harness, self.installation.profile
        session.native_id = ref.native_id
        session.source_path = str(Path(ref.path).absolute())
        return session


BUILTINS = {"codex": Codex, "claude": Claude, "muse": Muse, "agy": Antigravity, "copilot": Copilot}


def get_adapters(installations, config=None):
    config = config or {}
    result = {}
    for installation in installations:
        options = dict(config.get("harnesses", {}).get(installation.harness, {}))
        options.update(next((p for p in config.get("profiles", []) if p.get("name") == installation.profile and p.get("harness") == installation.harness), {}))
        result[installation.endpoint] = BUILTINS[installation.harness](installation, options)
    for name, options in config.get("adapters", {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in result:
            raise SyncError("invalid or duplicate adapter name: " + name)
        installation = Installation(name, "default", str(Path(options.get("home", ".")).expanduser()),
                                    options.get("launch", [""])[0] if options.get("launch") else "", history_exists=True)
        result[name] = (CommandAdapter if "command" in options else FileAdapter)(installation, options)
    entries = importlib.metadata.entry_points()
    entries = entries.select(group="agent_session_sync.adapters") if hasattr(entries, "select") else entries.get("agent_session_sync.adapters", [])
    enabled = config.get("plugins", [])
    for entry in entries:
        if entry.name in enabled:
            if entry.name in result:
                raise SyncError("duplicate adapter: " + entry.name)
            result[entry.name] = entry.load()(config)
    return result
