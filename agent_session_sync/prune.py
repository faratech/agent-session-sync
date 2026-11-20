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

Each revision is also imported into native destinations as a separate session, so
superseded imports nobody resumed are removed from Claude and Codex history too
(native_copies): never the newest import of a route, never one whose bytes changed.

Images a version 2 archive stores in archives/blobs are removed once no remaining
archive references them and they are older than SETTLE_SECONDS, so a blob a sync
has just written for an archive it has not written yet is never collected.
"""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from .blobs import BLOB_DIR, references_from_file
from .io import SyncError, zstd_decompress
from .store import lineage_summary, load_document

DEFAULT_RETENTION_DAYS = 3
# Hourly, so an active conversation's copies never pile up for a day between prunes.
PRUNE_INTERVAL_SECONDS = 3600
# Inside the retention window: keep all of the last hour, then one transfer per route per 6 hours.
SETTLE_SECONDS = 3600
THIN_BUCKET_SECONDS = 6 * 3600
LAST_PRUNE_KEY = "last_prune"
# A native target touched after its manifest was written has been continued.
TOUCH_SLACK_SECONDS = 60
# Destinations whose superseded, never-resumed imports are removed. Codex indexes rollouts in
# its own databases, so a Codex copy is removed only through `codex delete` (see prune's
# delete argument), never by unlinking the file.
DEFAULT_NATIVE_HARNESSES = ("claude", "codex")


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
    backfill = []
    for key, summary in store.db.execute("SELECT key, lineage FROM sessions").fetchall():
        try:
            info = json.loads(summary) if summary else None
        except ValueError:
            info = None
        if not isinstance(info, dict):
            # A row written by an older release: read its document once and remember the summary.
            row = store.db.execute("SELECT document FROM sessions WHERE key=?", (key,)).fetchone()
            try:
                session = load_document(row[0]) if row else None
            except (ValueError, TypeError):
                continue
            if not isinstance(session, dict):
                continue
            events = session.get("events")
            summary = lineage_summary(session.get("provenance"), len(events) if isinstance(events, list) else 0)
            backfill.append((summary, key))
            info = json.loads(summary)
        transfer_id = info.get("transfer_id")
        if not isinstance(transfer_id, str) or not transfer_id:
            continue
        if info.get("events", 0) > _baseline(info, transfers.get(transfer_id)):
            keep.add(transfer_id)
    if backfill:
        with store.db:
            store.db.executemany("UPDATE sessions SET lineage=? WHERE key=?", backfill)
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


def native_copies(store, settle, harnesses, cutoff=None, gone=()):
    """Superseded native imports nobody resumed, as (path, stat) pairs.

    Every source revision is imported as a new native session, so a conversation leaves
    one copy per revision in the destination's resume picker. A copy may go when it is not
    the newest import of its route, is named for its recorded native id inside the
    harness's history layout, its bytes are exactly what was written (never resumed or
    continued), and it has settled. The newest import of every route always stays.
    gone: plain files this prune removes first (compressed duplicates), seen as absent.
    """
    newest, items = {}, []
    for rowid, source_key, destination, document in store.db.execute(
            "SELECT rowid, source_key, destination, document FROM transfers"):
        try:
            item = json.loads(document)
        except ValueError:
            continue
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("target", "target_sha", "native_id")):
            continue
        route = (source_key, destination)
        items.append((rowid, route, str(destination).split("@", 1)[0], item))
        # Only a finished import supersedes others: an interrupted newer one may never exist.
        if item.get("state") == "complete" and (route not in newest or rowid > newest[route][0]):
            newest[route] = (rowid, item.get("source_revision"), item.get("renderer_version"))

    def current(rowid, route, item):
        # The newest finished import of a route, anything after it, and every import of the same
        # revision and renderer (an explicit handoff with a note or budget sits beside the
        # automatic one).
        top = newest.get(route)
        if top is None or rowid >= top[0]:
            return True
        same_revision = item.get("source_revision") == top[1]
        if same_revision and item.get("renderer_version") == top[2]:
            return True
        if same_revision or item.get("explicit") or item.get("note"):
            # An older rendering of the same revision (an upgrade), or a handoff the user asked
            # for: kept through the retention window, then one copy per conversation remains.
            written = _mtime(Path(item["context"]).with_name("manifest.json")) if isinstance(item.get("context"), str) else None
            return cutoff is None or (written is not None and written >= cutoff)
        return False
    protected = {os.path.normpath(item["target"]) for rowid, route, _, item in items if current(rowid, route, item)}
    for rowid, route, harness, item in items:
        path = Path(item["target"])
        if current(rowid, route, item) or harness not in harnesses or os.path.normpath(str(path)) in protected:
            continue
        if path.suffix != ".jsonl" or not path.stem.endswith(item["native_id"]):
            continue
        if harness == "claude" and path.parent.parent.name != "projects":
            continue
        if harness == "codex" and (len(path.parts) < 5 or path.parts[-5] != "sessions" or not path.name.startswith("rollout-")):
            continue
        # Codex compresses cold rollouts into a .zst beside the (then removed) plain file.
        compressed = Path(str(path) + ".zst")
        plain = path.is_file() and path not in gone
        if plain and compressed.is_file():
            continue  # twins may differ (a continuation in the .zst); compressed_duplicates goes first
        stored = path if plain else compressed
        try:
            stat = stored.stat()
            if not stored.is_file() or stat.st_mtime >= settle:
                continue
            # Touched after its manifest: continued. Rejected cheaply, without reading a grown file
            # (Codex keeps a rollout's modification time when it compresses it).
            written = _mtime(Path(item["context"]).with_name("manifest.json")) if isinstance(item.get("context"), str) else None
            if written is not None and stat.st_mtime > written + TOUCH_SLACK_SECONDS:
                continue
            if not unchanged(stored, item):
                continue
        except (OSError, SyncError):
            continue
        yield path, stat, harness, route[1], item["native_id"]


def compressed_duplicates(store, settle):
    """Plain Codex imports written again after Codex had compressed or archived them (yields
    the plain paths).

    The plain file holds exactly what sync wrote (its recorded digest), so it is a copy of a
    thread Codex already keeps compressed at the same path, or archived in archived_sessions/;
    that copy, which holds anything the user continued before, always stays."""
    for (document,) in store.db.execute("SELECT document FROM transfers WHERE destination='codex' OR destination LIKE 'codex@%'"):
        try:
            item = json.loads(document)
            path = Path(item["target"])
            twins = [Path(str(path) + ".zst")]
            if len(path.parts) >= 5 and path.parts[-5] == "sessions":
                archived = path.parents[4] / "archived_sessions" / path.name
                twins += [archived, Path(str(archived) + ".zst")]
            if not path.is_file() or not any(twin.is_file() for twin in twins) or path.stat().st_mtime >= settle:
                continue
            if file_digest(path) == item["target_sha"]:
                yield path
        except (ValueError, KeyError, TypeError, OSError, SyncError):
            continue


def unchanged(stored, item):
    """Whether a native import still holds only what sync wrote.

    A plain file must match the recorded digest. A Codex .zst may also differ only in metadata
    a later render changed (the Codex version in session_meta): it is unchanged when it parses
    to exactly the import's two baseline events.
    """
    if not str(stored).endswith(".zst"):
        return file_digest(stored) == item["target_sha"]
    content = zstd_decompress(stored, Path(stored).read_bytes())
    if hashlib.sha256(content).hexdigest() == item["target_sha"]:
        return True
    if not item.get("baseline_sha"):
        return False
    from .adapters import Codex, SessionRef
    from .discovery import Installation
    from .engine import event_fingerprint
    try:
        records = [json.loads(line) for line in content.decode("utf-8").split("\n") if line.strip()]
        session = Codex(Installation("codex", "default", str(Path(stored).parent))).parse(records, SessionRef(item["native_id"], str(stored)))
    except (ValueError, TypeError, AttributeError, KeyError, SyncError):
        return False
    return len(session.events) == item.get("baseline_count", 2) and event_fingerprint(session.events) == item["baseline_sha"]


def content_digest(path):
    """sha256 of a file's content, decompressing a Codex .zst rollout."""
    if str(path).endswith(".zst"):
        return hashlib.sha256(zstd_decompress(path, Path(path).read_bytes())).hexdigest()
    return file_digest(path)


