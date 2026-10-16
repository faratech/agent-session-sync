import json
import os
from pathlib import Path
from unittest import mock

from test_sync import Workspace
from agent_session_sync.adapters import FileAdapter, SessionRef
from agent_session_sync.discovery import Installation
from agent_session_sync.io import normalized_workspace, resolved_workspace, workspace_path, SyncError, json_bytes, atomic_share
from agent_session_sync.discovery import load_config


class FinalReviewTests(Workspace):
    def test_normalized_continuation_survives_portable_adapter_roundtrip(self):
        engine = self.engine()
        original = self.read("codex")
        item = engine.handoff(original, "claude")
        target = Path(item["target"])
        records = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                             "cwd": str(self.project), "timestamp": "2026-10-15T11:00:00Z",
                             "message": {"role": "user", "content": "Now add regression tests."}})
        native = self.adapters["claude"].read(SessionRef(item["native_id"], str(target)))
        continued = engine.lineage(native)
        exported = self.base / "exported.json"
        exported.write_bytes(json_bytes(continued.to_dict()))
        adapter = FileAdapter(Installation("portable", "default", str(self.base), "", "", True))
        reread = adapter.read(SessionRef(exported.stem, str(exported)))
        restored = engine.lineage(reread)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.events, continued.events)
        self.assertEqual(restored.parent, continued.parent)
        self.assertEqual(engine.lineage(continued).events, continued.events)
        self.assertNotIn("lineage_normalized", native.provenance)

    def test_legacy_continuation_is_recognized_through_directory_alias(self):
        engine = self.engine()
        actual = self.paths["claude"]
        alias = self.base / "claude-alias"
        try:
            alias.symlink_to(self.base / "claude", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        registered = alias / actual.relative_to(self.base / "claude")
        engine.store.db.execute("INSERT INTO legacy_targets VALUES (?, ?, ?, ?)",
                                (str(self.paths["codex"]), "claude", str(registered), "old-hash"))
        native = self.read("claude")
        native.provenance = {"legacy_import": True}
        continued = engine.lineage(native)
        self.assertIsNotNone(continued)
        self.assertEqual(continued.parent, "legacy:" + str(self.paths["codex"]))

    def test_sync_serializes_source_once_and_shares_readable_transcripts(self):
        import agent_session_sync.engine as module
        engine = self.engine()
        with mock.patch.object(module, "markdown", wraps=module.markdown) as render:
            results = engine.sync(source="muse", days=None)
        self.assertEqual(len(results), 4)
        self.assertEqual(render.call_count, 1)
        paths = [Path(item["context"]).with_name("transcript.md") for item in results]
        self.assertTrue(all(path.read_bytes() == paths[0].read_bytes() for path in paths))
        # State artifacts share one filesystem, where normal hard-link support
        # is already used by atomic_write. Fallback behavior is tested below.
        self.assertTrue(all(os.path.samefile(path, paths[0]) for path in paths))

    def test_shared_artifact_falls_back_to_a_guarded_copy(self):
        source, target = self.base / "source.md", self.base / "copy.md"
        source.write_bytes(b"Complete transcript.")
        link = os.link
        def no_source_link(original, destination):
            if Path(original) == source.resolve():
                raise OSError("hard links unavailable for this source")
            return link(original, destination)
        with mock.patch("agent_session_sync.io.os.link", side_effect=no_source_link):
            atomic_share(source, target, source.read_bytes())
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertFalse(os.path.samefile(source, target))
        target.write_bytes(b"My own content.")
        with self.assertRaises(SyncError):
            atomic_share(source, target, source.read_bytes())
        self.assertEqual(target.read_bytes(), b"My own content.")

    def test_changed_shared_source_is_rejected(self):
        source = self.base / "source.md"
        source.write_bytes(b"changed content")
        target = self.base / "target.md"
        with self.assertRaises(SyncError):
            atomic_share(source, target, b"expected content")
        self.assertFalse(target.exists())
    def test_file_uri_forms_decode_without_losing_host_identity(self):
        self.assertEqual(workspace_path("file:///C:/a%20b", windows=True), "C:/a b")
        self.assertEqual(workspace_path("file://localhost/C:/a", windows=True), "C:/a")
        self.assertEqual(workspace_path("file://server/share/a%20b", windows=True), "//server/share/a b")
        self.assertEqual(workspace_path("file://C:/a", windows=True), "C:/a")
        with self.assertRaises(SyncError):
            workspace_path("file://server/share/a", windows=False)
        for uri in ("file://user:password@server/share/a", "file:///tmp/a?query", "file:///tmp/a#fragment"):
            with self.subTest(uri=uri), self.assertRaises(SyncError):
                workspace_path(uri, windows=True)

    def test_local_file_uri_works_in_native_output_and_configuration(self):
        engine = self.engine()
        source = self.read("muse")
        uri = self.project.as_uri()
        source.workspace = uri
        self.assertEqual(normalized_workspace(uri), normalized_workspace(str(self.project)))
        for destination in ("codex", "claude"):
            item, files = engine.plan(source, destination)
            records = [json.loads(line) for line in files[item["target"]].decode().splitlines()]
            cwd = records[0]["payload"]["cwd"] if destination == "codex" else records[0]["cwd"]
            self.assertEqual(cwd, resolved_workspace(str(self.project)))
            self.assertEqual(item["workspace"], cwd)
        config = self.base / "uris.json"
        config.write_bytes(json_bytes({"harnesses": {"agy": {"workspace_map": {"id": uri}}},
                                      "routes": [{"from": "agy", "to": "muse", "project": uri}]}))
        loaded = load_config(config)
        self.assertEqual(loaded["routes"][0]["project"], resolved_workspace(str(self.project)))
        self.assertEqual(loaded["harnesses"]["agy"]["workspace_map"]["id"], resolved_workspace(str(self.project)))

    def test_nonlocal_workspace_does_not_abort_other_sources_on_posix(self):
        if os.name == "nt":
            self.skipTest("nonlocal file URIs map to UNC paths on Windows")
        engine = self.engine()
        source = self.read("muse")
        source.workspace = "file://remote/share/project"
        with mock.patch.object(engine, "scan", return_value=[source, self.read("codex")]):
            results = engine.sync(destination="claude", days=None)
        self.assertTrue(results)
        self.assertTrue(any("nonlocal file URI" in error for error in engine.errors))
