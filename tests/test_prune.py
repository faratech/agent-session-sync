import contextlib
import io
import os
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from test_sync import Workspace
from agent_session_sync import cli
from agent_session_sync.adapters import SessionRef
from agent_session_sync.io import read_records
from agent_session_sync import engine as engine_module
from agent_session_sync.prune import (DEFAULT_RETENTION_DAYS, PRUNE_INTERVAL_SECONDS, SETTLE_SECONDS,
                                       THIN_BUCKET_SECONDS, due, mark_pruned, prune, retention_from)

DAY = 86400


def age(paths, days):
    stamp = time.time() - days * DAY
    for path in paths:
        path = Path(path)
        if path.exists():
            os.utime(path, (stamp, stamp))


def age_transfer(state, item, days):
    stamp_transfer(item, time.time() - days * DAY)


def stamp_transfer(item, stamp):
    folder = Path(item["context"]).parent
    archive = Path(item["archive"])
    targets = [item["target"]] if item.get("target") else []
    for path in [p for p in folder.rglob("*")] + [folder, archive, archive.with_suffix(".md")] + targets:
        if Path(path).exists():
            os.utime(path, (stamp, stamp))


class PruneTests(Workspace):
    def grow(self, name, text):
        self.append(self.paths[name], {"timestamp": "2026-10-15T12:00:00Z", "type": "response_item",
                                       "payload": {"type": "message", "role": "assistant",
                                                   "content": [{"type": "output_text", "text": text}]}})
        return self.read(name)

    def two_revisions(self, engine):
        first = engine.handoff(self.read("codex"), "claude")
        second = engine.handoff(self.grow("codex", "A later turn."), "claude")
        self.assertNotEqual(first["source_revision"], second["source_revision"])
        return first, second

    def test_superseded_snapshot_and_handoff_are_removed_after_retention(self):
        engine = self.engine()
        first, second = self.two_revisions(engine)
        engine.sync(destination="claude")   # every other route exists before the prune
        everything = list(engine.store.db.execute("SELECT document FROM transfers"))
        for (document,) in everything:
            age_transfer(self.state, __import__("json").loads(document), 30)
        latest = [r[0] for r in engine.store.db.execute(
            "SELECT id FROM transfers WHERE rowid IN (SELECT MAX(rowid) FROM transfers GROUP BY source_key, destination)")]
        self.assertGreater(len(latest), 1, "several routes, so most latest transfers are not the inbox pointer")
        report = prune(engine.store, self.state, 7)
        for transfer_id in latest:
            self.assertTrue(engine.complete(engine.store.transfer(transfer_id)), "every route's newest transfer stays complete")
        self.assertFalse(Path(first["context"]).parent.exists())
        self.assertFalse(Path(first["archive"]).exists())
        self.assertFalse(Path(first["archive"]).with_suffix(".md").exists())
        self.assertTrue(Path(second["archive"]).is_file())
        self.assertTrue(engine.complete(engine.store.transfer(second["id"])), "the newest transfer per route stays complete")
        self.assertEqual((report["handoffs_removed"], report["archives_removed"]), (1, 1))
        self.assertGreater(report["bytes_freed"], 0)
        files = {str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file() and p.suffix != ".sqlite3"}
        self.assertEqual(engine.sync(destination="claude"), [], "nothing is re-rendered after a prune")
        self.assertEqual({str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file() and p.suffix != ".sqlite3"}, files)

    def test_nothing_inside_the_retention_window_is_removed(self):
        engine = self.engine()
        first, _ = self.two_revisions(engine)
        report = prune(engine.store, self.state, 7)
        self.assertEqual((report["handoffs_removed"], report["archives_removed"]), (0, 0))
        self.assertTrue(Path(first["archive"]).is_file())

    def test_a_continued_import_keeps_its_parent_archive(self):
        engine = self.engine()
        first, second = self.two_revisions(engine)
        target = Path(first["target"])
        records, _, _ = read_records(target)
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                             "cwd": str(self.project), "timestamp": "2026-10-15T11:00:00Z",
                             "message": {"role": "user", "content": "Now add regression tests."}})
        age_transfer(self.state, first, 30)
        age_transfer(self.state, second, 30)
        os.utime(target, None)   # continued just now, after its manifest was written
        prune(engine.store, self.state, 7)
        self.assertTrue(Path(first["archive"]).is_file(), "the continuation's parent archive is pinned")
        continued = engine.lineage(self.adapters["claude"].read(SessionRef(first["native_id"], str(target))))
        self.assertEqual(len(continued.events), len(self.read("codex").events), "the lineage still rebuilds the full parent")

    def test_a_continuation_recorded_in_the_ledger_is_kept_after_its_file_ages(self):
        engine = self.engine()
        first, second = self.two_revisions(engine)
        target = Path(first["target"])
        records, _, _ = read_records(target)
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                             "cwd": str(self.project), "timestamp": "2026-10-15T11:00:00Z",
                             "message": {"role": "user", "content": "Now add regression tests."}})
        engine.scan(days=None, force=True)   # the ledger records the continued session
        age_transfer(self.state, first, 30)
        age_transfer(self.state, second, 30)
        age([target], 30)
        prune(engine.store, self.state, 7)
        self.assertTrue(Path(first["archive"]).is_file())

    def test_the_newest_snapshot_of_a_conversation_is_never_removed(self):
        engine = self.engine()
        folder = self.state / "archives" / "orphaned"
        folder.mkdir(parents=True)
        older, newer = folder / ("a" * 64 + ".json"), folder / ("b" * 64 + ".json")
        for path in (older, newer, older.with_suffix(".md"), newer.with_suffix(".md")):
            path.write_text("{}", encoding="utf-8")
        age([older, older.with_suffix(".md")], 40)
        age([newer, newer.with_suffix(".md")], 30)
        prune(engine.store, self.state, 7)
        self.assertFalse(older.exists())
        self.assertTrue(newer.is_file() and newer.with_suffix(".md").is_file())

    def revisions(self, engine, count):
        items = [engine.handoff(self.read("codex"), "claude")]
        for n in range(count - 1):
            items.append(engine.handoff(self.grow("codex", "Turn {}.".format(n)), "claude"))
        self.assertEqual(len({i["source_revision"] for i in items}), count)
        return items

    def gone(self, item):
        return not Path(item["context"]).parent.exists() and not Path(item["archive"]).exists()

    def test_an_active_conversation_is_thinned_inside_the_retention_window(self):
        # A long session is re-snapshotted every few minutes; keeping every copy for the whole
        # window let one conversation fill a disk. Past the settle hour, one per route per bucket stays.
        engine = self.engine()
        items = self.revisions(engine, 4)
        base = (int(time.time()) // THIN_BUCKET_SECONDS - 8) * THIN_BUCKET_SECONDS + 100
        for offset, item in zip((0, 60, 120, 3 * 3600), items):
            stamp_transfer(item, base + offset)
        report = prune(engine.store, self.state, 7, now=base + 2 * DAY)
        self.assertTrue(all(self.gone(i) for i in items[:3]), "older copies in the same bucket are removed")
        self.assertTrue(Path(items[3]["archive"]).is_file())
        self.assertTrue(engine.complete(engine.store.transfer(items[3]["id"])), "the newest transfer stays complete")
        self.assertEqual(report["archives_removed"], 3)

    def test_each_bucket_keeps_its_newest_copy(self):
        engine = self.engine()
        items = self.revisions(engine, 3)
        base = (int(time.time()) // THIN_BUCKET_SECONDS - 8) * THIN_BUCKET_SECONDS + 100
        for offset, item in zip((0, THIN_BUCKET_SECONDS, THIN_BUCKET_SECONDS + 60), items):
            stamp_transfer(item, base + offset)
        prune(engine.store, self.state, 7, now=base + DAY)
        self.assertFalse(self.gone(items[0]), "the only copy in the earlier bucket stays")
        self.assertTrue(self.gone(items[1]))
        self.assertFalse(self.gone(items[2]))

    def test_the_settle_hour_is_never_thinned(self):
        engine = self.engine()
        items = self.revisions(engine, 3)
        now = time.time()
        for offset, item in zip((300, 200, 100), items):
            stamp_transfer(item, now - offset)
        report = prune(engine.store, self.state, 7, now=now)
        self.assertLess(300, SETTLE_SECONDS)
        self.assertEqual((report["handoffs_removed"], report["archives_removed"]), (0, 0))

    def test_automatic_prune_is_hourly(self):
        self.assertLessEqual(PRUNE_INTERVAL_SECONDS, 3600)
        store = self.engine().store
        mark_pruned(store, now=time.time() - PRUNE_INTERVAL_SECONDS - 1)
        self.assertTrue(due(store))

    def test_sync_writes_nothing_when_the_disk_is_nearly_full(self):
        engine = self.engine()
        with mock.patch.object(engine_module, "free_bytes", return_value=10 ** 9):
            self.assertEqual(engine.sync(destination="claude"), [])
        self.assertTrue(any("low disk space" in e for e in engine.errors))
        self.assertFalse(any((self.state / "handoffs").glob("*")) if (self.state / "handoffs").exists() else False)
        engine.config["min_free_gb"] = 0
        with mock.patch.object(engine_module, "free_bytes", return_value=0):
            self.assertTrue(engine.sync(destination="claude"), "min_free_gb: 0 disables the guard")
        self.assertEqual(engine_module.min_free_bytes({}), 5 * 10 ** 9)
        self.assertEqual(engine_module.min_free_bytes({"min_free_gb": 12}), 12 * 10 ** 9)

    def test_dry_run_reports_and_removes_nothing(self):
        engine = self.engine()
        first, second = self.two_revisions(engine)
        age_transfer(self.state, first, 30)
        report = prune(engine.store, self.state, 7, dry_run=True)
        self.assertEqual((report["handoffs_removed"], report["archives_removed"]), (1, 1))
        self.assertTrue(Path(first["archive"]).is_file() and Path(first["context"]).is_file())

    def test_negative_retention_is_refused(self):
        with self.assertRaises(ValueError):
            prune(self.engine().store, self.state, -1)

    def test_retention_config(self):
        self.assertEqual(retention_from({}), DEFAULT_RETENTION_DAYS)
        self.assertEqual(retention_from({"retention_days": 14}), 14)
        for disabled in (0, None, False, -3):
            self.assertIsNone(retention_from({"retention_days": disabled}))

    def test_automatic_prune_runs_at_most_daily_and_never_fails_the_sync(self):
        engine = self.engine()
        args = types.SimpleNamespace(dry_run=False, verbose=False, json=False)
        calls = []
        with mock.patch.object(cli, "prune", side_effect=lambda *a, **k: calls.append(a) or
                               {"handoffs_removed": 0, "archives_removed": 0, "bytes_freed": 0}):
            cli.auto_prune(engine, self.state, {}, args)
            cli.auto_prune(engine, self.state, {}, args)
        self.assertEqual(len(calls), 1, "a second run inside the interval does not prune again")
        self.assertFalse(due(engine.store))
        mark_pruned(engine.store, now=time.time() - 2 * DAY)
        stderr = io.StringIO()
        with mock.patch.object(cli, "prune", side_effect=OSError("disk went away")), contextlib.redirect_stderr(stderr):
            cli.auto_prune(engine, self.state, {}, args)
        self.assertIn("prune skipped", stderr.getvalue())
        with mock.patch.object(cli, "prune", side_effect=AssertionError("disabled")):
            cli.auto_prune(engine, self.state, {"retention_days": 0}, args)
            cli.auto_prune(engine, self.state, {}, types.SimpleNamespace(dry_run=True, verbose=False, json=False))

    def test_prune_is_a_command(self):
        self.assertEqual(cli.legacy_arguments(["prune", "--keep-days", "3"])[0], "prune")
        args = cli.parser().parse_args(["prune", "--keep-days", "3"])
        self.assertEqual((args.command, args.keep_days), ("prune", 3.0))


if __name__ == "__main__":
    unittest.main()
