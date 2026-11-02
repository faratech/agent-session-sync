import base64
import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from test_prune import age, age_transfer
from agent_session_sync.adapters import SessionRef
from agent_session_sync.blobs import MIN_BLOB_CHARS, compact_archives, read_archive
from agent_session_sync.cli import main
from agent_session_sync.io import SyncError, json_bytes, read_records
from agent_session_sync.model import Event
from agent_session_sync.prune import SETTLE_SECONDS, live_transfers, prune
from agent_session_sync.store import Store

DAY = 86400


def png(seed, size=MIN_BLOB_CHARS):
    """Distinct fake PNG bytes whose base64 form is at least size characters."""
    header = b"\x89PNG\r\n\x1a\n" + seed.encode("ascii")
    return header + hashlib.sha256(seed.encode("ascii")).digest() * (size // 32 + 1)


def data_url(raw, media="image/png"):
    return "data:{};base64,{}".format(media, base64.b64encode(raw).decode("ascii"))


class BlobWorkspace(Workspace):
    def screenshot(self, name, raw, call):
        """A tool result with an inline screenshot, as Codex records it."""
        self.append(self.paths[name], {"timestamp": "2026-10-15T12:00:00Z", "type": "response_item",
                                       "payload": {"type": "function_call_output", "call_id": call,
                                                   "output": [{"type": "input_text", "text": "screenshot " + call},
                                                              {"type": "input_image", "image_url": data_url(raw)}]}})
        return self.read(name)

    def blobs(self):
        folder = self.state / "archives" / "blobs"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class ArchiveFormatTests(BlobWorkspace):
    def test_images_are_stored_once_and_restored_exactly(self):
        engine = self.engine()
        first_image, second_image = png("one"), png("two")
        first_session = self.screenshot("codex", first_image, "shot-1")
        first = engine.handoff(first_session, "claude")
        second_session = self.screenshot("codex", second_image, "shot-2")
        second = engine.handoff(second_session, "claude")
        self.assertEqual(len(self.blobs()), 2, "the image both snapshots share is stored once")
        for item, session in ((first, first_session), (second, second_session)):
            archive = Path(item["archive"])
            raw = archive.read_bytes()
            self.assertLess(len(raw), MIN_BLOB_CHARS, "the archive no longer inlines images")
            stored = json.loads(raw)
            self.assertEqual(stored["archive_version"], 2)
            for entry in stored["blobs"]:
                node = stored
                for step in entry["path"]:
                    node = node[step]
                self.assertEqual(node, entry["file"])
                self.assertTrue((archive.parent / entry["file"]).is_file(), "a reader can open the image beside the archive")
            self.assertEqual(read_archive(archive), session.to_dict())
            # The readable transcript and its hard link are unchanged by the archive format.
            readable = archive.with_suffix(".md")
            transcript = Path(item["context"]).with_name("transcript.md")
            self.assertEqual(readable.stat().st_ino, transcript.stat().st_ino)
        sha = hashlib.sha256(first_image).hexdigest()
        self.assertEqual((self.state / "archives" / "blobs" / (sha + ".png")).read_bytes(), first_image)
        engine.sync(destination="claude")   # the other fixture conversations
        files = {str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file() and p.suffix != ".sqlite3"}
        self.assertEqual(engine.sync(destination="claude"), [], "a v2 archive is recognized as complete")
        self.assertEqual(engine.handoff(second_session, "claude")["id"], second["id"])
        self.assertEqual({str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file() and p.suffix != ".sqlite3"}, files)

    def test_small_or_unusual_images_stay_inline_in_a_version_1_archive(self):
        engine = self.engine()
        session = self.screenshot("codex", b"\x89PNG small", "small")
        unusual = data_url(png("odd"))
        session.events.append(Event("odd", "tool_result", "", "2026-10-15T12:00:01Z",
                                    data={"wrapped": unusual[:200] + "\n" + unusual[200:]}))
        item = engine.handoff(session, "claude")
        self.assertEqual(Path(item["archive"]).read_bytes(), json_bytes(session.to_dict()))
        self.assertEqual(self.blobs(), [])

    def test_a_continuation_is_rebuilt_from_a_version_2_parent(self):
        engine = self.engine()
        session = self.screenshot("codex", png("parent"), "shot")
        item = engine.handoff(session, "claude")
        self.assertEqual(json.loads(Path(item["archive"]).read_bytes())["archive_version"], 2)
        target = Path(item["target"])
        records, _, _ = read_records(target)
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                             "cwd": str(self.project), "timestamp": "2026-10-15T13:00:00Z",
                             "message": {"role": "user", "content": "Now add regression tests."}})
        continued = engine.lineage(self.adapters["claude"].read(SessionRef(item["native_id"], str(target))))
        self.assertEqual(continued.events[:len(session.events)], session.events, "the parent's images are restored")
        self.assertEqual(continued.events[-1].text, "Now add regression tests.")
        self.assertFalse(any("unavailable" in issue for issue in continued.issues))

    def test_a_version_1_archive_still_loads_and_is_kept(self):
        engine = self.engine()
        session = self.screenshot("codex", png("legacy"), "shot")
        archive = engine.archive_path(session)
        archive.parent.mkdir(parents=True)
        legacy = json_bytes(session.to_dict())   # written by 2.1.2 and earlier
        archive.write_bytes(legacy)
        self.assertEqual(read_archive(archive), session.to_dict())
        item = engine.handoff(session, "claude")
        self.assertEqual(archive.read_bytes(), legacy, "an existing archive of this revision is not rewritten")
        self.assertEqual(self.blobs(), [])
        target = Path(item["target"])
        records, _, _ = read_records(target)
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                             "cwd": str(self.project), "timestamp": "2026-10-15T13:00:00Z",
                             "message": {"role": "user", "content": "Continue."}})
        continued = engine.lineage(self.adapters["claude"].read(SessionRef(item["native_id"], str(target))))
        self.assertEqual(continued.events[:len(session.events)], session.events)

    def test_a_missing_or_changed_blob_is_an_error(self):
        engine = self.engine()
        item = engine.handoff(self.screenshot("codex", png("lost"), "shot"), "claude")
        blob = self.state / "archives" / "blobs" / self.blobs()[0]
        original = blob.read_bytes()
        blob.write_bytes(original[:-1] + b"!")
        with self.assertRaisesRegex(SyncError, "corrupt"):
            read_archive(item["archive"])
        blob.unlink()
        with self.assertRaisesRegex(SyncError, "blob missing"):
            read_archive(item["archive"])
        target = Path(item["target"])
        records, _, _ = read_records(target)
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                             "cwd": str(self.project), "timestamp": "2026-10-15T13:00:00Z",
                             "message": {"role": "user", "content": "Continue."}})
        continued = engine.lineage(self.adapters["claude"].read(SessionRef(item["native_id"], str(target))))
        self.assertIn("parent archive unavailable or invalid; keeping the continuation and its provenance", continued.issues)

    def test_handing_off_again_restores_a_missing_blob(self):
        engine = self.engine()
        session = self.screenshot("codex", png("again"), "shot")
        engine.handoff(session, "claude")
        blob = self.state / "archives" / "blobs" / self.blobs()[0]
        blob.unlink()
        engine.handoff(session, "muse", mode="context")
        self.assertTrue(blob.is_file())
        self.assertEqual(read_archive(engine.archive_path(session)), session.to_dict())

    def test_import_accepts_a_version_2_archive(self):
        engine = self.engine()
        session = self.screenshot("codex", png("import"), "shot")
        item = engine.handoff(session, "claude")
        engine.close()
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {i.harness: {"home": i.home, "executable": sys.executable} for i in self.installations}}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--state-dir", str(self.state), "--config", str(config), "import", item["archive"], "--json"]), 0)
        key = json.loads(output.getvalue())["session"]
        imported = self.engine().select(key)
        self.assertEqual(imported.events, session.events)
        archive = self.engine().archive_path(imported)
        self.assertEqual(json.loads(archive.read_bytes())["archive_version"], 2)


