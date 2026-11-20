"""Local index and durable transfer intents. Archives remain portable JSON."""
import itertools
import json
import os
import sqlite3
import stat as stat_module
import tempfile
import time
import zlib
from contextlib import contextmanager
from pathlib import Path

from .io import SyncError, atomic_write, loads, normalized_workspace, zstd_content_size
from .model import Session, imported_snapshot

APPLICATION_ID = 0x41535359
BACKUP_BUSY_TIMEOUT = 5.0
# Index version 3 stores large session documents zlib-compressed. Older builds refuse it
# instead of misreading those rows.
INDEX_VERSION = 3
COMPRESS_OVER = 4096
COMPRESSED = b"z1:"


def encode_document(text):
    """A sessions.document value: plain JSON text, or z1:-prefixed zlib when larger than COMPRESS_OVER."""
    data = text.encode("utf-8")
    if len(data) <= COMPRESS_OVER:
        return text
    return sqlite3.Binary(COMPRESSED + zlib.compress(data, 6))


def decode_document(value):
    """JSON text or bytes for a sessions.document value of either encoding (raises ValueError when corrupt)."""
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes) and value.startswith(COMPRESSED):
        try:
            return zlib.decompress(value[len(COMPRESSED):])
        except zlib.error as exc:
            raise ValueError("corrupt compressed session document: {}".format(exc))
    return value


def load_document(value):
    """The parsed session document stored in a sessions row."""
    return loads(decode_document(value))


def lineage_summary(provenance, event_count):
    """The fields retention needs from a session row (its sessions.lineage column), as JSON."""
    value = {"events": event_count}
    if isinstance(provenance, dict):
        if isinstance(provenance.get("transfer_id"), str):
            value["transfer_id"] = provenance["transfer_id"]
        if type(provenance.get("baseline_count")) is int:
            value["baseline_count"] = provenance["baseline_count"]
    return json.dumps(value, sort_keys=True)


def plain_path(path):
    """A Codex rollout's path without the .zst suffix Codex adds when it compresses it."""
    return path[:-4] if isinstance(path, str) and path.endswith(".jsonl.zst") else path


class Busy(SyncError):
    """Another process holds the sync lock."""


@contextmanager
def single_flight(directory, dry_run=False, wait=0):
    """The state directory's sync mutex, waiting up to `wait` seconds for it."""
    if dry_run:
        yield
        return
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stream = (directory / "lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            def attempt():
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            def attempt():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + wait
        while True:
            try:
                attempt()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy("another sync is already running")
                time.sleep(0.1)
        yield
    finally:
        stream.close()


PENDING = "pending"
_sequence = itertools.count()


def add_pending(directory, request):
    """Leave a sync request for the process holding the lock.

    Each request is its own atomically created file, so concurrent requesters never
    interleave and nothing renames a file another process may still hold open.
    """
    folder = Path(directory) / PENDING
    folder.mkdir(parents=True, exist_ok=True)
    name = "{:020d}-{}-{}.json".format(time.time_ns(), os.getpid(), next(_sequence))
    # A dot-prefixed temporary file (ignored by readers) renamed into place: a reader never
    # sees a partial request, even where hard links are unavailable. The name is unique.
    descriptor, temporary = tempfile.mkstemp(prefix=".request-", dir=str(folder))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(folder / name))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# Requests this process took but could not remove (a lock or scanner holds the file): run
# once, then ignored here, so one stuck file cannot make every drain repeat it.
_stuck = set()


def _pending_files(directory):
    try:
        return sorted(p for p in (Path(directory) / PENDING).iterdir()
                      if p.suffix == ".json" and not p.name.startswith(".") and str(p) not in _stuck)
    except OSError:
        return []


def take_pending(directory):
    """Claim every queued sync request, oldest first and without repeats. Only the lock holder calls this.

    A request that cannot be read yet stays for the next holder; a corrupt one is dropped.
    """
    requests = []
    for path in _pending_files(directory):
        try:
            value = json.loads(path.read_bytes())
        except FileNotFoundError:
            continue
        except ValueError:
            value = None
        except OSError:
            _stuck.add(str(path))  # unreadable here (a directory, another user's file): set aside
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _stuck.add(str(path))
            if value is None:
                continue
        if isinstance(value, dict) and value not in requests:
            requests.append(value)
    return requests


