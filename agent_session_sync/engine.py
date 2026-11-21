"""Harness-independent discovery, branching, transfer planning, and application."""
import copy
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import BUILTINS, PARSER_VERSION, SessionRef, WORKSPACE_UNKNOWN, choose_title
from .blobs import Blob, archive_bytes, blob_dir, internalize, read_archive, write_blob
from .io import SyncError, atomic_write, digest, json_bytes, loads, normalized_workspace, resolved_workspace, SharedArtifact, atomic_share, zstd_available
from .model import Session, Event, identity, markdown, instant
from .render import DEFAULT_BUDGET, RENDER_VERSION, compact, fits_hook_output, hook_output_limit, injection_budget, jsonl_bytes, native_plan
from .store import Store


def event_fingerprint(events):
    return digest(json_bytes([{k: v for k, v in asdict(e).items() if k not in ("id", "timestamp")} for e in events]))


def prefix_match(selector, key, native_id):
    """Unambiguous-prefix selection over a native id or a full harness:profile:id key."""
    # An empty selector is a prefix of everything; it names no session.
    return bool(selector) and (native_id.startswith(selector) or key.startswith(selector))


# A transcript at least this large, written within HOT_SECONDS, is still being written:
# its full document is not cached and its snapshots are rate-limited.
HOT_BYTES = 32 * 1024 * 1024
HOT_SECONDS = 120


def hot(stat, now=None):
    return bool(stat) and stat.st_size >= HOT_BYTES and (time.time() if now is None else now) - stat.st_mtime < HOT_SECONDS


def compressed_from(previous, path, stamp):
    """A stored parse of the plain rollout Codex has since compressed into path (same mtime)."""
    if not previous or not stamp or not path.endswith(".jsonl.zst"):
        return False
    source, old, _ = previous
    return (source == path[:-4] and isinstance(old, list) and len(old) == 4
            and old[0] == stamp[0] and old[2] == stamp[2] and old[3] == stamp[3])


def grown_since(previous, path, stamp):
    """True when a stored parse stamp describes an earlier, shorter state of the same append-only file
    whose parser and cache inputs are unchanged, so the workspace parsed then still applies."""
    if not previous or not stamp:
        return False
    source, old, _ = previous
    return (source == path and isinstance(old, list) and len(old) == 4 and isinstance(old[1], int)
            and old[1] < stamp[1] and old[2] == stamp[2] and old[3] == stamp[3])


def keeps_workspace(adapter, path, size):
    """Adapter.keeps_workspace for any adapter (plugins need not define it); unreadable is no."""
    check = getattr(adapter, "keeps_workspace", None)
    try:
        return bool(check and check(path, size))
    except (OSError, ValueError):
        return False


DEFAULT_HOT_MINUTES = 30


def hot_interval(config):
    """Seconds between snapshots of a large session still being written
    (config hot_handoff_minutes; null, false or not positive: no limit)."""
    value = (config or {}).get("hot_handoff_minutes", DEFAULT_HOT_MINUTES)
    if value is None or value is False:
        return None
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_HOT_MINUTES
    return minutes * 60 if minutes > 0 else None


DEFAULT_INJECT_HOURS = 2


def injection_max_age(config):
    """Seconds since its last activity within which a foreign conversation is injected into a
    new session (config auto_handoff_max_age_hours; null, false or not positive: no limit)."""
    value = (config or {}).get("auto_handoff_max_age_hours", DEFAULT_INJECT_HOURS)
    if value is None or value is False:
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = DEFAULT_INJECT_HOURS
    # Beyond a century (or not finite) is no limit: the window must stay a representable date.
    return hours * 3600 if 0 < hours <= 24 * 365 * 100 else None


def file_sha(path, chunk=1 << 20):
    value = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            value.update(block)
    return value.hexdigest()


def present(target):
    """A native target exists: possibly as the .zst Codex compresses a cold rollout into, or
    moved by `codex archive` into archived_sessions/ (the user archived it: not written again)."""
    path = Path(target)
    if path.is_file() or Path(str(path) + ".zst").is_file():
        return True
    if len(path.parts) >= 5 and path.parts[-5] == "sessions":
        archived = path.parents[4] / "archived_sessions" / path.name
        return archived.is_file() or Path(str(archived) + ".zst").is_file()
    return False


def inbox_path(directory, destination, workspace):
    """The pointer to the newest context handoff for a destination and workspace."""
    return Path(directory) / "inbox" / identity(destination) / identity(normalized_workspace(workspace)) / "latest.json"


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_MIN_FREE_GB = 5


def min_free_bytes(config):
    """Free space below which sync writes no new handoffs (config min_free_gb; 0 or false disables)."""
    value = (config or {}).get("min_free_gb", DEFAULT_MIN_FREE_GB)
    if value is None or value is False:
        return 0
    try:
        return max(0, int(float(value) * 1e9))
    except (TypeError, ValueError):
        return int(DEFAULT_MIN_FREE_GB * 1e9)


def free_bytes(directory):
    path = Path(directory)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return float("inf")


@dataclass(frozen=True)
class SyncSummary:
    key: str
    endpoint: str
    source_path: str
    stamp: list
    workspace: str
    updated: str
    revision: str


