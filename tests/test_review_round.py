"""Regressions for the adversarial review of the live-findings fixes (#135-#140 and follow-ups)."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import contextlib
import io
import json
import os
import random
import sqlite3
import string
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync import prune as prune_module
from agent_session_sync.adapters import FileAdapter, SessionRef
from agent_session_sync.discovery import Installation
from agent_session_sync.io import SyncError, digest, json_bytes, read_records, resolved_workspace
from agent_session_sync.model import MARKER, Event, Session
from agent_session_sync.prune import prune
from agent_session_sync.render import compact
from agent_session_sync.store import add_pending, has_pending, single_flight, take_pending


def settle(*paths, hours=2):
    stamp = time.time() - hours * 3600
    for path in paths:
        os.utime(str(path), (stamp, stamp))


def recent(minutes=10):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def counted_revisions():
    """Count Session.revision computations."""
    calls = []
    original = Session.revision

    def revision(self):
        calls.append(self.key)
        return original.fget(self)
    with mock.patch.object(Session, "revision", property(revision)):
        yield calls


class CliWorkspace(Workspace):
    def setUp(self):
        super().setUp()
        self.config = self.base / "config.json"
        self.write_config({})

    def write_config(self, extra):
        value = {"harnesses": {i.harness: {"home": i.home, "executable": sys.executable} for i in self.installations}}
        value.update(extra)
        self.config.write_bytes(json_bytes(value))

    def cli(self, *argv, payload=None, state=None):
        stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
        with mock.patch("sys.stdin", stdin), mock.patch.object(cli, "discover", return_value=self.installations), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            status = cli.main(["--state-dir", str(state or self.state), "--config", str(self.config)] + list(argv))
        return status, output.getvalue(), errors.getvalue()


class LaunchBeatsHookBinding(Workspace):
    """An explicit launch keeps merging when the start hook bound a different transfer."""

    def test_launch_with_a_note_or_budget_merges_its_own_source(self):
        for options in ({"note": "Focus on the failing test."}, {"budget": 4096}):
            with self.subTest(options=options):
                engine = self.engine()
                parent = self.read("codex")
                launch = engine.handoff(parent, "muse", mode="context", **options)
                injected = engine.handoff(parent, "muse", mode="context")
                self.assertNotEqual(launch["id"], injected["id"])
                session = Session("muse", "launched-" + "-".join(options), str(self.project),
                                  [Event("l", "user", launch["command"][-1]), Event("a", "assistant", "Continuing.")],
                                  provenance={"transfer_id": launch["id"], "context": True})
                engine.store.db.execute("INSERT INTO metadata VALUES (?, ?)", ("binding:muse:" + session.native_id, injected["id"]))
                engine.store.db.commit()
                result = engine.lineage(session)
                self.assertFalse(result.provenance.get("linked"))
                self.assertEqual(result.title, parent.title)
                self.assertTrue(result.parent.startswith(parent.key + "@"))
                extra = 1 if options.get("note") else 0
                self.assertEqual(len(result.events), len(parent.events) + extra + 1)


class InjectionWindowBounds(Workspace):
    def test_huge_or_infinite_window_means_no_limit(self):
        for value in (1e9, float("inf"), float("nan"), -1):
            self.assertIsNone(engine_module.injection_max_age({"auto_handoff_max_age_hours": value}))
        engine = self.engine(config={"auto_handoff_max_age_hours": 1e9})
        self.assertIsNotNone(engine.incoming("muse", str(self.project)))


class HookAnswerIsOneObject(CliWorkspace):
    def test_spawn_failure_after_the_answer_queues_the_sync_and_prints_nothing_more(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with mock.patch.object(cli.subprocess, "Popen", side_effect=OSError(12, "Cannot allocate memory")):
            status, output, errors = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), {})  # exactly one JSON document
        self.assertIn("detached sync not started", errors)
        self.assertTrue(has_pending(self.state))


class HookLockWaits(CliWorkspace):
    def test_a_start_hook_that_cannot_inject_does_not_wait_for_the_injection_lock(self):
        payload = {"session_id": "resumed", "cwd": str(self.project), "source": "resume"}
        with single_flight(self.state), mock.patch.object(cli.subprocess, "Popen") as popen:
            started = time.monotonic()
            status, output, _ = self.cli("hook", "--harness", "muse", "--event", "start", payload=payload)
            elapsed = time.monotonic() - started
        self.assertEqual((status, json.loads(output)), (0, {}))
        self.assertLess(elapsed, cli.INJECTION_LOCK_WAIT - 2)
        popen.assert_not_called()  # a resume start hands nothing on

    def test_a_hook_with_nothing_to_do_takes_no_lock_and_spawns_nothing(self):
        foreign = self.base / "elsewhere" / "transcript.jsonl"
        payload = {"conversationId": "ide", "cwd": str(self.project), "transcript_path": str(foreign)}
        with single_flight(self.state), mock.patch.object(cli.subprocess, "Popen") as popen:
            started = time.monotonic()
            status, output, _ = self.cli("hook", "--harness", "agy", "--event", "stop", payload=payload)
            self.assertLess(time.monotonic() - started, cli.HOOK_LOCK_WAIT)
        self.assertEqual((status, json.loads(output)), (0, {}))
        popen.assert_not_called()

    def test_busy_injecting_start_hook_injects_nothing_and_hands_its_sync_on(self):
        # Without the lock nothing can tell whether the injection is still due (a newer
        # conversation of the destination, routes), so nothing is injected.
        self.write_config({"auto_handoff_max_age_hours": None})
        engine = self.engine()
        self.assertIsNotNone(engine.incoming("muse", str(self.project)))
        engine.close()
        payload = {"session_id": "new-session", "cwd": str(self.project), "source": "startup"}
        with single_flight(self.state), mock.patch.object(cli, "INJECTION_LOCK_WAIT", 0), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            status, output, _ = self.cli("hook", "--harness", "muse", "--event", "start", payload=payload)
        self.assertEqual((status, json.loads(output)), (0, {}))
        self.assertIn("--for-hook", popen.call_args[0][0])


class QueuedRequestsAreNotStranded(CliWorkspace):
    def test_a_queuer_whose_holder_already_left_runs_its_own_request(self):
        real = cli.run_locked
        calls = []

        def busy_once(*args, **kwargs):
            if not calls:
                calls.append(1)
                raise cli.Busy("another sync is already running")
            return real(*args, **kwargs)
        with mock.patch.object(cli, "run_locked", side_effect=busy_once):
            status, _, _ = self.cli("sync", "--quiet", "--queued", "--project", str(self.project))
        self.assertEqual(status, 0)
        self.assertFalse(has_pending(self.state))
        self.assertTrue(self.engine().store.db.execute("SELECT count(*) FROM transfers").fetchone()[0])

    def test_other_commands_hand_queued_requests_to_a_drain_only_sync(self):
        add_pending(self.state, {"project": str(self.project), "hook": None})
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            status, _, _ = self.cli("list", "--days", "100000")
        self.assertEqual(status, 0)
        command = popen.call_args[0][0]
        self.assertEqual(command[command.index("sync"):], ["sync", "--quiet", "--queued"])
        status, _, _ = self.cli("sync", "--quiet", "--queued")
        self.assertEqual(status, 0)
        self.assertFalse(has_pending(self.state))
        self.assertTrue(self.engine().store.db.execute("SELECT count(*) FROM transfers").fetchone()[0])


class PendingQueueFiles(Workspace):
    def test_corrupt_requests_are_dropped_and_unreadable_ones_wait(self):
        add_pending(self.state, {"project": "/a", "hook": None})
        folder = self.state / "pending"
        (folder / "00000000000000000001-1-0.json").write_bytes(b"{not json")
        add_pending(self.state, {"project": "/b", "hook": None})
        blocked = sorted(folder.glob("*.json"))[-1]
        real = Path.read_bytes

        def read_bytes(self):
            if self == blocked:
                raise PermissionError(13, "in use")
            return real(self)
        with mock.patch.object(Path, "read_bytes", read_bytes):
            self.assertEqual(take_pending(self.state), [{"project": "/a", "hook": None}])
        self.assertTrue(blocked.exists())
        # Set aside for the rest of this process (no drain loops on it); a new process retries it.
        self.assertEqual(take_pending(self.state), [])
        self.assertFalse(has_pending(self.state))
        with mock.patch("agent_session_sync.store._stuck", set()):
            self.assertEqual(take_pending(self.state), [{"project": "/b", "hook": None}])


class GrownTranscriptsThatChangeWorkspace(Workspace):
    """A scoped scan re-reads a grown transcript when the appended records can move it."""

    def setUp(self):
        super().setUp()
        self.other = self.base / "other-project"
        self.other.mkdir()

    def keys(self, engine):
        return [s.key for s in engine.scan(project=str(self.other), days=None)]

    def test_copilot_resumed_in_another_directory(self):
        engine = self.engine()
        engine.scan(days=None)
        self.append(self.paths["copilot"], {"type": "session.start", "id": "s9", "timestamp": "2026-10-15T11:00:00Z",
                                            "data": {"context": {"cwd": str(self.other)}}})
        self.assertIn(self.read("copilot").key, self.keys(engine))

    def test_claude_chain_re_rooted_in_another_directory(self):
        engine = self.engine()
        engine.scan(days=None)
        self.append(self.paths["claude"], {"type": "user", "uuid": "new-root", "parentUuid": None, "cwd": str(self.other),
                                           "sessionId": IDS["claude"], "timestamp": "2026-10-15T11:00:00Z",
                                           "message": {"role": "user", "content": "An edited first prompt."}})
        self.assertIn(self.read("claude").key, self.keys(engine))

    def test_rewritten_portable_session_file(self):
        home = self.base / "portable"
        home.mkdir()
        self.adapters["portable"] = FileAdapter(Installation("portable", "default", str(home)), {"launch": ["x"]})
        path = home / "conversation.json"
        session = self.read("muse")
        path.write_bytes(json_bytes(session.to_dict()))
        engine = self.engine()
        engine.scan(days=None)
        session.workspace = str(self.other)
        session.events.append(Event("more", "assistant", "More."))
        path.write_bytes(json_bytes(session.to_dict()))
        self.assertIn("portable:default:conversation", self.keys(engine))

    def test_equal_size_rewrite_is_not_growth(self):
        stamp = [2, 10, "x", []]
        self.assertFalse(engine_module.grown_since(("/p", [1, 10, "x", []], "/w"), "/p", stamp))
        self.assertTrue(engine_module.grown_since(("/p", [1, 9, "x", []], "/w"), "/p", stamp))


class HotSourcesPerRoute(Workspace):
    def hot_codex(self, engine):
        self.append(self.paths["codex"], {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Still working " + str(time.monotonic())}]}})

    def count(self, engine, source, destination):
        return engine.store.db.execute("SELECT count(*) FROM transfers WHERE source_key=? AND destination=?",
                                       (source, destination)).fetchone()[0]

    def test_a_context_injection_does_not_throttle_the_native_import(self):
        engine = self.engine()
        key = self.read("codex").key
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.handoff(self.read("codex"), "muse", mode="context")
            self.hot_codex(engine)
            engine.sync(source="codex", days=None)
            self.assertEqual(self.count(engine, key, "claude"), 1)  # an injection is not a sync pass
            self.hot_codex(engine)
            engine.sync(source="codex", days=None)
            self.assertEqual(self.count(engine, key, "claude"), 1)  # handled within the interval

    def test_a_deferred_hot_session_keeps_its_context_slot(self):
        engine = self.engine()
        hot_key, older_key = self.read("codex").key, self.read("claude").key
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.sync(source="codex", days=None)
            self.hot_codex(engine)
            engine.sync(days=None)
        self.assertTrue(any(source == "codex" for source, _, _ in engine.deferred_sources))
        self.assertEqual(self.count(engine, older_key, "muse"), 0)
        self.assertEqual(self.count(engine, older_key, "codex"), 1)


class CodexDeleteSafety(Workspace):
    native_id = "01a1de7d-cade-79fc-92c2-f7edb633664a"

    def setUp(self):
        super().setUp()
        self.home = self.adapters["codex"].home
        self.rollout = self.home / "sessions/2026/11/20" / ("rollout-2026-11-20T10-00-00-" + self.native_id + ".jsonl")
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.rollout.write_text("{}\n", encoding="utf-8")
        self.deleter = cli.codex_delete(self.adapters)

    def attempt(self):
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            result = self.deleter("codex", self.native_id, self.rollout)
        return result, run.called

    def test_a_resumed_or_archived_copy_of_the_thread_blocks_deletion(self):
        archived = self.home / "archived_sessions" / self.rollout.name
        archived.parent.mkdir(parents=True)
        archived.write_text("{}\n", encoding="utf-8")
        self.assertEqual(self.attempt(), (False, False))

    def test_codex_index_showing_use_blocks_deletion(self):
        state = sqlite3.connect(str(self.home / "state_5.sqlite"))
        state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
        state.execute("INSERT INTO threads VALUES (?, ?, 0)", (self.native_id, str(self.rollout)))
        state.commit()
        history = sqlite3.connect(str(self.home / "thread_history_1.sqlite"))
        history.execute("CREATE TABLE thread_turns (thread_id TEXT)")
        history.execute("INSERT INTO thread_turns VALUES (?)", (self.native_id,))
        history.commit()
        self.assertEqual(self.attempt(), (True, True))  # one turn: the import itself
        history.execute("INSERT INTO thread_turns VALUES (?)", (self.native_id,))
        history.commit()
        self.assertEqual(self.attempt(), (False, False))  # continued in Codex
        history.execute("DELETE FROM thread_turns")
        history.commit()
        state.execute("UPDATE threads SET archived=1")
        state.commit()
        self.assertEqual(self.attempt(), (False, False))
        state.execute("DROP TABLE threads")
        state.commit()
        self.assertEqual(self.attempt(), (False, False))  # unknown layout: never delete
        state.close()
        history.close()

    def test_a_path_outside_the_codex_home_is_never_deleted(self):
        elsewhere = self.base / "elsewhere" / self.rollout.name
        elsewhere.parent.mkdir()
        elsewhere.write_text("{}\n", encoding="utf-8")
        with mock.patch.object(cli.subprocess, "run") as run:
            self.assertFalse(self.deleter("codex", self.native_id, elsewhere))
        run.assert_not_called()


class NativeCopySupersession(Workspace):
    def revisions(self, engine, **options):
        first = engine.handoff(self.read("muse"), "claude")
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "Later."}}})
        second = engine.handoff(self.read("muse"), "claude", **options)
        return first, second

    def test_an_interrupted_newer_import_does_not_supersede_the_last_complete_one(self):
        engine = self.engine()
        first, second = self.revisions(engine)
        engine.store.save_transfer(dict(second, state="planned"))
        os.remove(second["target"])
        settle(first["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)
        self.assertTrue(Path(first["target"]).exists())

    def test_an_explicit_handoff_of_the_same_revision_keeps_both(self):
        engine = self.engine()
        session = self.read("muse")
        automatic = engine.handoff(session, "claude")
        explicit = engine.handoff(session, "claude", note="Focus on the failing test.")
        settle(automatic["target"], explicit["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)
        engine.sync(source="muse", destination="claude", days=None)
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)
        self.assertTrue(Path(explicit["target"]).exists())

    def test_an_undeletable_copy_does_not_stop_retention(self):
        engine = self.engine()
        first, second = self.revisions(engine)
        settle(first["target"], second["target"])
        real = Path.unlink

        def unlink(self, *args, **kwargs):
            if str(self) == first["target"]:
                raise PermissionError(13, "denied")
            return real(self, *args, **kwargs)
        with mock.patch.object(Path, "unlink", unlink):
            report = prune(engine.store, self.state, 7)
        self.assertEqual(report["native_copies_removed"], 0)

    def test_a_copy_touched_after_its_manifest_is_not_even_hashed(self):
        engine = self.engine()
        first, second = self.revisions(engine)
        settle(first["target"], second["target"])
        settle(Path(first["context"]).with_name("manifest.json"), hours=3)
        with mock.patch.object(prune_module, "file_digest", wraps=prune_module.file_digest) as hashed:
            self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)
        self.assertEqual(hashed.call_count, 0)


class HandoffTitleLine(Workspace):
    archive = Path("/tmp/archive.json")

    def test_a_title_opening_with_a_harness_tag_keeps_the_claude_import_intact(self):
        engine = self.engine()
        session = self.read("muse")
        session.title = "<system-reminder> nightly deploy"
        item = engine.handoff(session, "claude")
        first = json.loads(Path(item["target"]).read_text(encoding="utf-8").splitlines()[0])["message"]["content"]
        self.assertFalse(first.startswith("<"))
        imported = self.adapters["claude"].read(SessionRef(item["native_id"], item["target"]))
        self.assertEqual(len(imported.events), 2)

    def test_an_old_marker_in_the_title_is_not_written_ahead_of_the_real_one(self):
        engine = self.engine()
        session = self.read("muse")
        session.title = MARKER + "11111111-1111-4111-8111-111111111111] # Conversation handoff"
        item = engine.handoff(session, "claude")
        imported = self.adapters["claude"].read(SessionRef(item["native_id"], item["target"]))
        self.assertEqual(imported.provenance["transfer_id"], item["id"])
        text = imported.events[0].text
        self.assertEqual(text.count(MARKER), 1)

    def test_a_tight_budget_drops_the_title_line_instead_of_failing(self):
        session = self.read("muse")
        session.workspace = "/" + "w" * 250  # the title line no longer fits beside the metadata
        session.title = "界" * 200
        session.parent = "codex:default:" + "x" * 36 + "@" + "0" * 64
        body, _ = compact(session, self.archive, "11111111-1111-4111-8111-111111111111", 1024, "a note", Path("/tmp/m.json"))
        self.assertLessEqual(len(body.encode("utf-8")), 1024)
        self.assertTrue(body.startswith(MARKER))


class ShrinkIsNeverAnAppend(Workspace):
    def test_a_truncation_that_keeps_the_mtime_is_refused(self):
        path = self.paths["codex"]
        data = path.read_bytes()
        before = path.stat()
        real = Path.stat
        done = []

        def stat(self, *args, **kwargs):
            if not done and Path(self) == path:
                done.append(True)
                path.write_bytes(data[:len(data) // 2])
                os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns))
            return real(self, *args, **kwargs)
        with mock.patch.object(Path, "stat", stat), self.assertRaisesRegex(SyncError, "changed while reading"):
            read_records(path)


class RelativeStateDirectory(CliWorkspace):
    def test_the_detached_sync_gets_an_absolute_state_directory(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        cwd = os.getcwd()
        os.chdir(str(self.base))
        try:
            with mock.patch.object(cli.subprocess, "Popen") as popen:
                status, _, _ = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload, state="relative-state")
        finally:
            os.chdir(cwd)
        self.assertEqual(status, 0)
        state = Path(popen.call_args[0][0][popen.call_args[0][0].index("--state-dir") + 1])
        self.assertTrue(state.is_absolute())
        self.assertEqual(state.resolve(), (self.base / "relative-state").resolve())  # /var -> /private/var on macOS


class LargeTranscripts(Workspace):
    """#135: line-by-line parsing and incremental revision hashing, byte-identical."""

    def test_an_unfinished_multibyte_character_at_the_end_is_deferred(self):
        path = self.paths["codex"]
        with path.open("ab") as stream:
            stream.write('{"type": "event_msg", "payload": {"message": "漢'.encode("utf-8")[:-1])
        records, issues, _ = read_records(path)
        self.assertIn("incomplete trailing record deferred", issues)
        self.assertEqual(len(records), len(read_records(self.paths["codex"])[0]))

    def test_invalid_utf8_inside_the_file_is_still_refused(self):
        path = self.paths["codex"]
        lines = path.read_bytes().split(b"\n")
        lines.insert(1, b'{"bad": "\xff"}')
        path.write_bytes(b"\n".join(lines))
        with self.assertRaisesRegex(SyncError, "invalid UTF-8"):
            read_records(path)

    def test_revision_and_index_document_match_the_original_formulas(self):
        rng = random.Random(11)

        def text(n):
            return "".join(rng.choice(string.ascii_letters + "äé漢😀\n\"\\") for _ in range(n))
        for trial in range(50):
            events = [Event("e{}".format(i), rng.choice(["user", "assistant", "tool_call"]), text(rng.randint(0, 30)),
                            rng.choice(["", "2026-11-20T10:00:00Z"]), text(2), text(2),
                            {"arguments": {"x": [1, 2.5, None, True, text(4)]}} if i % 2 else {}) for i in range(rng.randint(0, 8))]
            session = Session("codex", "id-{}".format(trial), text(8), events, title=text(12), issues=[text(3)])
            original = digest(json_bytes({"key": session.key, "workspace": session.workspace, "title": session.title,
                                          "branch": session.branch, "parent": session.parent, "issues": session.issues,
                                          "events": [asdict(e) for e in session.events]}))
            self.assertEqual(session.revision, original)
            self.assertEqual(json.dumps(session.document()), json.dumps(session.to_dict()))