def file_digest(path, chunk=1 << 20):
    value = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            value.update(block)
    return value.hexdigest()


# Harnesses whose index rows name real transcript files (executable and plugin adapters
# may use other identifiers).
FILE_HARNESSES = ("codex", "claude", "muse", "agy", "copilot")


def forget_missing(store, dry_run=False, transfers=None):
    """Drop parse-cache rows of native transcripts that were deleted. They are rebuilt if the
    file reappears. Kept: portable imports, rows that pin a continued import's lineage, rows of
    adapters without files, files Codex compressed, and files whose directory is gone (an
    unmounted or moved home is not a deletion)."""
    gone = []
    for key, source_path, summary in store.db.execute(
            "SELECT key, source_path, lineage FROM sessions WHERE imported=0").fetchall():
        if not source_path or key.split(":", 1)[0] not in FILE_HARNESSES:
            continue
        try:
            info = json.loads(summary) if summary else None
        except ValueError:
            info = None
        if not isinstance(info, dict):
            continue  # not summarized yet
        if info.get("transfer_id") and info.get("events", 0) > _baseline(info, (transfers or {}).get(info["transfer_id"])):
            continue  # pins a continued import's lineage
        path = Path(source_path)
        if path.exists() or Path(source_path + ".zst").exists() or not path.parent.is_dir():
            continue
        gone.append(key)
    if gone and not dry_run:
        with store.db:
            for table in ("sessions", "session_workspaces", "revisions"):
                store.db.executemany("DELETE FROM {} WHERE key=?".format(table), [(key,) for key in gone])
    if not dry_run:
        stale = [(path,) for (path,) in store.db.execute("SELECT path FROM verified_archives").fetchall() if not os.path.exists(path)]
        if stale:
            with store.db:
                store.db.executemany("DELETE FROM verified_archives WHERE path=?", stale)
    return len(gone)