def has_pending(directory):
    return bool(_pending_files(directory))


def has_takeable(directory):
    """Whether some queued request can be read: worth starting a sync for. An entry that cannot
    (a directory, another user's file) is set aside for this process. Entries are only stat'ed:
    an open handle outside the lock would make the holder's unlink fail on Windows."""
    for path in _pending_files(directory):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            _stuck.add(str(path))
            continue
        if stat_module.S_ISREG(st.st_mode) and os.access(str(path), os.R_OK):
            return True
        _stuck.add(str(path))
    return False


class Store:
    def __init__(self, directory, dry_run=False):
        self.directory = Path(directory)
        self.dry_run = dry_run
        path = self.directory / "sessions.sqlite3"
        if dry_run:
            self.db = sqlite3.connect(":memory:")
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            self.db = sqlite3.connect(str(path))
        try:
            if dry_run and path.exists():
                self.copy_snapshot(path)
            self.db.row_factory = sqlite3.Row
            self.initialize()
            if not dry_run:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except BaseException:
            self.db.close()
            raise

    def copy_snapshot(self, path):
        source = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0)
        busy_since = None
        def progress(status, remaining, total):
            nonlocal busy_since
            if status in (getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)):
                busy_since = time.monotonic() if busy_since is None else busy_since
                if time.monotonic() - busy_since >= BACKUP_BUSY_TIMEOUT:
                    raise SyncError("index snapshot is busy; retry after the active writer completes")
            else:
                busy_since = None
        try:
            source.backup(self.db, pages=128, progress=progress, sleep=0.05)
        finally:
            source.close()

    def initialize(self):
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        if application_id not in (0, APPLICATION_ID):
            raise SyncError("selected SQLite file belongs to another application")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, INDEX_VERSION):
            raise SyncError("unsupported index version: {}".format(version))
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if application_id == 0 and tables:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
            if not {"key", "source_path", "stat", "document"}.issubset(columns):
                raise SyncError("selected SQLite file is not an agent-session-sync index")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY, source_path TEXT, stat TEXT, document TEXT NOT NULL,
                imported INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY, source_key TEXT, destination TEXT, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_targets (
                source_path TEXT, destination TEXT, target TEXT, written_sha TEXT,
                PRIMARY KEY(source_path, destination));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS session_workspaces (
                key TEXT PRIMARY KEY, source_path TEXT, stat TEXT, workspace TEXT);
            CREATE TABLE IF NOT EXISTS revisions (key TEXT PRIMARY KEY, memo TEXT, revision TEXT);
            CREATE TABLE IF NOT EXISTS verified_archives (path TEXT PRIMARY KEY, signature TEXT);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "lineage" not in columns:
            # Filled by write_session; rows from older releases stay NULL until prune backfills them.
            with self.db:
                self.db.execute("ALTER TABLE sessions ADD COLUMN lineage TEXT")
        if "imported" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE sessions ADD COLUMN imported INTEGER NOT NULL DEFAULT 0")
                for row in self.db.execute("SELECT key, document FROM sessions").fetchall():
                    parsed = load_document(row[1])
                    if isinstance(parsed, dict) and isinstance(parsed.get("provenance"), dict) and parsed["provenance"].get("portable_import"):
                        self.db.execute("UPDATE sessions SET imported=1 WHERE key=?", (row[0],))
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS sessions_imported ON sessions(imported) WHERE imported=1;
            CREATE INDEX IF NOT EXISTS transfers_route ON transfers(source_key, destination);
        """)
        self.db.execute("PRAGMA user_version=" + str(INDEX_VERSION))
        self.db.execute("PRAGMA application_id=" + str(APPLICATION_ID))
        self.migrate_legacy()
        self.migrate_import_namespace()

    def migrate_import_namespace(self):
        if self.db.execute("SELECT 1 FROM metadata WHERE key='import_namespace_v1'").fetchone():
            return
        keys = [row[0] for row in self.db.execute("SELECT key FROM sessions WHERE imported=1")]
        with self.db:
            for key in keys:
                row = self.db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
                if row["stat"] != "null":
                    self.db.execute("UPDATE sessions SET imported=0 WHERE key=?", (key,))
                    continue
                try:
                    session = imported_snapshot(Session.from_dict(load_document(row["document"])))
                except (SyncError, ValueError) as exc:
                    raise SyncError("cannot migrate portable import {}: {}".format(key, exc))
                self.write_session(session, None, True)
                if session.key != key:
                    self.db.execute("DELETE FROM sessions WHERE key=?", (key,))
                    self.db.execute("DELETE FROM session_workspaces WHERE key=?", (key,))
            self.db.execute("INSERT INTO metadata VALUES ('import_namespace_v1', '1')")

    def close(self):
        self.db.close()

    def migrate_legacy(self):
        if self.db.execute("SELECT 1 FROM metadata WHERE key='legacy_migrated'").fetchone():
            return
        path = self.directory / "state.json"
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(state, dict):
                raise ValueError("expected object")
            for direction, destination in (("codex_to_claude", "claude"), ("claude_to_codex", "codex")):
                for source, entry in state.get(direction, {}).items():
                    if entry.get("target"):
                        self.db.execute("INSERT OR IGNORE INTO legacy_targets VALUES (?, ?, ?, ?)",
                                        (source, destination, entry["target"], entry.get("written_sha")))
        except (ValueError, AttributeError, TypeError) as exc:
            raise SyncError("legacy registry is invalid; refusing migration: {}".format(exc))
        if not self.dry_run:
            atomic_write(self.directory / "state-v1.json.bak", path.read_bytes())
        self.db.execute("INSERT INTO metadata VALUES ('legacy_migrated', '1')")
        self.db.commit()

    def cached(self, key, path, stat):
        row = self.db.execute("SELECT document FROM sessions WHERE key=? AND source_path=? AND stat=?",
                              (key, path, json.dumps(stat))).fetchone()
        if row:
            try:
                value = load_document(row[0])
                return value if isinstance(value, dict) else None
            except ValueError:
                pass
        return None

    def compressed_twin(self, key, path, stat):
        """The cached parse of the plain rollout Codex compressed into path (same mtime, same
        parser inputs), moved to path; None when there is none."""
        row = self.db.execute("SELECT source_path, stat, document FROM sessions WHERE key=?", (key,)).fetchone()
        if not row or row[0] != plain_path(path):
            return None
        try:
            old = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        if not (isinstance(old, list) and len(old) == 4 and old[0] == stat[0] and old[2] == stat[2] and old[3] == stat[3]):
            return None
        # The same mtime can hide an append within one clock tick: the compressed content must
        # also be exactly as long as the plain file that was parsed.
        if type(old[1]) is not int or zstd_content_size(path) != old[1]:
            return None
        try:
            value = load_document(row[2])
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        value = dict(value, source_path=path)
        self.db.execute("UPDATE sessions SET source_path=?, stat=?, document=? WHERE key=?",
                        (path, json.dumps(stat), encode_document(json.dumps(value)), key))
        self.db.commit()
        return value

    def workspace_hint(self, key, path, stat):
        row = self.db.execute("SELECT workspace FROM session_workspaces WHERE key=? AND source_path=? AND stat=?",
                              (key, path, json.dumps(stat))).fetchone()
        return row[0] if row else None

    def last_workspace(self, key):
        """(source path, stamp, workspace) of the last parse of key, whatever its stamp."""
        row = self.db.execute("SELECT source_path, stat, workspace FROM session_workspaces WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            stamp = json.loads(row[1]) if row[1] else None
        except ValueError:
            return None
        return row[0], stamp, row[2]

    def remember_workspace(self, session, stat):
        self.db.execute("INSERT OR REPLACE INTO session_workspaces VALUES (?, ?, ?, ?)",
                        (session.key, session.source_path, json.dumps(stat), session.workspace))
        if session.workspace:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                            ("workspace:{}:{}".format(session.endpoint, session.native_id), session.workspace))

    def write_session(self, session, stat, imported):
        session.validate()
        self.db.execute("INSERT OR REPLACE INTO sessions (key, source_path, stat, document, imported, lineage) VALUES (?, ?, ?, ?, ?, ?)",
                        (session.key, session.source_path, json.dumps(stat), encode_document(json.dumps(session.document())), int(imported),
                         lineage_summary(session.provenance, len(session.events))))
        self.remember_workspace(session, stat)

    def remember(self, session, stat=None, imported=False):
        if imported:
            session = imported_snapshot(session)
        self.write_session(session, stat, imported)
        self.db.commit()
        return session

    def sessions(self, imported_only=False):
        query = "SELECT document FROM sessions" + (" WHERE imported=1" if imported_only else "")
        return [load_document(row[0]) for row in self.db.execute(query)]

    def compact(self):
        """Compress every large session row still stored as text, then VACUUM. Documents are kept verbatim."""
        page = self.db.execute("PRAGMA page_size").fetchone()[0]
        before = self.db.execute("PRAGMA page_count").fetchone()[0] * page
        rewritten = 0
        keys = [row[0] for row in self.db.execute("SELECT key FROM sessions WHERE typeof(document)='text'")]
        with self.db:
            for key in keys:
                # One row at a time: the documents together can be far larger than memory should hold.
                row = self.db.execute("SELECT document FROM sessions WHERE key=?", (key,)).fetchone()
                if row is None or not isinstance(row[0], str):
                    continue
                encoded = encode_document(row[0])
                if not isinstance(encoded, str):
                    self.db.execute("UPDATE sessions SET document=? WHERE key=?", (encoded, key))
                    rewritten += 1
        if not self.dry_run:
            self.db.execute("VACUUM")
        after = self.db.execute("PRAGMA page_count").fetchone()[0] * page
        return {"index_rows_compressed": rewritten, "index_bytes_before": before, "index_bytes_after": after}

    def transfer(self, transfer_id):
        row = self.db.execute("SELECT document FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return loads(row[0]) if row else None

    def previous(self, source_key, destination):
        row = self.db.execute("SELECT document FROM transfers WHERE source_key=? AND destination=? ORDER BY rowid DESC LIMIT 1",
                              (source_key, destination)).fetchone()
        return loads(row[0]) if row else None

    def save_transfer(self, item):
        self.db.execute("INSERT OR REPLACE INTO transfers VALUES (?, ?, ?, ?)",
                        (item["id"], item["source_key"], item["destination"], json.dumps(item)))
        self.db.commit()

    def legacy_target(self, source_path, destination):
        source_path = plain_path(source_path)
        row = self.db.execute("SELECT * FROM legacy_targets WHERE source_path=? AND destination=?", (source_path, destination)).fetchone()
        return dict(row) if row else None

    def legacy_import_target(self, path):
        path = plain_path(path)  # Codex may have compressed a registered target
        row = self.db.execute("SELECT * FROM legacy_targets WHERE target=?", (path,)).fetchone()
        if row:
            return row
        # Migration preserves original registry paths. Compare their canonical
        # identities when a configured data root follows a directory alias.
        if not hasattr(self, "_legacy_target_aliases"):
            self._legacy_target_aliases = {}
            for candidate in self.db.execute("SELECT * FROM legacy_targets"):
                try:
                    key = normalized_workspace(candidate["target"])
                    self._legacy_target_aliases.setdefault(key, []).append(candidate)
                except SyncError:
                    continue
        matches = self._legacy_target_aliases.get(normalized_workspace(path), [])
        return matches[0] if len(matches) == 1 else None

    def notify_workspace(self, harness, session_id, workspace):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                        ("workspace:{}:{}".format(harness, session_id), workspace))
        self.db.commit()

    def workspace(self, harness, session_id):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", ("workspace:{}:{}".format(harness, session_id),)).fetchone()
        return row[0] if row else ""