class Engine:
    def __init__(self, adapters, directory, dry_run=False, config=None):
        self._delivered = {}
        self.adapters = adapters
        self.directory = Path(directory).absolute()
        self.dry_run = dry_run
        self.config = config or {}
        self.store = Store(directory, dry_run)
        self.errors = []
        # A hook's sync request, run by a detached process after the hook answers.
        self.deferred = None
        # Sources the last scan left out as still being written: (endpoint, workspace, mtime).
        self.deferred_sources = []
        # The parse stamp each scanned native session came from (see revision_of).
        self.stamps = {}
        # Parent archives read and verified by this process (see parent_archive).
        self.parents = {}
        # Codex import targets and the digests written there (see compressed_original).
        self.written = None

    def close(self):
        self.store.close()

    def allowed(self, session, destination):
        return self.route_allowed(session.endpoint, session.workspace, destination)

    def route_allowed(self, source, workspace, destination):
        routes = self.config.get("routes")
        return routes is None or any(r.get("from") == source and r.get("to") == destination
            and (not r.get("project") or normalized_workspace(r["project"]) == normalized_workspace(workspace)) for r in routes)

    def scan(self, project=None, harness=None, days=30, force=False, selector=None, defer=None, _summary_sources=None):
        """Sessions in scope, newest first. defer=(interval, fresh keys) leaves out large
        sources still being written that sync handled within the interval (see sync);
        they are listed in self.deferred_sources as (endpoint, workspace, updated).
        Automatic sync accepts summaries of unchanged sessions. _summary_sources
        records the path and stamp of full sessions eligible for a summary."""
        result = []
        use_summaries = _summary_sources is not None
        has_legacy_targets = use_summaries and self.store.db.execute("SELECT 1 FROM legacy_targets LIMIT 1").fetchone()
        self.deferred_sources = []
        since = time.time() - days * 86400 if days is not None else 0
        project_key = normalized_workspace(project) if project else None
        spellings = {}

        def normalize(value):
            # Many sessions share a few workspaces; resolve each spelling once per scan.
            if value not in spellings:
                spellings[value] = normalized_workspace(value)
            return spellings[value]

        sources = []
        scoped = next((endpoint for endpoint, a in self.adapters.items()
                       if selector and selector.startswith(a.installation.harness + ":" + a.installation.profile + ":")), None)
        for endpoint, adapter in self.adapters.items():
            if harness and harness not in (endpoint, adapter.installation.harness) or scoped and endpoint != scoped:
                continue
            try:
                sources.append((endpoint, adapter, list(adapter.discover())))
            except (OSError, SyncError) as exc:
                self.errors.append("{}: {}".format(endpoint, exc))
        imports = [Session.from_dict(document) for document in self.store.sessions(imported_only=True)]

        def source_key(adapter, ref):
            return "{}:{}:{}".format(adapter.installation.harness, adapter.installation.profile, ref.native_id)

        exact = selector is not None and (any(selector in (source_key(a, ref), ref.native_id)
                    for _, a, refs in sources for ref in refs) or any(selector in (s.key, s.native_id) for s in imports))

        def wanted(key, native_id):
            return selector is None or (selector in (key, native_id) if exact else prefix_match(selector, key, native_id))

        for endpoint, adapter, refs in sources:
            refs = [self.compressed_original(ref) for ref in refs if wanted(source_key(adapter, ref), ref.native_id)]
            if not refs:
                continue
            refs, duplicates = self.unique_refs(refs)
            try:
                # Parses depend on the parser version and on Antigravity's workspace caches.
                extra = digest(json_bytes([PARSER_VERSION, adapter.options]))
                cache_stamp = [p.stat().st_mtime_ns if p.is_file() else None
                               for p in ((adapter.home / "cache/last_conversations.json", adapter.home / "cache/conversation_metadata.json",
                                          adapter.home / "history.jsonl") if adapter.installation.harness == "agy" else ())]
            except OSError as exc:
                self.errors.append("{}: {}".format(endpoint, exc))
                continue
            for ref in refs:
                try:
                    path = Path(ref.path)
                    st = path.stat() if path.is_file() else None
                    if st and st.st_mtime < since:
                        continue
                    stamp = [st.st_mtime_ns, st.st_size, extra, cache_stamp] if st else None
                    key = source_key(adapter, ref)
                    considered = self.throttled(key, st, defer[0]) if defer and key not in defer[1] else None
                    if considered:
                        # Its conversation time (not the file's), so it competes for context slots like the rest.
                        self.deferred_sources.append((endpoint, considered[1], considered[2]))
                        continue
                    absolute = str(path.absolute())
                    summarize = (use_summaries and not force and st and not hot(st) and ref.native_id not in duplicates
                                 and type(adapter) is BUILTINS.get(adapter.installation.harness))
                    if summarize:
                        row = self.store.sync_summary(key, absolute, stamp, endpoint, ref.native_id)
                        if row and (not has_legacy_targets or self.store.legacy_import_target(absolute) is None):
                            if not project_key or normalize(row["workspace"]) == project_key:
                                result.append(SyncSummary(key, endpoint, absolute, stamp, *row))
                            continue
                    hint = self.store.workspace_hint(key, absolute, stamp) if st and not force else None
                    known = hint
                    if project_key and known is None and st and not force:
                        # A transcript that only grew since its last parse keeps that parse's
                        # workspace, so another project's active session is skipped unread.
                        previous = self.store.last_workspace(key)
                        if grown_since(previous, absolute, stamp) and keeps_workspace(adapter, absolute, previous[1][1]):
                            known = previous[2]
                        elif compressed_from(previous, absolute, stamp):
                            known = previous[2]
                    if project_key and known and normalize(known) != project_key:
                        continue
                    cached = self.store.cached(key, str(path.absolute()), stamp) if st and not force else None
                    if cached is None and st and not force and absolute.endswith(".jsonl.zst"):
                        # Codex compressed the rollout this tool had parsed (it keeps the mtime): same content.
                        cached = self.store.compressed_twin(key, absolute, stamp)
                    try:
                        session = Session.from_dict(cached) if cached else adapter.read(ref)
                    except SyncError:
                        if not cached:
                            raise
                        cached = None
                        session = adapter.read(ref)
                    has_workspace = bool(session.workspace)
                    explicit_unknown = ref.native_id in adapter.options.get("workspace_map", {}) and not adapter.options["workspace_map"][ref.native_id]
                    if not explicit_unknown:
                        session.workspace = session.workspace or self.store.workspace(endpoint, ref.native_id)
                    if session.workspace:
                        session.issues = [issue for issue in session.issues if issue != WORKSPACE_UNKNOWN]
                    if not cached and hot(st) and session.events:
                        # Stale within seconds: keep only the workspace hint, not the full document.
                        self.store.remember_workspace(session, stamp)
                        self.store.db.commit()
                    elif not cached:
                        self.store.remember(session, stamp)
                    elif hint != session.workspace:
                        self.store.remember_workspace(session, stamp)
                        self.store.db.commit()
                    if ref.native_id in duplicates:
                        session.issues = session.issues + ["the same session id is also stored at {}; this newest copy was used".format(
                            ", ".join(duplicates[ref.native_id]))]
                    raw_session = session
                    session = self.lineage(session)
                    if session and session.workspace and session.workspace != raw_session.workspace:
                        self.store.remember_workspace(session, stamp)
                        self.store.db.commit()
                    if session is None or not session.events:
                        continue
                    if project_key and normalize(session.workspace) != project_key:
                        continue
                    self.stamps[session.key] = stamp
                    result.append(session)
                    # These sessions can be reconstructed from the parse cache without
                    # consulting a workspace guess, a binding, or a parent archive.
                    if (summarize and has_workspace and session is raw_session and session.key == key
                            and session.source_path == absolute and session.provenance.keys() <= {"user_title"}):
                        _summary_sources[id(session)] = (absolute, json.dumps(stamp))
                except (SyncError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                    self.errors.append("{}: {}".format(ref.path, exc))
        keys = {s.key for s in result}
        # Imports have no source file to stat; their newest event dates them.
        # timedelta, not fromtimestamp: a window reaching before 1970 must not raise (Windows),
        # and one reaching before year 1 (or infinite) has no lower bound.
        try:
            oldest = EPOCH + timedelta(seconds=since) if days is not None else None
        except (OverflowError, ValueError):
            # Past year 9999 nothing is new enough (as for native files); before year 1, everything is.
            oldest = datetime.max.replace(tzinfo=timezone.utc) if since > 0 else None
        for session in imports:
            try:
                if oldest is not None and instant(session.updated) < oldest:
                    continue
                if (session.key not in keys and wanted(session.key, session.native_id)
                        and (not harness or harness in (session.harness, session.endpoint))
                        and (not project_key or normalized_workspace(session.workspace) == project_key)):
                    # An imported snapshot's key names its content, so its revision can be memoized.
                    self.stamps[session.key] = ["import"]
                    result.append(session)
            except SyncError as exc:
                self.errors.append("{}: {}".format(session.key, exc))
        return sorted(result, key=lambda s: (instant(s.updated), s.key), reverse=True)

    def compressed_original(self, ref):
        """The ref to read for a Codex rollout that exists both plain and compressed.

        Earlier releases wrote an import back beside the .zst Codex had compressed it into. When
        the plain file is exactly that re-write (the recorded digest), the .zst is the thread:
        it holds anything the user continued before Codex compressed it.
        """
        compressed = ref.path + ".zst"
        if not ref.path.endswith(".jsonl") or not os.path.isfile(compressed) or not zstd_available():
            return ref
        if self.written is None:
            self.written = {}
            for (document,) in self.store.db.execute("SELECT document FROM transfers WHERE destination='codex' OR destination LIKE 'codex@%'"):
                try:
                    item = json.loads(document)
                    self.written.setdefault(os.path.normpath(item["target"]), set()).add(item["target_sha"])
                except (ValueError, KeyError, TypeError):
                    continue
        expected = self.written.get(os.path.normpath(ref.path))
        try:
            if expected and file_sha(ref.path) in expected:
                return SessionRef(ref.native_id, compressed)
        except OSError:
            pass
        return ref

    @staticmethod
    def unique_refs(refs):
        """One ref per native id. Copies of a session (a moved or restored project) share
        its key, so only the most recently modified copy is read; the others are named."""
        groups = {}
        for ref in refs:
            groups.setdefault(ref.native_id, []).append(ref)
        if all(len(group) == 1 for group in groups.values()):
            return refs, {}
        def modified(ref):
            try:
                return Path(ref.path).stat().st_mtime_ns
            except OSError:
                return -1
        chosen, duplicates = set(), {}
        for native_id, group in groups.items():
            group = sorted(group, key=lambda ref: (modified(ref), ref.path), reverse=True)
            chosen.add(id(group[0]))
            if len(group) > 1:
                duplicates[native_id] = [ref.path for ref in group[1:]]
        return [ref for ref in refs if id(ref) in chosen], duplicates

    def lineage(self, session):
        provenance = session.provenance
        user_titled = bool(provenance.get("user_title"))
        if provenance.get("portable_import") or provenance.get("lineage_normalized"):
            return session
        binding = None
        if not provenance.get("transfer_id") or provenance.get("context"):
            binding = self.store.db.execute("SELECT value FROM metadata WHERE key=?",
                                           ("binding:{}:{}".format(session.endpoint, session.native_id),)).fetchone()
        if (binding and provenance.get("context") and provenance.get("transfer_id") not in (None, binding[0])
                and self.launched(session, self.store.transfer(provenance["transfer_id"]))):
            # An explicit open / handoff --launch names its own transfer in its first prompt; the
            # start hook may also have injected (and bound) another one. The launch wins.
            binding = None
        mutable = bool(provenance.get("transfer_id") or provenance.get("legacy_import") or binding)
        if mutable:
            session = copy.copy(session)
            session.issues = list(session.issues)
            session.provenance = dict(provenance, lineage_normalized=True)
        if binding and (not provenance.get("transfer_id") or provenance.get("context")):
            provenance = {"transfer_id": binding[0], "context": True}
            session.provenance = dict(provenance, lineage_normalized=True)
        if not provenance.get("transfer_id") or provenance.get("legacy_import"):
            # Legacy generated targets have no v2 provenance. Registry ownership is authoritative.
            row = self.store.legacy_import_target(session.source_path)
            if row:
                if session.source_sha == row["written_sha"]:
                    return None
                if not mutable:
                    session = copy.copy(session)
                    session.issues = list(session.issues)
                    session.provenance = dict(provenance, lineage_normalized=True)
                session.parent = "legacy:" + row["source_path"]
                session.issues.append("continued legacy import preserved as a separate branch")
            elif provenance.get("legacy_import"):
                return None
            return session
        transfer = self.store.transfer(provenance.get("transfer_id", ""))
        if provenance.get("context") and not transfer:
            # A quoted marker alone is not proof that ordinary messages were imported.
            session.provenance = {}
            return session
        baseline_count = provenance.get("baseline_count", 0)
        if (provenance.get("context") and transfer.get("mode") == "native" and transfer.get("destination") == session.endpoint
                and transfer.get("native_id") and transfer.get("native_id") == session.native_id):
            # A native import whose structural provenance was dropped (Codex migrate-rollouts
            # rewrites rollouts without it) is still that import: the thread is the one written.
            # The baseline fingerprint below still checks its imported prefix.
            baseline_count = transfer.get("baseline_count", 2)
        elif provenance.get("context"):
            # launched: the first prompt is the launch command of `open` / `handoff --launch`.
            # injected: the first prompt is the prepared context itself.
            launched, injected = self.launched(session, transfer), False
            if session.events and session.events[0].kind == "user" and transfer.get("destination") == session.endpoint:
                first = session.events[0].text
                if not launched:
                    try:
                        prepared = Path(transfer["context"]).read_text(encoding="utf-8-sig")
                        def normalize(value):
                            return value.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
                        injected = normalize(first) == normalize(prepared)
                    except (OSError, UnicodeError):
                        pass
            if binding and not launched and not injected and not self.delivered(session.endpoint, transfer):
                # Earlier releases bound a context the harness discarded (over its hook output
                # ceiling): the session never received it.
                session.provenance = {}
                return session
            if binding and not launched:
                # The start hook put this context into a new session on its own; the user's
                # turns may be any task. Link the source instead of merging its history.
                return self.link(session, transfer, strip=1 if injected else 0)
            if not (launched or injected):
                session.provenance = {}
                return session
            baseline_count = 1
            if transfer.get("mode") == "native":
                baseline_count = transfer.get("baseline_count", 2)
        if not isinstance(baseline_count, int) or baseline_count < 0 or baseline_count > len(session.events):
            session.issues.append("import boundary changed; preserving complete branch snapshot")
            session.parent = provenance.get("source_key", "")
            return session
        if transfer and transfer.get("baseline_sha") and event_fingerprint(session.events[:baseline_count]) != transfer["baseline_sha"]:
            session.issues.append("imported prefix changed; preserving complete branch snapshot")
            session.parent = transfer["source_key"]
            return session
        suffix = session.events[baseline_count:]
        if not suffix:
            return None
        session.parent = (transfer or {}).get("source_key", provenance.get("source_key", ""))
        if not transfer and provenance.get("source_key") and re.fullmatch(r"[a-f0-9]{64}", provenance.get("source_revision", "")):
            # Read a pinned local archive after ledger loss; this grants no write ownership.
            archive = self.directory / "archives" / identity(provenance["source_key"]) / (provenance["source_revision"] + ".json")
            transfer = {"archive": str(archive), "source_key": provenance["source_key"], "source_revision": provenance["source_revision"]}
        if transfer:
            try:
                parent = self.parent_archive(transfer)
                note = transfer.get("note", provenance.get("handoff_note", ""))
                notes = [Event(identity(provenance.get("transfer_id", ""), "handoff-note"), "user", note)] if note else []
                session.events = parent.events + notes + suffix
                session.parent += "@" + transfer["source_revision"]
                session.workspace = session.workspace or parent.workspace
                if not user_titled:
                    session.title = parent.title  # a title the user set on the continuation stays
            except (SyncError, OSError, ValueError):
                session.issues.append("parent archive unavailable or invalid; keeping the continuation and its provenance")
        else:
            # No ownership is reconstructed from markers alone after ledger loss.
            session.events = suffix
            session.issues.append("transfer ledger unavailable; only the new continuation could be reconstructed")
        return session

    def parent_archive(self, transfer):
        """The parent session a transfer pins, checked against its pinned key and revision.

        Archives are write-once, so the check (a full serialization of the parent) runs once
        per archive file state and is remembered; the parsed parent is reused in-process.
        """
        path = Path(transfer["archive"])
        stat = path.stat()
        expected = [stat.st_size, stat.st_mtime_ns, transfer["source_key"], transfer.get("source_revision")]
        held = self.parents.get(str(path))
        if held and held[0] == expected:
            return held[1]
        parent = Session.from_dict(read_archive(path))
        row = self.store.db.execute("SELECT signature FROM verified_archives WHERE path=?", (str(path),)).fetchone()
        if parent.key != transfer["source_key"] or not (row and loads(row[0]) == expected) and parent.revision != expected[3]:
            raise SyncError("parent archive does not match its pinned identity/revision")
        if not (row and loads(row[0]) == expected):
            self.store.db.execute("INSERT OR REPLACE INTO verified_archives VALUES (?, ?)", (str(path), json.dumps(expected)))
            self.store.db.commit()
        self.parents[str(path)] = (expected, parent)
        return parent

    @staticmethod
    def launched(session, transfer):
        """The session's first prompt is the launch command `open` / `handoff --launch` gave it."""
        command = (transfer or {}).get("command")
        return bool(transfer and transfer.get("destination") == session.endpoint and session.events
                    and session.events[0].kind == "user" and isinstance(command, list) and command
                    and session.events[0].text == command[-1])

    def delivered(self, endpoint, transfer):
        """Whether a context bound to a session of endpoint could have reached it: a harness with a
        hook output ceiling discards a larger answer. A pruned context cannot be measured: kept."""
        adapter = self.adapters.get(endpoint)
        if adapter is None or hook_output_limit(adapter) is None or not isinstance(transfer.get("context"), str):
            return True
        key = transfer.get("id")
        if key not in self._delivered:
            try:
                self._delivered[key] = fits_hook_output(adapter, Path(transfer["context"]).read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError):
                self._delivered[key] = True
        return self._delivered[key]

    @staticmethod
    def link(session, transfer, strip=0):
        """A session that started with automatically injected context: its own turns and
        title, with the injected conversation recorded as its parent rather than merged."""
        own = session.events[strip:]
        if strip and session.title == choose_title([e.text for e in session.events if e.kind == "user"], session.native_id):
            # The title was derived from the injected context; derive it from the user's own turns.
            session.title = choose_title([e.text for e in own if e.kind == "user"], session.native_id)
        session.events = own
        session.parent = "{}@{}".format(transfer["source_key"], transfer.get("source_revision", ""))
        session.workspace = session.workspace or transfer.get("workspace", "")
        session.provenance = dict(session.provenance, linked=True)
        session.issues.append("started with context injected from {}; linked, not merged".format(transfer["source_key"]))
        return session

    def select(self, selector, project=None):
        if not isinstance(selector, str) or not selector.strip():
            raise SyncError("empty session identifier")
        sessions = self.scan(days=None, force=True, selector=selector)
        candidates = [s for s in sessions if selector in (s.key, s.native_id)]
        if not candidates:
            candidates = [s for s in sessions if prefix_match(selector, s.key, s.native_id)]
        if project:
            known = [s for s in candidates if normalized_workspace(s.workspace) == normalized_workspace(project)]
            unknown = [s for s in candidates if not s.workspace]
            candidates = known or unknown
        if len(candidates) != 1:
            raise SyncError("{} session identifier: {} ({} matches)".format("ambiguous" if candidates else "unknown", selector, len(candidates)))
        session = candidates[0]
        if project and not session.workspace:
            session.workspace = resolved_workspace(project)
        return session

    def archive_path(self, session, revision=None):
        return self.directory / "archives" / identity(session.key) / ((revision or session.revision) + ".json")

    def transfer_id(self, session, destination, native, budget, note="", revision=None):
        return identity(session.key, revision or session.revision, destination, "native" if native else "context", str(budget), note, "renderer-" + str(RENDER_VERSION))

    def inbox_path(self, destination, workspace):
        return inbox_path(self.directory, destination, workspace)

    def complete(self, item):
        """Check artifact availability without re-rendering large conversations."""
        if not item or item.get("state") != "complete":
            return False
        context = Path(item["context"])
        paths = [context, Path(item["archive"]), context.with_name("manifest.json"), context.with_name("transcript.md")]
        if not all(path.is_file() for path in paths):
            return False
        if item.get("target") and not present(item["target"]):
            return False
        try:
            pointer = json.loads(self.inbox_path(item["destination"], item["workspace"]).read_text(encoding="utf-8-sig"))
            return isinstance(pointer, dict) and isinstance(pointer.get("updated"), str) and instant(pointer["updated"]) >= instant(item["updated"])
        except (OSError, ValueError, SyncError):
            return False

    def plan(self, session, destination, mode="auto", budget=DEFAULT_BUDGET, note="", _revision=None, _serialization=None,
             explicit=False):
        adapter = self.adapters.get(destination)
        if adapter is None:
            raise SyncError("unknown destination: " + destination)
        if mode == "native" and not adapter.native_import:
            raise SyncError("{} supports context handoff, not native import".format(destination))
        native = adapter.native_import and mode != "context"
        if native and adapter.installation.harness not in ("codex", "claude"):
            raise SyncError("no native writer registered for " + destination)
        if native and not session.workspace:
            raise SyncError("native handoff needs a workspace; supply --project")
        revision = _revision or session.revision
        transfer_id = self.transfer_id(session, destination, native, budget, note, revision)
        folder = self.directory / "handoffs" / transfer_id
        archive = self.archive_path(session, revision)
        context, issues = compact(session, archive, transfer_id, budget, note, folder / "manifest.json")
        item = {"id": transfer_id, "source_key": session.key, "source_revision": revision,
                "destination": destination, "mode": "native" if native else "context", "archive": str(archive),
                "context": str(folder / "context.md"), "workspace": resolved_workspace(session.workspace),
                "updated": session.updated, "issues": issues, "state": "planned"}
        item.update(renderer_version=RENDER_VERSION, note=note)
        if explicit:
            # Asked for by the user (handoff, open, sync --session): kept through the retention window.
            item["explicit"] = True
        serialization = {} if _serialization is None else _serialization
        if serialization.get("identity") != (session.key, revision):
            serialization.clear()
            serialization["identity"] = (session.key, revision)
        if "archive" not in serialization:
            data, blobs = archive_bytes(session)
            if archive.is_file():
                existing = archive.read_bytes()
                if existing != data:
                    # An archive in another format (or an earlier writer's) is kept when it holds this revision.
                    try:
                        stored = Session.from_dict(internalize(loads(existing), archive))
                    except ValueError as exc:
                        raise SyncError("corrupt archive {}: {}".format(archive, exc))
                    if stored.revision != revision:
                        raise SyncError("archive identity mismatch: " + str(archive))
                    data, blobs = existing, {}
            serialization["archive"] = data
            serialization["blobs"] = {str(blob_dir(archive) / name): blob for name, blob in blobs.items()}
        if "markdown" not in serialization:
            serialization["markdown"] = markdown(session).encode("utf-8")
        readable = archive.with_suffix(".md")
        # Blobs come first, so an archive never names a blob that is not written yet.
        files = dict(serialization.get("blobs", {}))
        files.update({str(archive): serialization["archive"], str(readable): serialization["markdown"],
                      str(folder / "transcript.md"): SharedArtifact(readable, serialization["markdown"]),
                      str(folder / "context.md"): context.encode("utf-8")})
        if native:
            old = self.store.legacy_target(session.source_path, destination)
            target, sid, records = native_plan(session, adapter, transfer_id, context, revision=revision, note=note)
            files[str(target)] = jsonl_bytes(records)
            item.update(target=str(target), native_id=sid, target_sha=digest(files[str(target)]), target_size=len(files[str(target)]),
                        command=adapter.resume(sid), baseline_count=2,
                        baseline_sha=event_fingerprint(adapter.parse(records, SessionRef(sid, str(target))).events))
            if old:
                item["legacy_target"] = old["target"]
        else:
            item["command"] = adapter.launch(folder / "context.md")
        files[str(folder / "manifest.json")] = json_bytes(dict(item, state="complete"))
        return item, files

    def apply(self, item, files):
        if self.dry_run:
            return dict(item, dry_run=True, files=list(files))
        previous = self.store.transfer(item["id"])
        # The intent is durable before any artifact is created, allowing crash recovery.
        if not previous:
            self.store.save_transfer(item)
        continued = False
        manifest = str(Path(item["context"]).with_name("manifest.json"))
        for path, data in files.items():
            if isinstance(data, SharedArtifact):
                atomic_share(data.source, path, data.data)
                continue
            if isinstance(data, Blob):
                write_blob(path, data)
                continue
            expected = None
            target = path == item.get("target")
            if target and not Path(path).exists() and present(path):
                # Codex compressed or archived this import; writing it again would duplicate the thread.
                # Keep the digest of what was written then, which is what the compressed copy holds.
                if previous and previous.get("target_sha"):
                    item = dict(item, target_sha=previous["target_sha"])
                continue
            if target and Path(path).exists():
                current = file_sha(path)
                if previous and previous.get("state") == "complete" and current != previous["target_sha"]:
                    # A resumed import must remain resumable; its continuation is ingested separately.
                    continued = True
                    continue
                if current == item["target_sha"] or previous and current == previous.get("target_sha"):
                    # An existing native history is never rewritten: a harness can continue it at any
                    # moment, and no check-then-replace is atomic against that. The copy from an
                    # earlier or interrupted attempt of this transfer is kept as it is, even when
                    # environment details outside the transfer id (CLI version) changed the bytes.
                    if current != item["target_sha"]:
                        item = dict(item, target_sha=current)
                    continue
            if path == manifest:
                # The manifest follows the target it describes.
                data = json_bytes(dict(item, state="complete"))
            if path == manifest and Path(path).is_file():
                # The manifest records the launch command, which follows the executable's location.
                try:
                    owned = json.loads(Path(path).read_bytes()).get("id") == item["id"]
                except (OSError, ValueError, AttributeError):
                    owned = False
                if owned:
                    expected = digest(Path(path).read_bytes())
            atomic_write(path, data, expected=expected)
        result = dict(item, state="complete")
        self.store.save_transfer(result)
        pointer = self.inbox_path(item["destination"], item["workspace"])
        try:
            old = json.loads(pointer.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            old = {}
        try:
            old_instant = instant(old.get("updated", "")) if isinstance(old, dict) else instant("")
        except SyncError:
            old_instant = instant("")
        if instant(item["updated"]) >= old_instant:
            atomic_write(pointer, json_bytes(result), replace=True)
        return dict(result, continued=True) if continued else result

    def handoff(self, session, destination, **kwargs):
        return self.apply(*self.plan(session, destination, **kwargs))

    def archived_earlier(self, source_key, destination, revision):
        """An import of this revision on this route, made by an earlier renderer, that the user
        archived in Codex: a re-render (an upgrade) must not bring it back as active."""
        for (document,) in self.store.db.execute("SELECT document FROM transfers WHERE source_key=? AND destination=?",
                                                 (source_key, destination)):
            try:
                item = json.loads(document)
                target = Path(item["target"])
            except (ValueError, KeyError, TypeError):
                continue
            if item.get("source_revision") != revision or Path(str(target) + ".zst").exists():
                continue
            if len(target.parts) >= 5 and target.parts[-5] == "sessions":
                archived = target.parents[4] / "archived_sessions" / target.name
                if not (archived.is_file() or Path(str(archived) + ".zst").is_file()):
                    continue
                if not target.exists():
                    return True
                # Releases before 2.2.0 wrote an archived import back into sessions/; that
                # unchanged copy does not make the conversation active again.
                from .prune import unchanged  # migration-aware: Codex may have rewritten the copy
                try:
                    if target.is_file() and item.get("target_sha") and unchanged(target, item):
                        return True
                except (OSError, SyncError):
                    pass
        return False

    def throttled(self, key, stat, interval):
        """For a large source still being written that sync handled within the interval,
        the record of that pass: [handled at, workspace, conversation updated]; else None.

        Each new revision writes a complete archive and transcript, so such a session is
        handled at most once per interval; the session whose own hook asked for the sync is
        exempt (see sync's fresh). A single handoff (an injection) does not count.
        """
        if not hot(stat):
            return None
        row = self.store.db.execute("SELECT value FROM metadata WHERE key=?", ("considered:" + key,)).fetchone()
        try:
            value = json.loads(row[0]) if row else None
            return value if time.time() - float(value[0]) < interval else None
        except (TypeError, ValueError, IndexError):
            return None

    def considered(self, session, stat):
        """Remember that sync handled this large, still-written session (see throttled)."""
        try:
            updated = instant(session.updated).timestamp() if session.updated else 0
        except (SyncError, ValueError, OverflowError):
            updated = 0
        self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                              ("considered:" + session.key, json.dumps([time.time(), session.workspace, updated])))
        self.store.db.commit()

    def revision_of(self, session):
        """session.revision, remembered against everything it is computed from.

        A revision serializes every event, which costs seconds for a long session, while
        an unchanged session's revision depends only on its parse stamp and the fields
        scan and lineage settle: workspace, title, branch, parent, issues, provenance.
        """
        stamp = self.stamps.get(session.key)
        if stamp is None:
            return session.revision
        memo = digest(json_bytes([stamp, session.workspace, session.title, session.branch, session.parent,
                                  session.issues, session.provenance, len(session.events)]))
        row = self.store.db.execute("SELECT revision FROM revisions WHERE key=? AND memo=?", (session.key, memo)).fetchone()
        if row:
            return row[0]
        revision = session.revision
        self.store.db.execute("INSERT OR REPLACE INTO revisions VALUES (?, ?, ?)", (session.key, memo, revision))
        self.store.db.commit()
        return revision

    def load_sync_summary(self, summary):
        """Materialize the exact snapshot selected during the scan."""
        try:
            document = self.store.cached(summary.key, summary.source_path, summary.stamp)
            session = Session.from_dict(document)
            session.issues = [issue for issue in session.issues if issue != WORKSPACE_UNKNOWN]
            if session.key == summary.key and session.revision == summary.revision:
                return session
        except (SyncError, ValueError, TypeError, KeyError, RecursionError):
            pass
        self.store.forget_sync_summary(summary.key)
        raise SyncError("cached transcript changed; retry on the next sync: " + summary.source_path)

    def sync(self, source=None, destination=None, project=None, days=30, budget=DEFAULT_BUDGET, fresh=None):
        """Hand off every in-scope session. fresh: keys of sessions a hook just reported,
        which are never deferred as still being written."""
        if source and not any(source in (key, adapter.installation.harness) for key, adapter in self.adapters.items()):
            raise SyncError("unknown source: " + source)
        fresh = set(fresh or ())
        interval = hot_interval(self.config)
        results = []
        targets = [a for key, a in self.adapters.items() if (not destination or destination == key)
                   and (a.installation.executable or destination)]
        if destination and destination not in self.adapters:
            raise SyncError("unknown destination: " + destination)
        # A throttled source is skipped before it is parsed: reading a long session being
        # written costs as much as the snapshot it would not get.
        summary_sources, summaries = {}, []
        sessions = self.scan(project, source, days, defer=(interval, fresh) if interval else None,
                             _summary_sources=summary_sources)
        # One current source per project/destination prevents a context inbox from replaying old tasks.
        # A deferred source is still that workspace's newest conversation where it is newer
        # (both sides compare conversation times).
        context_seen, held = set(), {}
        for endpoint, workspace, written in self.deferred_sources:
            try:
                workspace_key = normalized_workspace(workspace) if workspace else None
            except SyncError:
                workspace_key = None
            for target in targets:
                if (workspace_key and not target.native_import and target.endpoint != endpoint
                        and self.route_allowed(endpoint, workspace, target.endpoint)):
                    slot = (workspace_key, target.endpoint)
                    held[slot] = max(held.get(slot, 0), written)
        floor = min_free_bytes(self.config)
        sessions.reverse()
        while sessions:
            session = sessions.pop()
            summarized = isinstance(session, SyncSummary)
            full_session = None if summarized else session
            if floor and not self.dry_run and free_bytes(self.directory) < floor:
                self.errors.append("low disk space: under {:.1f} GB free at {}; skipped the remaining handoffs".format(
                    floor / 1e9, self.directory))
                break
            source_endpoint = session.endpoint
            if not session.workspace:
                self.errors.append("{}: workspace unknown; skipped automatic handoff".format(session.key))
                continue
            hot_stat = None
            if interval and session.source_path and not summarized:
                try:
                    stat = Path(session.source_path).stat()
                except OSError:
                    stat = None
                hot_stat = stat if hot(stat) else None
            try:
                revision = session.revision if summarized else self.revision_of(session)
                workspace_key = normalized_workspace(session.workspace)
                updated_text = session.updated
                updated = instant(updated_text).timestamp() if updated_text else 0
            except (SyncError, ValueError, OverflowError) as exc:
                self.errors.append("{}: {}".format(session.key, exc))
                continue
            if id(session) in summary_sources:
                path, stamp = summary_sources[id(session)]
                summaries.append((session.key, path, stamp, session.workspace, updated_text, revision))
            serialization, failed = {}, False
            for target in targets:
                if source_endpoint == target.endpoint:
                    continue
                if not self.allowed(session, target.endpoint):
                    continue
                context_key = (workspace_key, target.endpoint)
                if not target.native_import:
                    if context_key in context_seen:
                        continue
                    context_seen.add(context_key)
                    if held.get(context_key, 0) > updated:
                        continue
                try:
                    # A context destination gets what its start hook injects (sized to its hook output).
                    route_budget = budget if target.native_import else injection_budget(target, budget)
                    transfer_id = self.transfer_id(session, target.endpoint, target.native_import, route_budget, revision=revision)
                    stored = self.store.transfer(transfer_id)
                    if self.complete(stored):
                        top = self.store.previous(session.key, target.endpoint)
                        if not self.dry_run and top and top.get("source_revision") != revision:
                            # The source returned to this revision: its transfer is the route's
                            # newest again (prune keeps the newest, so it must not lose that place).
                            self.store.save_transfer(stored)
                        continue
                    if target.native_import and self.archived_earlier(session.key, target.endpoint, revision):
                        continue
                    if full_session is None:
                        full_session = self.load_sync_summary(session)
                    results.append(self.apply(*self.plan(full_session, target.endpoint, budget=route_budget,
                                                         _revision=revision, _serialization=serialization)))
                except (OSError, SyncError, ValueError) as exc:
                    self.errors.append("{} -> {}: {}".format(session.key, target.endpoint, exc))
                    failed = True
            if hot_stat is not None and not failed and not destination:
                # Handled for every destination: not again (except by its own hook) until the interval passes.
                self.considered(session, hot_stat)
        if summaries:
            self.store.save_sync_summaries(summaries)
        return results

    def incoming(self, destination, project, exclude_session=None, budget=None):
        max_age = injection_max_age(self.config)
        # Only a recently active conversation is injected, so only that window is scanned.
        days = 30 if max_age is None else max_age / 86400
        sessions = [s for s in self.scan(project=project, days=days) if not (s.native_id == exclude_session and
                    s.endpoint == destination) and (s.endpoint == destination or self.allowed(s, destination))]
        if sessions and sessions[0].endpoint == destination:
            return None
        candidates = [s for s in sessions if s.endpoint != destination and self.allowed(s, destination)]
        if max_age is not None:
            oldest = datetime.now(timezone.utc) - timedelta(seconds=max_age)
            candidates = [s for s in candidates if instant(s.updated) >= oldest]
        if not candidates:
            return None
        session = candidates[0]
        revision = self.revision_of(session)
        if budget is None:
            budget = injection_budget(self.adapters[destination]) if destination in self.adapters else DEFAULT_BUDGET
        stored = self.store.transfer(self.transfer_id(session, destination, False, budget, revision=revision))
        if self.complete(stored):
            return stored  # this exact handoff exists; a new session need not render it again
        floor = min_free_bytes(self.config)
        if floor and not self.dry_run and free_bytes(self.directory) < floor:
            self.errors.append("low disk space: under {:.1f} GB free at {}; skipped the injection".format(floor / 1e9, self.directory))
            return None
        return self.handoff(session, destination, mode="context", budget=budget, _revision=revision)
