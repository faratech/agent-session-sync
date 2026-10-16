"""Harness-independent discovery, branching, transfer planning, and application."""
import json
import copy
import re
import time
from dataclasses import asdict
from pathlib import Path

from .adapters import PARSER_VERSION, SessionRef, WORKSPACE_UNKNOWN
from .io import SyncError, atomic_write, digest, json_bytes, normalized_workspace, resolved_workspace, SharedArtifact, atomic_share
from .model import Session, Event, identity, markdown, instant
from .render import DEFAULT_BUDGET, RENDER_VERSION, compact, jsonl_bytes, native_plan
from .store import Store


def event_fingerprint(events):
    return digest(json_bytes([{k: v for k, v in asdict(e).items() if k not in ("id", "timestamp")} for e in events]))


def prefix_match(selector, key, native_id):
    """Unambiguous-prefix selection over a native id or a full harness:profile:id key."""
    return native_id.startswith(selector) or key.startswith(selector)


class Engine:
    def __init__(self, adapters, directory, dry_run=False, config=None):
        self.adapters = adapters
        self.directory = Path(directory).absolute()
        self.dry_run = dry_run
        self.config = config or {}
        self.store = Store(directory, dry_run)
        self.errors = []

    def close(self):
        self.store.close()

    def allowed(self, session, destination):
        source = session.endpoint
        routes = self.config.get("routes")
        return routes is None or any(r.get("from") == source and r.get("to") == destination
            and (not r.get("project") or normalized_workspace(r["project"]) == normalized_workspace(session.workspace)) for r in routes)

    def scan(self, project=None, harness=None, days=30, force=False, selector=None):
        result = []
        since = time.time() - days * 86400 if days is not None else 0
        project_key = normalized_workspace(project) if project else None
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
            refs = [ref for ref in refs if wanted(source_key(adapter, ref), ref.native_id)]
            if not refs:
                continue
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
                    hint = self.store.workspace_hint(key, str(path.absolute()), stamp) if st and not force else None
                    if project_key and hint and normalized_workspace(hint) != project_key:
                        continue
                    cached = self.store.cached(key, str(path.absolute()), stamp) if st and not force else None
                    try:
                        session = Session.from_dict(cached) if cached else adapter.read(ref)
                    except SyncError:
                        if not cached:
                            raise
                        cached = None
                        session = adapter.read(ref)
                    explicit_unknown = ref.native_id in adapter.options.get("workspace_map", {}) and not adapter.options["workspace_map"][ref.native_id]
                    if not explicit_unknown:
                        session.workspace = session.workspace or self.store.workspace(endpoint, ref.native_id)
                    if session.workspace:
                        session.issues = [issue for issue in session.issues if issue != WORKSPACE_UNKNOWN]
                    if not cached:
                        self.store.remember(session, stamp)
                    elif hint != session.workspace:
                        self.store.remember_workspace(session, stamp)
                        self.store.db.commit()
                    raw_session = session
                    session = self.lineage(session)
                    if session and session.workspace and session.workspace != raw_session.workspace:
                        self.store.remember_workspace(session, stamp)
                        self.store.db.commit()
                    if session is None or not session.events:
                        continue
                    if project_key and normalized_workspace(session.workspace) != project_key:
                        continue
                    result.append(session)
                except (SyncError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    self.errors.append("{}: {}".format(ref.path, exc))
        keys = {s.key for s in result}
        for session in imports:
            try:
                if (session.key not in keys and wanted(session.key, session.native_id)
                        and (not harness or harness in (session.harness, session.endpoint))
                        and (not project_key or normalized_workspace(session.workspace) == project_key)):
                    result.append(session)
            except SyncError as exc:
                self.errors.append("{}: {}".format(session.key, exc))
        return sorted(result, key=lambda s: (instant(s.updated), s.key), reverse=True)

    def lineage(self, session):
        provenance = session.provenance
        if provenance.get("portable_import") or provenance.get("lineage_normalized"):
            return session
        binding = None
        if not provenance.get("transfer_id") or provenance.get("context"):
            binding = self.store.db.execute("SELECT value FROM metadata WHERE key=?",
                                           ("binding:{}:{}".format(session.endpoint, session.native_id),)).fetchone()
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
        if provenance.get("context"):
            verified = False
            if session.events and session.events[0].kind == "user" and transfer.get("destination") == session.endpoint:
                first = session.events[0].text
                command = transfer.get("command", [])
                verified = bool(isinstance(command, list) and command and first == command[-1])
                if not verified:
                    try:
                        prepared = Path(transfer["context"]).read_text(encoding="utf-8-sig")
                        def normalize(value):
                            return value.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
                        verified = normalize(first) == normalize(prepared)
                    except (OSError, UnicodeError):
                        pass
            if not verified and not binding:
                session.provenance = {}
                return session
            baseline_count = 1 if verified else 0
            if verified and transfer.get("mode") == "native":
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
                parent = Session.from_dict(json.loads(Path(transfer["archive"]).read_text(encoding="utf-8-sig")))
                if parent.key != transfer["source_key"] or parent.revision != transfer.get("source_revision"):
                    raise SyncError("parent archive does not match its pinned identity/revision")
                note = transfer.get("note", provenance.get("handoff_note", ""))
                notes = [Event(identity(provenance.get("transfer_id", ""), "handoff-note"), "user", note)] if note else []
                session.events = parent.events + notes + suffix
                session.parent += "@" + parent.revision
                session.workspace = session.workspace or parent.workspace
                session.title = parent.title
            except (SyncError, OSError, ValueError):
                session.issues.append("parent archive unavailable or invalid; keeping the continuation and its provenance")
        else:
            # No ownership is reconstructed from markers alone after ledger loss.
            session.events = suffix
            session.issues.append("transfer ledger unavailable; only the new continuation could be reconstructed")
        return session

    def select(self, selector, project=None):
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
        return self.directory / "inbox" / identity(destination) / identity(normalized_workspace(workspace)) / "latest.json"

    def complete(self, item):
        """Check artifact availability without re-rendering large conversations."""
        if not item or item.get("state") != "complete":
            return False
        context = Path(item["context"])
        paths = [context, Path(item["archive"]), context.with_name("manifest.json"), context.with_name("transcript.md")]
        if item.get("target"):
            paths.append(Path(item["target"]))
        if not all(path.is_file() for path in paths):
            return False
        try:
            pointer = json.loads(self.inbox_path(item["destination"], item["workspace"]).read_text(encoding="utf-8-sig"))
            return isinstance(pointer, dict) and isinstance(pointer.get("updated"), str) and instant(pointer["updated"]) >= instant(item["updated"])
        except (OSError, ValueError, SyncError):
            return False

    def plan(self, session, destination, mode="auto", budget=DEFAULT_BUDGET, note="", _revision=None, _serialization=None):
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
        serialization = {} if _serialization is None else _serialization
        if serialization.get("identity") != (session.key, revision):
            serialization.clear()
            serialization["identity"] = (session.key, revision)
        if "archive" not in serialization:
            if archive.is_file():
                existing = archive.read_bytes()
                if Session.from_dict(json.loads(existing)).revision != revision:
                    raise SyncError("archive identity mismatch: " + str(archive))
                serialization["archive"] = existing
            else:
                serialization["archive"] = json_bytes(session.to_dict())
        if "markdown" not in serialization:
            serialization["markdown"] = markdown(session).encode("utf-8")
        readable = archive.with_suffix(".md")
        files = {str(archive): serialization["archive"], str(readable): serialization["markdown"],
                 str(folder / "transcript.md"): SharedArtifact(readable, serialization["markdown"]),
                 str(folder / "context.md"): context.encode("utf-8")}
        if native:
            old = self.store.legacy_target(session.source_path, destination)
            target, sid, records = native_plan(session, adapter, transfer_id, context, revision=revision, note=note)
            files[str(target)] = jsonl_bytes(records)
            item.update(target=str(target), native_id=sid, target_sha=digest(files[str(target)]),
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
            expected = None
            target = path == item.get("target")
            if target and previous and previous.get("state") == "complete":
                if Path(path).exists() and digest(Path(path).read_bytes()) != previous["target_sha"]:
                    # A resumed import must remain resumable; its continuation is ingested separately.
                    continued = True
                    continue
                # Environment details outside the transfer id (CLI version) may change the bytes.
                expected = previous["target_sha"]
            elif target and previous and previous.get("target_sha"):
                # An interrupted attempt may have written its planned target before the crash.
                expected = previous["target_sha"]
            elif path == manifest and Path(path).is_file():
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

    def sync(self, source=None, destination=None, project=None, days=30, budget=DEFAULT_BUDGET):
        if source and not any(source in (key, adapter.installation.harness) for key, adapter in self.adapters.items()):
            raise SyncError("unknown source: " + source)
        results = []
        sessions = self.scan(project, source, days)
        targets = [a for key, a in self.adapters.items() if (not destination or destination == key)
                   and (a.installation.executable or destination)]
        if destination and destination not in self.adapters:
            raise SyncError("unknown destination: " + destination)
        # One current source per project/destination prevents a context inbox from replaying old tasks.
        context_seen = set()
        for session in sessions:
            source_endpoint = session.endpoint
            if not session.workspace:
                self.errors.append("{}: workspace unknown; skipped automatic handoff".format(session.key))
                continue
            try:
                revision = session.revision
                workspace_key = normalized_workspace(session.workspace)
            except (SyncError, ValueError) as exc:
                self.errors.append("{}: {}".format(session.key, exc))
                continue
            serialization = {}
            for target in targets:
                if source_endpoint == target.endpoint:
                    continue
                if not self.allowed(session, target.endpoint):
                    continue
                context_key = (workspace_key, target.endpoint)
                if not target.native_import and context_key in context_seen:
                    continue
                context_seen.add(context_key)
                try:
                    transfer_id = self.transfer_id(session, target.endpoint, target.native_import, budget, revision=revision)
                    if self.complete(self.store.transfer(transfer_id)):
                        continue
                    item, files = self.plan(session, target.endpoint, budget=budget, _revision=revision, _serialization=serialization)
                    results.append(self.apply(item, files))
                except (OSError, SyncError) as exc:
                    self.errors.append("{} -> {}: {}".format(session.key, target.endpoint, exc))
        return results

    def incoming(self, destination, project, exclude_session=None):
        sessions = [s for s in self.scan(project=project) if not (s.native_id == exclude_session and
                    s.endpoint == destination) and (s.endpoint == destination or self.allowed(s, destination))]
        if sessions and sessions[0].endpoint == destination:
            return None
        candidates = [s for s in sessions if s.endpoint != destination and self.allowed(s, destination)]
        if not candidates:
            return None
        return self.handoff(candidates[0], destination, mode="context")
