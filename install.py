#!/usr/bin/env python3
"""agent-session-sync installer: install, or update an existing install in place.

    curl -fsSL https://raw.githubusercontent.com/faratech/agent-session-sync/main/install.py | python3 -
    python3 install.py [--ref TAG|BRANCH|SHA] [--source CHECKOUT] [--dry-run]

Only Python 3.8+ is required. The installer

1. fetches the source pinned to one commit (or uses --source),
2. installs it into a stable managed location -- or, when agent-session-sync
   is already a uv or pipx tool, upgrades that tool instead,
3. detects the automation already in place (v1 or v2 cron lines, scheduled
   tasks, harness hooks, the memory companion), and
4. re-points exactly that automation at the new code, keeping its state
   directory and captured configuration.

Machines that only use the CLI get new code and no new automation.
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "faratech/agent-session-sync"
API = "https://api.github.com/repos/" + REPO
TARBALL = "https://codeload.github.com/" + REPO + "/tar.gz/"
SHA = re.compile(r"^[0-9a-f]{40}$")
WINDOWS = os.name == "nt"


def say(message):
    print(message, flush=True)


def fetch(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": "agent-session-sync-installer"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        declared = response.headers.get("Content-Length")
    if declared is not None and len(data) != int(declared):
        raise OSError("short read from {}: {} of {} bytes".format(url, len(data), declared))
    return data


def resolve(ref):
    """Branch or tag -> commit; raw branch URLs can be served minutes stale."""
    if SHA.match(ref):
        return ref
    sha = json.loads(fetch(API + "/commits/" + ref, timeout=20).decode("utf-8")).get("sha", "")
    if not SHA.match(sha):
        raise OSError("cannot resolve {} to a commit".format(ref))
    return sha


def extract(data, destination):
    """Unpack a GitHub tarball, refusing links and paths outside the tree."""
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise OSError("empty source archive")
        root = members[0].name.split("/", 1)[0]
        for member in members:
            parts = member.name.split("/")
            if parts[0] != root or ".." in parts or member.name.startswith("/") or not (member.isfile() or member.isdir()):
                if member.issym() or member.islnk():
                    continue  # the project ships no links
                raise OSError("unsafe path in source archive: " + member.name)
            relative = "/".join(parts[1:])
            if not relative:
                continue
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, open(str(target), "wb") as stream:
                    shutil.copyfileobj(source, stream)


def stage_from(source, destination):
    """Copy only what the installed tool needs from a checkout."""
    source, destination = Path(source), Path(destination)
    shutil.copytree(str(source / "agent_session_sync"), str(destination / "agent_session_sync"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("agent-session-sync.py", "memory-sync.py", "install.py", "pyproject.toml", "README.md", "LICENSE"):
        if (source / name).is_file():
            shutil.copy2(str(source / name), str(destination / name))


def vet(tree):
    """Refuse a tree that is not a complete, compiling copy of this project."""
    tree = Path(tree)
    for required in ("agent_session_sync/__init__.py", "agent_session_sync/cli.py", "agent-session-sync.py", "memory-sync.py"):
        if not (tree / required).is_file():
            raise OSError("source is missing " + required)
    for path in list(tree.glob("agent_session_sync/*.py")) + [tree / "agent-session-sync.py", tree / "memory-sync.py"]:
        compile(path.read_bytes(), str(path), "exec")
    match = re.search(r'__version__ = "([^"]+)"', (tree / "agent_session_sync/__init__.py").read_text(encoding="utf-8"))
    if not match:
        raise OSError("source does not declare a version")
    return match.group(1)


def data_home():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")) / "agent-session-sync"
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")) / "agent-session-sync"


def bin_dir(prefix):
    return prefix / "bin" if WINDOWS else Path.home() / ".local/bin"


def tool_manager():
    """'uv' or 'pipx' when agent-session-sync is already installed as that tool."""
    for name, listing in (("uv", ["uv", "tool", "list"]), ("pipx", ["pipx", "list", "--short"])):
        if shutil.which(name):
            try:
                out = subprocess.run(listing, capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            if re.search(r"^agent-session-sync\b", out, re.M):
                return name
    return None


def detect(tree, state_dir=None):
    """Automation currently installed, judged by the new code's ownership rules."""
    sys.path.insert(0, str(tree))
    try:
        for name in [m for m in sys.modules if m == "agent_session_sync" or m.startswith("agent_session_sync.")]:
            del sys.modules[name]
        from agent_session_sync import automation, discovery, legacy, ownership
    finally:
        sys.path.pop(0)
    directory = Path(state_dir).expanduser() if state_dir else discovery.state_dir()
    found = {"scheduler": False, "hooks": False, "memories": False, "state_dir": directory,
             "captured": directory / "automation-config.json", "checkouts": set()}

    def note(command):
        owner = ownership.command_owner(command)
        if owner is None:
            return None
        # A command running a script inside a git checkout of this repo is a developer install.
        for token in (command if isinstance(command, list) else _split(command)):
            if token.endswith(("agent-session-sync.py", "memory-sync.py")):
                checkout = _checkout(Path(token).parent)
                if checkout:
                    found["checkouts"].add(str(checkout))
        return owner

    if WINDOWS:
        for spec, key in ((legacy.session_spec(), "scheduler"), (legacy.memory_spec(), "memories")):
            found[key] = found[key] or legacy._task_exists(spec)
    elif shutil.which("crontab"):
        for line in legacy._crontab_read():
            owner = ownership.cron_owner(line)
            if owner and not line.strip().startswith("#"):
                fields = line.split(None, 5)
                note(fields[5] if len(fields) == 6 else line)
                found["scheduler" if owner == ownership.SESSION else "memories"] = True
    settings = {Path(discovery.default_homes()["claude"]) / "settings.json"}
    try:
        settings.update(Path(a.options["settings"]) for a in automation.recorded_adapters(directory))
    except Exception:  # an unreadable record must not block an update
        pass
    agy = Path.home() / ".gemini/config/hooks.json"
    copilot = Path(discovery.default_homes()["copilot"]) / "hooks/agent-session-sync.json"
    muse = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "muse/settings.json"
    settings.update((agy, copilot, muse))
    for path in settings:
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for command in _commands(document):
            owner = note(command)
            if owner == ownership.SESSION:
                found["hooks"] = True
            elif owner == ownership.MEMORY:
                found["memories"] = True
    return found


