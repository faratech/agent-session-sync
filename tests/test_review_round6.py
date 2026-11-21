"""Regressions for the sixth adversarial review round."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import json
import os
import shutil
import sys
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from test_review_round import CliWorkspace, settle
from test_review_round2 import compress, zstd_compress
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync import io as io_module
from agent_session_sync import store as store_module
from agent_session_sync.adapters import SessionRef
from agent_session_sync.io import SyncError, read_records, salvage, zstd_content_size
from agent_session_sync.prune import prune
from agent_session_sync.store import add_pending, has_takeable


def grow_codex(case, turns=40):
    for index in range(turns):
        case.append(case.paths["codex"], {"timestamp": "2026-10-15T12:00:%02dZ" % (index % 60), "type": "response_item",
                                          "payload": {"type": "message", "role": "assistant" if index % 2 else "user",
                                                      "content": [{"type": "output_text" if index % 2 else "input_text",
                                                                   "text": ("Turn %d \"quoted\"\n" % index) * 90}]}})


class MuseHookOutputCeiling(CliWorkspace):
    def setUp(self):
        super().setUp()
        self.write_config({"auto_handoff_max_age_hours": None})
        grow_codex(self)

    def start(self, sid):
        payload = {"session_id": sid, "cwd": str(self.project), "source": "startup"}
        return self.cli("hook", "--harness", "muse", "--event", "start", "--inline", payload=payload)

    def binding(self, sid):
        engine = self.engine()
        try:
            return engine.store.db.execute("SELECT value FROM metadata WHERE key=?", ("binding:muse:" + sid,)).fetchone()
        finally:
            engine.close()

    def test_a_large_context_is_sized_to_fit_muses_hook_output(self):
        status, output, _ = self.start("new-muse")
        self.assertEqual(status, 0)
        self.assertLessEqual(len(output.encode("utf-8")), 16384)
        self.assertIn("additionalContext", output)
        self.assertIsNotNone(self.binding("new-muse"))

    def test_an_answer_that_still_does_not_fit_is_not_given_and_not_bound(self):
        with mock.patch.dict(cli.HOOK_OUTPUT_LIMITS, {"muse": 1100}):
            status, output, errors = self.start("tight-muse")
        self.assertEqual((status, json.loads(output)), (0, {}))
        self.assertIn("nothing injected", errors)
        self.assertIsNone(self.binding("tight-muse"))

    def test_other_harnesses_keep_the_full_budget(self):
        payload = {"session_id": "new-agy", "cwd": str(self.project), "source": "startup"}
        output = self.cli("hook", "--harness", "agy", "--event", "start", "--inline", payload=payload)[1]
        self.assertGreater(len(output.encode("utf-8")), 16384)


class PurgedWhileWaiting(CliWorkspace):
    def test_a_hook_waiting_for_the_lock_does_nothing_after_a_purge(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with mock.patch.object(cli, "config_removed", return_value=True), \
                mock.patch.object(cli, "Engine") as engine, mock.patch.object(cli.subprocess, "Popen") as popen:
            status, output, _ = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload)
        self.assertEqual((status, json.loads(output)), (0, {}))
        engine.assert_not_called()
        popen.assert_not_called()

    def test_a_sync_waiting_for_the_lock_does_nothing_after_a_purge(self):
        with mock.patch.object(cli, "config_removed", return_value=True), mock.patch.object(cli, "Engine") as engine:
            status, _, errors = self.cli("sync", "--quiet", "--queued")
        self.assertEqual(status, 1)
        self.assertIn("removed", errors)
        engine.assert_not_called()

    def test_config_removed_follows_the_selected_file(self):
        config = self.base / "selected.json"
        config.write_text("{}", encoding="utf-8")
        args = Namespace(config=str(config))
        self.assertFalse(cli.config_removed(args))
        config.unlink()
        self.assertTrue(cli.config_removed(args))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_SESSION_SYNC_CONFIG", None)
            self.assertFalse(cli.config_removed(Namespace(config=None)))


class QueueEntriesAreOnlyStatted(Workspace):
    def test_has_takeable_never_opens_an_entry(self):
        add_pending(self.state, {"project": "/p", "hook": None})
        with mock.patch.object(store_module, "_stuck", set()), \
                mock.patch.object(Path, "open", side_effect=AssertionError("opened outside the lock")):
            self.assertTrue(has_takeable(self.state))


class CompressedCacheNeedsTheSameLength(Workspace):
    def test_a_parse_taken_before_a_same_tick_append_is_not_reused(self):
        engine = self.engine()
        engine.scan(days=None)
        path = self.paths["codex"]
        stat = path.stat()
        self.append(path, {"timestamp": "2026-10-15T12:00:00Z", "type": "response_item",
                           "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Late tail."}]}})
        os.utime(str(path), ns=(stat.st_atime_ns, stat.st_mtime_ns))  # the append fell in the same clock tick
        compressed = compress(path)
        os.utime(str(compressed), ns=(stat.st_atime_ns, stat.st_mtime_ns))
        sessions = engine.scan(project=str(self.project), days=None)
        session = next(s for s in sessions if s.key == "codex:default:" + IDS["codex"])
        self.assertIn("Late tail.", [e.text for e in session.events])

    def test_frame_content_size(self):
        path = self.base / "plain.jsonl"
        path.write_bytes(b"x" * 1000)
        self.assertEqual(zstd_content_size(compress(path)), 1000)
        self.assertIsNone(zstd_content_size(self.paths["claude"]))


class TranscriptReadLimit(Workspace):
    def test_scan_continues_after_an_oversized_plain_transcript(self):
        path = self.paths["codex"]
        limit = max(p.stat().st_size for p in self.paths.values())
        with path.open("ab") as handle:
            handle.truncate(limit + 1)
        engine = self.engine()
        with mock.patch.object(io_module, "MAX_TRANSCRIPT_BYTES", limit):
            sessions = engine.scan(days=None)
        self.assertNotIn("codex", {session.harness for session in sessions})
        self.assertIn("claude", {session.harness for session in sessions})
        self.assertTrue(any("read limit" in error and str(path) in error for error in engine.errors))
        self.assertEqual(path.stat().st_size, limit + 1)

    def test_oversized_declared_frame_is_rejected_before_decoding(self):
        path = self.base / "large.jsonl.zst"
        path.write_bytes(zstd_compress(b" " * 8192))
        with mock.patch.object(io_module, "MAX_TRANSCRIPT_BYTES", 4096), \
                mock.patch.object(io_module, "read_zstd", side_effect=AssertionError("opened for decompression")):
            with self.assertRaisesRegex(SyncError, "read limit"):
                read_records(path)

    def test_compressed_limit_applies_to_the_sum_of_all_frames(self):
        path = self.base / "frames.jsonl.zst"
        frame = zstd_compress(b"{}" + b" " * 4093 + b"\n")
        path.write_bytes(frame * 2)
        with mock.patch.object(io_module, "MAX_TRANSCRIPT_BYTES", 8192):
            records, issues, _ = read_records(path)
            self.assertEqual(records, [{}, {}])
            self.assertEqual(issues, [])
        with mock.patch.object(io_module, "MAX_TRANSCRIPT_BYTES", 8191):
            with self.assertRaisesRegex(SyncError, "read limit"):
                read_records(path)


class SubagentRolloutsReadOneRecord(Workspace):
    def test_only_the_session_meta_of_a_subagent_rollout_is_read(self):
        path = self.paths["codex"]
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            if record.get("type") == "session_meta":
                record["payload"]["thread_source"] = "subagent"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        adapter = self.adapters["codex"]
        expected = adapter.parse(records, SessionRef(IDS["codex"], str(path)))
        with mock.patch("agent_session_sync.adapters.read_records", side_effect=AssertionError("read in full")):
            session = adapter.read(SessionRef(IDS["codex"], str(path)))
        self.assertEqual((session.native_id, session.workspace, session.events), (expected.native_id, expected.workspace, []))


def strip_provenance(path):
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for record in records:
        record.pop("agent_session_sync", None)
    records.append({"timestamp": records[-1]["timestamp"], "type": "event_msg",
                    "payload": {"type": "item_completed", "item": {"type": "Reasoning", "id": "migrated"}}})
    stat = Path(path).stat()
    Path(path).write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    os.utime(str(path), ns=(stat.st_atime_ns, stat.st_mtime_ns))  # migrate-rollouts keeps the mtime


class CodexMigratedImports(Workspace):
    def test_a_migrated_import_without_its_handoff_folder_is_still_the_import(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        strip_provenance(item["target"])
        shutil.rmtree(str(Path(item["context"]).parent))
        session = self.adapters["codex"].read(SessionRef(item["native_id"], item["target"]))
        self.assertTrue(session.provenance.get("context"))
        self.assertIsNone(engine.lineage(session))  # nothing beyond the import: not exported again

    def test_a_superseded_migrated_plain_import_is_pruned(self):
        engine = self.engine()
        first = engine.handoff(self.read("muse"), "codex")
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})
        second = engine.handoff(self.read("muse"), "codex")
        strip_provenance(first["target"])
        settle(first["target"], second["target"])
        delete = mock.Mock(side_effect=lambda destination, native_id, path, **kwargs: (Path(path).unlink(), True)[1])
        self.assertEqual(prune(engine.store, self.state, 7, delete=delete)["native_copies_removed"], 1)
        self.assertEqual(Path(delete.call_args[0][2]), Path(first["target"]))
        self.assertTrue(Path(second["target"]).exists())

    def test_a_continued_plain_import_is_kept(self):
        engine = self.engine()
        first = engine.handoff(self.read("muse"), "codex")
        self.append(Path(first["target"]), {"timestamp": "2026-10-15T12:00:00Z", "type": "event_msg",
                                            "payload": {"type": "user_message", "message": "Continued here."}})
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})
        second = engine.handoff(self.read("muse"), "codex")
        settle(first["target"], second["target"])
        delete = mock.Mock(return_value=True)
        prune(engine.store, self.state, 7, delete=delete)
        delete.assert_not_called()


class RevisionReturningToAnEarlierValue(Workspace):
    def test_the_current_revisions_transfer_becomes_the_newest_again(self):
        engine = self.engine()
        original = self.paths["muse"].read_bytes()
        engine.sync(source="muse", destination="claude", days=None)
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})
        engine.sync(source="muse", destination="claude", days=None)
        self.paths["muse"].write_bytes(original)
        engine.sync(source="muse", destination="claude", days=None)
        key = self.read("muse").key
        current = engine.revision_of(self.read("muse"))
        self.assertEqual(engine.store.previous(key, "claude")["source_revision"], current)


class EscapeAndNumberEdges(Workspace):
    def lines(self):
        return self.paths["codex"].read_bytes().rstrip(b"\n").split(b"\n")

    def test_a_record_cut_after_a_backslash_with_nul_padding_is_cut_short(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "event_msg", "payload": {"m": "C:\\' + b"\x00" * 16)
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        records, issues, _ = read_records(self.paths["codex"])
        self.assertTrue(any("cut short" in issue for issue in issues))

    @unittest.skipUnless(hasattr(sys, "get_int_max_str_digits"), "no integer digit limit")
    def test_an_over_long_integer_is_invalid_json_not_a_crash(self):
        lines = self.lines()
        lines.insert(2, b'{"n": ' + b"9" * (sys.get_int_max_str_digits() + 10) + b"}")
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        with self.assertRaises(SyncError):
            read_records(self.paths["codex"])

    def test_a_deeply_nested_cut_line_is_bounded(self):
        line = '{"a": ' * 400 + '"' + "x" * 400000
        started = time.monotonic()
        self.assertIsNone(salvage(line, None, [], 1))
        self.assertLess(time.monotonic() - started, 10)


class ArchivedWriteBacksOfEarlierReleases(Workspace):
    def test_an_archived_import_written_back_by_an_earlier_release_is_not_imported_again(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "RENDER_VERSION", 3):
            older = engine.handoff(self.read("muse"), "codex")
        archived = self.adapters["codex"].home / "archived_sessions" / Path(older["target"]).name
        archived.parent.mkdir()
        shutil.copy2(older["target"], str(archived))  # archived, and the plain copy written back
        before = set((self.adapters["codex"].home / "sessions").rglob("rollout-*"))
        engine.sync(source="muse", destination="codex", days=None)
        self.assertEqual(set((self.adapters["codex"].home / "sessions").rglob("rollout-*")), before)


if __name__ == "__main__":
    unittest.main()
