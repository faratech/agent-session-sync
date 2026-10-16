"""CLI entry point. No arguments automatically sync all detected harnesses."""
import argparse
import json
import signal
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .adapters import SessionRef, get_adapters
from .automation import setup, shell_command, settings_path
from .discovery import discover, load_config, state_dir
from .engine import Engine
from .io import SyncError, atomic_write, json_bytes, loads, write_output, digest, resolved_workspace
from .model import Session, markdown, imported_snapshot
from .render import DEFAULT_BUDGET
from .store import single_flight


def parser():
    common = argparse.ArgumentParser(add_help=False)
    for flag in ("--dry-run", "--json", "--quiet", "--refresh"):
        common.add_argument(flag, action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--state-dir", default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    root = argparse.ArgumentParser(description="Automatically share conversations across detected CLI harnesses.", parents=[common])
    root.add_argument("--version", action="version", version=__version__)
    subs = root.add_subparsers(dest="command")
    for name in ("doctor", "discover"):
        subs.add_parser(name, parents=[common], help="detect harnesses and inspect cached installations")
    listing = subs.add_parser("list", parents=[common], help="list portable conversations")
    listing.add_argument("--harness")
    listing.add_argument("--project")
    listing.add_argument("--days", type=float, default=30)
    for name in ("show", "export"):
        command = subs.add_parser(name, parents=[common])
        command.add_argument("session")
        command.add_argument("--project")
        command.add_argument("--format", choices=("json", "markdown"), default="json" if name == "export" else "markdown")
        command.add_argument("--output")
    command = subs.add_parser("import", parents=[common], help="import a portable bundle or native transcript/export")
    command.add_argument("file")
    command.add_argument("--harness", help="source harness for native transcripts; omit for portable JSON")
    command.add_argument("--project")
    command = subs.add_parser("handoff", parents=[common], help="prepare a specific conversation for another harness")
    command.add_argument("session")
    command.add_argument("--to", required=True)
    command.add_argument("--project")
    command.add_argument("--launch", action="store_true")
    command.add_argument("--mode", choices=("auto", "native", "context"), default="auto")
    command.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    command.add_argument("--note", default="")
    command = subs.add_parser("open", parents=[common], help="open a harness with the latest conversation for this project")
    command.add_argument("harness")
    command.add_argument("--project", default=".")
    command.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    command = subs.add_parser("sync", parents=[common], help="automatically sync all detected harnesses")
    command.add_argument("--from", dest="source")
    command.add_argument("--to", dest="destination")
    command.add_argument("--to-claude", action="store_true")
    command.add_argument("--to-codex", action="store_true")
    command.add_argument("--project")
    command.add_argument("--days", type=float, default=30)
    command.add_argument("--session")
    command.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    for name in ("install", "uninstall"):
        command = subs.add_parser(name, parents=[common])
        command.add_argument("--no-cron", action="store_true")
        command.add_argument("--no-hooks", action="store_true")
        command.add_argument("--memories", action="store_true")
        command.add_argument("--purge", action="store_true")
    command = subs.add_parser("update", parents=[common], help="show package update instructions")
    command.add_argument("--ref", default="main")
    command.add_argument("--memories", action="store_true")
    command = subs.add_parser("hook", parents=[common], help="internal lifecycle handler")
    command.add_argument("--harness", required=True)
    command.add_argument("--event", choices=("start", "stop"), required=True)
    return root


def legacy_arguments(argv):
    aliases = {"--install": "install", "--uninstall": "uninstall", "--update": "update"}
    commands = {"doctor", "discover", "list", "show", "export", "import", "handoff", "open", "sync", "install", "uninstall", "update", "hook"}
    value_options = {"--config", "--state-dir", "--days", "--session", "--project", "--from", "--to", "--budget", "--ref"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in commands or token in ("--help", "-h", "--version", "--"):
            return argv
        if token in aliases:
            return [aliases[token]] + argv[:index] + argv[index + 1:]
        if token in value_options:
            index += 2
        else:
            index += 1
    return ["sync"] + argv


def emit(value, args):
    if args.json:
        write_output(json.dumps(value, ensure_ascii=False, indent=2))
    elif not args.quiet:
        if isinstance(value, str):
            write_output(value)
        elif isinstance(value, list):
            for item in value:
                write_output(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
        else:
            write_output(json.dumps(value, ensure_ascii=False, indent=2))


def hook(engine, args):
    payload = json.loads(getattr(sys.stdin, "buffer", sys.stdin).read())
    if not isinstance(payload, dict):
        raise SyncError("hook payload must be a JSON object")
    for field in ("cwd", "session_id", "sessionId", "conversationId", "transcript_path", "transcriptPath", "source"):
        if payload.get(field) is not None and not isinstance(payload[field], str):
            raise SyncError("hook {} must be a string".format(field))
    if "workspacePaths" in payload and (not isinstance(payload["workspacePaths"], list)
            or any(not isinstance(path, str) for path in payload["workspacePaths"])):
        raise SyncError("hook workspacePaths must be an array of strings")
    adapter = engine.adapters.get(args.harness)
    if adapter is None:
        return {}
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    shared = [a for a in engine.adapters.values() if a.installation.harness == adapter.installation.harness
              and settings_path(a).resolve() == settings_path(adapter).resolve()]
    if len(shared) > 1:
        matching = []
        if transcript:
            for candidate in shared:
                try:
                    Path(transcript).expanduser().resolve().relative_to(candidate.home.expanduser().resolve())
                    matching.append(candidate)
                except ValueError:
                    pass
        if matching:
            longest = max(len(str(a.home.resolve())) for a in matching)
            matching = [a for a in matching if len(str(a.home.resolve())) == longest]
        if len(matching) != 1:
            print("agent-session-sync: shared hook cannot identify the active profile; use separate profile settings or `open harness@profile`", file=sys.stderr)
            return {}
        adapter = matching[0]
    endpoint = adapter.endpoint
    if adapter.installation.harness == "agy" and transcript:
        # Antigravity shares its global hook file with desktop/IDE surfaces.
        try:
            Path(transcript).expanduser().resolve().relative_to(adapter.home.expanduser().resolve())
        except ValueError:
            return {}
    workspace = payload.get("cwd") or next(iter(payload.get("workspacePaths", [])), "")
    sid = payload.get("session_id") or payload.get("sessionId") or payload.get("conversationId")
    if not workspace:
        return {}
    # Stored workspaces keep their spelling (launch cwd, Claude project names); only comparisons normcase.
    workspace = resolved_workspace(workspace)
    if sid:
        engine.store.notify_workspace(endpoint, sid, workspace)
    # Startup injection must not alter an explicit resume, fork, or compaction.
    if args.event == "start" and payload.get("source") in ("resume", "compact", "fork", "clear", "new"):
        return {}
    if payload.get("agent_id") or payload.get("agentId"):
        return {}
    if args.event == "stop" or adapter.native_import or not engine.config.get("auto_handoff", True) or not adapter.options.get("auto_handoff", True):
        # Native mirrors are available in their normal resume picker.
        engine.sync(project=workspace)
        return {}
    if sid and transcript and Path(transcript).is_file():
        existing = adapter.read(SessionRef(sid, transcript))
        if any(e.kind in ("assistant", "tool_call", "tool_result") for e in existing.events) or sum(e.kind == "user" for e in existing.events) > 1:
            engine.sync(project=workspace)
            return {}
    item = engine.incoming(endpoint, workspace, sid)
    if not item:
        engine.sync(project=workspace)
        return {}
    context = Path(item["context"]).read_text(encoding="utf-8-sig")
    if sid:
        engine.store.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                                ("binding:{}:{}".format(endpoint, sid), item["id"]))
        engine.store.db.commit()
    engine.sync(project=workspace)
    name = adapter.installation.harness
    if name == "agy":
        return {"injectSteps": [{"userMessage": context}]}
    if name == "copilot":
        return {"additionalContext": context}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def execute(args):
    config = load_config(args.config)
    directory = Path(args.state_dir).expanduser().absolute() if args.state_dir else state_dir()
    if args.command == "update":
        emit("Update an installed package with `uv tool upgrade agent-session-sync` or `pipx upgrade agent-session-sync`.\n"
             "For a git checkout, use `git pull`. Raw single-script self-updates are no longer used.", args)
        return 0
    installations = discover(config, args.refresh, args.dry_run, directory)
    adapters = get_adapters(installations, config)
    if args.command in ("doctor", "discover"):
        report = [dict(asdict(i), endpoint=i.endpoint, mode="native + context" if adapters[i.endpoint].native_import else "context",
                       format=adapters[i.endpoint].format_name) for i in installations]
        if args.json:
            emit({"installations": report, "cache": str(directory / "discovery.json"), "cache_ttl_seconds": 86400}, args)
        else:
            emit(["{}: {} | {} | {}".format(i["endpoint"], i["executable"] or "not installed", i["version"], i["mode"]) for i in report], args)
        return 0
    if args.command in ("install", "uninstall"):
        changes = setup(adapters, directory, config, args.config, args.command == "uninstall", args.dry_run,
                        args.no_cron, args.no_hooks, args.memories, args.purge)
        emit({"dry_run": args.dry_run, "changes": changes} if args.json else changes or ("Nothing to remove." if args.command == "uninstall" else "Automation already configured."), args)
        return 0
    pending_launch = None
    with single_flight(directory, args.dry_run):
        engine = Engine(adapters, directory, args.dry_run, config)
        try:
            if args.command == "hook":
                write_output(json.dumps(hook(engine, args), ensure_ascii=False))
                return 0
            if args.command == "list":
                sessions = engine.scan(args.project, args.harness, args.days)
                emit([s.to_dict() for s in sessions] if args.json else ["{}  {}  [{}]".format(s.key, s.title, s.workspace or "workspace unknown") for s in sessions], args)
            elif args.command in ("show", "export"):
                session = engine.select(args.session, args.project)
                data = json_bytes(session.to_dict()) if args.format == "json" or args.json else markdown(session).encode("utf-8")
                if args.output:
                    if not args.dry_run:
                        # An explicitly named output path is the user's to replace.
                        atomic_write(args.output, data, replace=True)
                    emit({"output": args.output, "dry_run": args.dry_run}, args)
                else:
                    write_output(data.decode("utf-8"), end="")
            elif args.command == "import":
                if args.harness:
                    if args.harness not in adapters:
                        raise SyncError("unknown harness: " + args.harness)
                    session = adapters[args.harness].read(SessionRef(Path(args.file).stem, args.file))
                    source_sha = session.source_sha
                else:
                    raw = Path(args.file).read_bytes()
                    session = Session.from_dict(loads(raw))
                    source_sha = digest(raw)
                if args.project:
                    session.workspace = resolved_workspace(args.project)
                session = imported_snapshot(session, args.file, source_sha)
                session = engine.store.remember(session, imported=True)
                if not args.dry_run:
                    archive = engine.archive_path(session)
                    if archive.is_file():
                        existing = Session.from_dict(loads(archive.read_bytes()))
                        if existing.revision != session.revision:
                            raise SyncError("archive identity mismatch: " + str(archive))
                    else:
                        atomic_write(archive, json_bytes(session.to_dict()))
                emit({"session": session.key, "dry_run": args.dry_run}, args)
            elif args.command in ("handoff", "open"):
                destination = args.to if args.command == "handoff" else args.harness
                if destination not in adapters:
                    raise SyncError("unknown destination: " + destination)
                if args.command == "handoff":
                    session = engine.select(args.session, args.project)
                    item = engine.handoff(session, destination, mode=args.mode, budget=args.budget, note=args.note)
                else:
                    sessions = engine.scan(project=args.project)
                    session = next((s for s in sessions if s.endpoint != destination), None)
                    item = engine.handoff(session, destination, budget=args.budget) if session else {
                        "command": [adapters[destination].installation.executable or destination], "workspace": resolved_workspace(args.project)}
                emit(item if args.json else ("Would prepare " if args.dry_run else "Prepared ") + destination + ":\n" + shell_command(item["command"]), args)
                launch = args.command == "open" or args.launch
                if launch and not args.dry_run and not args.json:
                    if not adapters[destination].installation.executable:
                        raise SyncError("destination executable is not installed: " + destination)
                    # The harness runs after the lock is released so its own hooks can sync.
                    pending_launch = (item["command"], item["workspace"] or None)
            elif args.command == "sync":
                if args.to_claude and args.to_codex:
                    raise SyncError("choose only one of --to-claude and --to-codex")
                source = "codex" if args.to_claude else "claude" if args.to_codex else args.source
                destination = "claude" if args.to_claude else "codex" if args.to_codex else args.destination
                if args.session:
                    session = engine.select(args.session, args.project)
                    if source and source not in (session.harness, session.endpoint):
                        raise SyncError("session does not belong to selected source")
                    targets = [destination] if destination else [name for name, a in adapters.items() if a.installation.executable and name != session.endpoint]
                    results = [engine.handoff(session, target, budget=args.budget) for target in targets]
                else:
                    results = engine.sync(source, destination, args.project, args.days, args.budget)
                emit({"transfers": results, "errors": engine.errors, "dry_run": args.dry_run} if args.json else
                     "{}{} handoff(s) across detected harnesses{}".format("Would prepare " if args.dry_run else "Prepared ", len(results), " ({} errors)".format(len(engine.errors)) if engine.errors else ""), args)
                if args.verbose and not args.json:
                    for item in results:
                        print("{} -> {} ({})".format(item["source_key"], item["destination"], item["mode"]), file=sys.stderr)
            if engine.errors and not args.json:
                for error in engine.errors:
                    print("agent-session-sync: " + error, file=sys.stderr)
            status = 1 if engine.errors else 0
        finally:
            engine.close()
    if pending_launch:
        # Ctrl-C belongs to the interactive harness. A no-op handler (unlike SIG_IGN)
        # is reset to the default in the child on exec.
        previous = signal.signal(signal.SIGINT, lambda signum, frame: None)
        try:
            return subprocess.call(pending_launch[0], cwd=pending_launch[1])
        finally:
            signal.signal(signal.SIGINT, previous)
    return status


def main(argv=None):
    args = parser().parse_args(legacy_arguments(list(sys.argv[1:] if argv is None else argv)))
    for name, value in dict(dry_run=False, json=False, quiet=False, refresh=False, config=None, state_dir=None, verbose=False).items():
        if not hasattr(args, name):
            setattr(args, name, value)
    try:
        return execute(args)
    except Exception as exc:
        # Hooks must never block a turn or emit malformed protocol output.
        if args.command == "hook":
            print("{}")
            print("agent-session-sync hook: " + str(exc), file=sys.stderr)
            return 0
        if not isinstance(exc, (SyncError, OSError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError)):
            raise
        if args.json:
            write_output(json.dumps({"error": str(exc)}))
        else:
            print("agent-session-sync: " + str(exc), file=sys.stderr)
        return 1
