"""Strict transcript reading and private, atomic artifact writes."""
import codecs
import hashlib
import json
import os
import re
import shutil
import tempfile
import sys
import ntpath
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


class SyncError(Exception):
    pass


MAX_TRANSCRIPT_BYTES = 256 << 20
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
    try:
        value = json.loads(data)
        return scrub(value) if SURROGATE_ESCAPE.search(data) or LONE_SURROGATE.search(data) else value
    except RecursionError:
        # Pathologically nested input is malformed data, not an interpreter failure.
        raise json.JSONDecodeError("JSON nesting is too deep", data, 0)
    except json.JSONDecodeError:
        raise
    except ValueError as exc:
        # json reports an integer literal over the digit limit as a plain ValueError.
        raise json.JSONDecodeError(str(exc), data, 0)


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
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SyncError("value is not valid Unicode (lone surrogate): {}".format(exc))
    except RecursionError:
        raise SyncError("value is nested too deeply to serialize")


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


def exclusive_copy(path, data):
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        raise SyncError("destination appeared during commit: {}".format(path))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # A partial copy must not be mistaken for a complete artifact.
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise


def read_records(path):
    """Read a consistent snapshot; only an invalid, unterminated tail is deferred.

    JSONL transcripts are append-only, so the bytes up to the size seen at open stay
    valid while a harness keeps writing: they are read from the open handle and kept
    when the same file has not shrunk. A single-document .json must be unchanged.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    with path.open("rb", buffering=0) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_TRANSCRIPT_BYTES:
            raise transcript_too_large(path, MAX_TRANSCRIPT_BYTES)
        if suffix == ".zst":
            declared = frame_content_size(stream.read(18))
            if declared is not None and declared > MAX_TRANSCRIPT_BYTES:
                raise transcript_too_large(path, MAX_TRANSCRIPT_BYTES)
            stream.seek(0)
            raw = read_zstd(path, stream, MAX_TRANSCRIPT_BYTES)
        else:
            raw = stream.read(before.st_size)
    after = path.stat()
    same = (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    if suffix in (".json", ".zst"):
        consistent = same and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    else:
        # Only growth is an append. An unchanged size with a new mtime is an in-place rewrite,
        # and a smaller file was rewritten even when a coarse clock kept its mtime.
        consistent = same and len(raw) == before.st_size and (
            after.st_size > before.st_size or (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns))
    if not consistent:
        raise SyncError("transcript changed while reading; retry: {}".format(path))
    issues = []
    if suffix == ".json":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SyncError("invalid UTF-8 in {}: {}".format(path, exc))
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
    # as separators too. Only LF (with optional CR) delimits JSONL records. Lines are
    # decoded one at a time (LF never occurs inside a multibyte UTF-8 sequence), so a
    # large transcript is not also held as one decoded string and a list of lines.
    start = len(codecs.BOM_UTF8) if raw.startswith(codecs.BOM_UTF8) else 0
    index, last = -1, False
    while not last:
        end = raw.find(b"\n", start)
        last = end < 0
        index += 1
        chunk, bad = (raw[start:] if last else raw[start:end]), None
        try:
            line = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            if last:
                # A record still being written can end inside a multibyte character.
                issues.append("incomplete trailing record deferred")
                break
            # A record cut short or torn inside a multibyte character: keep the bad bytes as lone
            # surrogates (strict decoding never yields them) and let the recovery below decide.
            bad = SyncError("invalid UTF-8 in {}: line {}: {}".format(path, index + 1, exc))
            line = chunk.decode("utf-8", "surrogateescape")
        start = end + 1
        if not line.strip(" \t\r"):
            continue
        try:
            found = [loads(line)]
            if bad is not None:
                raise bad  # invalid bytes inside a record that is otherwise complete
        except ValueError as exc:
            if last and not line.endswith(("\n", "\r")):
                issues.append("incomplete trailing record deferred")
                break
            found = salvage(line, bad, issues, index + 1)
            if found is None and not isinstance(exc, json.JSONDecodeError):
                raise SyncError("invalid JSON at {}:{}: {}".format(path, index + 1, exc))
            if found is None:
                # Power loss can also leave NUL padding where the rest of the record should be.
                cut_short = exc.msg.startswith("Unterminated string") or exc.pos >= len(line.rstrip(" \t\r\x00")) \
                    or cut_in_token(line, exc.pos) or cut_in_escape(line, exc)
                if not cut_short or truncated is not None:
                    raise SyncError("invalid JSON at {}:{}: {}".format(path, index + 1, exc))
                # A crash can cut a record short; it is skipped only once the writer is seen to continue.
                truncated = (index + 1, exc)
                continue
        if any(not isinstance(record, dict) for record in found):
            raise SyncError("expected object at {}:{}".format(path, index + 1))
        if truncated is not None:
            issues.append("record cut short at line {} dropped; later records were kept".format(truncated[0]))
            truncated = None
        records.extend(found)
    if truncated is not None:
        raise SyncError("invalid JSON at {}:{}: {}".format(path, truncated[0], truncated[1]))
    return records, issues, digest(raw)


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def zstd_content_size(path):
    """The decompressed size the first frame header of a .zst file declares, or None."""
    try:
        with open(str(path), "rb") as handle:
            return frame_content_size(handle.read(18))
    except OSError:
        return None


def frame_content_size(head):
    if len(head) < 6 or head[:4] != ZSTD_MAGIC:
        return None
    descriptor = head[4]
    single = (descriptor >> 5) & 1
    position = 5 + (0 if single else 1) + (0, 1, 2, 4)[descriptor & 3]
    size = (1 if single else 0, 2, 4, 8)[descriptor >> 6]
    if not size or len(head) < position + size:
        return None
    value = int.from_bytes(head[position:position + size], "little")
    return value + 256 if size == 2 else value


def find_program(name):
    """A program on the absolute PATH entries, never in the current directory: on Windows,
    shutil.which searches the current directory first, where an untrusted checkout could
    plant the program. PATHEXT is applied explicitly (Python 3.8's which ignores it for
    a name with a directory), and symlinked directories are compared resolved."""
    try:
        cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        cwd = None  # a removed directory cannot hold a planted program
    extensions = [""]
    if os.name == "nt":
        # As shutil.which: the bare name only when it already carries an executable extension.
        pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        extensions = ([""] if any(name.upper().endswith(e.upper()) for e in pathext) else []) + pathext
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        try:
            resolved = os.path.normcase(os.path.realpath(entry))
        except OSError:
            resolved = os.path.normcase(os.path.abspath(entry))  # unreachable (a network share): compare as written
        if cwd is not None and resolved == cwd:
            continue
        for extension in extensions:
            candidate = os.path.join(entry, name + extension)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


_ZSTD = []


def transcript_too_large(path, limit):
    return SyncError("transcript exceeds the {:g} MiB read limit: {}".format(limit / (1 << 20), path))


def read_zstd(path, stream, limit):
    """Decode at most limit bytes, including across frames with no declared size."""
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        try:
            with zstd.ZstdFile(stream) as decoded:
                data = decoded.read(limit + 1)
        except (zstd.ZstdError, EOFError, OSError) as exc:
            raise SyncError("cannot decompress {}: {}".format(path, exc))
        if len(data) > limit:
            raise transcript_too_large(path, limit)
        if stream.tell() == 0:
            raise SyncError("cannot decompress {}: empty file".format(path))
        return data
    try:
        import zstandard
    except ImportError:
        return read_zstd_command(path, stream, limit)

    # stream_reader can accept a truncated final frame. decompressobj exposes EOF
    # but has no output limit. At most 1 KiB of input limits the output of even
    # a run of 4-byte RLE blocks to about 32 MiB per call.
    data = bytearray()
    try:
        decompressor = zstandard.ZstdDecompressor()
        decoder = decompressor.decompressobj()
        if not hasattr(decoder, "eof"):
            # Older releases could only report one frame. Keep that behavior
            # using their bounded streaming interface.
            for chunk in decompressor.read_to_iter(stream):
                if len(data) + len(chunk) > limit:
                    raise transcript_too_large(path, limit)
                data.extend(chunk)
            return data
        for block in iter(lambda: stream.read(1024), b""):
            while block:
                if decoder.eof:
                    decoder = decompressor.decompressobj()
                chunk = decoder.decompress(block)
                if len(data) + len(chunk) > limit:
                    raise transcript_too_large(path, limit)
                data.extend(chunk)
                block = decoder.unused_data
    except zstandard.ZstdError as exc:
        raise SyncError("cannot decompress {}: {}".format(path, exc))
    if not decoder.eof:
        raise SyncError("cannot decompress {}: empty or truncated file".format(path))
    return data


def read_zstd_command(path, stream, limit):
    command = find_program("zstd")
    if command is None:
        raise SyncError("reading compressed {} needs Python 3.14, the zstandard package, or the zstd command".format(path))
    import subprocess
    from threading import Timer

    # Input is already a file. A separate stderr file lets us read bounded stdout
    # without waiting for a feeder thread or filling either process pipe.
    with tempfile.TemporaryFile() as errors:
        try:
            with subprocess.Popen([command, "-dc"], stdin=stream, stdout=subprocess.PIPE, stderr=errors,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
                timer = Timer(300, process.kill)
                timer.start()
                try:
                    data = process.stdout.read(limit + 1)
                    if len(data) > limit:
                        raise transcript_too_large(path, limit)
                    status = process.wait()
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
        except (OSError, subprocess.SubprocessError) as exc:
            raise SyncError("cannot decompress {}: {}".format(path, exc))
        if status:
            errors.seek(0)
            detail = errors.read(4096).decode("utf-8", "replace").strip()
            raise SyncError("cannot decompress {}: {}".format(path, detail or "zstd exited with status " + str(status)))
    return data


def zstd_available():
    """Whether some zstd decoder exists here (checked once per process)."""
    if not _ZSTD:
        try:
            from compression import zstd  # noqa: F401
            _ZSTD.append(True)
        except ImportError:
            try:
                import zstandard  # noqa: F401
                _ZSTD.append(True)
            except ImportError:
                _ZSTD.append(find_program("zstd") is not None)
    return _ZSTD[0]


def zstd_decompress(path, data):
    """The content of a zstd file: Python 3.14's compression.zstd, else zstandard, else the zstd command."""
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        try:
            return zstd.decompress(data)
        except Exception as exc:  # compression.zstd.ZstdError and friends
            raise SyncError("cannot decompress {}: {}".format(path, exc))
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        # Frame by frame, refusing empty or truncated input as compression.zstd and zstd do.
        if not data:
            raise SyncError("cannot decompress {}: empty file".format(path))
        parts = []
        try:
            while data:
                stream = zstandard.ZstdDecompressor().decompressobj()
                parts.append(stream.decompress(data))
                if getattr(stream, "eof", None) is None:
                    # An older zstandard cannot tell where a frame ends; read one frame as before.
                    return parts[0]
                if not stream.eof:
                    raise SyncError("cannot decompress {}: truncated".format(path))
                data = stream.unused_data
        except (zstandard.ZstdError, AttributeError) as exc:
            raise SyncError("cannot decompress {}: {}".format(path, exc))
        return b"".join(parts)
    command = find_program("zstd")
    if command is None:
        raise SyncError("reading compressed {} needs Python 3.14, the zstandard package, or the zstd command".format(path))
    import subprocess
    try:
        result = subprocess.run([command, "-dc"], input=data, capture_output=True, timeout=300,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SyncError("cannot decompress {}: {}".format(path, exc))
    if result.returncode:
        raise SyncError("cannot decompress {}: {}".format(path, result.stderr.decode("utf-8", "replace").strip()))
    return result.stdout


def appended_matches(path, start, pattern, chunk=1 << 20):
    """Whether the bytes of path after offset start match a compiled bytes pattern."""
    with open(str(path), "rb") as stream:
        stream.seek(start)
        carry = b""
        while True:
            block = stream.read(chunk)
            if not block:
                return False
            data = carry + block
            if pattern.search(data):
                return True
            carry = data[-256:]


# What a record cut inside its last token ends with: part of true/false/null, a number, or a
# \\u escape (the line is also cut short then, not malformed).
PARTIAL_TOKEN = re.compile(r'(?:t(?:ru?)?|f(?:a(?:ls?)?)?|n(?:ul?)?|-?\d*(?:\.\d*)?(?:[eE][-+]?\d*)?|"(?:[^"\\]|\\.)*\\u[0-9a-fA-F]{0,3})')


def cut_in_escape(line, exc):
    """Whether a JSON error is a record cut inside an escape (whatever the string held before):
    in a \\uXXXX escape, or right after its backslash with NUL padding after it."""
    tail = line.rstrip(" \t\r\x00")[exc.pos:]
    if exc.msg.startswith("Invalid \\escape"):
        return tail == "\\"
    return exc.msg.startswith("Invalid \\uXXXX escape") and re.fullmatch(r"\\?u[0-9a-fA-F]{0,3}", tail) is not None


def cut_in_token(line, position):
    """Whether a JSON error at position is a record cut inside its final token."""
    text = line.rstrip(" \t\r\x00")
    start = max(text.rfind(c, 0, max(position, 1)) for c in ':,[') + 1
    tail = text[start:].lstrip()
    return bool(tail) and position >= start and PARTIAL_TOKEN.fullmatch(tail) is not None


# Bytes that were not UTF-8, as surrogateescape keeps them; strict decoding never yields these.
ESCAPED_BYTE = re.compile("[\udc80-\udcff]")


def torn_suffix(line, after=1):
    """(object, its offset) for the complete object ending a line whose start was cut off by
    another writer, unscrubbed; None when there is none."""
    end = len(line.rstrip(" \t\r"))
    decoder = json.JSONDecoder()
    absorbed = None  # whether the cut record parses to the end of the line (computed once, if needed)
    # The decoding work actually done is bounded: a deeply nested cut record, whose every candidate
    # parses to the end of the line, is left to the cut-short / invalid-JSON handling instead of
    # quadratic work; many small nested objects cost only their own length.
    examined, bound = 0, 32 * len(line) + 4096
    for match in re.finditer(r'\{\s*"', line):
        if match.start() < after:
            continue
        before = match.start() - 1
        while before >= 0 and line[before] in " \t\r\x00":
            before -= 1
        if before >= 0 and line[before] in ":,[":
            # After ':' ',' or '[' an object may be a value inside the cut record: it is one only
            # when the cut record is a valid JSON prefix up to the end of the line (a cut inside a
            # string, or after an object's comma, cannot end in a value).
            if absorbed is None:
                first = line.find("{", max(after - 1, 0))
                try:
                    decoder.raw_decode(line, first if first >= 0 else 0)
                    absorbed = False
                except ValueError as exc:
                    absorbed = getattr(exc, "pos", -1) >= end
                except RecursionError:
                    absorbed = True  # too deep to judge: as before, its nested objects are not records
            if absorbed:
                continue
        if examined > bound:
            return None
        try:
            value, stop = decoder.raw_decode(line, match.start())
        except ValueError as exc:
            # An unterminated string (or a plain ValueError) was scanned to the end of the line.
            partial = isinstance(exc, json.JSONDecodeError) and not exc.msg.startswith("Unterminated string")
            examined += (max(exc.pos, match.start() + 1) if partial else end) - match.start()
            continue
        except RecursionError:
            examined += end - match.start()
            continue
        examined += stop - match.start()
        if stop == end and isinstance(value, dict):
            return value, match.start()
    return None


def salvage(line, bad, issues, number):
    """Records recoverable from a line that is not one JSON object, or None.

    A complete record followed by NUL padding (a crash before the writer continued) is kept,
    as is a complete record glued to the next one; otherwise the complete successor of a
    record cut off by an interleaved writer is kept. A kept record holding bytes that were
    not UTF-8 makes the line invalid (raises bad); this is checked before any scrubbing.
    """
    kept, following = [], 1
    try:
        value, stop = json.JSONDecoder().raw_decode(line, 0)
    except (ValueError, RecursionError):
        value, stop = None, 0
    if isinstance(value, dict) and stop < len(line):
        following = stop + len(line[stop:]) - len(line[stop:].lstrip(" \t\r\x00"))
        if following == len(line):
            kept.append((value, 0, stop))
            issues.append("NUL padding after the record at line {} ignored".format(number))
        else:
            successor = torn_suffix(line, following)
            if successor is not None and successor[1] == following:
                kept = [(value, 0, stop), (successor[0], following, len(line))]
                issues.append("two records joined on line {} were split".format(number))
    if not kept:
        # After a complete leading record, look for a successor only past it.
        successor = torn_suffix(line, following)
        if successor is None:
            return None
        if isinstance(value, dict) and stop <= successor[1]:
            kept.append((value, 0, stop))  # a complete leading record stays
        kept.append((successor[0], successor[1], len(line)))
        # Interleaved writers can leave a cut-off record glued to a complete one.
        issues.append("torn record at line {} dropped; its complete successor was kept".format(number))
    if bad is not None and any(ESCAPED_BYTE.search(line, start, stop) for _, start, stop in kept):
        raise bad
    try:
        return [scrub(value) if SURROGATE_ESCAPE.search(line, start, stop) else value for value, start, stop in kept]
    except RecursionError:
        return None  # nested too deeply to be a record; refused like any invalid line


def atomic_write(path, data, expected=None, replace=False):
    """Unknown destinations are never replaced. expected is the owned baseline.

    A symlinked destination is written through, so the link and its target stay in place.
    """
    path = Path(path)
    if path.is_symlink():
        path = Path(os.path.realpath(str(path)))
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
                    # No hard links here (FAT, exFAT, some FUSE and network mounts). An exclusively
                    # created copy still never replaces a file a concurrent harness created.
                    exclusive_copy(path, data)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return digest(data)