class ParentArchiveVerifiedOnce(Workspace):
    """#136: a continued import's parent archive is verified once per file state."""

    def test_second_scan_does_not_recompute_the_parent_revision(self):
        engine = self.engine()
        item = engine.handoff(self.read("codex"), "claude")
        with open(item["target"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "uuid": "next", "parentUuid": json.loads(
                Path(item["target"]).read_text(encoding="utf-8").splitlines()[-1])["uuid"], "cwd": str(self.project),
                "timestamp": "2026-10-15T12:00:00Z", "message": {"role": "user", "content": "A continued turn."}}) + "\n")
        session = self.adapters["claude"].read(SessionRef(item["native_id"], item["target"]))
        parent_key = self.read("codex").key
        with counted_revisions() as calls:
            merged = engine.lineage(session)
            self.assertEqual(calls.count(parent_key), 1)
            again = self.engine()
            self.assertEqual(again.lineage(session).events, merged.events)
            self.assertEqual(calls.count(parent_key), 1)
        self.assertEqual(len(merged.events), len(self.read("codex").events) + 1)


class RevisionMemo(Workspace):
    """#137: an unchanged session's revision is not recomputed on the next sync."""

    def test_idle_sync_computes_no_revision(self):
        self.engine().sync(days=None)
        with counted_revisions() as calls:
            self.engine().sync(days=None)
        self.assertEqual(calls, [])

    def test_a_changed_session_gets_a_new_revision(self):
        engine = self.engine()
        engine.sync(days=None)
        before = engine.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.append(self.paths["codex"], {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "A new reply."}]}})
        self.engine().sync(days=None)
        self.assertGreater(engine.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0], before)


