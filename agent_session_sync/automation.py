"""Surgical scheduler and startup-hook installation for detected harnesses."""
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from . import legacy
from .io import SyncError, atomic_write, digest, json_bytes
from .ownership import command_owner, SESSION, MEMORY
from .discovery import Installation, load_config, default_homes
from .adapters import BUILTINS
from .store import single_flight

OWNER = "agent-session-sync"


def entry_command():
    checkout = Path(__file__).resolve().parent.parent / "agent-session-sync.py"
    if checkout.is_file():
        return [sys.executable, str(checkout)]
    return [sys.executable, "-m", "agent_session_sync"]


def shell_command(argv, windows=None, powershell=False):
    if powershell:
        return "& " + " ".join("'" + a.replace("'", "''") + "'" for a in argv)
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        # Every argument is quoted, including metacharacters in paths without spaces.
        return " ".join('"' + a.replace('"', '""') + '"' for a in argv)
    return " ".join(shlex.quote(a) for a in argv)


SCHTASKS_TR_LIMIT = 261


def schtasks_create(name, command, minutes):
    """schtasks arguments, plus a task definition when /TR would exceed its limit."""
    line = shell_command(command, windows=True)
    if len(line) <= SCHTASKS_TR_LIMIT:
        return ["schtasks", "/Create", "/TN", name, "/SC", "MINUTE", "/MO", str(minutes), "/F", "/TR", line], None
    start = datetime.now().replace(microsecond=0).isoformat()
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <Triggers><TimeTrigger><Repetition><Interval>PT{}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>'
        '<StartBoundary>{}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>\n'
        '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
        '  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><Enabled>true</Enabled></Settings>\n'
        '  <Actions Context="Author"><Exec><Command>{}</Command><Arguments>{}</Arguments></Exec></Actions>\n'
        '</Task>\n').format(minutes, start, escape(command[0]), escape(shell_command(command[1:], windows=True)))
    return ["schtasks", "/Create", "/TN", name, "/F", "/XML"], xml


def command_is_ours(value):
    return any(command_owner(value.get(field)) == SESSION for field in ("command", "bash", "powershell"))


def session_line_is_ours(line, spec):
    return legacy.line_is_ours(line, spec)


def settings_path(adapter):
    if adapter.options.get("settings"):
        return Path(adapter.options["settings"]).expanduser()
    if adapter.installation.harness == "muse":
        return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "muse/settings.json"
    if adapter.installation.harness == "agy":
        return Path.home() / ".gemini/config/hooks.json"
    return adapter.home / "settings.json"


def edit_hooks(adapter, command_base, uninstall=False):
    """Return a preview; no file writes occur here."""
    name = adapter.installation.harness
    path = settings_path(adapter)
    if name == "copilot" and not adapter.options.get("settings"):
        path = adapter.home / "hooks/agent-session-sync.json"
    path = path.expanduser().resolve()
    try:
        raw = path.read_bytes()
        cfg = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        raw, cfg = None, {}
    except (OSError, ValueError) as exc:
        raise SyncError("refusing to edit {}: {}".format(path, exc))
    original = copy.deepcopy(cfg)
    if name == "muse" and not uninstall:
        cfg.setdefault("schema_version", 1)
    if name == "copilot" and not uninstall:
        cfg.setdefault("version", 1)
    hooks = cfg if name == "agy" and path.name == "hooks.json" else cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SyncError("hooks must be an object: " + str(path))
    if name == "agy":
        old = hooks.get(OWNER)
        if old is not None and not isinstance(old, dict):
            raise SyncError("invalid named hook definition: " + str(path))
        if old and not all(isinstance(h, dict) and command_is_ours(h) for e, handlers in old.items() if isinstance(handlers, list) for h in handlers):
            raise SyncError("hook name is already owned by another command: " + str(path))
        hooks.pop(OWNER, None)
        if not uninstall:
            hooks[OWNER] = {event: [{"type": "command", "command": shell_command(command_base + ["hook", "--harness", adapter.endpoint, "--event", action]), "timeout": 120}]
                            for event, action in (("SessionStart", "start"), ("Stop", "stop"))}
    elif name in ("muse", "claude"):
        for event in ("SessionStart", "Stop"):
            groups = []
            entries = hooks.get(event, [])
            if not isinstance(entries, list):
                raise SyncError("hook event must contain groups: " + event)
            for group in entries:
                if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                    raise SyncError("invalid hook group in " + str(path))
                handlers = group.get("hooks", [])
                if any(not isinstance(h, dict) for h in handlers):
                    raise SyncError("invalid hook handler in " + str(path))
                kept = [h for h in handlers if not command_is_ours(h)]
                if kept or not handlers:
                    groups.append(dict(group, hooks=kept))
            if not uninstall:
                action = "start" if event == "SessionStart" else "stop"
                groups.append({"matcher": "*" if name == "muse" else "", "hooks": [{"type": "command", "timeout": 120,
                    "command": shell_command(command_base + ["hook", "--harness", adapter.endpoint, "--event", action])}]})
            if groups:
                hooks[event] = groups
            elif entries:
                # Only containers we emptied are pruned; the user's own empty ones remain.
                hooks.pop(event, None)
    elif name == "copilot":
        for event, action in (("sessionStart", "start"), ("agentStop", "stop")):
            entries = hooks.get(event, [])
            if not isinstance(entries, list) or any(not isinstance(h, dict) for h in entries):
                raise SyncError("invalid Copilot hook handlers in " + str(path))
            handlers = [h for h in entries if not command_is_ours(h)]
            if not uninstall:
                argv = command_base + ["hook", "--harness", adapter.endpoint, "--event", action]
                handlers.append({"type": "command", "bash": shell_command(argv, windows=False),
                                 "powershell": shell_command(argv, powershell=True), "timeoutSec": 120})
            if handlers:
                hooks[event] = handlers
            elif entries:
                hooks.pop(event, None)
    if not hooks and (original.get("hooks") or "hooks" not in original):
        cfg.pop("hooks", None)
    return path, raw, json_bytes(cfg), cfg != original


