"""Regressions for the second adversarial review round (#141-#143 and follow-ups)."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from test_review_round import CliWorkspace, counted_revisions, settle
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync import render
from agent_session_sync.adapters import Codex, SessionRef
from agent_session_sync.discovery import Installation
from agent_session_sync.io import SyncError, read_records, resolved_workspace, zstd_decompress
from agent_session_sync.model import MARKER, Event, Session, instant
from agent_session_sync.prune import prune
from agent_session_sync.render import DEFAULT_BUDGET, compact
from agent_session_sync.store import add_pending, has_pending, single_flight, take_pending

MARKED = re.compile(re.escape(MARKER) + r"([a-f0-9-]{36})\]")


def zstd_compress(data):
    from agent_session_sync.io import zstd_available
    if not zstd_available():
        raise unittest.SkipTest("no zstd decoder (Python 3.14, zstandard, or the zstd command)")
    try:
        from compression import zstd
        return zstd.compress(data)
    except ImportError:
        pass
    from agent_session_sync.io import find_program
    command = find_program("zstd")
    if command is None:
        raise unittest.SkipTest("no zstd compressor (Python 3.14 or the zstd command)")
    return subprocess.run([command, "-q", "-c"], input=data, capture_output=True, check=True).stdout


def compress(path):
    """Do what Codex does to a cold rollout: write path.zst and remove the plain file."""
    path = Path(path)
    compressed = Path(str(path) + ".zst")
    compressed.write_bytes(zstd_compress(path.read_bytes()))
    path.unlink()
    return compressed


class QueuedSyncsNamingNothing(CliWorkspace):
    def test_a_busy_drain_only_sync_queues_nothing(self):
        with single_flight(self.state), mock.patch.object(cli, "QUEUED_LOCK_WAIT", 0):
            status, _, _ = self.cli("sync", "--quiet", "--queued")
        self.assertEqual(status, 0)
        self.assertFalse(has_pending(self.state))

    def test_a_request_naming_nothing_is_not_run_as_a_full_sync(self):
        engine = self.engine()
        add_pending(self.state, {"project": None, "hook": None})
        with mock.patch.object(engine, "sync") as sync:
            cli.drain_pending(engine, self.state, Namespace(budget=DEFAULT_BUDGET))
        sync.assert_not_called()
        self.assertFalse(has_pending(self.state))

    def test_a_hook_without_a_sync_of_its_own_hands_on_queued_requests(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        payload = {"session_id": "resumed", "cwd": str(self.project), "source": "resume"}
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            status, _, _ = self.cli("hook", "--harness", "muse", "--event", "start", payload=payload)
        self.assertEqual(status, 0)
        command = popen.call_args[0][0]
        self.assertEqual(command[command.index("sync"):], ["sync", "--quiet", "--queued"])

    def test_a_request_still_being_written_is_invisible(self):
        folder = self.state / "pending"
        folder.mkdir(parents=True)
        partial = folder / ".request-in-progress"
        partial.write_bytes(b"")
        self.assertFalse(has_pending(self.state))
        self.assertEqual(take_pending(self.state), [])
        self.assertTrue(partial.exists())
        add_pending(self.state, {"project": "/p", "hook": None})
        self.assertEqual([p.name.startswith(".") for p in folder.iterdir()].count(False), 1)


class HotSourceHandledOncePerInterval(Workspace):
    def grow(self):
        self.append(self.paths["codex"], {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Still working " + str(time.monotonic())}]}})

    def test_routes_that_exclude_some_destinations_do_not_keep_it_from_being_deferred(self):
        engine = self.engine(config={"auto_handoff_max_age_hours": None, "routes": [{"from": "codex", "to": "claude"}]})
        adapter = self.adapters["codex"]
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.sync(source="codex", days=None)
            self.grow()
            with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
                engine.sync(source="codex", days=None)
        self.assertEqual(read.call_count, 0)

    def test_a_failed_pass_is_not_counted_as_handled(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "HOT_BYTES", 1), \
                mock.patch.object(engine, "plan", side_effect=SyncError("disk full")):
            engine.sync(source="codex", days=None)
        key = self.read("codex").key
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM metadata WHERE key=?", ("considered:" + key,)).fetchone())

    def test_a_deferred_source_holds_slots_with_its_conversation_time(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.sync(source="codex", days=None)
            self.grow()
            engine.sync(days=None)
        deferred = [entry for entry in engine.deferred_sources if entry[0] == "codex"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0][2], instant(self.read("codex").updated).timestamp())


class CodexCuration(Workspace):
    native_id = "01a1de7d-cade-79fc-92c2-f7edb633664a"

    def setUp(self):
        super().setUp()
        self.home = self.adapters["codex"].home
        self.rollout = self.home / "sessions/2026/11/20" / ("rollout-2026-11-20T10-00-00-" + self.native_id + ".jsonl")
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.rollout.write_text("{}\n", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(str(self.home / "state_5.sqlite"))) as state:
            state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER, name TEXT, is_pinned INTEGER,"
                          " thread_section_id TEXT, project_id TEXT)")
            state.execute("INSERT INTO threads (id, rollout_path, archived) VALUES (?, ?, 0)", (self.native_id, str(self.rollout)))
            state.commit()

    def attempt(self, adapters=None, rollout=None, destination="codex"):
        deleter = cli.codex_delete(adapters or self.adapters)
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            result = deleter(destination, self.native_id, rollout or self.rollout)
        return result, run.called

    def test_an_untouched_import_may_go(self):
        self.assertEqual(self.attempt(), (True, True))

    def test_renamed_pinned_or_filed_threads_stay(self):
        for column, value in (("name", "Keep me: login fix"), ("is_pinned", 1), ("thread_section_id", "s1"), ("project_id", "p1")):
            with self.subTest(column=column):
                with contextlib.closing(sqlite3.connect(str(self.home / "state_5.sqlite"))) as state:
                    state.execute("UPDATE threads SET {}=?".format(column), (value,))
                    state.commit()
                    self.assertEqual(self.attempt(), (False, False))
                    state.execute("UPDATE threads SET {}=NULL".format(column))
                    state.commit()

    def test_a_rename_recorded_only_in_the_session_index_keeps_the_thread(self):
        (self.home / "session_index.jsonl").write_text(json.dumps({"id": self.native_id, "thread_name": "Mine"}) + "\n", encoding="utf-8")
        self.assertEqual(self.attempt(), (False, False))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlinked_sessions_directory_still_allows_deletion(self):
        home = self.base / "codex-linked"
        real = self.base / "big-disk" / "sessions"
        shutil.copytree(str(self.home / "sessions"), str(real))
        home.mkdir()
        os.symlink(str(real), str(home / "sessions"))
        shutil.copy2(str(self.home / "state_5.sqlite"), str(home / "state_5.sqlite"))
        rollout = home / "sessions" / self.rollout.relative_to(self.home / "sessions")
        with contextlib.closing(sqlite3.connect(str(home / "state_5.sqlite"))) as state:
            state.execute("UPDATE threads SET rollout_path=?", (str(rollout),))
            state.commit()
        linked = Codex(Installation("codex", "linked", str(home), sys.executable))
        self.assertEqual(self.attempt({"codex@linked": linked}, rollout, "codex@linked"), (True, True))


class ForgetOnlyDeletedTranscripts(Workspace):
    def test_lineage_pins_compressed_and_unmounted_transcripts_are_kept(self):
        engine = self.engine()
        engine.scan(days=None)
        item = engine.handoff(self.read("muse"), "claude")
        gone_dir = self.base / "claude-projects-elsewhere"
        gone_dir.mkdir()
        continued = Session("claude", item["native_id"], str(self.project), [Event(str(i), "user", "turn") for i in range(4)],
                            source_path=str(gone_dir / "continued.jsonl"),
                            provenance={"transfer_id": item["id"], "baseline_count": 2})
        engine.store.remember(continued, None)
        compress_target = self.paths["copilot"]
        (Path(str(compress_target) + ".zst")).write_bytes(b"compressed")
        os.remove(str(compress_target))
        shutil.rmtree(str(self.paths["agy"].parent))  # its home is gone (unmounted, moved)
        os.remove(str(self.paths["muse"]))  # deleted for real
        before = prune(engine.store, self.state, 7, dry_run=True)
        after = prune(engine.store, self.state, 7)
        self.assertEqual(before["index_rows_forgotten"], 1)
        self.assertEqual((before["handoffs_removed"], before["archives_removed"]), (after["handoffs_removed"], after["archives_removed"]))
        keys = {row[0] for row in engine.store.db.execute("SELECT key FROM sessions")}
        self.assertNotIn(self.read_key("muse"), keys)
        for kept in (continued.key, self.read_key("copilot"), self.read_key("agy")):
            self.assertIn(kept, keys)
        self.assertIn(item["id"], {t for t in live(engine)})

    def read_key(self, name):
        return "{}:default:{}".format(name, IDS[name])


def live(engine):
    from agent_session_sync.prune import live_transfers
    return live_transfers(engine.store, engine.directory)[0]


class ExplicitHandoffsStay(Workspace):
    def test_an_explicit_handoff_is_kept_through_the_retention_window(self):
        engine = self.engine()
        explicit = engine.handoff(self.read("muse"), "claude", note="Keep this note.", explicit=True)
        self.assertTrue(engine.store.transfer(explicit["id"])["explicit"])
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})
        newer = engine.handoff(self.read("muse"), "claude")
        settle(explicit["target"], newer["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)
        self.assertTrue(Path(explicit["target"]).exists())
        self.assertTrue(Path(explicit["context"]).with_name("manifest.json").exists())
        old = 24 * 8
        settle(explicit["target"], hours=old)
        for path in Path(explicit["context"]).parent.iterdir():
            settle(path, hours=old)
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 1)


class WideInjectionWindows(Workspace):
    def test_a_window_reaching_before_1970_needs_no_pre_epoch_timestamp(self):
        class Strict(datetime):
            @classmethod
            def fromtimestamp(cls, value, tz=None):
                if value < 0:
                    raise OSError(22, "Invalid argument")  # as on Windows
                return super().fromtimestamp(value, tz)
        engine = self.engine(config={"auto_handoff_max_age_hours": 24 * 365 * 90})
        with mock.patch.object(engine_module, "datetime", Strict):
            self.assertIsNotNone(engine.incoming("muse", str(self.project)))


class ImportRevisionMemo(Workspace):
    def test_an_unchanged_import_is_not_hashed_again(self):
        engine = self.engine()
        imported = engine.store.remember(Session("external", "bundle", str(self.project),
                                                 [Event("a", "user", "Imported prompt.", "2026-10-15T09:00:00Z")]), imported=True)
        engine.sync(days=None)
        with counted_revisions() as calls:
            self.engine().sync(days=None)
        self.assertNotIn(imported.key, calls)
        self.assertEqual(calls, [])


class StartRecordedWorkspace(Workspace):
    def test_a_stop_before_the_first_parse_syncs_the_launch_directory(self):
        engine = self.engine()
        sub = self.project / "src"
        sub.mkdir()
        start = {"session_id": "fresh", "cwd": str(self.project), "source": "startup"}
        stop = {"session_id": "fresh", "cwd": str(sub)}
        with mock.patch.object(engine, "sync") as sync:
            for event, payload in (("start", start), ("stop", stop)):
                with mock.patch("sys.stdin", __import__("io").StringIO(json.dumps(payload))):
                    cli.hook(engine, Namespace(harness="claude", event=event))
        projects = [call.kwargs["project"] for call in sync.call_args_list]
        self.assertEqual(projects, [resolved_workspace(str(self.project))] * 2)
        self.assertEqual(engine.store.workspace("claude", "fresh"), resolved_workspace(str(self.project)))


class TitleMarkers(Workspace):
    real = "22222222-2222-4222-8222-222222222222"

    def test_no_marker_survives_or_is_rebuilt_in_the_title(self):
        uuid = "11111111-1111-4111-8111-111111111111"
        titles = (MARKER.replace(" ", "\t") + uuid + "] old handoff",
                  MARKER.replace(" ", "\r\n ") + uuid + "]",
                  "[agent-session-sync:" + MARKER.strip() + "v1 " + uuid + "]",
                  "< \t<" + MARKER + uuid + "] title")
        for title in titles:
            with self.subTest(title=title):
                text = render.title_text(title)
                self.assertNotIn(MARKER.strip(), text)
                self.assertFalse(text.startswith(("<", " ")))
                session = self.read("muse")
                session.title = title
                body, _ = compact(session, Path("/tmp/archive.json"), self.real)
                self.assertEqual(MARKED.search(body).group(1), self.real)


class MultibyteCuts(Workspace):
    def lines(self):
        return self.paths["codex"].read_bytes().rstrip(b"\n").split(b"\n")

    def test_a_record_cut_inside_a_multibyte_character_is_dropped_once_the_writer_continued(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "event_msg", "payload": {"message": "' + "語".encode("utf-8")[:2])
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        records, issues, _ = read_records(self.paths["codex"])
        self.assertEqual(len(records), len(lines) - 1)
        self.assertTrue(any("cut short" in issue for issue in issues))

    def test_a_torn_record_cut_inside_a_multibyte_character_keeps_its_successor(self):
        lines = self.lines()
        lines.insert(2, b'{"type": "x", "m": "' + "語".encode("utf-8")[:2] + b'{"type": "event_msg", "payload": {}}')
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        records, issues, _ = read_records(self.paths["codex"])
        self.assertIn({"type": "event_msg", "payload": {}}, records)
        self.assertTrue(any("torn record" in issue for issue in issues))

    def test_invalid_bytes_inside_a_complete_record_are_refused(self):
        for bad in (b'{"bad": "\xff"}', b'{"a": "x', b'{"bad": "\xff", "b": {"type": "y"}}'):
            with self.subTest(line=bad):
                lines = self.lines()
                lines.insert(2, bad if bad != b'{"a": "x' else b'{"cut": "\xe8\xaa' + b'{"next": "\xff"}')
                self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
                with self.assertRaises(SyncError):
                    read_records(self.paths["codex"])


class CompressedCodexRollouts(Workspace):
    """#141: Codex compresses cold rollouts to .jsonl.zst."""

    def test_a_compressed_rollout_is_discovered_and_read(self):
        before = self.read("codex")
        compressed = compress(self.paths["codex"])
        refs = list(self.adapters["codex"].discover())
        self.assertEqual([ref.path for ref in refs], [str(compressed)])
        session = self.adapters["codex"].read(refs[0])
        self.assertEqual(session.events, before.events)
        self.assertIn(session.key, [s.key for s in self.engine().scan(days=None)])

    def test_a_plain_copy_is_preferred_to_its_compressed_twin(self):
        data = self.paths["codex"].read_bytes()
        compress(self.paths["codex"])
        self.paths["codex"].write_bytes(data)
        refs = list(self.adapters["codex"].discover())
        self.assertEqual([ref.path for ref in refs], [str(self.paths["codex"])])

    def test_a_compressed_import_counts_as_present_and_is_not_written_again(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        compress(item["target"])
        self.assertTrue(engine.complete(engine.store.transfer(item["id"])))
        engine.sync(source="muse", destination="codex", days=None)
        engine.handoff(self.read("muse"), "codex")  # an explicit repeat of the same transfer
        self.assertFalse(Path(item["target"]).exists())

    def test_a_plain_copy_written_again_beside_its_compressed_twin_is_removed(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        data = Path(item["target"]).read_bytes()
        compressed = compress(item["target"])
        Path(item["target"]).write_bytes(data)  # what earlier releases did on the next sync
        settle(item["target"], compressed)
        dry = prune(engine.store, self.state, 7, dry_run=True)
        self.assertEqual(dry["compressed_duplicates_removed"], 1)
        self.assertTrue(Path(item["target"]).exists())
        self.assertEqual(prune(engine.store, self.state, 7)["compressed_duplicates_removed"], 1)
        self.assertFalse(Path(item["target"]).exists())
        self.assertTrue(compressed.exists())
        Path(item["target"]).write_bytes(data + b"\n")  # changed since sync wrote it (resumed): kept
        settle(item["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["compressed_duplicates_removed"], 0)
        compressed.write_bytes(zstd_compress(b"a continued thread"))  # the compressed side may differ
        Path(item["target"]).write_bytes(data)
        settle(item["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["compressed_duplicates_removed"], 1)
        self.assertEqual(zstd_decompress(compressed, compressed.read_bytes()), b"a continued thread")

    def test_a_superseded_compressed_import_is_removed_through_codex(self):
        engine = self.engine()
        first = engine.handoff(self.read("muse"), "codex")
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})
        second = engine.handoff(self.read("muse"), "codex")
        compressed = compress(first["target"])
        settle(compressed, second["target"])
        calls = []

        def delete(destination, native_id, path):
            calls.append(path)
            os.remove(str(compressed))
            return True
        self.assertEqual(prune(engine.store, self.state, 7, delete=delete)["native_copies_removed"], 1)
        self.assertEqual(calls, [Path(first["target"])])

    def test_decompression_falls_back_to_the_zstd_command(self):
        data = b'{"type": "session_meta"}\n'
        packed = zstd_compress(data)
        from agent_session_sync.io import find_program
        if find_program("zstd") is None:
            self.skipTest("no zstd command")
        with mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None, "zstandard": None}):
            self.assertEqual(zstd_decompress("x.zst", packed), data)
        with mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None, "zstandard": None}), \
                mock.patch("agent_session_sync.io.find_program", return_value=None), self.assertRaisesRegex(SyncError, "needs Python 3.14"):
            zstd_decompress("x.zst", packed)


if __name__ == "__main__":
    unittest.main()