def _split(command):
    try:
        import shlex
        return shlex.split(command, posix=not WINDOWS)
    except ValueError:
        return command.split()


def _commands(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("command", "bash", "powershell") and isinstance(item, (str, list)):
                yield item
            else:
                yield from _commands(item)
    elif isinstance(value, list):
        for item in value:
            yield from _commands(item)


def _checkout(path):
    """The root of a git checkout of this repo containing path, read without git."""
    path = Path(path).resolve()
    for candidate in [path] + list(path.parents):
        dot = candidate / ".git"
        if not dot.exists():
            continue
        gitdir = dot
        if dot.is_file():  # worktree pointer; its config lives in the common git dir
            match = re.match(r"gitdir:\s*(.+)", dot.read_text(encoding="utf-8", errors="replace").strip())
            gitdir = (candidate / match.group(1)).resolve() if match else dot
            common = gitdir / "commondir"
            if common.is_file():
                gitdir = (gitdir / common.read_text(encoding="utf-8").strip()).resolve()
        try:
            text = (gitdir / "config").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return candidate if REPO in text else None
    return None


def swap(staged, prefix):
    """Replace prefix/current with staged; a concurrent run sees one tree or the other."""
    current, previous = prefix / "current", prefix / "previous"
    if previous.exists():
        shutil.rmtree(str(previous))
    for attempt in range(10):
        try:
            if current.exists():
                os.replace(str(current), str(previous))
            os.replace(str(staged), str(current))
            break
        except OSError:
            # Windows refuses to rename a directory while a file inside is open.
            if attempt == 9:
                raise
            time.sleep(1)
    if previous.exists():
        shutil.rmtree(str(previous), ignore_errors=True)
    return current


def launchers(current, directory):
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, script in (("agent-session-sync", "agent-session-sync.py"), ("memory-sync", "memory-sync.py")):
        target = current / script
        if WINDOWS:
            path = directory / (name + ".cmd")
            body = '@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, target)
        else:
            path = directory / name
            body = '#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, target)
        temporary = path.with_name("." + path.name + ".new")
        temporary.write_text(body, encoding="utf-8")
        if not WINDOWS:
            temporary.chmod(0o755)
        os.replace(str(temporary), str(path))
        written.append(path)
    return written


def automation_command(entry, found, dry_run):
    command = entry + ["install", "--state-dir", str(found["state_dir"])]
    if found["captured"].is_file():
        # Keep the data roots and options captured by the previous install.
        command += ["--config", str(found["captured"])]
    if not found["scheduler"]:
        command.append("--no-cron")
    if not found["hooks"]:
        command.append("--no-hooks")
    if found["memories"]:
        command.append("--memories")
    if dry_run:
        command.append("--dry-run")
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install or update agent-session-sync and its existing automation.")
    parser.add_argument("--ref", default=None, help="branch, tag, or commit to install (default: main)")
    parser.add_argument("--source", help="install from this checkout instead of downloading")
    parser.add_argument("--prefix", help="managed install location (default: {})".format(data_home()))
    parser.add_argument("--state-dir", help="state directory of the existing automation")
    parser.add_argument("--managed", action="store_true", help="use the managed install even when a uv/pipx tool exists")
    parser.add_argument("--no-automation", action="store_true", help="update code only; leave cron and hooks untouched")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without changing it")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 8):
        say("agent-session-sync needs Python 3.8 or newer")
        return 1
    here = Path(__file__).resolve().parent if "__file__" in globals() and Path(__file__).is_file() else None
    if args.source is None and args.ref is None and here and (here / "agent_session_sync/cli.py").is_file():
        args.source = str(here)
    prefix = Path(args.prefix).expanduser() if args.prefix else data_home()
    prefix.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(prefix)))
    try:
        if args.source:
            say("source: " + str(Path(args.source).resolve()))
            stage_from(args.source, staged)
        else:
            ref = args.ref or "main"
            try:
                sha = resolve(ref)
                say("source: {} @ {} ({})".format(REPO, ref, sha[:12]))
                unpacked = Path(tempfile.mkdtemp(prefix=".download-", dir=str(prefix)))
                try:
                    extract(fetch(TARBALL + sha), unpacked)
                    stage_from(unpacked, staged)
                finally:
                    shutil.rmtree(str(unpacked), ignore_errors=True)
            except (OSError, urllib.error.URLError, ValueError) as exc:
                say("cannot download {}@{}: {}".format(REPO, ref, exc))
                return 1
        try:
            version = vet(staged)
        except (OSError, SyntaxError, ValueError) as exc:
            say("refusing source: {}".format(exc))
            return 1
        say("version: " + version)
        found = detect(staged, args.state_dir)
        say("existing automation: scheduler={scheduler} hooks={hooks} memories={memories} (state {state_dir})".format(**found))
        for checkout in sorted(found["checkouts"]):
            say("note: automation runs from the checkout {}; update it with `git pull` there.".format(checkout))
        if found["checkouts"] and not args.managed:
            say("left the checkout install as is; pass --managed to move automation to a managed install")
            return 0
        manager = None if args.managed else tool_manager()
        if args.dry_run:
            if manager:
                say("would upgrade the {} tool from this source".format(manager))
            else:
                say("would install into {} and write launchers to {}".format(prefix / "current", bin_dir(prefix)))
            if not args.no_automation and (found["scheduler"] or found["hooks"] or found["memories"]):
                entry = [sys.executable, str(staged / "agent-session-sync.py")]
                say("would refresh automation; preview:")
                subprocess.call(automation_command(entry, found, dry_run=True))
            return 0
        if manager == "uv":
            subprocess.run(["uv", "tool", "install", "--force", "--reinstall", str(staged)], check=True)
            entry = [shutil.which("agent-session-sync") or "agent-session-sync"]
        elif manager == "pipx":
            subprocess.run(["pipx", "install", "--force", str(staged)], check=True)
            entry = [shutil.which("agent-session-sync") or "agent-session-sync"]
        else:
            current = swap(staged, prefix)
            subprocess.run([sys.executable, str(current / "agent-session-sync.py"), "--version"], check=True, capture_output=True)
            for path in launchers(current, bin_dir(prefix)):
                say("launcher: " + str(path))
            if str(bin_dir(prefix)) not in os.environ.get("PATH", "").split(os.pathsep):
                say("note: add {} to PATH to run agent-session-sync directly".format(bin_dir(prefix)))
            entry = [sys.executable, str(current / "agent-session-sync.py")]
        if not args.no_automation and (found["scheduler"] or found["hooks"] or found["memories"]):
            say("refreshing automation...")
            status = subprocess.call(automation_command(entry, found, dry_run=False))
            if status:
                say("automation refresh failed; the previous automation is unchanged where it could not be rewritten")
                return status
        elif not (found["scheduler"] or found["hooks"] or found["memories"]):
            say("no automation found; enable it with `agent-session-sync install`")
        say("agent-session-sync {} is installed.".format(version))
        return 0
    except subprocess.CalledProcessError as exc:
        say("install step failed: {}".format(exc))
        return 1
    finally:
        if staged.exists():
            shutil.rmtree(str(staged), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