class BlobPruneTests(BlobWorkspace):
    def test_unreferenced_blobs_are_collected_and_referenced_or_settling_ones_kept(self):
        engine = self.engine()
        shared, old_only = png("shared"), png("old only")
        self.screenshot("codex", shared, "shot-1")
        first = engine.handoff(self.screenshot("codex", old_only, "shot-2"), "claude")
        # The newer revision keeps the shared image and drops the other (an edited history).
        self.paths["codex"].write_text("\n".join(line for line in self.paths["codex"].read_text(encoding="utf-8").splitlines()
                                                 if "shot-2" not in line) + "\n", encoding="utf-8")
        second = engine.handoff(self.screenshot("codex", png("new"), "shot-3"), "claude")
        self.assertEqual(len(self.blobs()), 3)
        age_transfer(self.state, first, 30)
        age_transfer(self.state, second, 30)
        folder = self.state / "archives" / "blobs"
        names = {hashlib.sha256(raw).hexdigest() + ".png": raw for raw in (shared, old_only)}
        old_name = next(n for n, raw in names.items() if raw == old_only)
        age([folder / name for name in self.blobs()], 30)
        settling = folder / ("f" * 64 + ".png")
        settling.write_bytes(b"written moments ago by a sync")
        unreferenced = folder / ("e" * 64 + ".png")
        unreferenced.write_bytes(b"left behind")
        age([unreferenced], 30)
        dry = prune(engine.store, self.state, 7, dry_run=True)
        self.assertEqual(dry["blobs_removed"], 2)
        self.assertTrue((folder / old_name).is_file(), "a dry run removes nothing")
        report = prune(engine.store, self.state, 7)
        self.assertFalse(Path(first["archive"]).exists())
        self.assertEqual(report["blobs_removed"], 2)
        self.assertEqual(report["blob_bytes_freed"], len(old_only) + len(b"left behind"))
        self.assertFalse((folder / old_name).exists())
        self.assertFalse(unreferenced.exists())
        self.assertTrue(settling.is_file(), "a blob younger than the settle period is never collected")
        self.assertEqual(read_archive(second["archive"])["events"], self.read("codex").to_dict()["events"])

    def test_an_unreadable_archive_stops_collection(self):
        engine = self.engine()
        engine.handoff(self.screenshot("codex", png("kept"), "shot"), "claude")
        folder = self.state / "archives" / "blobs"
        age([folder / name for name in self.blobs()], 30)
        broken = self.state / "archives" / "broken" / ("c" * 64 + ".json")
        broken.parent.mkdir()
        broken.write_bytes(b'{\n  "archive_version": 2,\n  "blobs": [')
        report = prune(engine.store, self.state, 7)
        self.assertEqual(report["blobs_removed"], 0)
        self.assertEqual(len(self.blobs()), 1)

    def test_compact_store_converts_older_archives_in_place(self):
        engine = self.engine()
        session = self.screenshot("codex", png("compact"), "shot")
        archive = engine.archive_path(session)
        archive.parent.mkdir(parents=True)
        archive.write_bytes(json_bytes(session.to_dict()))
        plain = archive.with_name("d" * 64 + ".json")
        plain.write_bytes(json_bytes(self.read("muse").to_dict()))
        age([archive], 2)
        stamp = archive.stat().st_mtime_ns
        dry = compact_archives(self.state, dry_run=True)
        self.assertEqual(dry["archives_converted"], 1)
        self.assertNotIn(b"archive_version", archive.read_bytes())
        report = compact_archives(self.state)
        self.assertEqual((report["archives_converted"], report["blobs_written"]), (1, 1))
        self.assertLess(report["archive_bytes_after"], report["archive_bytes_before"])
        self.assertEqual(json.loads(archive.read_bytes())["archive_version"], 2)
        self.assertEqual(archive.stat().st_mtime_ns, stamp, "prune orders snapshots by modification time")
        self.assertEqual(read_archive(archive), session.to_dict())
        self.assertNotIn(b"archive_version", plain.read_bytes())
        self.assertEqual(compact_archives(self.state)["archives_converted"], 0)


