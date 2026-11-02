"""Archive format v2: large inline images are stored once, beside the archives.

A conversation with screenshots repeats the same base64 images in every archive
snapshot, and base64 barely compresses. Format v2 moves each `data:image/...;base64,`
string of MIN_BLOB_CHARS or more into archives/blobs/<sha256>.<ext> (the decoded
image, so it opens as an ordinary file) and puts the blob's path, relative to the
archive file, where the string was:

    {"archive_version": 2,
     "blobs": [{"path": ["events", 3, "data", "output", 1, "image_url"],
                "file": "../blobs/<sha256>.png", "prefix": "data:image/png;base64,",
                "sha256": "<sha256>"}],
     ... the version 1 portable session fields ...}

Only the listed paths are references, so no conversation text is ever mistaken for
one. An archive without such images is written exactly as version 1. Every reader
goes through internalize(), which restores the original strings; a missing or
changed blob is an error, never a silent loss.
"""
import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .io import SyncError, atomic_write, json_bytes, loads

ARCHIVE_VERSION = 2
MIN_BLOB_CHARS = 64 * 1024
BLOB_DIR = "blobs"
REFERENCE = re.compile(r"\.\./" + BLOB_DIR + r"/([a-f0-9]{64})\.([a-z0-9]{1,10})")
PREFIX = re.compile(r"data:image/[A-Za-z0-9.+-]+(?:;[^,;]*)*;base64,")
EXTENSIONS = {"image/jpeg": "jpg", "image/svg+xml": "svg", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico"}
# json_bytes sorts keys, so a v2 archive written here begins with this key.
V2_HEAD = b'"archive_version"'


@dataclass(frozen=True)
class Blob:
    """Content-addressed bytes: an existing file of the same size is the same blob."""
    data: bytes


def write_blob(path, blob):
    path = Path(path)
    try:
        if path.stat().st_size == len(blob.data):
            return
    except OSError:
        pass
    # A blob of the wrong size is garbage under its own content address.
    atomic_write(path, blob.data, replace=True)


def _extension(media):
    if media in EXTENSIONS:
        return EXTENSIONS[media]
    subtype = re.sub(r"[^a-z0-9]", "", media.split("/", 1)[1])[:10]
    return subtype or "img"


def _split(value):
    """(prefix, decoded image) for a canonical base64 image data URL, else None."""
    if len(value) < MIN_BLOB_CHARS or not value[:11].lower() == "data:image/":
        return None
    match = PREFIX.match(value, 0, 512)
    if not match:
        return None
    payload = value[match.end():]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    # Only a payload that re-encodes to the same text can be restored byte for byte.
    if base64.b64encode(raw).decode("ascii") != payload:
        return None
    return match.group(0), raw


def externalize(document):
    """(archive document, {file name: Blob}). Without large images the document is returned as-is."""
    entries, blobs = [], {}

    def walk(value, path):
        if isinstance(value, str):
            split = _split(value)
            if split is None:
                return value
            prefix, raw = split
            sha = hashlib.sha256(raw).hexdigest()
            name = "{}.{}".format(sha, _extension(prefix[5:].split(";", 1)[0].lower()))
            reference = "../{}/{}".format(BLOB_DIR, name)
            blobs[name] = Blob(raw)
            entries.append({"path": list(path), "file": reference, "prefix": prefix, "sha256": sha})
            return reference
        if isinstance(value, list):
            return [walk(item, path + [index]) for index, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: walk(item, path + [key]) for key, item in value.items()}
        return value

    converted = walk(document, [])
    if not entries:
        return document, {}
    if not isinstance(converted, dict) or "archive_version" in converted or "blobs" in converted:
        raise SyncError("archive document already carries archive fields")
    converted["archive_version"] = ARCHIVE_VERSION
    converted["blobs"] = entries
    return converted, blobs


def archive_bytes(session):
    """The archive file for a session and the blobs it references ({file name: Blob})."""
    document, blobs = externalize(session.to_dict())
    return json_bytes(document), blobs


def blob_dir(archive):
    """Where an archive's blobs live: archives/blobs beside archives/<conversation>/."""
    return Path(archive).parent.parent / BLOB_DIR


def references(document):
    """Blob file names a parsed archive document references (none for version 1)."""
    if not isinstance(document, dict) or "archive_version" not in document:
        return set()
    names = set()
    for entry in document.get("blobs") or ():
        match = REFERENCE.fullmatch(entry.get("file", "")) if isinstance(entry, dict) and isinstance(entry.get("file"), str) else None
        if match:
            names.add(match.group(1) + "." + match.group(2))
    return names


def internalize(document, archive):
    """Restore a parsed v2 archive document to the version 1 session it was written from.

    The document is changed in place. Version 1 documents are returned unchanged.
    """
    if not isinstance(document, dict) or "archive_version" not in document:
        return document
    version = document.pop("archive_version")
    if type(version) is not int or version != ARCHIVE_VERSION:
        raise SyncError("unsupported archive version: {!r}".format(version))
    entries = document.pop("blobs", None)
    if not isinstance(entries, list):
        raise SyncError("archive blob list is invalid: {}".format(archive))
    base, cache = Path(archive).parent, {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SyncError("archive blob entry is invalid: {}".format(archive))
        path, reference, prefix, sha = (entry.get(k) for k in ("path", "file", "prefix", "sha256"))
        match = REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if (not match or match.group(1) != sha or not isinstance(prefix, str) or not PREFIX.fullmatch(prefix)
                or not isinstance(path, list) or not path or any(type(p) not in (str, int) for p in path)):
            raise SyncError("archive blob entry is invalid: {}".format(archive))
        parent = document
        try:
            for step in path[:-1]:
                parent = parent[step]
            current = parent[path[-1]]
        except (KeyError, IndexError, TypeError):
            raise SyncError("archive blob reference points nowhere: {}".format(archive))
        if current != reference:
            raise SyncError("archive blob reference does not match its entry: {}".format(archive))
        if reference not in cache:
            location = base / reference
            try:
                raw = location.read_bytes()
            except OSError as exc:
                raise SyncError("archive blob missing: {} ({})".format(os.path.normpath(str(location)), exc))
            if hashlib.sha256(raw).hexdigest() != sha:
                raise SyncError("archive blob is corrupt: {}".format(os.path.normpath(str(location))))
            cache[reference] = base64.b64encode(raw).decode("ascii")
        parent[path[-1]] = prefix + cache[reference]
    return document


def read_archive(path):
    """The version 1 session document stored in an archive file of either format."""
    return internalize(loads(Path(path).read_bytes()), path)


def is_v2(path):
    """Cheap check of an archive written by this module, from its first bytes (OSError if unreadable)."""
    with open(str(path), "rb") as stream:
        return V2_HEAD in stream.read(64)


def compact_archives(directory, dry_run=False):
    """Convert version 1 archives that inline large images to version 2, in place.

    Each conversion is verified to restore the same document before the file is
    replaced, and the file keeps its modification time, which prune relies on.
    """
    archives = Path(directory) / "archives"
    report = {"archives_converted": 0, "archive_bytes_before": 0, "archive_bytes_after": 0, "blobs_written": 0}
    if not archives.is_dir():
        return report
    for conversation in sorted(archives.iterdir()):
        if conversation.name == BLOB_DIR or not conversation.is_dir():
            continue
        for snapshot in sorted(conversation.glob("*.json")):
            if not snapshot.is_file() or is_v2(snapshot):
                continue
            raw = snapshot.read_bytes()
            if b"data:image/" not in raw:
                continue
            try:
                original = loads(raw)
            except ValueError:
                continue
            converted, blobs = externalize(original)
            if not blobs:
                continue
            data = json_bytes(converted)
            folder = blob_dir(snapshot)
            if not dry_run:
                for name, blob in blobs.items():
                    if not (folder / name).is_file():
                        report["blobs_written"] += 1
                    write_blob(folder / name, blob)
                if internalize(loads(data), snapshot) != original:
                    raise SyncError("archive conversion does not restore the original: {}".format(snapshot))
                stat = snapshot.stat()
                atomic_write(snapshot, data, replace=True)
                os.utime(str(snapshot), ns=(stat.st_atime_ns, stat.st_mtime_ns))
            report["archives_converted"] += 1
            report["archive_bytes_before"] += len(raw)
            report["archive_bytes_after"] += len(data)
    return report
