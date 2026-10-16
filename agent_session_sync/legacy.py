"""Compatibility helpers retained by v2; execution delegates to the supported CLI."""
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid

from .io import SyncError
from .ownership import command_owner, cron_owner

CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
CLAUDE_PROJECTS = os.path.join(CLAUDE_HOME, "projects")
TOOL_NAME = "agent-session-sync"
CLAUDE_VERSION = "2.1.206"
MEMORY_NAME = "memory-sync"
MEMORY_SCRIPT = "memory-sync.py"
_codex_version = None

def default_state_dir():
    """Where the registry lives: $AGENT_SESSION_SYNC_STATE_DIR, else %LOCALAPPDATA%
    on Windows, else /var/lib as root (system-wide cron install), else an XDG
    user dir."""
    override = os.environ.get("AGENT_SESSION_SYNC_STATE_DIR")
    if override:
        return os.path.expanduser(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "agent-session-sync")
    if getattr(os, "geteuid", lambda: 1)() == 0:
        return "/var/lib/agent-session-sync"
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-session-sync")

def default_log_file():
    if os.name != "nt" and getattr(os, "geteuid", lambda: 1)() == 0:
        return "/var/log/agent-session-sync.log"
    return os.path.join(default_state_dir(), "sync.log")

STATE_DIR = default_state_dir()
LOG_FILE = default_log_file()



def munge_cwd(cwd):
    """Claude Code project-directory name for a cwd.

    /home/me/proj_x -> -home-me-proj-x ; C:\\src\\app -> C--src-app
    """
    # Claude Code works on UTF-16 code units, so astral characters become "--",
    # and truncates long names to 200 units plus a base-36 hash of the path.
    units = cwd.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
    name = "".join(chr(u) if u < 128 and chr(u).isalnum() else "-" for u in units)
    if len(name) <= 200:
        return name
    value = 0
    for unit in units:
        value = (value * 31 + unit) & 0xFFFFFFFF
    value = abs(value - (1 << 32) if value & 0x80000000 else value)
    suffix = ""
    while True:
        value, rest = divmod(value, 36)
        suffix = "0123456789abcdefghijklmnopqrstuvwxyz"[rest] + suffix
        if not value:
            break
    return name[:200] + "-" + suffix

def det_uuid7(seed, dt):
    """Deterministic UUIDv7: timestamp from dt, 'random' bits from sha256(seed)."""
    # UUIDv7 time is unsigned; historic imported metadata can precede epoch.
    ms = max(0, int(dt.timestamp() * 1000))
    digest = hashlib.sha256(seed.encode()).digest()
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6:16] = digest[:10]
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(b)))

def codex_cli_version():
    global _codex_version
    if _codex_version is None:
        try:
            with open(os.path.join(CODEX_HOME, "version.json")) as fh:
                _codex_version = json.load(fh).get("latest_version", "0.144.0")
        except (OSError, json.JSONDecodeError, AttributeError):
            _codex_version = "0.144.0"
    return _codex_version

def script_path():
    checkout = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent-session-sync.py")
    return checkout if os.path.isfile(checkout) else os.path.join(os.path.dirname(__file__), "compat.py")

def _pythonw():
    """On Windows prefer pythonw.exe so the scheduled task never flashes a console."""
    exe = sys.executable
    if os.name == "nt":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe

def session_spec():
    return {
        "name": TOOL_NAME,
        "file": os.path.basename(script_path()),   # matched inside cron/hook commands
        "path": script_path(),
        "events": ("SessionStart", "Stop"),
        "status": "Syncing Codex/Claude sessions",
        "schedule": "*/2 * * * *",
        "log": LOG_FILE,
        "blurb": "bidirectional Codex <-> Claude Code session sync (idempotent, ~100ms when idle)",
    }

def memory_spec():
    checkout = os.path.join(os.path.dirname(script_path()), MEMORY_SCRIPT)
    target = checkout if os.path.isfile(checkout) else os.path.join(os.path.dirname(__file__), "memory_sync.py")
    return {
        "name": MEMORY_NAME,
        "file": os.path.basename(target),
        "path": target,
        "events": ("Stop",),
        "status": None,
        "schedule": "*/5 * * * *",
        "log": os.path.join(os.path.dirname(LOG_FILE), "memory-sync.log")
               if os.path.dirname(LOG_FILE) else "memory-sync.log",
        "blurb": "bidirectional Codex <-> Claude Code memory sync (opt-in companion)",
    }

def line_is_ours(line, spec):
    """A crontab line belonging to `spec`.

    Comments are matched on the tool name; command lines on the *script
    filename*. Matching commands on the bare tool name would be wrong: this
    repo's directory is itself called agent-session-sync, so every path to
    memory-sync.py contains it.
    """
    return cron_owner(line) == spec["name"]

def hook_is_ours(hook, spec):
    return command_owner(hook.get("command")) == spec["name"]

def _say(dry, msg):
    print(("would " if dry else "") + msg)

def _crontab_read():
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
    if p.returncode == 0:
        return p.stdout.splitlines()
    if p.returncode == 1 and "no crontab for" in p.stderr.lower():
        return []
    raise SyncError("cannot read crontab; refusing to replace it: " + p.stderr.strip())

def _crontab_write(lines):
    body = "\n".join(lines).rstrip("\n") + "\n"
    subprocess.run(["crontab", "-"], input=body, text=True, check=True, capture_output=True)

def _minutes(schedule):
    """'*/5 * * * *' -> 5, for the Windows scheduler and for messages."""
    m = re.match(r"\*/(\d+)", schedule)
    return int(m.group(1)) if m else 2

def _task_exists(spec):
    p = subprocess.run(["schtasks", "/Query", "/TN", spec["name"]],
                       capture_output=True, text=True)
    return p.returncode == 0

def uninstall_task(dry, spec):
    if not _task_exists(spec):
        print(f"{spec['name']}: no scheduled task found")
        return False
    if dry:
        _say(True, f"{spec['name']}: delete scheduled task")
        return True
    subprocess.run(["schtasks", "/Delete", "/TN", spec["name"], "/F"], check=True)
    print(f"{spec['name']}: scheduled task deleted")
    return True


def main():
    from .cli import main as supported_main
    return supported_main()


if __name__ == "__main__":
    raise SystemExit(main())