class IncomingReusesCompleteHandoff(Workspace):
    """#138: a new session reuses the complete context handoff instead of rendering it again."""

    def test_second_injection_renders_nothing(self):
        engine = self.engine()
        first = engine.incoming("muse", str(self.project))
        with mock.patch.object(engine, "handoff", side_effect=AssertionError("rendered again")):
            second = engine.incoming("muse", str(self.project))
        self.assertEqual(second["id"], first["id"])


class TestIsolation(unittest.TestCase):
    """#139: the suite never uses the real state directory or configuration."""

    def test_state_and_config_point_at_a_temporary_directory(self):
        root = os.environ["AGENT_SESSION_SYNC_TEST_ISOLATION"]
        self.assertTrue(root.startswith(tempfile.gettempdir()))
        self.assertTrue(os.environ["AGENT_SESSION_SYNC_STATE_DIR"].startswith(root))
        self.assertTrue(os.environ["XDG_CONFIG_HOME"].startswith(root))


class ForgetDeletedTranscripts(Workspace):
    """#140: prune drops index rows of transcripts that no longer exist; imports stay."""

    def test_rows_of_a_deleted_transcript_are_dropped(self):
        engine = self.engine()
        engine.scan(days=None)
        imported = engine.store.remember(Session("external", "kept", str(self.project), [Event("a", "user", "Hi.")]), imported=True)
        key = self.read("copilot").key
        os.remove(str(self.paths["copilot"]))
        dry = prune(engine.store, self.state, 7, dry_run=True)
        self.assertEqual(dry["index_rows_forgotten"], 1)
        self.assertIsNotNone(engine.store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone())
        self.assertEqual(prune(engine.store, self.state, 7)["index_rows_forgotten"], 1)
        for table in ("sessions", "session_workspaces"):
            self.assertIsNone(engine.store.db.execute("SELECT 1 FROM {} WHERE key=?".format(table), (key,)).fetchone())
        self.assertIsNotNone(engine.store.db.execute("SELECT 1 FROM sessions WHERE key=?", (imported.key,)).fetchone())


if __name__ == "__main__":
    unittest.main()
