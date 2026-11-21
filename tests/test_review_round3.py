"""Regressions for the third adversarial review round (#144-#147 and follow-ups)."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
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
from agent_session_sync import prune as prune_module
from agent_session_sync import render
from agent_session_sync.adapters import SessionRef
from agent_session_sync.io import SyncError, find_program, read_records
from agent_session_sync.model import MARKER, Event, Session
from agent_session_sync.prune import native_from, prune
from agent_session_sync.render import compact
from agent_session_sync.store import add_pending, has_pending, single_flight, take_pending


def binding(engine, endpoint, sid):
    row = engine.store.db.execute("SELECT value FROM metadata WHERE key=?", ("binding:{}:{}".format(endpoint, sid),)).fetchone()
    return row[0] if row else None


def grow_muse(test):
    test.append(test.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                     "payload": {"kind": "event", "run_id": "r1",
                                                 "event": {"kind": "assistant_message_committed", "text": "Later."}}})


class LaunchedSessionsAreNotInjectedInto(CliWorkspace):
    """#144: an explicit open / handoff --launch gets no second context from its start hook."""

    def setUp(self):
        super().setUp()
        self.write_config({"auto_handoff_max_age_hours": None})

    def launch(self, destination):
        with mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            status, _, _ = self.cli("handoff", IDS["codex"], "--to", destination, "--launch", "--mode", "context")
        self.assertEqual(status, 0)
        return call.call_args[0][0]

    def start(self, harness, sid, transcript=None):
        payload = {"session_id": sid, "cwd": str(self.project), "source": "startup"}
        if transcript:
            payload["transcript_path"] = str(transcript)
        status, output, _ = self.cli("hook", "--harness", harness, "--event", "start", "--inline", payload=payload)
        self.assertEqual(status, 0)
        return json.loads(output)

    def test_the_launch_record_suppresses_injection_once(self):
        self.launch("muse")
        engine = self.engine()
        launched = engine.store.db.execute("SELECT value FROM metadata WHERE key='launching:muse'").fetchone()
        self.assertIsNotNone(launched)
        engine.close()
        self.assertEqual(self.start("muse", "launched-session"), {})
        engine = self.engine()
        self.assertEqual(binding(engine, "muse", "launched-session"), json.loads(launched[0])[0])
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM metadata WHERE key='launching:muse'").fetchone())
        engine.close()
        self.assertIn("hookSpecificOutput", self.start("muse", "an-ordinary-later-session"))  # consumed

    def test_a_recorded_launch_prompt_suppresses_injection(self):
        command = self.launch("copilot")
        engine = self.engine()
        engine.store.db.execute("DELETE FROM metadata WHERE key LIKE 'launching:%'")
        engine.store.db.commit()
        engine.close()
        transcript = self.base / "copilot-launched" / "events.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("\n".join(json.dumps(r) for r in (
            {"type": "session.start", "timestamp": "2026-10-15T12:00:00Z", "data": {"context": {"cwd": str(self.project)}}},
            {"type": "user.message", "timestamp": "2026-10-15T12:00:01Z", "data": {"content": command[-1]}})) + "\n", encoding="utf-8")
        self.assertEqual(self.start("copilot", "copilot-launched", transcript), {})

    def test_a_stale_or_foreign_launch_record_is_ignored(self):
        self.launch("muse")
        engine = self.engine()
        value = json.loads(engine.store.db.execute("SELECT value FROM metadata WHERE key='launching:muse'").fetchone()[0])
        engine.store.db.execute("UPDATE metadata SET value=? WHERE key='launching:muse'",
                                (json.dumps([value[0], value[1], time.time() - cli.LAUNCH_SECONDS - 5]),))
        engine.store.db.commit()
        engine.close()
        self.assertIn("hookSpecificOutput", self.start("muse", "much-later"))


class OverflowingWindows(CliWorkspace):
    def test_an_enormous_days_value_means_everything(self):
        for days in ("10000000", "inf"):
            status, _, errors = self.cli("list", "--days", days)
            self.assertEqual(status, 0, errors)


