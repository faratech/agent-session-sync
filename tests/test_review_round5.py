"""Regressions for the fifth adversarial review round (#149 and follow-ups)."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import contextlib
import json
import os
import shutil
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from test_review_round import CliWorkspace, settle
from test_review_round2 import compress
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync import io as io_module
from agent_session_sync import store as store_module
from agent_session_sync.adapters import SessionRef
from agent_session_sync.io import find_program, read_records, salvage
from agent_session_sync.prune import prune
from agent_session_sync.store import add_pending


class GluedSuccessorsAfterSeparators(Workspace):
    """A successor glued after a cut ending in ',' ':' or '[' inside text is a record again."""

    def lines(self):
        return self.paths["codex"].read_bytes().rstrip(b"\n").split(b"\n")

    def check(self, cut, expected):
        lines = self.lines()
        lines.insert(2, cut)
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        records, _, _ = read_records(self.paths["codex"])
        for record in expected:
            self.assertIn(record, records)

    def test_cuts_inside_text_and_after_structural_commas(self):
        successor = b'{"type": "event_msg", "payload": {"c": 1}}'
        for prefix in (b'{"type": "x", "content": "Meeting on Nov 20,', b'{"type": "x", "content": "a: [',
                       b'{"type": "x", "uuid": "b1",', b'{"type": "x", "content": "list,\x00\x00'):
            with self.subTest(prefix=prefix):
                self.check(prefix + successor, [{"type": "event_msg", "payload": {"c": 1}}])

    def test_a_leading_record_and_a_successor_after_a_torn_comma_both_stay(self):
        self.check(b'{"type": "a"} {"type": "b", "t": "x,{"type": "c"}', [{"type": "a"}, {"type": "c"}])

    def test_a_nested_value_of_a_cut_record_is_still_refused(self):
        self.assertIsNone(salvage('{"type": "event_msg", "payload": {"message": {"role": "user"}}', None, [], 1))

    def test_many_candidates_stay_linear(self):
        line = '{"type": "x", "m": "' + '{"a": 1}, ' * 20000
        started = time.monotonic()
        salvage(line, None, [], 1)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_cut_inside_an_escape_after_a_separator_in_the_same_string(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "event_msg", "payload": {"m": "red, then \\u001b[31m and \\u00')
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        records, issues, _ = read_records(self.paths["codex"])
        self.assertTrue(any("cut short" in issue for issue in issues))


class ProgramLookup(Workspace):
    def test_windows_prefers_an_executable_extension_to_a_bare_name(self):
        folder = tempfile.mkdtemp(dir=str(self.base))
        for name in ("zstd", "zstd.exe"):
            path = os.path.join(folder, name)
            open(path, "w").close()
            os.chmod(path, 0o755)
        with mock.patch.object(io_module.os, "name", "nt"), mock.patch.dict(os.environ, {"PATH": folder, "PATHEXT": ".exe"}):
            self.assertEqual(find_program("zstd"), os.path.join(folder, "zstd.exe"))

    def test_an_unresolvable_path_entry_is_compared_as_written(self):
        real = os.path.realpath

        def realpath(path, *args, **kwargs):
            if "unreachable" in str(path):
                raise OSError(1231, "The network location cannot be reached")
            return real(path, *args, **kwargs)
        with mock.patch.object(io_module.os.path, "realpath", realpath), \
                mock.patch.dict(os.environ, {"PATH": os.pathsep.join([os.sep + "unreachable", str(self.base)])}):
            find_program("zstd")  # no exception


class LaunchRecordEdges(CliWorkspace):
    def setUp(self):
        super().setUp()
        self.write_config({"auto_handoff_max_age_hours": None})

    def launch(self, destination):
        with mock.patch.object(cli.subprocess, "call", return_value=0):
            self.assertEqual(self.cli("handoff", IDS["codex"], "--to", destination, "--launch", "--mode", "context")[0], 0)
        engine = self.engine()
        try:
            return json.loads(engine.store.db.execute("SELECT value FROM metadata WHERE key=?", ("launching:" + destination,)).fetchone()[0])[0]
        finally:
            engine.close()

    def binding(self, endpoint, sid):
        engine = self.engine()
        try:
            row = engine.store.db.execute("SELECT value FROM metadata WHERE key=?", ("binding:{}:{}".format(endpoint, sid),)).fetchone()
            return row[0] if row else None
        finally:
            engine.close()

    def test_an_unreadable_transcript_still_finds_the_launch_record(self):
        launch = self.launch("copilot")
        transcript = self.base / "unreadable" / "events.jsonl"
        transcript.parent.mkdir()
        transcript.write_bytes(b'{"type": "session.start"}\n{"bad": "\xff"}\n{"type": "user.message"}\n')
        payload = {"session_id": "launched", "cwd": str(self.project), "source": "startup", "transcript_path": str(transcript)}
        status, output, _ = self.cli("hook", "--harness", "copilot", "--event", "start", "--inline", payload=payload)
        self.assertEqual((status, json.loads(output)), (0, {}))
        self.assertEqual(self.binding("copilot", "launched"), launch)

    def test_a_session_whose_first_prompt_is_not_the_launch_prompt_leaves_the_record(self):
        self.launch("copilot")
        transcript = self.base / "other" / "events.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("\n".join(json.dumps(r) for r in (
            {"type": "session.start", "timestamp": "2026-10-15T12:00:00Z", "data": {"context": {"cwd": str(self.project)}}},
            {"type": "user.message", "timestamp": "2026-10-15T12:00:01Z", "data": {"content": "Something else entirely."}})) + "\n",
            encoding="utf-8")
        payload = {"session_id": "other", "cwd": str(self.project), "source": "startup", "transcript_path": str(transcript)}
        self.assertEqual(self.cli("hook", "--harness", "copilot", "--event", "start", "--inline", payload=payload)[0], 0)
        self.assertIsNone(self.binding("copilot", "other"))
        engine = self.engine()
        try:
            self.assertIsNotNone(engine.store.db.execute("SELECT 1 FROM metadata WHERE key='launching:copilot'").fetchone())
        finally:
            engine.close()


class QueueEntriesThatCannotBeOpened(Workspace):
    def test_no_drain_is_started_for_an_entry_no_process_can_open(self):
        (self.state / "pending" / "00000000000000000001-1-0.json").mkdir(parents=True)
        with mock.patch.object(store_module, "_stuck", set()), mock.patch.object(cli.subprocess, "Popen") as popen:
            cli.hand_on_pending(Namespace(config=None), self.state)
        popen.assert_not_called()
        add_pending(self.state, {"project": "/p", "hook": None})
        with mock.patch.object(store_module, "_stuck", set()), mock.patch.object(cli.subprocess, "Popen") as popen:
            cli.hand_on_pending(Namespace(config=None), self.state)
        popen.assert_called_once()


class HotSourcesWithoutEvents(Workspace):
    def test_a_hot_subagent_rollout_is_cached(self):
        path = self.paths["codex"]
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            if record.get("type") == "session_meta":
                record["payload"]["thread_source"] = "subagent"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        engine = self.engine()
        adapter = self.adapters["codex"]
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.scan(days=None)
            with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
                engine.scan(days=None)
        self.assertEqual(read.call_count, 0)


class CompressionKeepsTheCache(Workspace):
    def test_a_rollout_codex_compressed_is_neither_re_read_nor_unskippable(self):
        engine = self.engine()
        engine.scan(days=None)
        path = self.paths["codex"]
        stat = path.stat()
        compressed = compress(path)
        os.utime(str(compressed), ns=(stat.st_atime_ns, stat.st_mtime_ns))  # Codex keeps the mtime
        other = self.base / "other-project"
        other.mkdir()
        adapter = self.adapters["codex"]
        with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
            self.assertEqual(engine.scan(project=str(other), days=None), [])
            sessions = engine.scan(project=str(self.project), days=None)
        self.assertEqual(read.call_count, 0)
        self.assertIn(self.read_key(), [s.key for s in sessions])

    def read_key(self):
        return "codex:default:" + IDS["codex"]


class RetentionOfEarlierRenderings(Workspace):
    def test_an_earlier_renderings_folder_survives_thinning_inside_the_window(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "RENDER_VERSION", 3):
            older = engine.handoff(self.read("muse"), "claude")
        newer = engine.handoff(self.read("muse"), "claude")
        for item in (older, newer):
            for path in [item["target"]] + list(Path(item["context"]).parent.iterdir()):
                settle(path)
        prune(engine.store, self.state, 7)
        self.assertTrue(Path(older["context"]).with_name("manifest.json").exists())
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)
        self.assertTrue(Path(older["target"]).exists())

    def test_an_earlier_rendering_the_user_archived_is_not_imported_again(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "RENDER_VERSION", 3):
            older = engine.handoff(self.read("muse"), "codex")
        archived = self.adapters["codex"].home / "archived_sessions" / Path(older["target"]).name
        archived.parent.mkdir()
        os.replace(older["target"], str(archived))
        before = set((self.adapters["codex"].home / "sessions").rglob("rollout-*"))
        engine.sync(source="muse", destination="codex", days=None)
        self.assertEqual(set((self.adapters["codex"].home / "sessions").rglob("rollout-*")), before)


class InjectionRespectsLowDisk(Workspace):
    """#149."""

    def test_no_new_injection_is_rendered_on_a_nearly_full_disk(self):
        engine = self.engine(config={"auto_handoff_max_age_hours": None})
        with mock.patch.object(engine_module, "free_bytes", return_value=0):
            self.assertIsNone(engine.incoming("muse", str(self.project)))
        self.assertTrue(any("low disk space" in error for error in engine.errors))
        item = engine.incoming("muse", str(self.project))
        with mock.patch.object(engine_module, "free_bytes", return_value=0):
            self.assertEqual(engine.incoming("muse", str(self.project))["id"], item["id"])  # an existing one is served


if __name__ == "__main__":
    unittest.main()
