"""Strict transcript reading and private, atomic artifact writes."""
import hashlib
import json
import os
import re
import tempfile
import sys
import ntpath
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


class SyncError(Exception):
    pass


SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub(value):
    """Replace lone UTF-16 surrogates, which JSON permits but UTF-8 cannot encode."""
    if isinstance(value, str):
        return LONE_SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {scrub(key): scrub(item) for key, item in value.items()}
    return value


def loads(data):
    """json.loads that only pays for surrogate scrubbing when an escape could produce one."""
    if isinstance(data, (bytes, bytearray)):
        # As json.loads does: detect UTF-8/16/32, keeping raw surrogates for scrubbing.
        data = bytes(data).decode(json.detect_encoding(data), "surrogatepass")
    value = json.loads(data)
    return scrub(value) if SURROGATE_ESCAPE.search(data) or LONE_SURROGATE.search(data) else value


def workspace_path(value, windows=None):
    """Convert a file URI to a filesystem spelling without accessing it."""
    if not isinstance(value, str):
        raise SyncError("workspace must be a path string")
    if not value.lower().startswith("file:"):
        return value
    windows = os.name == "nt" if windows is None else windows
    try:
        uri = urlsplit(value)
    except ValueError as exc:
        raise SyncError("invalid file URI: {}".format(exc))
    if uri.query or uri.fragment or uri.username is not None or uri.password is not None:
        raise SyncError("file workspace URI cannot contain credentials, query, or fragment")
    host, path = unquote(uri.netloc), unquote(uri.path)
    if host and host.lower() != "localhost":
        if windows and len(host) == 2 and host[0].isalpha() and host[1] == ":":
            path = host + path
        elif windows:
            if ":" in host or "/" in host or "\\" in host:
                raise SyncError("invalid UNC workspace host")
            path = "//" + host + path
        else:
            raise SyncError("nonlocal file URI requires a mounted local path or --project mapping")
    if windows and len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        path = path[1:]
    if not path:
        raise SyncError("file workspace URI has no path")
    return path


def resolved_workspace(value):
    if not value:
        return ""
    path = workspace_path(value)
    # Resolving a UNC comparison must not probe or authenticate to a server.
    if os.name == "nt" and path.startswith(("\\\\", "//")):
        return ntpath.normpath(path)
    try:
        resolved = str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SyncError("cannot resolve workspace: {}".format(exc))
    try:
        resolved.encode("utf-8")
    except UnicodeEncodeError:
        # POSIX names with undecodable bytes cannot be stored in the UTF-8 portable format.
        raise SyncError("workspace path is not valid UTF-8: {!r}".format(resolved))
    return resolved


def normalized_workspace(value):
    return os.path.normcase(resolved_workspace(value))


def write_output(value, end="\n"):
    """CLI/protocol output is UTF-8 even when a Windows pipe uses an ACP locale."""
    data = value + end
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(data.encode("utf-8"))
        binary.flush()
    else:
        sys.stdout.write(data)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SharedArtifact:
    source: Path
    data: bytes


def atomic_share(source, path, data):
    """Link immutable content, falling back to an exclusively created copy."""
    source, path = Path(source), Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_bytes() == data:
            return
        raise SyncError("refusing to overwrite unfamiliar shared artifact: {}".format(path))
    if source.read_bytes() != data:
        raise SyncError("shared artifact source changed: {}".format(source))
    try:
        os.link(source.resolve(), path)
    except FileExistsError:
        raise SyncError("shared artifact destination appeared during commit: {}".format(path))
    except OSError:
        atomic_write(path, data)


def read_records(path):
    """Read a consistent snapshot; only an invalid, unterminated tail is deferred."""
    path = Path(path)
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SyncError("transcript changed while reading; retry: {}".format(path))
    issues = []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SyncError("invalid UTF-8 in {}: {}".format(path, exc))
    if path.suffix.lower() == ".json":
        try:
            value = loads(text)
        except ValueError as exc:
            raise SyncError("invalid JSON in {}: {}".format(path, exc))
        if isinstance(value, dict) and "export_schema_version" in value and value["export_schema_version"] != 1:
            raise SyncError("unsupported Muse export schema version: {}".format(value["export_schema_version"]))
        if isinstance(value, dict) and "events" in value:
            value = value["events"]
        elif isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
            raise SyncError("expected JSON records in {}".format(path))
        return value, issues, digest(raw)
    records, truncated = [], None
    # JSON permits U+0085/U+2028/U+2029 inside strings; splitlines treats those
    # as separators too. Only LF (with optional CR) delimits JSONL records.
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.strip(" \t\r"):
            continue
        try:
            record = loads(line)
        except ValueError as exc:
            if index == len(lines) - 1 and not line.endswith(("\n", "\r")):
                issues.append("incomplete trailing record deferred")
                break
            record = torn_suffix(line)
            if record is None:
                # Power loss can also leave NUL padding where the rest of the record should be.
                cut_short = exc.msg.startswith("Unterminated string") or exc.pos >= len(line.rstrip(" \t\r\x00"))
                if not cut_short or truncated is not None:
                    raise SyncError("invalid JSON at {}:{}: {}".format(path, index + 1, exc))
                # A crash can cut a record short; it is skipped only once the writer is seen to continue.
                truncated = (index + 1, exc)
                continue
            # Interleaved writers can leave a cut-off record glued to a complete one.
            issues.append("torn record at line {} dropped; its complete successor was kept".format(index + 1))
        if not isinstance(record, dict):
            raise SyncError("expected object at {}:{}".format(path, index + 1))
        if truncated is not None:
            issues.append("record cut short at line {} dropped; later records were kept".format(truncated[0]))
            truncated = None
        records.append(record)
    if truncated is not None:
        raise SyncError("invalid JSON at {}:{}: {}".format(path, truncated[0], truncated[1]))
    return records, issues, digest(raw)


def torn_suffix(line):
    """The complete object ending a line whose start was cut off by another writer."""
    end = len(line.rstrip(" \t\r"))
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{\s*"', line):
        if match.start() == 0:
            continue
        try:
            value, stop = decoder.raw_decode(line, match.start())
        except ValueError:
            continue
        if stop == end and isinstance(value, dict):
            return scrub(value) if SURROGATE_ESCAPE.search(line, match.start()) else value
    return None


def atomic_write(path, data, expected=None, replace=False):
    """Unknown destinations are never replaced. expected is the owned baseline."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = path.read_bytes()
        if old == data:
            return digest(data)
        if not replace and (expected is None or digest(old) != expected):
            raise SyncError("refusing to overwrite unowned or continued file: {}".format(path))
    fd, temporary = tempfile.mkstemp(prefix=".session-sync-", dir=str(path.parent))
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            if not replace and (expected is None or digest(path.read_bytes()) != expected):
                # Identical artifacts can be reused, never adopted as writable ownership.
                if path.read_bytes() == data:
                    return digest(data)
                raise SyncError("destination changed before commit: {}".format(path))
            os.replace(temporary, path)
        else:
            # Exclusive creation prevents replacing a file created by a concurrent harness.
            # link is atomic on supported filesystems; fall back to exclusive open.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise SyncError("destination appeared during commit: {}".format(path))
            except OSError as exc:
                if os.name == "nt":
                    # Windows rename refuses existing destinations, unlike POSIX rename.
                    try:
                        os.rename(temporary, path)
                    except FileExistsError:
                        raise SyncError("destination appeared during commit: {}".format(path))
                else:
                    raise SyncError("filesystem lacks atomic exclusive creation for {}: {}".format(path, exc))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return digest(data)
