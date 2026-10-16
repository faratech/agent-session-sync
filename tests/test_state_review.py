import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import time
from unittest import mock
from argparse import Namespace

from test_sync import Workspace, IDS
from agent_session_sync.adapters import FileAdapter, CommandAdapter
from agent_session_sync.automation import edit_hooks
from agent_session_sync.cli import main, hook, emit
from agent_session_sync.discovery import Installation, load_config
from agent_session_sync.io import SyncError, json_bytes, digest, resolved_workspace
from agent_session_sync.model import Event, imported_snapshot, timestamp
from agent_session_sync.store import Store, APPLICATION_ID


class StateReviewTests(Workspace):
    def test_native_activity_timestamps_use_latest_source_and_keep_creation(self):
        engine = self.engine()
        source = self.read("muse")
        source.events.append(Event("latest", "assistant", "Latest state.", "2030-01-01T12:00:00Z"))
        for destination in ("codex", "claude"):
            item, files = engine.plan(source, destination)
            records = [json.loads(line) for line in files[item["target"]].decode("utf-8").splitlines()]
            self.assertEqual(records[-1]["timestamp"], source.updated)
            if destination == "codex":
                self.assertEqual(records[0]["timestamp"], source.events[0].timestamp)
                self.assertEqual(records[-1]["payload"]["completed_at"], 1893499200)

    def test_project_cache_skips_unrelated_documents_and_invalidates_changed_files(self):
        engine = self.engine()
        engine.scan(days=None)
        with mock.patch.object(engine.store, "cached", wraps=engine.store.cached) as read:
            self.assertEqual(engine.scan(project=str(self.base / "other-project"), days=None), [])
        read.assert_not_called()
        records = [json.loads(line) for line in self.paths["muse"].read_text(encoding="utf-8").splitlines()]
        records[0]["payload"]["record"]["workspace_root"] = str(self.base / "other-project")
        self.paths["muse"].write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        found = engine.scan(project=str(self.base / "other-project"), days=None)
        self.assertEqual([s.harness for s in found], ["muse"])

    def test_busy_snapshot_has_bounded_wait_and_releases_connections(self):
        engine = self.engine()
        blocker = sqlite3.connect(str(self.state / "sessions.sqlite3"))
        blocker.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        try:
            with mock.patch("agent_session_sync.store.BACKUP_BUSY_TIMEOUT", 0.05):
                with self.assertRaisesRegex(SyncError, "snapshot is busy"):
                    Store(self.state, dry_run=True)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertLess(time.monotonic() - start, 2)
        snapshot = Store(self.state, dry_run=True)
        snapshot.close()
        self.assertEqual(engine.store.db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)

    def test_foreign_database_is_not_adopted_or_modified(self):
        folder = self.base / "foreign"
        folder.mkdir()
        path = folder / "sessions.sqlite3"
        db = sqlite3.connect(str(path))
        db.execute("CREATE TABLE other_application(value TEXT)")
        db.execute("INSERT INTO other_application VALUES ('keep')")
        db.commit()
        db.close()
        before = path.read_bytes()
        mode = path.stat().st_mode
        with self.assertRaisesRegex(SyncError, "not an agent-session-sync index"):
            Store(folder)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode, mode)

    def test_unsupported_index_initialization_closes_its_connection(self):
        self.state.mkdir()
        path = self.state / "sessions.sqlite3"
        db = sqlite3.connect(str(path))
        db.execute("PRAGMA user_version=99")
        db.close()
        real_connect = sqlite3.connect
        connections = []
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with mock.patch("agent_session_sync.store.sqlite3.connect", side_effect=connect):
            with self.assertRaises(SyncError):
                Store(self.state)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_antigravity_mapping_precedence_and_historical_workspace_retention(self):
        adapter = self.adapters["agy"]
        cache = adapter.home / "cache/last_conversations.json"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(json_bytes({str(self.base / "wrong-project"): IDS["agy"]}))
        self.assertEqual(self.read("agy").workspace, str(self.project))
        adapter.options = {}
        cache.write_bytes(json_bytes({str(self.project): IDS["agy"]}))
        engine = self.engine()
        self.assertEqual(engine.scan(harness="agy", days=None)[0].workspace, str(self.project))
        cache.write_bytes(json_bytes({str(self.project): "new-session"}))
        retained = engine.scan(harness="agy", days=None)[0]
        self.assertEqual(retained.workspace, str(self.project))
        self.assertNotIn("workspace unknown", " ".join(retained.issues))
        adapter.options = {"workspace_map": {IDS["agy"]: ""}}
        self.assertEqual(engine.scan(harness="agy", days=None)[0].workspace, "")

    def test_portable_import_is_namespaced_and_survives_native_scans(self):
        engine = self.engine()
        source = self.read("muse")
        source.workspace = str(self.base / "imported-project")
        snapshot = engine.store.remember(source, imported=True)
        self.assertNotEqual(snapshot.key, source.key)
        self.assertEqual(snapshot.provenance["import_source_key"], source.key)
        for _ in range(3):
            found = engine.scan(project=snapshot.workspace, days=None)
            self.assertEqual([s.key for s in found], [snapshot.key])
        self.assertEqual(imported_snapshot(snapshot).key, snapshot.key)

    def test_watched_portable_export_is_not_retained_after_its_file_is_removed(self):
        folder = self.base / "watched"
        folder.mkdir()
        exported = self.read("muse")
        exported.provenance = {"portable_import": True}
        path = folder / "session.JSON"
        path.write_bytes(json_bytes(exported.to_dict()))
        self.adapters["watched"] = FileAdapter(Installation("watched", "default", str(folder)))
        engine = self.engine()
        self.assertEqual(len(engine.scan(harness="watched", days=None)), 1)
        path.unlink()
        self.assertEqual(engine.scan(harness="watched", days=None), [])

    def test_reimporting_an_export_from_another_filename_is_idempotent(self):
        first = self.base / "one.json"
        first.write_bytes(json_bytes(self.read("muse").to_dict()))
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {name: {"home": str(a.home)} for name, a in self.adapters.items()}}))
        def run(path):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(["import", str(path), "--config", str(config), "--state-dir", str(self.state), "--json"])
            self.assertEqual(result, 0, output.getvalue())
            return json.loads(output.getvalue())["session"]
        key = run(first)
        engine = self.engine()
        snapshot = engine.select(key)
        second = self.base / "two.json"
        second.write_bytes(json_bytes(snapshot.to_dict()))
        self.assertEqual(run(second), key)
        current = self.engine().select(key)
        self.assertEqual(current.source_sha, digest(second.read_bytes()))

    def test_utf8_bom_config_settings_and_portable_files(self):
        config = self.base / "bom-config.json"
        config.write_bytes(b"\xef\xbb\xbf" + json_bytes({"auto_handoff": False}))
        self.assertFalse(load_config(config)["auto_handoff"])
        adapter = self.adapters["claude"]
        settings = self.base / "bom-settings.json"
        settings.write_bytes(b"\xef\xbb\xbf" + json_bytes({"unrelated": "界"}))
        adapter.options["settings"] = str(settings)
        _, raw, data, _ = edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"])
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(json.loads(data)["unrelated"], "界")
        folder = self.base / "portable"
        folder.mkdir()
        path = folder / "EXPORT.JSON"
        path.write_bytes(b"\xef\xbb\xbf" + json_bytes(self.read("muse").to_dict()))
        reader = FileAdapter(Installation("file", "default", str(folder)))
        ref = next(reader.discover())
        self.assertEqual(reader.read(ref).events[0].text, "Fix the login bug.")

    def test_json_output_and_hook_input_use_utf8_in_a_non_utf8_pipe_locale(self):
        output_bytes = io.BytesIO()
        output = io.TextIOWrapper(output_bytes, encoding="cp1252")
        try:
            with mock.patch("sys.stdout", output):
                emit({"text": "界😀"}, Namespace(json=True, quiet=False))
            self.assertEqual(json.loads(output_bytes.getvalue().decode("utf-8"))["text"], "界😀")
        finally:
            output.close()
        payload = json_bytes({"cwd": str(self.project), "session_id": "unicode-session", "source": "startup"})
        stdin = io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf" + payload), encoding="cp1252")
        engine = self.engine()
        try:
            with mock.patch("sys.stdin", stdin):
                hook(engine, Namespace(harness="codex", event="start"))
        finally:
            stdin.close()
        # The stored spelling is resolved but not case-folded (Claude project names depend on it).
        self.assertEqual(engine.store.workspace("codex", "unicode-session"), resolved_workspace(str(self.project)))

    def test_explicit_new_session_does_not_reinject_old_context(self):
        engine = self.engine()
        payload = {"cwd": str(self.project), "session_id": "reset", "source": "new"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch.object(engine, "incoming") as incoming:
            self.assertEqual(hook(engine, Namespace(harness="copilot", event="start")), {})
        incoming.assert_not_called()

    def test_external_protocol_json_is_decoded_from_bytes_and_rejects_boolean_versions(self):
        adapter = CommandAdapter(Installation("external", "default", str(self.base)), {"command": ["fixture"]})
        for data, valid in ((b'\xef\xbb\xbf{"protocol_version":1,"text":"\xe7\x95\x8c"}', True), (b'{"protocol_version":true}', False)):
            with self.subTest(valid=valid), mock.patch("agent_session_sync.adapters.subprocess.run", return_value=subprocess.CompletedProcess([], 0, data, b"")) as run:
                if valid:
                    self.assertEqual(adapter.request("list")["text"], "界")
                else:
                    with self.assertRaises(SyncError):
                        adapter.request("list")
                self.assertIsInstance(run.call_args.kwargs["input"], bytes)

    def test_oversized_numbers_have_actionable_validation(self):
        with self.assertRaises(SyncError):
            timestamp(10 ** 1000)
        path = self.base / "config.json"
        path.write_bytes(json_bytes({"adapters": {"external": {"command": ["fixture"], "timeout": 10 ** 1000}}}))
        with self.assertRaises(SyncError):
            load_config(path)
