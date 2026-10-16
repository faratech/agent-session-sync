"""The version 1 portable session contract."""
import json
import copy
import math
import re
import uuid
from dataclasses import asdict, dataclass, field as dataclass_field
from datetime import datetime, timezone

from .io import SyncError, digest, json_bytes

SCHEMA_VERSION = 1
MARKER = "[agent-session-sync:v1 "


def identity(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(parts, ensure_ascii=False)))


def timestamp(value=None):
    if isinstance(value, (int, float)):
        if isinstance(value, bool) or isinstance(value, float) and not math.isfinite(value):
            raise SyncError("timestamp must be a finite epoch value or ISO timestamp")
        # Journal builds use seconds, milliseconds, microseconds, or nanoseconds.
        magnitude = abs(value)
        divisor = 1000000000 if magnitude >= 100000000000000000 else 1000000 if magnitude >= 100000000000000 else 1000 if magnitude >= 100000000000 else 1
        try:
            return datetime.fromtimestamp(value / divisor, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError) as exc:
            raise SyncError("timestamp is out of range: {}".format(exc))
    return value or "1970-01-01T00:00:00+00:00"


FRACTION = re.compile(r"([T ]\d{2}:\d{2}:\d{2})\.(\d+)", re.I)


def parse_iso(value):
    """datetime.fromisoformat for RFC 3339 on every supported Python.

    Before 3.11 only 3- or 6-digit fractions and no "Z" suffix are accepted.
    """
    value = re.sub(r"[zZ]$", "+00:00", value)
    value = FRACTION.sub(lambda m: m.group(1) + "." + (m.group(2) + "000000")[:6], value, count=1)
    return datetime.fromisoformat(value)


