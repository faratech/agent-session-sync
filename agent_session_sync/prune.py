"""Retention for the state directory: superseded archive snapshots and handoff folders.

Every time a conversation changes, sync writes a complete new archive snapshot
(archives/<key>/<revision>.json and .md) and one handoff folder per destination
(handoffs/<transfer>/). Nothing supersedes the old ones, so an active
conversation leaves hundreds of full copies behind. Prune removes the ones
nothing can use any more, and keeps:

- the newest transfer of every route (source conversation, destination): the
  next sync checks it with Engine.complete(), so deleting it would re-render;
- every transfer a conversation was continued from: Engine.lineage() rebuilds a
  continuation from the parent archive its transfer pins;
- every transfer an inbox pointer names;
- the newest snapshot of every conversation, so each conversation keeps one
  portable archive;
- inside the retention window, everything written in the last SETTLE_SECONDS
  and, before that, the newest transfer of each route per THIN_BUCKET_SECONDS.
  An active conversation is re-snapshotted every few minutes, and keeping every
  copy for days let a single long session fill a disk; the thinned copies still
  leave a recent ancestor for an older import someone continues later.

Transfer rows stay in the ledger (they are small); only files are removed.
"""
import json
import os
import shutil
import time
from pathlib import Path

DEFAULT_RETENTION_DAYS = 3
# Hourly, so an active conversation's copies never pile up for a day between prunes.
PRUNE_INTERVAL_SECONDS = 3600
# Inside the retention window: keep all of the last hour, then one transfer per route per 6 hours.
SETTLE_SECONDS = 3600
THIN_BUCKET_SECONDS = 6 * 3600
LAST_PRUNE_KEY = "last_prune"
# A native target touched after its manifest was written has been continued.
TOUCH_SLACK_SECONDS = 60


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _freed(path):
    """Bytes released by unlinking path: zero while another hard link remains."""
    try:
        stat = Path(path).stat()
    except OSError:
        return 0
    return stat.st_size if stat.st_nlink <= 1 else 0


def _baseline(provenance, transfer):
    value = provenance.get("baseline_count")
    if isinstance(value, int):
        return value
    transfer = transfer or {}
    value = transfer.get("baseline_count")
    if isinstance(value, int):
        return value
    return 2 if transfer.get("mode") == "native" else 1


def live_transfers(store, directory):
    """Transfer ids whose artifacts must stay, and the parsed ledger."""
    directory = Path(directory)
    transfers, latest, routes = {}, {}, {}
    for rowid, transfer_id, source_key, destination, document in store.db.execute(
            "SELECT rowid, id, source_key, destination, document FROM transfers"):
        try:
            item = json.loads(document)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        transfers[transfer_id] = item
        route = (source_key, destination)
        routes[transfer_id] = route
        if route not in latest or rowid > latest[route][0]:
            latest[route] = (rowid, transfer_id)
    keep = {transfer_id for _, transfer_id in latest.values()}
    for (document,) in store.db.execute("SELECT document FROM sessions"):
        try:
            session = json.loads(document)
        except ValueError:
            continue
        provenance = session.get("provenance") if isinstance(session, dict) else None
        if not isinstance(provenance, dict) or not provenance.get("transfer_id"):
            continue
        transfer_id = provenance["transfer_id"]
        if len(session.get("events") or []) > _baseline(provenance, transfers.get(transfer_id)):
            keep.add(transfer_id)
    for transfer_id, item in transfers.items():
        target = item.get("target")
        if not target or transfer_id in keep:
            continue
        written = _mtime(Path(item.get("context", "")).with_name("manifest.json")) if item.get("context") else None
        touched = _mtime(target)
        if touched is not None and (written is None or touched > written + TOUCH_SLACK_SECONDS):
            keep.add(transfer_id)
    inbox = directory / "inbox"
    if inbox.is_dir():
        for pointer in inbox.glob("*/*/latest.json"):
            try:
                value = json.loads(pointer.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict) and value.get("id"):
                keep.add(value["id"])
    return keep, transfers, routes


def prune(store, directory, retention_days=DEFAULT_RETENTION_DAYS, dry_run=False, now=None):
    """Delete superseded snapshots and handoff folders older than retention_days."""
    if retention_days is None or retention_days < 0:
        raise ValueError("retention_days must be zero or more")
    directory = Path(directory)
    now = time.time() if now is None else now
    cutoff = now - float(retention_days) * 86400
    keep, transfers, routes = live_transfers(store, directory)
    settle = now - SETTLE_SECONDS
    # A transfer written inside the window may still be continued, so it and its parent archive stay,
    # thinned to the newest per route per bucket once it is past the settle period.
    recent, newest_in_bucket = set(), {}
    for t in transfers:
        written = _mtime(directory / "handoffs" / t / "manifest.json")
        if written is None or written < cutoff:
            continue
        if written >= settle:
            recent.add(t)
            continue
        bucket = (routes.get(t), int(written // THIN_BUCKET_SECONDS))
        if bucket not in newest_in_bucket or written > newest_in_bucket[bucket][0]:
            newest_in_bucket[bucket] = (written, t)
    recent.update(t for _, t in newest_in_bucket.values())
    pinned = {os.path.normpath(transfers[t]["archive"]) for t in keep | recent
              if t in transfers and transfers[t].get("archive")}
    report = {"retention_days": retention_days, "dry_run": dry_run, "kept_transfers": len(keep),
              "handoffs_removed": 0, "archives_removed": 0, "bytes_freed": 0}

    handoffs = directory / "handoffs"
    if handoffs.is_dir():
        for folder in handoffs.iterdir():
            if not folder.is_dir() or folder.name in keep or folder.name in recent:
                continue
            written = _mtime(folder / "manifest.json") or _mtime(folder)
            if written is None or written >= settle:
                continue
            report["bytes_freed"] += sum(_freed(p) for p in folder.rglob("*") if p.is_file())
            report["handoffs_removed"] += 1
            if not dry_run:
                shutil.rmtree(folder, ignore_errors=True)

    archives = directory / "archives"
    if archives.is_dir():
        for conversation in archives.iterdir():
            if not conversation.is_dir():
                continue
            snapshots = [p for p in conversation.glob("*.json") if p.is_file()]
            if not snapshots:
                continue
            newest = max(snapshots, key=lambda p: (_mtime(p) or 0, p.name))
            for snapshot in snapshots:
                if snapshot == newest or os.path.normpath(str(snapshot)) in pinned:
                    continue
                written = _mtime(snapshot)
                if written is None or written >= settle:
                    continue
                readable = snapshot.with_suffix(".md")
                report["bytes_freed"] += _freed(snapshot) + _freed(readable)
                report["archives_removed"] += 1
                if not dry_run:
                    for path in (snapshot, readable):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
    return report


def due(store, now=None, interval=PRUNE_INTERVAL_SECONDS):
    row = store.db.execute("SELECT value FROM metadata WHERE key=?", (LAST_PRUNE_KEY,)).fetchone()
    try:
        last = float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        last = 0.0
    return (time.time() if now is None else now) - last >= interval


def mark_pruned(store, now=None):
    store.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (LAST_PRUNE_KEY, str(time.time() if now is None else now)))
    store.db.commit()


def retention_from(config):
    """Days to keep superseded data; None disables automatic pruning."""
    value = (config or {}).get("retention_days", DEFAULT_RETENTION_DAYS)
    if value is None or value is False:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else None
