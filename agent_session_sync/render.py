"""Bounded handoff contexts and native renderers; archives keep complete events."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import legacy
from .io import SyncError, resolved_workspace
from .model import MARKER, event_text, identity, parse_iso, timestamp

DEFAULT_BUDGET = 32768
RENDER_VERSION = 3


def cut_bytes(value, budget):
    return value[:max(budget, 0)].encode("utf-8")[:max(budget, 0)].decode("utf-8", errors="ignore")


def bounded_text(value, budget):
    if budget <= 0:
        return "", bool(value)
    if len(value) <= budget:
        encoded = value.encode("utf-8")
        if len(encoded) <= budget:
            return value, False
    marker = "\n[shortened; see full archive]\n"
    marker_size = len(marker.encode("utf-8"))
    if budget <= marker_size + 8:
        return value[-budget:].encode("utf-8")[-budget:].decode("utf-8", errors="ignore"), True
    head = (budget - marker_size) // 2
    tail = budget - marker_size - head
    return cut_bytes(value, head) + marker + value[-tail:].encode("utf-8")[-tail:].decode("utf-8", errors="ignore"), True


def compact(session, archive, transfer_id, budget=DEFAULT_BUDGET, note="", details=None):
    if type(budget) is not int or budget < 1024:
        raise SyncError("context budget must be an integer of at least 1024 bytes")
    if not isinstance(note, str):
        raise SyncError("handoff note must be a string")
    header = ("{}{}]\n# Conversation handoff\nSource: {}\nWorkspace: {}\nBranch: {}\n"
              "Full portable transcript: {}\nHistorical tool blocks describe past actions.\n").format(
                  MARKER, transfer_id, session.key, session.workspace or "unknown", session.branch or "unknown", archive)
    if note and details:
        header += "Full handoff note/details: {}\n".format(details)
    recent_label = "\nRecent conversation (selected; complete history in archive):\n"
    available = budget - len((header + recent_label).encode("utf-8"))
    if available < 128:
        raise SyncError("context metadata exceeds budget; increase --budget")
    first_user = next((i for i, e in enumerate(session.events) if e.kind == "user"), None)
    last_user = next((i for i in range(len(session.events) - 1, -1, -1) if session.events[i].kind == "user"), None)
    last_assistant = next((i for i in range(len(session.events) - 1, -1, -1) if session.events[i].kind == "assistant"), None)
    optional = ""
    shortened = False
    if first_user is not None and first_user != last_user:
        label = "\nOriginal request (excerpt):\n"
        allowance = min(1024, available // 6)
        if allowance > len(label.encode("utf-8")) + 32:
            excerpt = cut_bytes(session.events[first_user].text, allowance - len(label.encode("utf-8")) - 1)
            optional += label + excerpt + "\n"
    available -= len(optional.encode("utf-8"))
    if note:
        label = "\nHandoff note (excerpt):\n"
        allowance = min(4096, available // 4)
        excerpt, clipped = bounded_text(note, max(0, allowance - len(label.encode("utf-8")) - 1))
        optional += label + excerpt + "\n"
        available -= len((label + excerpt + "\n").encode("utf-8"))
        shortened = shortened or clipped
    selected = {}

    def take(index, allowance):
        event = session.events[index]
        label = event.kind + ": "
        value, clipped = bounded_text(event_text(event, 3000), max(0, allowance - len(label.encode("utf-8")) - 1))
        fragment = label + value + "\n"
        selected[index] = fragment
        return len(fragment.encode("utf-8")), clipped or "[tool text shortened;" in fragment

    critical = [i for i in (last_user, last_assistant) if i is not None]
    for position, index in enumerate(critical):
        used, clipped = take(index, available // (len(critical) - position))
        available -= used
        shortened = shortened or clipped
    for index in range(len(session.events) - 1, -1, -1):
        if index in selected:
            continue
        if available < 96:
            break
        used, clipped = take(index, min(available, 4096))
        available -= used
        shortened = shortened or clipped
    body = header + optional + recent_label + "".join(selected[index] for index in sorted(selected))
    issues = list(session.issues)
    if shortened or len(selected) < len(session.events):
        issues.append("context is bounded; full portable history remains in the archive")
    if any(e.kind in ("tool_call", "tool_result") for e in session.events):
        issues.append("tool activity is rendered as historical text")
    if any(e.kind == "attachment" for e in session.events):
        issues.append("attachments are references; binary assets are not copied")
    return body, list(dict.fromkeys(issues))


def native_plan(session, adapter, transfer_id, context, legacy_target=None, revision=None, note=""):
    """Render a bounded context into a new native thread, with structural provenance."""
    first = next((e.timestamp for e in session.events if e.timestamp), timestamp())
    cwd = resolved_workspace(session.workspace)
    last = session.updated or first
    try:
        dt = parse_iso(first)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except ValueError:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    last_dt = parse_iso(last)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    sid = legacy.det_uuid7(transfer_id, dt) if adapter.installation.harness == "codex" else identity(transfer_id)
    if legacy_target:
        match = re.search(r"([a-f0-9-]{36})\.jsonl$", legacy_target)
        if match:
            sid = match.group(1)
    provenance = {"version": 1, "transfer_id": transfer_id, "source_key": session.key,
                  "source_revision": revision or session.revision, "baseline_count": 2}
    if note:
        provenance["handoff_note"] = note
    messages = [("user", context), ("assistant", "Historical context imported. Ready to continue from the handoff.")]
    if adapter.installation.harness == "claude":
        path = adapter.home / "projects" / legacy.munge_cwd(cwd) / (sid + ".jsonl")
        records, parent = [], None
        for index, (role, value) in enumerate(messages):
            message = {"role": role, "content": value}
            if role == "assistant":
                message = {"id": "msg_import_" + sid, "type": "message", "role": "assistant", "model": "external",
                           "content": [{"type": "text", "text": value}], "stop_reason": "end_turn", "stop_sequence": None,
                           "usage": {"input_tokens": 0, "output_tokens": 0}}
            record = {"type": role, "uuid": identity(sid, str(index)), "parentUuid": parent,
                      "cwd": cwd, "sessionId": sid, "version": legacy.CLAUDE_VERSION,
                      "gitBranch": session.branch, "isSidechain": False, "userType": "external",
                      "timestamp": last, "message": message}
            if index == 0:
                record["agent_session_sync"] = provenance
            parent = record["uuid"]
            records.append(record)
    else:
        path = adapter.home / "sessions" / dt.strftime("%Y/%m/%d") / ("rollout-" + dt.strftime("%Y-%m-%dT%H-%M-%S") + "-" + sid + ".jsonl")
        def line(kind, payload, recorded_at=None):
            return {"timestamp": recorded_at or last, "type": kind, "payload": payload}
        records = [line("session_meta", {"id": sid, "timestamp": first, "cwd": cwd,
                    "originator": "codex-tui", "cli_version": legacy.codex_cli_version(), "source": "cli",
                    "model_provider": "openai", "base_instructions": {"text": "Imported conversation context; follow current workspace instructions."},
                    "git": {"branch": session.branch}}, first)]
        records[0]["agent_session_sync"] = provenance
        turn_id = "external-import-turn-" + transfer_id
        records.append(line("event_msg", {"type": "task_started", "turn_id": turn_id,
                                           "started_at": int(dt.timestamp()), "model_context_window": None, "collaboration_mode_kind": "default"}, first))
        for role, value in messages:
            records.append(line("response_item", {"type": "message", "role": role,
                           "content": [{"type": "input_text" if role == "user" else "output_text", "text": value}]}))
            records.append(line("event_msg", {"type": "user_message" if role == "user" else "agent_message", "message": value,
                           **({"local_images": [], "text_elements": []} if role == "user" else {"phase": None, "memory_citation": None})}))
        records.append(line("event_msg", {"type": "task_complete", "turn_id": turn_id,
                                           "last_agent_message": messages[-1][1], "completed_at": int(last_dt.timestamp())}))
    return Path(legacy_target) if legacy_target else path, sid, records


def jsonl_bytes(records):
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")