def instant(value):
    """Comparable UTC instant without rewriting serialized source timestamps."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        raise SyncError("timestamp must be an ISO string")
    try:
        parsed = parse_iso(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise SyncError("invalid ISO timestamp: {}".format(exc))


@dataclass
class Event:
    id: str
    kind: str
    text: str = ""
    timestamp: str = ""
    name: str = ""
    call_id: str = ""
    data: dict = dataclass_field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in ("user", "assistant", "tool_call", "tool_result", "attachment"):
            raise SyncError("unsupported portable event kind: {}".format(self.kind))
        if not all(isinstance(x, str) for x in (self.id, self.text, self.timestamp, self.name, self.call_id)):
            raise SyncError("portable event text, identifiers, and timestamps must be strings")
        if not isinstance(self.data, dict):
            raise SyncError("portable event data must be an object")
        instant(self.timestamp)


@dataclass
class Session:
    harness: str
    native_id: str
    workspace: str
    events: list
    profile: str = "default"
    title: str = ""
    branch: str = ""
    source_path: str = ""
    source_sha: str = ""
    parent: str = ""
    provenance: dict = dataclass_field(default_factory=dict)
    issues: list = dataclass_field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if not isinstance(self.events, list):
            raise SyncError("portable events must be an array")
        self.events = [Event(**e) if isinstance(e, dict) else e for e in self.events]
        self.validate()
        if not self.title:
            self.title = next((e.text[:100].replace("\n", " ") for e in self.events if e.kind == "user"), self.native_id)

    def validate(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise SyncError("unsupported portable schema version: {}".format(self.schema_version))
        if not all(isinstance(v, str) for v in (self.harness, self.profile, self.native_id, self.workspace,
                  self.title, self.branch, self.source_path, self.source_sha, self.parent)):
            raise SyncError("portable identity, paths, title, branch, and parent must be strings")
        if (not self.native_id.strip() or not re.fullmatch(r"[a-z][a-z0-9_-]*", self.harness)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.profile)):
            raise SyncError("invalid portable source identity")
        if not isinstance(self.events, list) or not isinstance(self.provenance, dict) or not isinstance(self.issues, list):
            raise SyncError("invalid portable session collections")
        if any(not isinstance(e, Event) for e in self.events):
            raise SyncError("invalid portable event")
        if any(not isinstance(issue, str) for issue in self.issues):
            raise SyncError("portable issues must contain strings")
        for field in ("portable_import", "legacy_import", "context"):
            if field in self.provenance and not isinstance(self.provenance[field], bool):
                raise SyncError("provenance.{} must be a boolean".format(field))
        if "version" in self.provenance and (type(self.provenance["version"]) is not int or self.provenance["version"] != 1):
            raise SyncError("unsupported provenance version")
        if "transfer_id" in self.provenance:
            try:
                uuid.UUID(self.provenance["transfer_id"])
            except (ValueError, TypeError, AttributeError):
                raise SyncError("provenance.transfer_id must be a UUID string")
        if "baseline_count" in self.provenance and (type(self.provenance["baseline_count"]) is not int or self.provenance["baseline_count"] < 0):
            raise SyncError("provenance.baseline_count must be a nonnegative integer")
        if "source_key" in self.provenance and not isinstance(self.provenance["source_key"], str):
            raise SyncError("provenance.source_key must be a string")
        if "handoff_note" in self.provenance and not isinstance(self.provenance["handoff_note"], str):
            raise SyncError("provenance.handoff_note must be a string")
        if "import_source_key" in self.provenance and not isinstance(self.provenance["import_source_key"], str):
            raise SyncError("provenance.import_source_key must be a string")
        if "source_revision" in self.provenance and (not isinstance(self.provenance["source_revision"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", self.provenance["source_revision"])):
            raise SyncError("provenance.source_revision must be a SHA-256 revision")

    @property
    def endpoint(self):
        return self.harness if self.profile == "default" else self.harness + "@" + self.profile

    @property
    def key(self):
        return "{}:{}:{}".format(self.harness, self.profile, self.native_id)

    @property
    def revision(self):
        return digest(json_bytes({"key": self.key, "workspace": self.workspace,
                                  "title": self.title, "branch": self.branch, "parent": self.parent, "issues": self.issues,
                                  "events": [asdict(e) for e in self.events]}))

    @property
    def updated(self):
        return max((e.timestamp for e in self.events if e.timestamp), default="", key=instant)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise SyncError("expected a version 1 portable session")
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise SyncError("invalid portable session: {}".format(exc))


def event_text(event, tool_cap=None):
    if event.kind in ("tool_call", "tool_result"):
        body = event.text or json.dumps(event.data, ensure_ascii=False)
        if tool_cap and len(body) > tool_cap:
            body = body[:tool_cap] + "\n[tool text shortened; complete record in archive]"
        return "[external_agent_{}: {}]\n{}\n[/external_agent_{}]".format(
            event.kind, event.name or event.call_id, body, event.kind)
    if event.kind == "attachment":
        return "[Attachment: {}]".format(event.text or event.data.get("path", "unavailable"))
    return event.text


def markdown(session):
    lines = ["# " + session.title, "", "Session: " + session.key,
             "Workspace: " + (session.workspace or "unknown"), "Parent: " + (session.parent or "none"), ""]
    for event in session.events:
        lines.extend(["## {} {}".format(event.kind, event.timestamp), "", event_text(event), ""])
    return "\n".join(lines)


def imported_snapshot(session, source_path=None, source_sha=None):
    """Give manual imports a stable namespace independent of live cache keys."""
    session.validate()
    origin = session.key
    inherited = session.provenance.get("import_source_key")
    if inherited and session.profile.startswith("imported-"):
        parts = inherited.split(":", 2)
        if len(parts) != 3 or parts[0] != session.harness or parts[2] != session.native_id:
            raise SyncError("invalid portable import origin")
        origin = inherited
    parts = origin.split(":", 2)
    canonical = copy.copy(session)
    canonical.harness, canonical.profile, canonical.native_id = parts
    canonical.validate()
    snapshot = copy.copy(session)
    snapshot.profile = "imported-" + identity(origin, canonical.revision)[:16]
    snapshot.provenance = dict(session.provenance, portable_import=True, import_source_key=origin)
    snapshot.provenance.setdefault("agent_session_sync_import", {"source_key": origin, "source_path": session.source_path, "source_sha": session.source_sha})
    if source_path is not None:
        from pathlib import Path
        snapshot.source_path = str(Path(source_path).expanduser().absolute())
    if source_sha is not None:
        snapshot.source_sha = source_sha
    snapshot.validate()
    return snapshot