class ContinuationKeepsUserTitle(Workspace):
    """#145: a user's rename of a continued import survives the lineage merge."""

    def test_renamed_continuation(self):
        engine = self.engine()
        item = engine.handoff(self.read("codex"), "claude")
        last = json.loads(Path(item["target"]).read_text(encoding="utf-8").splitlines()[-1])
        with open(item["target"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "uuid": "next", "parentUuid": last["uuid"], "cwd": str(self.project),
                                     "timestamp": "2026-10-15T12:00:00Z", "message": {"role": "user", "content": "Continue."}}) + "\n")
            stream.write(json.dumps({"type": "custom-title", "customTitle": "Auth refactor", "sessionId": item["native_id"]}) + "\n")
        session = self.adapters["claude"].read(SessionRef(item["native_id"], item["target"]))
        self.assertTrue(session.provenance["user_title"])
        merged = engine.lineage(Session.from_dict(session.to_dict()))  # as cached
        self.assertEqual(merged.title, "Auth refactor")
        self.assertGreater(len(merged.events), 1)


class HookFailuresStillHandOn(CliWorkspace):
    def test_a_failing_hook_answers_and_still_syncs(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with mock.patch.object(cli, "hook", side_effect=SyncError("boom")), mock.patch.object(cli.subprocess, "Popen") as popen:
            status, output, errors = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload)
        self.assertEqual((status, json.loads(output)), (0, {}))
        self.assertIn("boom", errors)
        self.assertIn("--for-hook", popen.call_args[0][0])

    def test_a_failing_command_hands_on_queued_requests(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            status, _, _ = self.cli("show", "no-such-session")
        self.assertEqual(status, 1)
        self.assertIn("--queued", popen.call_args[0][0])

    def test_a_busy_hook_that_never_held_the_lock_spawns_nothing_extra(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        payload = {"session_id": "resumed", "cwd": str(self.project), "source": "resume"}
        with single_flight(self.state), mock.patch.object(cli, "HOOK_LOCK_WAIT", 0), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            self.cli("hook", "--harness", "muse", "--event", "start", payload=payload)
        popen.assert_not_called()

    def test_drain_hands_on_what_is_left_after_its_rounds(self):
        args = Namespace(config=None, budget=None)
        with mock.patch.object(cli, "has_pending", return_value=True), mock.patch.object(cli, "drain_pending", return_value=([], 1)), \
                mock.patch.object(cli, "hand_on_pending") as hand_on:
            cli.drain_after_release(args, self.adapters, {}, self.state)
        hand_on.assert_called_once()

    def test_queued_requests_use_the_default_budget(self):
        engine = self.engine()
        add_pending(self.state, {"project": str(self.project), "hook": None})
        with mock.patch.object(engine, "sync", return_value=[]) as sync:
            cli.drain_pending(engine, self.state, Namespace(budget=5000))
        self.assertNotIn("budget", sync.call_args.kwargs)


class StuckPendingRequest(Workspace):
    def test_a_request_that_cannot_be_removed_runs_once_per_process(self):
        add_pending(self.state, {"project": "/stuck", "hook": None})
        stuck = next((self.state / "pending").glob("*.json"))
        real = Path.unlink

        def unlink(self, *args, **kwargs):
            if self == stuck:
                raise PermissionError(13, "in use")
            return real(self, *args, **kwargs)
        with mock.patch.object(Path, "unlink", unlink):
            self.assertEqual(take_pending(self.state), [{"project": "/stuck", "hook": None}])
            self.assertEqual(take_pending(self.state), [])
            self.assertFalse(has_pending(self.state))


class ArchivedCodexImport(Workspace):
    """#146: an import the user archived in Codex is not written back."""

    def test_archived_plain_or_compressed(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                engine = self.engine()
                item = engine.handoff(self.read("muse"), "codex")
                target = Path(item["target"])
                if compressed:
                    target = compress(target)
                archived = self.adapters["codex"].home / "archived_sessions" / target.name
                archived.parent.mkdir(exist_ok=True)
                os.replace(str(target), str(archived))
                self.assertTrue(engine.complete(engine.store.transfer(item["id"])))
                engine.sync(source="muse", destination="codex", days=None)
                self.assertFalse(Path(item["target"]).exists())
                os.remove(str(archived))


class HotSessionsAndRoutes(Workspace):
    def grow_codex(self):
        self.append(self.paths["codex"], {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Still working " + str(time.monotonic())}]}})

    def test_a_destination_restricted_sync_does_not_mark_the_session_handled(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.sync(source="codex", destination="claude", days=None)
        key = self.read("codex").key
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM metadata WHERE key=?", ("considered:" + key,)).fetchone())

    def test_a_deferred_source_holds_no_slot_its_routes_never_use(self):
        engine = self.engine(config={"auto_handoff_max_age_hours": None,
                                     "routes": [{"from": "codex", "to": "claude"}, {"from": "muse", "to": "copilot"}]})
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.sync(days=None)
            self.grow_codex()
            engine.store.db.execute("DELETE FROM transfers WHERE source_key=?", (self.read("muse").key,))
            engine.store.db.commit()
            settle(self.paths["muse"])  # only the Codex session is still being written
            engine.sync(days=None)
        self.assertTrue(any(source == "codex" for source, _, _ in engine.deferred_sources))
        count = engine.store.db.execute("SELECT count(*) FROM transfers WHERE source_key=? AND destination='copilot'",
                                        (self.read("muse").key,)).fetchone()[0]
        self.assertEqual(count, 1)


class CompressedSupersededImports(Workspace):
    """Superseded Codex imports that Codex compressed."""

    def recompress_with_other_version(self, path):
        records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
        records[0]["payload"]["cli_version"] = "9.9.9"
        Path(path).write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return compress(path)

    def two(self, engine):
        first = engine.handoff(self.read("muse"), "codex")
        grow_muse(self)
        second = engine.handoff(self.read("muse"), "codex")
        return first, second

    def deleter(self, compressed):
        def delete(destination, native_id, path, check=False):
            if not check:
                os.remove(str(compressed))
            return True
        return delete

    def test_a_compressed_copy_differing_only_in_the_codex_version_goes(self):
        engine = self.engine()
        first, second = self.two(engine)
        compressed = self.recompress_with_other_version(first["target"])
        settle(compressed, second["target"])
        self.assertEqual(prune(engine.store, self.state, 7, delete=self.deleter(compressed))["native_copies_removed"], 1)

    def test_a_compressed_continuation_stays(self):
        engine = self.engine()
        first, second = self.two(engine)
        with open(first["target"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "2026-10-15T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "A continued reply."}]}}) + "\n")
        compressed = compress(first["target"])
        settle(compressed, second["target"])
        settle(Path(first["context"]).with_name("manifest.json"))
        self.assertEqual(prune(engine.store, self.state, 7, delete=self.deleter(compressed))["native_copies_removed"], 0)

    def test_twins_are_never_deleted_through_codex_while_both_exist(self):
        engine = self.engine()
        first, second = self.two(engine)
        data = Path(first["target"]).read_bytes()
        compressed = compress(first["target"])
        Path(first["target"]).write_bytes(data)
        settle(first["target"], compressed, second["target"])
        with mock.patch.object(prune_module, "compressed_duplicates", return_value=iter(())):
            self.assertEqual(prune(engine.store, self.state, 7, delete=self.deleter(compressed))["native_copies_removed"], 0)
        native_id = first["native_id"]
        home = self.adapters["codex"].home
        with contextlib.closing(sqlite3.connect(str(home / "state_5.sqlite"))) as state:
            state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            state.execute("INSERT INTO threads VALUES (?, ?, 0)", (native_id, first["target"]))
            state.commit()
        with mock.patch.object(cli.subprocess, "run") as run:
            self.assertFalse(cli.codex_delete(self.adapters)("codex", native_id, Path(first["target"])))
        run.assert_not_called()

    def test_a_re_handoff_keeps_the_digest_the_compressed_copy_holds(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        compress(item["target"])
        with mock.patch("agent_session_sync.legacy.codex_cli_version", return_value="9.9.9"):
            engine.handoff(self.read("muse"), "codex")
        self.assertEqual(engine.store.transfer(item["id"])["target_sha"], item["target_sha"])

    def test_dry_run_applies_the_codex_checks_and_sees_removed_twins(self):
        engine = self.engine()
        first, second = self.two(engine)
        compressed = compress(first["target"])
        settle(compressed, second["target"])
        refuse = mock.Mock(return_value=False)
        self.assertEqual(prune(engine.store, self.state, 7, dry_run=True, delete=refuse)["native_copies_removed"], 0)
        self.assertTrue(refuse.call_args.kwargs.get("check"))

    def test_a_continued_compressed_import_is_rejected_without_decompressing(self):
        engine = self.engine()
        first, second = self.two(engine)
        compressed = compress(first["target"])
        settle(Path(first["context"]).with_name("manifest.json"), hours=3)
        settle(compressed, second["target"])
        with mock.patch.object(prune_module, "read_zstd", side_effect=AssertionError("decompressed")):
            self.assertEqual(prune(engine.store, self.state, 7, delete=self.deleter(compressed))["native_copies_removed"], 0)

    def test_prune_keeps_oversized_or_truncated_later_frames(self):
        engine = self.engine()
        first, second = self.two(engine)
        compressed = compress(first["target"])
        valid = compressed.read_bytes()
        bound = 4 * first["target_size"] + (1 << 20)
        tail = zstd_compress(b"\n")
        compressed.write_bytes(valid + tail)
        self.assertTrue(prune_module.unchanged(compressed, first))
        delete = mock.Mock(return_value=True)
        for name, later_frame in (("oversized", zstd_compress(b"\n" * bound)), ("truncated", tail[:-1])):
            with self.subTest(frame=name):
                compressed.write_bytes(valid + later_frame)
                settle(compressed, second["target"])
                with mock.patch.object(prune_module, "read_zstd", wraps=io_module.read_zstd) as reader:
                    self.assertEqual(prune(engine.store, self.state, 7, delete=delete)["native_copies_removed"], 0)
                    reader.assert_called_once()
                delete.assert_not_called()
                self.assertTrue(compressed.is_file())


class RetentionDetails(Workspace):
    def test_an_explicit_import_without_its_folder_is_not_kept_forever(self):
        engine = self.engine()
        explicit = engine.handoff(self.read("muse"), "claude", note="n", explicit=True)
        grow_muse(self)
        newer = engine.handoff(self.read("muse"), "claude")
        shutil.rmtree(str(Path(explicit["context"]).parent))
        settle(explicit["target"], newer["target"], hours=24 * 8)
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 1)

    def test_an_import_from_an_earlier_renderer_of_the_same_revision_stays(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "RENDER_VERSION", 3):
            older = engine.handoff(self.read("muse"), "claude")
        newer = engine.handoff(self.read("muse"), "claude")
        self.assertNotEqual(older["id"], newer["id"])
        settle(older["target"], newer["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)

    def test_a_deleted_import_that_pins_nothing_is_forgotten(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "claude")
        engine.scan(days=None)
        key = "claude:default:" + item["native_id"]
        self.assertIsNotNone(engine.store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone())
        os.remove(item["target"])
        prune(engine.store, self.state, 7)
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone())


class ZstdBackends(Workspace):
    def test_without_a_decoder_compressed_rollouts_are_skipped_quietly(self):
        compress(self.paths["codex"])
        with mock.patch.object(io_module, "_ZSTD", [False]):
            self.assertEqual(list(self.adapters["codex"].discover()), [])
            engine = self.engine()
            engine.sync(days=None)
        self.assertEqual(engine.errors, [])

    def test_the_zstd_command_is_never_taken_from_the_current_directory(self):
        folder = Path(tempfile.mkdtemp(dir=str(self.base)))
        program = folder / ("zstd.cmd" if os.name == "nt" else "zstd")
        program.write_text("echo planted\n", encoding="utf-8")
        program.chmod(0o755)
        cwd = os.getcwd()
        os.chdir(str(folder))
        try:
            with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([".", "", str(self.base / "nowhere")])}):
                self.assertIsNone(find_program("zstd"))
            with mock.patch.dict(os.environ, {"PATH": str(folder)}):
                self.assertIsNone(find_program("zstd"))  # the current directory, even by absolute path
        finally:
            os.chdir(cwd)

    @unittest.skipUnless(__import__("importlib").util.find_spec("zstandard"), "zstandard is not installed")
    def test_zstandard_refuses_truncated_input_and_reads_every_frame(self):
        frames = zstd_compress(b"one\n") + zstd_compress(b"two\n")
        with mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None}):
            self.assertEqual(io_module.zstd_decompress("x", frames), b"one\ntwo\n")
            for broken in (b"", frames[:-3]):
                with self.assertRaises(SyncError):
                    io_module.zstd_decompress("x", broken)

    def test_a_rollout_compressed_between_discovery_and_read_is_still_read(self):
        ref = next(iter(self.adapters["codex"].discover()))
        compress(self.paths["codex"])
        self.assertTrue(self.adapters["codex"].read(ref).events)

    def test_the_legacy_registry_finds_a_compressed_target(self):
        engine = self.engine()
        engine.store.db.execute("INSERT INTO legacy_targets VALUES (?, ?, ?, ?)", ("/src.jsonl", "codex", "/t/rollout-x.jsonl", "sha"))
        engine.store.db.commit()
        self.assertIsNotNone(engine.store.legacy_import_target("/t/rollout-x.jsonl.zst"))

    def test_a_plain_re_write_does_not_hide_the_continuation_its_compressed_twin_holds(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        data = Path(item["target"]).read_bytes()
        with open(item["target"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "2026-10-15T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Continued in Codex."}]}}) + "\n")
        compress(item["target"])
        Path(item["target"]).write_bytes(data)  # what earlier releases wrote back
        sessions = {s.key: s for s in engine.scan(days=None)}
        texts = [e.text for e in sessions["codex:default:" + item["native_id"]].events]
        self.assertIn("Continued in Codex.", texts)


class RecordRecovery(Workspace):
    def lines(self):
        return self.paths["codex"].read_bytes().rstrip(b"\n").split(b"\n")

    def write(self, lines):
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")

    def test_a_complete_record_followed_by_nul_padding_is_kept(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "event_msg", "payload": {"n": 1}}\x00\x00\x00')
        self.write(lines)
        records, issues, _ = read_records(self.paths["codex"])
        self.assertIn({"type": "event_msg", "payload": {"n": 1}}, records)
        self.assertTrue(any("NUL padding" in issue for issue in issues))

    def test_two_records_joined_by_padding_are_both_kept(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "a"}\x00\x00{"type": "b"}')
        self.write(lines)
        records, _, _ = read_records(self.paths["codex"])
        self.assertIn({"type": "a"}, records)
        self.assertIn({"type": "b"}, records)

    def test_invalid_bytes_in_a_torn_successor_are_refused_despite_surrogate_escapes(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "x", "m": "cut{"type": "y", "v": "\xff \\ud83d\\ude00"}')
        self.write(lines)
        with self.assertRaises(SyncError):
            read_records(self.paths["codex"])


class TitleLineAtSmallBudgets(Workspace):
    def test_the_minimum_budget_keeps_the_conversation_over_the_title(self):
        session = self.read("muse")
        session.title = "A long conversation title " * 8
        session.workspace = "/" + "w" * 150
        session.events[-1].text = "LATEST CONCLUSION: " + "x" * 2000
        body, _ = compact(session, Path("/a/archive.json"), "11111111-1111-4111-8111-111111111111", 1024)
        self.assertTrue(body.startswith(MARKER))
        self.assertIn("LATEST CONCLUSION", body)
        body, _ = compact(session, Path("/a/archive.json"), "11111111-1111-4111-8111-111111111111", 4096)
        self.assertTrue(body.startswith("A long conversation title"))


if __name__ == "__main__":
    unittest.main()