def memory_hook_preview(path, raw, current, command, uninstall=False):
    cfg = copy.deepcopy(current)
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SyncError("hooks must be an object: " + str(path))
    groups = hooks.get("Stop", [])
    if not isinstance(groups, list):
        raise SyncError("Stop hooks must be an array: " + str(path))
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
            raise SyncError("invalid Stop hook group: " + str(path))
        handlers = group.get("hooks", [])
        if any(not isinstance(h, dict) for h in handlers):
            raise SyncError("invalid Stop hook handler: " + str(path))
        kept = [h for h in handlers if command_owner(h.get("command")) != MEMORY]
        if kept or not handlers:
            kept_groups.append(dict(group, hooks=kept))
    if not uninstall:
        kept_groups.append({"matcher": "", "hooks": [{"type": "command", "command": shell_command(command), "timeout": 120, "async": True}]})
    if kept_groups:
        hooks["Stop"] = kept_groups
    elif groups:
        hooks.pop("Stop", None)
    if not hooks and (current.get("hooks") or "hooks" not in current):
        cfg.pop("hooks", None)
    original = json.loads(raw.decode("utf-8-sig")) if raw is not None else {}
    return path, raw, json_bytes(cfg), cfg != original


def recorded_adapters(directory):
    path = Path(directory) / "automation-config.json"
    if not path.is_file():
        return []
    config = load_config(path)
    adapters = []
    if "installed_hooks" in config:
        records = config["installed_hooks"]
        if not isinstance(records, list):
            raise SyncError("invalid installed hook records")
        for record in records:
            if (not isinstance(record, dict) or record.get("harness") not in BUILTINS
                    or any(not isinstance(record.get(field), str) or not record[field] for field in ("profile", "home", "settings"))):
                raise SyncError("invalid installed hook destination")
            adapters.append(BUILTINS[record["harness"]](Installation(record["harness"], record["profile"], record["home"]), {"settings": record["settings"]}))
        return adapters
    for name, options in config.get("harnesses", {}).items():
        if name in BUILTINS and options.get("home"):
            adapters.append(BUILTINS[name](Installation(name, "default", options["home"]), options))
    for profile in config.get("profiles", []):
        if profile.get("harness") in BUILTINS and profile.get("home"):
            options = dict(config.get("harnesses", {}).get(profile["harness"], {}), **profile)
            adapters.append(BUILTINS[profile["harness"]](Installation(profile["harness"], profile["name"], profile["home"]), options))
    return adapters


def setup(adapters, directory, config, config_path=None, uninstall=False, dry_run=False, no_cron=False, no_hooks=False, memories=False, purge=False):
    with single_flight(Path(directory).expanduser().absolute(), dry_run):
        memory_lock = None
        try:
            if uninstall and purge and memories and not dry_run:
                from .memory_sync import acquire_lock
                memory_lock = acquire_lock(str(Path(directory).expanduser().absolute() / "memory.lock"))
                if memory_lock is None:
                    raise SyncError("memory sync is already running; retry purge after it completes")
            return _setup(adapters, directory, config, config_path, uninstall, dry_run, no_cron, no_hooks, memories, purge)
        finally:
            if memory_lock is not None:
                memory_lock.close()