def native_from(config):
    """Destination harnesses whose superseded imports prune removes (config prune_native_copies)."""
    value = (config or {}).get("prune_native_copies", True)
    if value is True:
        return DEFAULT_NATIVE_HARNESSES
    if isinstance(value, list):
        return tuple(name for name in value if isinstance(name, str))
    return ()


def prune(store, directory, retention_days=DEFAULT_RETENTION_DAYS, dry_run=False, now=None, native=None, delete=None):
    """Delete superseded snapshots and handoff folders older than retention_days, and
    superseded native imports in the `native` destination harnesses.

    delete(destination, native_id, path) -> bool removes a Codex import through Codex itself;
    without it Codex copies are left in place.
    """
    if retention_days is None or retention_days < 0:
        raise ValueError("retention_days must be zero or more")
    directory = Path(directory)
    now = time.time() if now is None else now
    cutoff = now - float(retention_days) * 86400
    # live_transfers backfills lineage summaries first; rows that pin lineage are never forgotten.
    keep, transfers, routes = live_transfers(store, directory)
    forgotten = forget_missing(store, dry_run, transfers)
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
    # A handoff the user asked for (and its note), and an earlier rendering of a route's newest
    # revision, stay for the whole retention window (prune's native_copies reads their manifest).
    newest_revision = {}
    for source_key, destination, document in store.db.execute(
            "SELECT source_key, destination, document FROM transfers WHERE rowid IN "
            "(SELECT MAX(rowid) FROM transfers GROUP BY source_key, destination)"):
        try:
            item = json.loads(document)
        except ValueError:
            continue
        if isinstance(item, dict):
            newest_revision[(source_key, destination)] = (item.get("source_revision"), item.get("renderer_version"))

    def rendered_earlier(t, item):
        top = newest_revision.get(routes.get(t))
        return bool(top) and item.get("source_revision") == top[0] and item.get("renderer_version") != top[1]
    recent.update(t for t, item in transfers.items() if item.get("explicit") or item.get("note") or rendered_earlier(t, item)
                  if (_mtime(directory / "handoffs" / t / "manifest.json") or 0) >= cutoff)
    pinned = {os.path.normpath(transfers[t]["archive"]) for t in keep | recent
              if t in transfers and transfers[t].get("archive")}
    report = {"retention_days": retention_days, "dry_run": dry_run, "kept_transfers": len(keep),
              "handoffs_removed": 0, "archives_removed": 0, "blobs_removed": 0, "blob_bytes_freed": 0, "bytes_freed": 0,
              "native_copies_removed": 0, "native_bytes_freed": 0, "index_rows_forgotten": forgotten,
              "compressed_duplicates_removed": 0}

    removed_twins = set()
    if "codex" in (DEFAULT_NATIVE_HARNESSES if native is None else native):
        for path in compressed_duplicates(store, settle):
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    continue
            removed_twins.add(path)
            report["compressed_duplicates_removed"] += 1

    # A dry run sees the twins it would remove as removed, so it reports what a real run does.
    for path, stat, harness, destination, native_id in native_copies(store, settle, DEFAULT_NATIVE_HARNESSES if native is None else native,
                                                                      cutoff, removed_twins if dry_run else ()):
        if harness == "codex" and delete is None:
            continue
        if dry_run and harness == "codex" and not delete(destination, native_id, path, check=True, gone=removed_twins):
            continue
        if not dry_run:
            if harness == "codex":
                # Codex's own deletion also removes the session from its thread index.
                if not delete(destination, native_id, path) or path.exists() or Path(str(path) + ".zst").exists():
                    continue
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # One copy that cannot go (permissions, a lock) must not stop retention.
                    continue
        report["native_copies_removed"] += 1
        report["native_bytes_freed"] += stat.st_size
        report["bytes_freed"] += stat.st_size

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
    removed = set()
    if archives.is_dir():
        for conversation in archives.iterdir():
            if conversation.name == BLOB_DIR or not conversation.is_dir():
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
                removed.add(snapshot)
                if not dry_run:
                    for path in (snapshot, readable):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
        collect_blobs(archives, removed, settle, dry_run, report)
    return report


def collect_blobs(archives, removed, settle, dry_run, report):
    """Remove settled blobs that no remaining archive references."""
    folder = archives / BLOB_DIR
    if not folder.is_dir():
        return
    referenced = set()
    for snapshot in archives.glob("*/*.json"):
        if snapshot.parent.name == BLOB_DIR or snapshot in removed:
            continue
        try:
            referenced |= references_from_file(snapshot)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            # An archive that cannot be read might reference anything; collect nothing.
            return
    for blob in folder.iterdir():
        if blob.name in referenced or blob.name.startswith(".") or not blob.is_file():
            continue
        written = _mtime(blob)
        if written is None or written >= settle:
            continue
        size = _freed(blob)
        report["blobs_removed"] += 1
        report["blob_bytes_freed"] += size
        report["bytes_freed"] += size
        if not dry_run:
            try:
                blob.unlink()
            except FileNotFoundError:
                pass


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
