"""Regressions for the findings of a live-data scan of a real install (#123-#134)."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import base64
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync.adapters import SessionRef
from agent_session_sync.blobs import MIN_BLOB_CHARS, archive_bytes, references, references_from_file
from agent_session_sync.io import SyncError, json_bytes, loads, read_records, resolved_workspace
from agent_session_sync.model import MARKER, Event, Session
from agent_session_sync import prune as prune_module
from agent_session_sync.prune import live_transfers, native_from, prune
from agent_session_sync.render import compact
from agent_session_sync.store import Busy, add_pending, has_pending, single_flight, take_pending


def recent(minutes=10):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def settle(*paths, hours=2):
    stamp = time.time() - hours * 3600
    for path in paths:
        os.utime(str(path), (stamp, stamp))


class AppendTolerantReads(Workspace):
    """#127: a transcript that grows while it is read is still read."""

    def read_while(self, path, change):
        real = Path.stat
        done = []

        def stat(self, *args, **kwargs):
            if not done and Path(self) == path:
                done.append(True)
                change()
            return real(self, *args, **kwargs)
        with mock.patch.object(Path, "stat", stat):
            return read_records(path)

    def test_append_during_read_keeps_the_snapshot(self):
        path = self.paths["codex"]
        before = read_records(path)[0]
        records, issues, _ = self.read_while(path, lambda: self.append(path, {"type": "event_msg", "payload": {}}))
        self.assertEqual(records, before)
        self.assertEqual(issues, [])
        self.assertEqual(len(read_records(path)[0]), len(before) + 1)

    def test_replaced_or_shrunk_transcripts_are_still_refused(self):
        path = self.paths["codex"]
        data = path.read_bytes()

        def replace():
            other = path.with_name("replacement.jsonl")
            other.write_bytes(data + b'{"type": "event_msg"}\n')
            os.replace(str(other), str(path))
        with self.assertRaisesRegex(SyncError, "changed while reading"):
            self.read_while(path, replace)
        with self.assertRaisesRegex(SyncError, "changed while reading"):
            self.read_while(path, lambda: path.write_bytes(data[:len(data) // 2]))

    def test_single_document_exports_must_be_unchanged(self):
        path = self.base / "export.json"
        path.write_text(json.dumps({"events": [{"kind": "record"}]}), encoding="utf-8")
        with self.assertRaisesRegex(SyncError, "changed while reading"):
            self.read_while(path, lambda: path.write_text(json.dumps({"events": [{"kind": "record"}, {}]}), encoding="utf-8"))


class ScopedScanSkipsOtherProjects(Workspace):
    """#128: a transcript that only grew keeps its parsed workspace for scoped scans."""

    def test_grown_transcript_of_another_project_is_not_read(self):
        engine = self.engine()
        engine.scan(days=None)
        self.append(self.paths["codex"], {"type": "event_msg", "payload": {"type": "agent_message", "message": "more"}})
        other = self.base / "other-project"
        other.mkdir()
        adapter = self.adapters["codex"]
        with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
            self.assertEqual(engine.scan(project=str(other), days=None), [])
            self.assertEqual(read.call_count, 0)
            scoped = engine.scan(project=str(self.project), days=None)
            self.assertEqual(read.call_count, 1)
        self.assertIn(self.read("codex").key, [s.key for s in scoped])

    def test_a_shrunk_transcript_is_read_again(self):
        engine = self.engine()
        engine.scan(days=None)
        path = self.paths["codex"]
        path.write_bytes(b"\n".join(path.read_bytes().splitlines()[:-1]) + b"\n")
        other = self.base / "other-project"
        other.mkdir()
        adapter = self.adapters["codex"]
        with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
            engine.scan(project=str(other), days=None)
            self.assertEqual(read.call_count, 1)


class HotSources(Workspace):
    """#129: large sessions still being written are not re-cached or re-archived on every sync."""

    def test_hot_transcript_keeps_only_its_workspace_hint(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.scan(days=None)
        key = self.read("codex").key
        self.assertIsNone(engine.store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone())
        self.assertEqual(engine.store.last_workspace(key)[2], str(self.project))
        engine.scan(days=None)  # no longer hot: cached as usual
        self.assertIsNotNone(engine.store.db.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone())

    def test_snapshots_of_a_hot_source_are_rate_limited_except_for_its_own_hook(self):
        engine = self.engine(config={"auto_handoff_max_age_hours": None})
        key = self.read("codex").key

        def transfers():
            return engine.store.db.execute("SELECT count(*) FROM transfers WHERE source_key=?", (key,)).fetchone()[0]
        with mock.patch.object(engine_module, "HOT_BYTES", 1):
            engine.sync(source="codex", days=None)
            first = transfers()
            self.assertGreater(first, 0)
            self.append(self.paths["codex"], {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Still working."}]}})
            adapter = self.adapters["codex"]
            with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
                engine.sync(source="codex", days=None)
            self.assertEqual(read.call_count, 0, "a throttled source is not even parsed")
            self.assertEqual(transfers(), first)
            engine.sync(source="codex", days=None, fresh=[key])
            self.assertGreater(transfers(), first)
        self.assertIsNone(engine_module.hot_interval({"hot_handoff_minutes": None}))
        self.assertEqual(engine_module.hot_interval({}), 30 * 60)


class LinkedBindings(Workspace):
    """#123: a hook-injected context links its source instead of merging it."""

    def bind(self, engine, session, transfer):
        engine.store.db.execute("INSERT INTO metadata VALUES (?, ?)", ("binding:muse:" + session.native_id, transfer["id"]))
        engine.store.db.commit()

    def test_recorded_injected_context_is_dropped_and_the_title_comes_from_own_turns(self):
        engine = self.engine()
        transfer = engine.handoff(self.read("codex"), "muse", mode="context")
        context = Path(transfer["context"]).read_text(encoding="utf-8")
        session = Session("muse", "fresh", str(self.project), [Event("c", "user", context), Event("u", "user", "My own task.")])
        self.bind(engine, session, transfer)
        result = engine.lineage(session)
        self.assertEqual([e.text for e in result.events], ["My own task."])
        self.assertEqual(result.title, "My own task.")
        self.assertEqual(result.parent, transfer["source_key"] + "@" + transfer["source_revision"])
        self.assertTrue(result.provenance["linked"])
        self.assertTrue(any("linked, not merged" in issue for issue in result.issues))

    def test_explicit_launch_still_continues_the_source(self):
        engine = self.engine()
        parent = self.read("codex")
        transfer = engine.handoff(parent, "muse", mode="context")
        session = Session("muse", "launched", str(self.project), [Event("l", "user", transfer["command"][-1]),
                                                                  Event("a", "assistant", "Continuing.")])
        self.bind(engine, session, transfer)
        result = engine.lineage(session)
        self.assertEqual(len(result.events), len(parent.events) + 1)
        self.assertEqual(result.title, parent.title)
        self.assertTrue(result.parent.startswith(parent.key + "@"))


class InjectionAge(Workspace):
    """#124: only a recently active foreign conversation is injected."""

    def test_default_window_skips_old_conversations_and_injects_recent_ones(self):
        engine = self.engine(config={})
        self.assertIsNone(engine.incoming("muse", str(self.project)))
        path = self.paths["codex"]
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            record["timestamp"] = recent()
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        item = engine.incoming("muse", str(self.project))
        self.assertEqual(item["source_key"], self.read("codex").key)

    def test_window_configuration(self):
        self.assertEqual(engine_module.injection_max_age({}), 2 * 3600)
        self.assertEqual(engine_module.injection_max_age({"auto_handoff_max_age_hours": 0.5}), 1800)
        self.assertIsNone(engine_module.injection_max_age({"auto_handoff_max_age_hours": None}))
        self.assertEqual(engine_module.injection_max_age({"auto_handoff_max_age_hours": "bad"}), 2 * 3600)


class ReadableHandoffTitles(Workspace):
    """#125: the first line of a handoff names the conversation."""

    def test_first_line_is_the_title_and_the_marker_still_identifies_the_transfer(self):
        engine = self.engine()
        source = self.read("muse")
        item = engine.handoff(source, "claude")
        first = json.loads(Path(item["target"]).read_text(encoding="utf-8").splitlines()[0])
        text = first["message"]["content"]
        self.assertEqual(text.splitlines()[0], "Fix the login bug. (from muse)")
        self.assertEqual(text.splitlines()[1], MARKER + item["id"] + "]")
        imported = self.adapters["claude"].read(SessionRef(item["native_id"], item["target"]))
        self.assertEqual(imported.provenance["transfer_id"], item["id"])

    def test_parent_link_is_named_and_long_titles_are_one_bounded_line(self):
        source = self.read("muse")
        source.parent = "codex:default:abc@" + "0" * 64
        source.title = "A title\nwith a line break " + "x" * 400
        body, _ = compact(source, self.base / "archive.json", "11111111-1111-4111-8111-111111111111")
        first = body.splitlines()[0]
        self.assertTrue(first.startswith("A title with a line break"))
        self.assertLessEqual(len(first.encode("utf-8")), 160 + len(" (from muse)"))
        self.assertIn("Parent: " + source.parent, body)


class NativeCopyPruning(Workspace):
    """#126: superseded imports nobody resumed are removed; the newest and resumed ones stay."""

    def two_revisions(self, engine, destination):
        first = engine.handoff(self.read("muse"), destination)
        self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1792058599000,
                                         "payload": {"kind": "event", "run_id": "r1",
                                                     "event": {"kind": "assistant_message_committed", "text": "A later reply."}}})
        second = engine.handoff(self.read("muse"), destination)
        self.assertNotEqual(first["target"], second["target"])
        return first, second

    def test_superseded_unmodified_claude_copy_is_removed(self):
        engine = self.engine()
        first, second = self.two_revisions(engine, "claude")
        settle(first["target"], second["target"])
        report = prune(engine.store, self.state, 7)
        self.assertEqual(report["native_copies_removed"], 1)
        self.assertFalse(Path(first["target"]).exists())
        self.assertTrue(Path(second["target"]).exists())
        engine.sync(source="muse", destination="claude", days=None)
        self.assertFalse(Path(first["target"]).exists(), "a superseded copy must not be restored")

    def test_resumed_and_settling_copies_stay(self):
        engine = self.engine()
        first, second = self.two_revisions(engine, "claude")
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)  # settling
        with open(first["target"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": {"content": "resumed"}}) + "\n")
        settle(first["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)  # resumed
        self.assertTrue(Path(first["target"]).exists())

    def test_codex_copies_are_removed_only_through_codex(self):
        engine = self.engine()
        first, second = self.two_revisions(engine, "codex")
        settle(first["target"], second["target"])
        self.assertEqual(prune(engine.store, self.state, 7)["native_copies_removed"], 0)  # no deleter
        refused = mock.Mock(return_value=False)
        self.assertEqual(prune(engine.store, self.state, 7, delete=refused)["native_copies_removed"], 0)
        refused.assert_called_once_with("codex", first["native_id"], Path(first["target"]))
        self.assertTrue(Path(first["target"]).exists())
        deleted = mock.Mock(side_effect=lambda destination, native_id, path: os.remove(str(path)) or True)
        self.assertEqual(prune(engine.store, self.state, 7, delete=deleted)["native_copies_removed"], 1)
        self.assertTrue(Path(second["target"]).exists())

    def test_codex_deleter_runs_codex_delete_in_the_destination_home(self):
        deleter = cli.codex_delete(self.adapters)
        native_id = "01a1de7d-cade-79fc-92c2-f7edb633664a"
        home = self.adapters["codex"].home
        rollout = home / "sessions/2026/11/20" / ("rollout-2026-11-20T10-00-00-" + native_id + ".jsonl")
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text("{}\n", encoding="utf-8")
        with mock.patch.object(cli.subprocess, "run") as run:
            self.assertFalse(deleter("codex", native_id, rollout))  # no Codex index to check: never
        run.assert_not_called()
        with contextlib.closing(sqlite3.connect(str(home / "state_5.sqlite"))) as state:
            state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            state.execute("INSERT INTO threads VALUES (?, ?, 0)", (native_id, str(rollout)))
            state.commit()
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(deleter("codex", native_id, rollout))
        command, options = run.call_args[0][0], run.call_args[1]
        self.assertEqual(command[1:], ["delete", "--force", native_id])
        self.assertEqual(options["env"]["CODEX_HOME"], str(home.resolve()))
        self.assertFalse(deleter("claude", "x", rollout))

    def test_configuration(self):
        self.assertEqual(native_from({}), ("claude", "codex"))
        self.assertEqual(native_from({"prune_native_copies": False}), ())
        self.assertEqual(native_from({"prune_native_copies": ["claude", "codex"]}), ("claude", "codex"))


class PruneReadsLittle(Workspace):
    """#132: prune reads lineage summaries and archive heads, not whole documents."""

    def test_live_transfers_uses_summaries_and_backfills_old_rows_once(self):
        engine = self.engine()
        engine.scan(days=None)
        with mock.patch.object(prune_module, "load_document", wraps=prune_module.load_document) as load:
            live_transfers(engine.store, self.state)
            self.assertEqual(load.call_count, 0)
            engine.store.db.execute("UPDATE sessions SET lineage=NULL")
            engine.store.db.commit()
            live_transfers(engine.store, self.state)
            rows = engine.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
            self.assertEqual(load.call_count, rows)
            live_transfers(engine.store, self.state)
            self.assertEqual(load.call_count, rows)

    def test_continued_import_is_still_kept(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "claude")
        continued = Session("claude", item["native_id"], str(self.project),
                            [Event(str(i), "user", "turn {}".format(i)) for i in range(4)],
                            provenance={"transfer_id": item["id"], "baseline_count": 2})
        engine.store.remember(continued, None)
        keep, _, _ = live_transfers(engine.store, self.state)
        self.assertIn(item["id"], keep)

    def test_head_references_match_full_parse(self):
        image = "data:image/png;base64," + base64.b64encode(os.urandom(MIN_BLOB_CHARS)).decode("ascii")
        session = self.read("muse")
        session.events.append(Event("img", "attachment", "shot", data={"image_url": image, "ünïcode": [image]}))
        data, blobs = archive_bytes(session)
        path = self.base / "archives" / "conversation" / "rev.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        expected = references(loads(data))
        self.assertTrue(expected)
        self.assertEqual(references_from_file(path), expected)
        self.assertEqual(references_from_file(path, chunk=64), expected)
        plain = path.with_name("plain.json")
        plain.write_bytes(json_bytes(self.read("muse").to_dict()))
        self.assertEqual(references_from_file(plain), set())
        cut = path.with_name("cut.json")
        cut.write_bytes(data[:data.index(b'"events"') - 40])
        with self.assertRaises(ValueError):
            references_from_file(cut, chunk=64)


class HookProject(Workspace):
    """#131: a hook syncs its session's recorded workspace, not the current directory."""

    def test_stop_hook_from_a_subdirectory_syncs_the_session_workspace(self):
        engine = self.engine()
        engine.scan(days=None)
        sub = self.project / "web"
        sub.mkdir()
        payload = {"session_id": IDS["claude"], "cwd": str(sub), "transcript_path": str(self.paths["claude"])}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch.object(engine, "sync") as sync:
            self.assertEqual(cli.hook(engine, Namespace(harness="claude", event="stop")), {})
        sync.assert_called_once_with(project=str(self.project), fresh=[self.read("claude").key])
        self.assertEqual(engine.store.workspace("claude", IDS["claude"]), str(self.project))

    def test_unindexed_session_uses_the_reported_directory(self):
        engine = self.engine()
        payload = {"session_id": "brand-new", "cwd": str(self.project)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch.object(engine, "sync") as sync:
            cli.hook(engine, Namespace(harness="claude", event="stop"))
        sync.assert_called_once_with(project=resolved_workspace(str(self.project)), fresh=["claude:default:brand-new"])
        self.assertEqual(engine.store.workspace("claude", "brand-new"), resolved_workspace(str(self.project)))


class DetachedHookSync(Workspace):
    """#130: hooks answer at once and sync in a detached process; busy syncs are queued."""

    def setUp(self):
        super().setUp()
        self.config = self.base / "config.json"
        self.config.write_bytes(json_bytes({"harnesses": {i.harness: {"home": i.home, "executable": sys.executable}
                                                          for i in self.installations}}))

    def cli(self, *argv, payload=None):
        stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
        # Discovery is stubbed: its version probes would also go through subprocess.Popen.
        with mock.patch("sys.stdin", stdin), mock.patch.object(cli, "discover", return_value=self.installations), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            status = cli.main(["--state-dir", str(self.state), "--config", str(self.config)] + list(argv))
        return status, output.getvalue()

    def test_stop_hook_answers_without_syncing_and_spawns_a_detached_sync(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with mock.patch.object(cli.subprocess, "Popen") as popen, mock.patch.object(engine_module.Engine, "sync") as sync:
            status, output = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload)
        self.assertEqual((status, json.loads(output)), (0, {}))
        sync.assert_not_called()
        command, options = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(command[command.index("sync"):], ["sync", "--quiet", "--queued", "--project", resolved_workspace(str(self.project)),
                                                           "--for-hook", "claude", IDS["claude"], "stop"])
        self.assertEqual(command[command.index("--state-dir") + 1], str(self.state))
        self.assertEqual(options["stdout"], cli.subprocess.DEVNULL)
        self.assertEqual(options["cwd"], str(self.state))
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & 0x08000000)  # CREATE_NO_WINDOW
        else:
            self.assertTrue(options["start_new_session"])

    def test_inline_hook_keeps_the_synchronous_path(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            status, _ = self.cli("hook", "--harness", "claude", "--event", "stop", "--inline", payload=payload)
        self.assertEqual(status, 0)
        popen.assert_not_called()
        self.assertTrue((self.state / "handoffs").is_dir())

    def test_busy_hook_hands_its_request_on_and_a_busy_queued_sync_leaves_it_for_the_holder(self):
        payload = {"session_id": IDS["claude"], "cwd": str(self.project)}
        with single_flight(self.state), mock.patch.object(cli, "HOOK_LOCK_WAIT", 0), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            status, output = self.cli("hook", "--harness", "claude", "--event", "stop", payload=payload)
            self.assertEqual((status, json.loads(output)), (0, {}))
            self.assertIn("--for-hook", popen.call_args[0][0])
            with mock.patch.object(cli, "QUEUED_LOCK_WAIT", 0):
                status, _ = self.cli("sync", "--quiet", "--queued", "--project", str(self.project),
                                     "--for-hook", "claude", IDS["claude"], "stop")
            self.assertEqual(status, 0)
            self.assertTrue(has_pending(self.state))
        status, _ = self.cli("sync", "--quiet", "--days", "100000")
        self.assertFalse(has_pending(self.state))
        engine = self.engine()
        self.assertTrue(engine.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0])

    def test_pending_queue_round_trip(self):
        add_pending(self.state, {"project": "/a", "hook": None})
        add_pending(self.state, {"project": "/a", "hook": None})
        add_pending(self.state, {"project": "/b", "hook": ["claude", "s", "stop"]})
        self.assertEqual(take_pending(self.state), [{"project": "/a", "hook": None}, {"project": "/b", "hook": ["claude", "s", "stop"]}])
        self.assertEqual(take_pending(self.state), [])
        self.assertFalse(has_pending(self.state))

    def test_lock_wait_and_busy(self):
        with single_flight(self.state):
            started = time.monotonic()
            with self.assertRaises(Busy):
                with single_flight(self.state, wait=0.3):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, 0.3)


if __name__ == "__main__":
    unittest.main()