def _setup(adapters, directory, config, config_path=None, uninstall=False, dry_run=False, no_cron=False, no_hooks=False, memories=False, purge=False):
    directory = Path(directory).expanduser().absolute()
    selected = [a for a in adapters.values() if a.installation.harness in BUILTINS and (a.installation.executable or uninstall)]
    recorded = recorded_adapters(directory)
    if uninstall:
        selected.extend(recorded)
    captured = copy.deepcopy(config)
    for adapter in selected:
        if adapter.installation.profile == "default":
            options = captured.setdefault("harnesses", {}).setdefault(adapter.installation.harness, {})
        else:
            profiles = captured.setdefault("profiles", [])
            options = next((p for p in profiles if p.get("harness") == adapter.installation.harness and p.get("name") == adapter.installation.profile), None)
            if options is None:
                options = {"harness": adapter.installation.harness, "name": adapter.installation.profile}
                profiles.append(options)
        options["home"] = str(adapter.home.expanduser().resolve())
        if adapter.installation.harness != "codex":
            path = settings_path(adapter)
            if adapter.installation.harness == "copilot" and not adapter.options.get("settings"):
                path = adapter.home / "hooks/agent-session-sync.json"
            options["settings"] = str(path.expanduser().resolve())
    destinations = {}
    for adapter in recorded + ([] if no_hooks else selected):
        if adapter.installation.harness == "codex":
            continue
        path = settings_path(adapter)
        if adapter.installation.harness == "copilot" and not adapter.options.get("settings"):
            path = adapter.home / "hooks/agent-session-sync.json"
        record = {"harness": adapter.installation.harness, "profile": adapter.installation.profile,
                  "home": str(adapter.home.expanduser().resolve()), "settings": str(path.expanduser().resolve())}
        destinations[tuple(record.values())] = record
    captured["installed_hooks"] = list(destinations.values())
    automation_config = directory / "automation-config.json"
    if config_path is not None and not Path(config_path).expanduser().is_file():
        raise SyncError("selected configuration file does not exist: {}".format(Path(config_path).expanduser().resolve()))
    # Scheduler and hook processes read the captured copy, which includes resolved data roots.
    argv = entry_command() + ["--state-dir", str(directory), "--config", str(automation_config)]
    previews, hook_paths = {}, {}
    if not no_hooks:
        for adapter in selected:
            if adapter.installation.harness == "codex":
                continue
            preview = edit_hooks(adapter, argv, uninstall)
            path = preview[0]
            if path in hook_paths and hook_paths[path] != adapter.installation.harness:
                raise SyncError("different harnesses cannot share a hook settings file: " + str(path))
            hook_paths[path] = adapter.installation.harness
            previews.setdefault(path, preview)
        if not uninstall:
            for adapter in recorded:
                path = settings_path(adapter).expanduser().resolve()
                if path not in previews:
                    previews[path] = edit_hooks(adapter, argv, uninstall=True)
    session = legacy.session_spec()
    session.update(file="agent-session-sync.py", log=str(directory / "sync.log"), blurb="automatic harness handoffs")
    jobs = [(session, argv + ["sync", "--quiet"])]
    if memories:
        memory = legacy.memory_spec()
        memory["log"] = str(directory / "memory-sync.log")
        defaults = {name: adapters.get(name) or next((a for a in recorded if a.endpoint == name), None) for name in ("codex", "claude")}
        if not uninstall and any(defaults[name] is None for name in defaults):
            raise SyncError("memory automation requires default Codex and Claude profiles")
        homes = default_homes()
        memory_argv = [sys.executable, memory["path"], "--quiet", "--state-dir", str(directory),
                       "--codex-home", str(defaults["codex"].home if defaults["codex"] else homes["codex"]),
                       "--claude-home", str(defaults["claude"].home if defaults["claude"] else homes["claude"])]
        jobs.append((memory, memory_argv))
        if not no_hooks:
            claudes = [a for a in selected if a.installation.harness == "claude" and (uninstall or a.endpoint == "claude")]
            if not claudes and defaults["claude"]:
                claudes = [defaults["claude"]]
            for claude in claudes:
                path = settings_path(claude).expanduser().resolve()
                if path in previews:
                    _, raw, data, _ = previews[path]
                    current = json.loads(data)
                else:
                    try:
                        raw = path.read_bytes()
                        current = json.loads(raw.decode("utf-8-sig"))
                    except FileNotFoundError:
                        raw, current = None, {}
                    except (OSError, ValueError) as exc:
                        raise SyncError("cannot read memory hook settings: {}".format(exc))
                    if not isinstance(current, dict):
                        raise SyncError("memory hook settings must be an object")
                previews[path] = memory_hook_preview(path, raw, current, memory_argv, uninstall)
    changes = []
    cron_lines = None
    if not no_cron and os.name != "nt":
        if shutil.which("crontab"):
            original_lines = legacy._crontab_read()
            cron_lines = list(original_lines)
            for spec, command in jobs:
                cron_lines = [line for line in cron_lines if not legacy.line_is_ours(line, spec)]
                if not uninstall:
                    # cron turns an unescaped % into a newline.
                    entry = (shell_command(command) + " >> " + shlex.quote(spec["log"]) + " 2>&1").replace("%", "\\%")
                    cron_lines.extend(["# " + spec["name"] + " — " + spec["blurb"], spec["schedule"] + " " + entry])
            if cron_lines == original_lines:
                cron_lines = None
            else:
                changes.append(("remove" if uninstall else "configure") + " automatic sync scheduler")
        elif not uninstall:
            changes.append("crontab unavailable; hooks still sync at session boundaries")
    if not uninstall:
        try:
            current = automation_config.read_bytes()
        except OSError:
            current = None
        if current != json_bytes(captured):
            # Scheduler and hook processes read this copy, so its changes are visible changes.
            changes.insert(0, "capture configuration: " + str(automation_config))
    # All configuration and scheduler reads finish before any mutation.
    if not dry_run and not uninstall:
        atomic_write(automation_config, json_bytes(captured), replace=True)
    for path, raw, data, changed in previews.values():
        if not changed:
            continue
        changes.append(("remove" if uninstall else "configure") + " hooks: " + str(path))
        if not dry_run:
            if raw is not None:
                backup = path.with_name(path.name + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))
                atomic_write(backup, raw)
            atomic_write(path, data, expected=digest(raw) if raw is not None else None)
    if not no_cron:
        if os.name == "nt":
            for spec, command in jobs:
                if uninstall:
                    if legacy._task_exists(spec):
                        changes.append("remove scheduled task: " + spec["name"])
                        if not dry_run:
                            subprocess.run(["schtasks", "/Delete", "/TN", spec["name"], "/F"], check=True, capture_output=True, text=True)
                else:
                    changes.append("schedule " + spec["name"] + " every " + str(legacy._minutes(spec["schedule"])) + " minutes")
                    if not dry_run:
                        command = [legacy._pythonw()] + command[1:]
                        arguments, xml = schtasks_create(spec["name"], command, legacy._minutes(spec["schedule"]))
                        if xml is None:
                            subprocess.run(arguments, check=True, capture_output=True, text=True)
                        else:
                            # Long commands are registered from a definition; Task Scheduler reads UTF-16.
                            fd, definition = tempfile.mkstemp(prefix=".task-", suffix=".xml", dir=str(directory))
                            try:
                                with os.fdopen(fd, "wb") as stream:
                                    stream.write(xml.encode("utf-16"))
                                subprocess.run(arguments + [definition], check=True, capture_output=True, text=True)
                            finally:
                                os.unlink(definition)
        elif cron_lines is not None and not dry_run:
            if cron_lines:
                legacy._crontab_write(cron_lines)
            else:
                subprocess.run(["crontab", "-r"], check=True, capture_output=True, text=True)
    if purge and uninstall:
        changes.append("purge state and logs: " + str(directory))
        if not dry_run:
            names = {"discovery.json", "sessions.sqlite3", "sessions.sqlite3-journal", "sessions.sqlite3-wal", "sessions.sqlite3-shm",
                     "lock", "sync.log", "automation-config.json", "archives", "handoffs", "inbox", "state.json", "state-v1.json.bak"}
            if memories:
                names.update({"memory-state.json", "memory.lock", "memory-sync.log"})
            # A process can still be waiting on these exact inodes. Unlinking
            # a held sentinel would let another process create a second mutex.
            names.difference_update({"lock", "memory.lock"})
            if no_cron or no_hooks:
                names.discard("automation-config.json")
            for name in names:
                path = directory / name
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            try:
                directory.rmdir()
            except OSError:
                pass
    return changes
