"""Local index and durable transfer intents. Archives remain portable JSON."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .io import SyncError, atomic_write, loads, normalized_workspace
from .model import Session, imported_snapshot

APPLICATION_ID = 0x41535359
BACKUP_BUSY_TIMEOUT = 5.0


@contextmanager
def single_flight(directory, dry_run=False):
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
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SyncError("another sync is already running")
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SyncError("another sync is already running")
        yield
    finally:
        stream.close()


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
        if version not in (0, 1, 2):
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
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "imported" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE sessions ADD COLUMN imported INTEGER NOT NULL DEFAULT 0")
                for row in self.db.execute("SELECT key, document FROM sessions").fetchall():
                    document = loads(row[1])
                    if isinstance(document, dict) and isinstance(document.get("provenance"), dict) and document["provenance"].get("portable_import"):
                        self.db.execute("UPDATE sessions SET imported=1 WHERE key=?", (row[0],))
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS sessions_imported ON sessions(imported) WHERE imported=1;
            CREATE INDEX IF NOT EXISTS transfers_route ON transfers(source_key, destination);
            PRAGMA user_version=2;
        """)
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
                    session = imported_snapshot(Session.from_dict(loads(row["document"])))
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
                value = loads(row[0])
                return value if isinstance(value, dict) else None
            except ValueError:
                pass
        return None

    def workspace_hint(self, key, path, stat):
        row = self.db.execute("SELECT workspace FROM session_workspaces WHERE key=? AND source_path=? AND stat=?",
                              (key, path, json.dumps(stat))).fetchone()
        return row[0] if row else None

    def remember_workspace(self, session, stat):
        self.db.execute("INSERT OR REPLACE INTO session_workspaces VALUES (?, ?, ?, ?)",
                        (session.key, session.source_path, json.dumps(stat), session.workspace))
        if session.workspace:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                            ("workspace:{}:{}".format(session.endpoint, session.native_id), session.workspace))

    def write_session(self, session, stat, imported):
        session.validate()
        self.db.execute("INSERT OR REPLACE INTO sessions (key, source_path, stat, document, imported) VALUES (?, ?, ?, ?, ?)",
                        (session.key, session.source_path, json.dumps(stat), json.dumps(session.to_dict()), int(imported)))
        self.remember_workspace(session, stat)

    def remember(self, session, stat=None, imported=False):
        if imported:
            session = imported_snapshot(session)
        self.write_session(session, stat, imported)
        self.db.commit()
        return session

    def sessions(self, imported_only=False):
        query = "SELECT document FROM sessions" + (" WHERE imported=1" if imported_only else "")
        return [loads(row[0]) for row in self.db.execute(query)]

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
        row = self.db.execute("SELECT * FROM legacy_targets WHERE source_path=? AND destination=?", (source_path, destination)).fetchone()
        return dict(row) if row else None

    def legacy_import_target(self, path):
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