class CompressedIndexTests(BlobWorkspace):
    def large(self, transfer_id):
        session = self.read("codex")
        session.events.append(Event("padding", "assistant", "x" * 10000, "2026-10-15T12:00:00Z"))
        session.provenance = {"transfer_id": transfer_id, "baseline_count": 1}
        return session

    def test_compressed_and_legacy_rows_both_decode(self):
        store = Store(self.state)
        self.addCleanup(store.close)
        compressed_id, legacy_id = str(uuid.uuid4()), str(uuid.uuid4())
        compressed = store.remember(self.large(compressed_id))
        legacy = self.large(legacy_id)
        legacy.native_id = IDS["muse"]
        store.db.execute("INSERT INTO sessions (key, source_path, stat, document) VALUES (?, ?, 'null', ?)",
                         (legacy.key, legacy.source_path, json.dumps(legacy.to_dict())))
        small = self.read("muse")
        store.remember(small)
        store.db.commit()
        kinds = dict(store.db.execute("SELECT key, typeof(document) FROM sessions"))
        self.assertEqual((kinds[compressed.key], kinds[legacy.key], kinds[small.key]), ("blob", "text", "text"))
        documents = {d["native_id"] + d["harness"]: d for d in store.sessions()}
        self.assertEqual(documents[compressed.native_id + "codex"], compressed.to_dict())
        self.assertEqual(documents[legacy.native_id + "codex"], legacy.to_dict())
        self.assertEqual(store.cached(compressed.key, compressed.source_path, None), compressed.to_dict())
        keep, _, _ = live_transfers(store, self.state)
        self.assertTrue({compressed_id, legacy_id} <= keep, "prune reads continuations from both encodings")
        report = store.compact()
        self.assertEqual(report["index_rows_compressed"], 1)
        self.assertEqual(dict(store.db.execute("SELECT key, typeof(document) FROM sessions"))[legacy.key], "blob")
        self.assertEqual({d["native_id"] + d["harness"]: d for d in store.sessions()}, documents)
        self.assertEqual(store.compact()["index_rows_compressed"], 0)

    def test_scan_uses_a_compressed_cache_row(self):
        engine = self.engine()
        session = self.screenshot("codex", png("cached"), "shot")
        scanned = next(s for s in engine.scan(days=None) if s.harness == "codex")
        self.assertEqual(scanned.events, session.events)
        kind = engine.store.db.execute("SELECT typeof(document) FROM sessions WHERE key=?", (session.key,)).fetchone()[0]
        self.assertEqual(kind, "blob")
        engine.close()
        with mock.patch.object(self.adapters["codex"], "read", side_effect=AssertionError("parsed again")):
            cached = next(s for s in self.engine().scan(days=None) if s.harness == "codex")
        self.assertEqual(cached.events, session.events)

    def test_older_builds_refuse_a_compressed_index(self):
        Store(self.state).close()
        database = sqlite3.connect(str(self.state / "sessions.sqlite3"))
        self.addCleanup(database.close)
        self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_prune_compact_store_command(self):
        engine = self.engine()
        session = self.screenshot("codex", png("command"), "shot")
        archive = engine.archive_path(session)
        archive.parent.mkdir(parents=True)
        archive.write_bytes(json_bytes(session.to_dict()))
        engine.store.db.execute("INSERT INTO sessions (key, source_path, stat, document) VALUES (?, ?, 'null', ?)",
                                ("codex:default:legacy", "", json.dumps(self.large(str(uuid.uuid4())).to_dict())))
        engine.store.db.commit()
        engine.close()
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {i.harness: {"home": i.home, "executable": sys.executable} for i in self.installations}}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--state-dir", str(self.state), "--config", str(config), "prune", "--compact-store", "--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual((report["archives_converted"], report["index_rows_compressed"]), (1, 1))
        self.assertEqual(read_archive(archive), session.to_dict())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--state-dir", str(self.state), "--config", str(config), "prune", "--compact-store"]), 0)
        self.assertIn("Converted 0 archive(s)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
