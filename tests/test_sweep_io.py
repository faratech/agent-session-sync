"""Regressions for the 2026-10-30 sweep: artifact writes."""
import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_sync import Workspace
from agent_session_sync import engine as engine_module, io as sync_io, legacy
from agent_session_sync.io import SyncError, atomic_share, atomic_write, digest


def can_symlink():
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink(os.path.join(directory, "target"), os.path.join(directory, "link"))
            return True
        except (OSError, NotImplementedError):
            return False


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    @unittest.skipUnless(can_symlink(), "symlinks unavailable")
    def test_symlinked_destination_is_written_through(self):
        # Issue #98
        target = self.base / "real" / "export.json"
        target.parent.mkdir()
        target.write_bytes(b"old")
        link = self.base / "export.json"
        os.symlink(str(target), str(link))
        atomic_write(link, b"new", replace=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"new")
        atomic_write(link, b"newer", expected=digest(b"new"))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"newer")
        with self.assertRaises(SyncError):
            atomic_write(link, b"unowned")
        self.assertEqual(target.read_bytes(), b"newer")

    @unittest.skipIf(os.name == "nt", "POSIX hard-link fallback")
    def test_filesystem_without_hard_links_uses_exclusive_copy(self):
        # Issue #107
        def no_links(*args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(os, "link", no_links):
            fresh = self.base / "fresh.json"
            atomic_write(fresh, b"data")
            self.assertEqual(fresh.read_bytes(), b"data")
            self.assertEqual(oct(fresh.stat().st_mode & 0o777), oct(0o600))
            shared = self.base / "shared.md"
            atomic_share(fresh, shared, b"data")
            self.assertEqual(shared.read_bytes(), b"data")
            # Owned replacement still works; unowned files are still refused.
            atomic_write(fresh, b"next", expected=digest(b"data"))
            self.assertEqual(fresh.read_bytes(), b"next")
            with self.assertRaises(SyncError):
                atomic_write(fresh, b"other")
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.startswith(".session-sync-")], [])

    def test_exclusive_copy_never_replaces_a_concurrent_file(self):
        # Issue #107
        path = self.base / "raced.json"
        path.write_bytes(b"harness")
        with self.assertRaises(SyncError):
            sync_io.exclusive_copy(path, b"ours")
        self.assertEqual(path.read_bytes(), b"harness")


class NativeTargetTests(Workspace):
    def test_existing_native_history_is_never_rewritten(self):
        # Issue #105: no check-then-replace of a file a harness may be continuing.
        self.addCleanup(setattr, legacy, "_codex_version", None)
        engine = self.engine()
        source = self.read("muse")
        first = engine.handoff(source, "codex")
        original = Path(first["target"]).read_bytes()
        Path(legacy.CODEX_HOME, "version.json").write_text(json.dumps({"latest_version": "9.9.9"}), encoding="utf-8")
        legacy._codex_version = None
        item, files = engine.plan(source, "codex")
        self.assertEqual(item["id"], first["id"])
        self.assertNotEqual(files[item["target"]], original)
        written = []
        real = engine_module.atomic_write
        def recording(path, data, **kwargs):
            written.append(str(path))
            return real(path, data, **kwargs)
        with mock.patch.object(engine_module, "atomic_write", recording):
            result = engine.apply(item, files)
        self.assertNotIn(first["target"], written)
        self.assertEqual(Path(first["target"]).read_bytes(), original)
        self.assertEqual(result["target_sha"], digest(original))
        self.assertEqual(engine.store.transfer(first["id"])["target_sha"], digest(original))
        # A continuation appended later is still recognized as one.
        with open(first["target"], "ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{"type":"user_message","message":"more"}}\n')
        self.assertTrue(engine.apply(item, files).get("continued"))


if __name__ == "__main__":
    unittest.main()
