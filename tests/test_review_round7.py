"""Regressions for the seventh adversarial review round."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
import contextlib
import json
import os
import shutil
import sqlite3
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from test_sync import IDS, Workspace
from test_review_round import CliWorkspace, settle
from test_review_round2 import compress
from test_review_round6 import grow_codex, strip_provenance
from agent_session_sync import cli
from agent_session_sync import engine as engine_module
from agent_session_sync.discovery import validate_config
from agent_session_sync.io import SyncError, read_records
from agent_session_sync.prune import unchanged
from agent_session_sync.render import DEFAULT_BUDGET, hook_output_limit, injection_budget


class StaleMuseBindings(CliWorkspace):
    def setUp(self):
        super().setUp()
        self.write_config({"auto_handoff_max_age_hours": None})
        grow_codex(self)

    def bind_like_an_earlier_release(self, sid):
        engine = self.engine()
        try:
            item = engine.incoming("muse", str(self.project), budget=DEFAULT_BUDGET)  # 32 KB: Muse dropped it
            engine.store.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("binding:muse:" + sid, item["id"]))
            engine.store.db.commit()
            return item
        finally:
            engine.close()

    def test_lineage_does_not_link_a_session_to_a_context_it_never_received(self):
        self.bind_like_an_earlier_release(IDS["muse"])
        engine = self.engine()
        try:
            session = engine.lineage(self.read("muse"))
        finally:
            engine.close()
        self.assertFalse(session.parent)
        self.assertFalse(session.provenance.get("linked"))

    def test_a_context_that_fit_stays_linked(self):
        engine = self.engine()
        try:
            item = engine.incoming("muse", str(self.project))  # sized to fit
            engine.store.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ("binding:muse:" + IDS["muse"], item["id"]))
            engine.store.db.commit()
            session = engine.lineage(self.read("muse"))
        finally:
            engine.close()
        self.assertTrue(session.provenance.get("linked"))

    def test_a_binding_the_harness_could_not_have_received_is_ignored(self):
        sid = "01a1ffff-0000-7000-8000-000000000001"
        self.bind_like_an_earlier_release(sid)
        engine = self.engine()
        try:
            transfer = engine.store.transfer(engine.store.db.execute(
                "SELECT value FROM metadata WHERE key=?", ("binding:muse:" + sid,)).fetchone()[0])
            self.assertFalse(engine.delivered("muse", transfer))
            self.assertTrue(engine.delivered("agy", transfer))  # no ceiling there
        finally:
            engine.close()

    def test_the_hook_and_sync_render_the_same_muse_transfer(self):
        engine = self.engine()
        try:
            item = engine.incoming("muse", str(self.project))
            engine.sync(source="codex", destination="muse", days=None)
            ids = [row[0] for row in engine.store.db.execute(
                "SELECT id FROM transfers WHERE source_key=? AND destination='muse'", (item["source_key"],))]
            self.assertEqual(ids, [item["id"]])
        finally:
            engine.close()


class HookOutputLimitOption(Workspace):
    def test_validation_and_fallback(self):
        for bad in (16000.0, "16000", True, 0, 512):
            with self.subTest(value=bad):
                with self.assertRaises(SyncError):
                    validate_config({"harnesses": {"muse": {"hook_output_limit": bad}}})
        validate_config({"harnesses": {"muse": {"hook_output_limit": None}}})
        adapter = self.adapters["muse"]
        with mock.patch.object(adapter, "options", {"hook_output_limit": "16000"}):
            self.assertEqual(hook_output_limit(adapter), 16384)  # malformed: the built-in ceiling stays
        with mock.patch.object(adapter, "options", {"hook_output_limit": None}):
            self.assertIsNone(hook_output_limit(adapter))
            self.assertEqual(injection_budget(adapter), DEFAULT_BUDGET)


class PurgeOnBusyPaths(CliWorkspace):
    def test_a_busy_queued_sync_after_a_purge_queues_nothing(self):
        with mock.patch.object(cli, "run_locked", side_effect=cli.Busy()), \
                mock.patch.object(cli, "config_removed", return_value=True), \
                mock.patch.object(cli, "add_pending") as add:
            status, _, _ = self.cli("sync", "--quiet", "--queued", "--project", str(self.project))
        self.assertEqual(status, 0)
        add.assert_not_called()

    def test_the_drain_after_release_stops_after_a_purge(self):
        cli.add_pending(self.state, {"project": str(self.project), "hook": None})
        with mock.patch.object(cli, "config_removed", return_value=True), mock.patch.object(cli, "Engine") as engine:
            cli.drain_after_release(Namespace(config=None), self.adapters, {}, self.state)
        engine.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no named pipes")
    def test_a_named_pipe_configuration_is_not_taken_for_removed(self):
        fifo = self.base / "config.fifo"
        os.mkfifo(str(fifo))
        self.assertFalse(cli.config_removed(Namespace(config=str(fifo))))


class PruneReadsNoGrownThread(Workspace):
    def test_a_continued_rollout_far_larger_than_the_import_is_not_parsed(self):
        engine = self.engine()
        item = engine.handoff(self.read("muse"), "codex")
        self.assertEqual(item["target_size"], Path(item["target"]).stat().st_size)
        with open(item["target"], "ab") as stream:
            stream.write(b"\n" * (5 * item["target_size"] + (1 << 20)))
        with mock.patch("agent_session_sync.adapters.Codex.parse", side_effect=AssertionError("parsed")):
            self.assertFalse(unchanged(Path(item["target"]), item))


class ForksAndSideChats(Workspace):
    native_id = "01a1de7d-cade-79fc-92c2-f7edb633664a"

    def setUp(self):
        super().setUp()
        self.home = self.adapters["codex"].home
        self.rollout = self.home / "sessions/2026/11/20" / ("rollout-2026-11-20T10-00-00-" + self.native_id + ".jsonl")
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.native_id}}) + "\n", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(str(self.home / "state_5.sqlite"))) as state:
            state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER, attached_thread_parent_id TEXT)")
            state.execute("INSERT INTO threads VALUES (?, ?, 0, NULL)", (self.native_id, str(self.rollout)))
            state.commit()

    def used(self):
        return cli.codex_thread_used(self.home.resolve(), self.native_id, self.rollout.resolve())

    def test_a_side_chat_keeps_its_parent(self):
        self.assertFalse(self.used())
        with contextlib.closing(sqlite3.connect(str(self.home / "state_5.sqlite"))) as state:
            state.execute("INSERT INTO threads VALUES ('side', '/x', 0, ?)", (self.native_id,))
            state.commit()
        self.assertTrue(self.used())

    def test_a_fork_keeps_its_parent(self):
        fork_id = "01a1de7d-cade-79fc-92c2-000000000002"
        fork = self.rollout.with_name("rollout-2026-11-20T11-00-00-" + fork_id + ".jsonl")
        fork.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": fork_id, "history_base": {"thread_id": self.native_id}}}) + "\n", encoding="utf-8")
        self.assertTrue(self.used())


class TornRecoveryChargesRealWork(Workspace):
    def test_a_wide_cut_record_still_yields_its_glued_successor(self):
        lines = self.paths["codex"].read_bytes().rstrip(b"\n").split(b"\n")
        wide = b'{"type": "x", "items": [' + b", ".join(b'{"k": %d}' % i for i in range(3000)) + b'], "t": "cut,'
        lines.insert(2, wide + b'{"type": "event_msg", "payload": {"c": 1}}')
        self.paths["codex"].write_bytes(b"\n".join(lines) + b"\n")
        records, issues, _ = read_records(self.paths["codex"])
        self.assertIn({"type": "event_msg", "payload": {"c": 1}}, records)


class MigratedWriteBacks(Workspace):
    def test_an_archived_import_whose_write_back_codex_migrated_is_not_imported_again(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "RENDER_VERSION", 3):
            older = engine.handoff(self.read("muse"), "codex")
        archived = self.adapters["codex"].home / "archived_sessions" / Path(older["target"]).name
        archived.parent.mkdir()
        shutil.copy2(older["target"], str(archived))
        strip_provenance(older["target"])  # the written-back plain copy, migrated by Codex
        before = set((self.adapters["codex"].home / "sessions").rglob("rollout-*"))
        engine.sync(source="muse", destination="codex", days=None)
        self.assertEqual(set((self.adapters["codex"].home / "sessions").rglob("rollout-*")), before)


if __name__ == "__main__":
    unittest.main()
