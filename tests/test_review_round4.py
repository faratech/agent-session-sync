"""Regressions for the fourth adversarial review round (#148 and follow-ups)."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from test_review_round import CliWorkspace, settle
from test_review_round2 import compress
from agent_session_sync import adapters as adapters_module
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync import io as io_module
from agent_session_sync.adapters import SessionRef
from agent_session_sync.io import SyncError, find_program, read_records
from agent_session_sync.prune import prune
from agent_session_sync.store import add_pending, has_pending


def spawned(popen):
    return [call[0][0] for call in popen.call_args_list]


class NoEndlessHandOnChains(CliWorkspace):
    """A failure that a successor would repeat is never handed on."""

    def break_index(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(str(self.state / "sessions.sqlite3"))) as db:
            db.execute("PRAGMA user_version=99")
            db.commit()

    def test_a_queued_sync_that_cannot_open_the_index_spawns_no_successor(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        self.break_index()
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            for argv in (("sync", "--quiet", "--queued"), ("sync", "--quiet", "--queued", "--project", str(self.project))):
                status, _, _ = self.cli(*argv)
                self.assertEqual(status, 1)
        self.assertEqual(spawned(popen), [])

    def test_a_hook_on_a_broken_index_spawns_its_own_sync_once_and_no_drain(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        self.break_index()
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            status, output, _ = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload)
        self.assertEqual((status, json.loads(output)), (0, {}))
        commands = spawned(popen)
        self.assertEqual(len(commands), 1)
        self.assertIn("--for-hook", commands[0])

    def test_an_unreadable_queue_entry_starts_no_successor(self):
        (self.state / "pending" / "00000000000000000001-1-0.json").mkdir(parents=True)
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            status, _, _ = self.cli("sync", "--quiet", "--queued")
        self.assertEqual(status, 0)
        self.assertEqual(spawned(popen), [])

    def test_hand_on_generations_are_capped(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        args = Namespace(config=None)
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            with mock.patch.dict(os.environ, {"AGENT_SESSION_SYNC_HAND_ON": "1"}):
                cli.hand_on_pending(args, self.state)
            self.assertEqual(popen.call_args.kwargs["env"]["AGENT_SESSION_SYNC_HAND_ON"], "2")
            popen.reset_mock()
            with mock.patch.dict(os.environ, {"AGENT_SESSION_SYNC_HAND_ON": str(cli.HAND_ON_DEPTH)}):
                cli.hand_on_pending(args, self.state)
            popen.assert_not_called()


class LaunchRecordScope(CliWorkspace):
    def setUp(self):
        super().setUp()
        self.write_config({"auto_handoff_max_age_hours": None})

    def launch(self, destination):
        with mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            self.assertEqual(self.cli("handoff", IDS["codex"], "--to", destination, "--launch", "--mode", "context")[0], 0)
        return call.call_args[0][0]

    def record(self, endpoint):
        engine = self.engine()
        try:
            return engine.store.db.execute("SELECT value FROM metadata WHERE key=?", ("launching:" + endpoint,)).fetchone()
        finally:
            engine.close()

    def test_a_session_with_turns_of_its_own_does_not_consume_the_record(self):
        self.launch("copilot")
        transcript = self.paths["copilot"]  # an existing session with replies
        payload = {"session_id": IDS["copilot"], "cwd": str(self.project), "source": "startup", "transcript_path": str(transcript)}
        self.cli("hook", "--harness", "copilot", "--event", "start", "--inline", payload=payload)
        self.assertIsNotNone(self.record("copilot"))

    def test_a_recognized_launch_prompt_clears_the_record(self):
        command = self.launch("copilot")
        transcript = self.base / "launched" / "events.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("\n".join(json.dumps(r) for r in (
            {"type": "session.start", "timestamp": "2026-10-15T12:00:00Z", "data": {"context": {"cwd": str(self.project)}}},
            {"type": "user.message", "timestamp": "2026-10-15T12:00:01Z", "data": {"content": command[-1]}})) + "\n", encoding="utf-8")
        payload = {"session_id": "launched", "cwd": str(self.project), "source": "startup", "transcript_path": str(transcript)}
        original = adapters_module.Copilot.read
        with mock.patch.object(adapters_module.Copilot, "read", autospec=True, side_effect=original) as read:
            self.cli("hook", "--harness", "copilot", "--event", "start", "--inline", payload=payload)
        self.assertIsNone(self.record("copilot"))
        self.assertEqual(sum(1 for call in read.call_args_list if call[0][1].path == str(transcript)), 1)  # parsed once


class CachesAndDecoders(Workspace):
    def test_parser_version_moved_past_the_releases_with_different_output(self):
        self.assertGreaterEqual(adapters_module.PARSER_VERSION, 4)

    def test_without_a_decoder_the_plain_rollout_is_read(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        data = Path(item["target"]).read_bytes()
        compress(item["target"])
        Path(item["target"]).write_bytes(data)
        ref = SessionRef(item["native_id"], item["target"])
        with mock.patch.object(engine_module, "zstd_available", return_value=False):
            self.assertEqual(engine.compressed_original(ref).path, item["target"])

    def test_a_removed_working_directory_does_not_break_the_lookup(self):
        gone = tempfile.mkdtemp(dir=str(self.base))
        cwd = os.getcwd()
        os.chdir(gone)
        try:
            os.rmdir(gone)
            find_program("zstd")  # no exception
        except OSError:
            self.skipTest("this platform cannot remove the working directory")
        finally:
            os.chdir(cwd)

    def test_a_window_past_year_9999_includes_no_import(self):
        engine = self.engine()
        engine.store.remember(engine_module.Session("external", "bundle", str(self.project),
                                                    [engine_module.Event("a", "user", "Hi.", "2026-10-15T09:00:00Z")]), imported=True)
        self.assertEqual(engine.scan(days=-1e10), [])

    def test_an_older_zstandard_without_frame_end_reports_falls_back(self):
        class Obj:
            def decompress(self, data):
                return b"decoded"

        class Decompressor:
            def decompressobj(self):
                return Obj()
        fake = types.SimpleNamespace(ZstdDecompressor=Decompressor, ZstdError=type("ZstdError", (Exception,), {}))
        with mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None, "zstandard": fake}):
            self.assertEqual(io_module.zstd_decompress("x.zst", b"frame"), b"decoded")


class CodexOnlyThreadState(Workspace):
    native_id = "01a1de7d-cade-79fc-92c2-f7edb633664a"

    def setUp(self):
        super().setUp()
        self.home = self.adapters["codex"].home
        self.rollout = self.home / "sessions/2026/11/20" / ("rollout-2026-11-20T10-00-00-" + self.native_id + ".jsonl")
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.rollout.write_text("{}\n", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(str(self.home / "state_5.sqlite"))) as state:
            state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            state.execute("INSERT INTO threads VALUES (?, ?, 0)", (self.native_id, str(self.rollout)))
            state.commit()

    def used(self):
        return cli.codex_thread_used(self.home.resolve(), self.native_id, self.rollout.resolve())

    def test_queued_messages_goals_attachments_and_spawned_threads_keep_the_thread(self):
        self.assertFalse(self.used())  # tables a Codex version does not have hold nothing
        cases = (("queue_1.sqlite", "queued_items", "thread_id"), ("goals_1.sqlite", "thread_goals", "thread_id"),
                 ("state_5.sqlite", "thread_attachments", "thread_id"), ("state_5.sqlite", "thread_spawn_edges", "child_thread_id"))
        for database, table, column in cases:
            with self.subTest(table=table):
                with contextlib.closing(sqlite3.connect(str(self.home / database))) as db:
                    if table == "thread_spawn_edges":
                        db.execute("CREATE TABLE IF NOT EXISTS thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
                    else:
                        db.execute("CREATE TABLE IF NOT EXISTS {} (thread_id TEXT)".format(table))
                    db.execute("INSERT INTO {} ({}) VALUES (?)".format(table, column), (self.native_id,))
                    db.commit()
                    self.assertTrue(self.used())
                    db.execute("DELETE FROM {}".format(table))
                    db.commit()


class PruneRound4(Workspace):
    def grow_muse(self):
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})

    def test_the_dry_run_check_sees_the_twins_it_would_remove(self):
        engine = self.engine()
        first = engine.handoff(self.read("muse"), "codex")
        self.grow_muse()
        second = engine.handoff(self.read("muse"), "codex")
        data = Path(first["target"]).read_bytes()
        compressed = compress(first["target"])
        Path(first["target"]).write_bytes(data)
        settle(first["target"], compressed, second["target"])
        check = mock.Mock(return_value=True)
        report = prune(engine.store, self.state, 7, dry_run=True, delete=check)
        self.assertEqual(report["compressed_duplicates_removed"], 1)
        self.assertEqual(report["native_copies_removed"], 1)
        self.assertIn(Path(first["target"]), check.call_args.kwargs["gone"])

    def test_a_plain_write_back_beside_an_archived_thread_is_removed(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        archived = self.adapters["codex"].home / "archived_sessions" / Path(item["target"]).name
        archived.parent.mkdir()
        shutil.copy2(item["target"], str(archived))
        settle(item["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["compressed_duplicates_removed"], 1)
        self.assertFalse(Path(item["target"]).exists())
        self.assertTrue(archived.exists())

    def test_an_older_rendering_of_the_same_revision_goes_after_the_retention_window(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "RENDER_VERSION", 3):
            older = engine.handoff(self.read("muse"), "claude")
        newer = engine.handoff(self.read("muse"), "claude")
        settle(older["target"], newer["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)  # inside the window
        for path in [older["target"]] + list(Path(older["context"]).parent.iterdir()):
            settle(path, hours=24 * 8)
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 1)
        self.assertTrue(Path(newer["target"]).exists())


class RecordRecoveryRound4(Workspace):
    """#148."""

    def lines(self):
        return self.paths["codex"].read_bytes().rstrip(b"\n").split(b"\n")

    def write(self, lines):
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")

    def test_a_record_cut_inside_a_literal_number_or_escape_is_dropped_once_the_writer_continued(self):
        for cut in (b'{"type": "event_msg", "payload": {"ok": tr', b'{"type": "event_msg", "payload": {"n": 12.',
                    b'{"type": "event_msg", "payload": {"m": "x\\u00e'):
            with self.subTest(cut=cut):
                lines = self.lines()
                lines.insert(2, cut)
                self.write(lines)
                records, issues, _ = read_records(self.paths["codex"])
                self.assertTrue(any("cut short" in issue for issue in issues))
                self.paths["codex"].write_bytes(b"\n".join(l for l in lines if l != cut) + b"\n")

    def test_a_nested_object_of_a_cut_record_is_not_taken_for_a_record(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "event_msg", "payload": {"message": {"role": "user"}}')
        self.write(lines)
        records, issues, _ = read_records(self.paths["codex"])
        self.assertNotIn({"role": "user"}, records)
        self.assertTrue(any("cut short" in issue for issue in issues))

    def test_a_complete_leading_record_survives_a_torn_successor(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "a"} garbage {"type": "b"}')
        self.write(lines)
        records, _, _ = read_records(self.paths["codex"])
        self.assertIn({"type": "a"}, records)
        self.assertIn({"type": "b"}, records)


if __name__ == "__main__":
    unittest.main()
