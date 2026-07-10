#!/usr/bin/env python3
"""agent-session-sync — bidirectional Codex <-> Claude Code conversation sync.

Makes OpenAI Codex CLI sessions resumable in Claude Code and vice versa:

  Codex -> Claude : ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
                    -> ~/.claude/projects/<munged-cwd>/<session-id>.jsonl
  Claude -> Codex : ~/.claude/projects/*/<uuid>.jsonl
                    -> ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid7>.jsonl

Tool calls are flattened into readable [*_tool_call]/[*_tool_result] text
blocks — the same strategy Codex's own (since-removed) external_migration
feature used, so both TUIs render imports natively and resume works without
tool-pairing constraints.

Idempotent: deterministic target IDs + a state registry (under
/var/lib/agent-session-sync as root, else ~/.local/state/agent-session-sync;
override with $AGENT_SESSION_SYNC_STATE_DIR). A target that was continued in
the other tool (diverged from what we wrote) is never overwritten. Imports of
imports are skipped in both directions.

Honors $CODEX_HOME and $CLAUDE_CONFIG_DIR.

Typical usage:
  agent-session-sync.py                 # sync both directions, last 30 days
  agent-session-sync.py --days 365      # deeper backfill
  agent-session-sync.py --session <id>  # force one source session by id
  agent-session-sync.py --dry-run -v    # show what would happen
Designed to run from cron every couple of minutes (mtime-skip makes an
idle run cost ~10ms).
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

try:                      # POSIX
    import fcntl
    msvcrt = None
except ImportError:       # Windows
    fcntl = None
    import msvcrt

CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
CLAUDE_PROJECTS = os.path.join(CLAUDE_HOME, "projects")


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
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_FILE = os.path.join(STATE_DIR, "lock")
LOG_FILE = default_log_file()
CLAUDE_SETTINGS = os.path.join(CLAUDE_HOME, "settings.json")
TOOL_NAME = "agent-session-sync"  # also the marker matched when un/installing

CLAUDE_VERSION = "2.1.206"  # stamped into generated records; display-only
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")
ROLLOUT_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})\.jsonl$")
TOOL_TEXT_CAP = 3000          # chars kept per flattened tool input/result
IMPORT_MARKER = "<EXTERNAL SESSION IMPORTED>"
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid5 namespace

VERBOSE = False


def log(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(ts):
    """ISO timestamp (with or without Z / fractional part) -> aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def munge_cwd(cwd):
    """Claude Code project-directory name for a cwd.

    /home/me/proj_x -> -home-me-proj-x ; C:\\src\\app -> C--src-app
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def det_uuid(*parts):
    return str(uuid.uuid5(NS, ":".join(parts)))


def det_uuid7(seed, dt):
    """Deterministic UUIDv7: timestamp from dt, 'random' bits from sha256(seed)."""
    ms = int(dt.timestamp() * 1000)
    digest = hashlib.sha256(seed.encode()).digest()
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6:16] = digest[:10]
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(b)))


def cap(text, limit=TOOL_TEXT_CAP):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_jsonl_atomic(path, records, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # newline="\n": never emit CRLF on Windows — both TUIs expect LF-delimited
    # JSONL, and the state registry hashes these bytes.
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if mtime is not None:
        os.utime(tmp, (mtime, mtime))
    os.replace(tmp, path)
    return sha256_file(path)


# ---------------------------------------------------------------------------
# state registry
# ---------------------------------------------------------------------------

def acquire_lock(path):
    """Non-blocking exclusive lock. Returns the open handle, or None if another
    run holds it. Released implicitly when the process exits."""
    fh = open(path, "w")
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # Windows: lock one byte; a range past EOF is legal
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:  # BlockingIOError (POSIX) / PermissionError (Windows)
        fh.close()
        return None
    return fh


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"codex_to_claude": {}, "claude_to_codex": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_FILE)


def produced_targets(state):
    """Every file path this tool has ever written (loop guard)."""
    out = set()
    for direction in ("codex_to_claude", "claude_to_codex"):
        for entry in state.get(direction, {}).values():
            if entry.get("target"):
                out.add(entry["target"])
    return out


# ---------------------------------------------------------------------------
# Codex -> Claude
# ---------------------------------------------------------------------------

def iter_codex_rollouts():
    for root, _dirs, files in os.walk(os.path.join(CODEX_HOME, "sessions")):
        for name in files:
            m = ROLLOUT_RE.match(name)
            if m:
                yield os.path.join(root, name), m.group(1)


def codex_texts(content):
    return "\n".join(
        c.get("text", "") for c in (content or [])
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
    )


def convert_codex_to_claude(src_path, session_id):
    """Codex rollout file -> (claude_records, cwd, last_dt). None if not convertible."""
    lines = read_jsonl(src_path)
    if not lines or lines[0].get("type") != "session_meta":
        return None
    meta = lines[0]["payload"]
    if meta.get("thread_source") == "subagent":
        return None
    cwd = meta.get("cwd") or "/"
    branch = (meta.get("git") or {}).get("branch") or ""
    # skip codex's own imports of claude sessions (and ours) — loop guard
    with open(src_path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if "external-import-turn-" in raw or IMPORT_MARKER in raw:
        return None

    # real typed prompts announce themselves as user_message events
    real_prompts = []
    model = None
    for d in lines:
        p = d.get("payload") or {}
        if d.get("type") == "event_msg" and p.get("type") == "user_message":
            real_prompts.append(p.get("message", ""))
        if d.get("type") == "turn_context" and p.get("model"):
            model = p["model"]
    model = model or meta.get("model") or "codex"
    prompt_pool = list(real_prompts)

    base = dict(isSidechain=False, userType="external", entrypoint="cli",
                cwd=cwd, sessionId=session_id, version=CLAUDE_VERSION,
                gitBranch=branch)
    records, parent, idx = [], None, 0
    last_dt = parse_ts(meta.get("timestamp")) or datetime.now(timezone.utc)

    def emit(kind, message, ts, extra=None):
        nonlocal parent, idx, last_dt
        rec = dict(base)
        rec.update(parentUuid=parent, type=kind, message=message,
                   uuid=det_uuid(session_id, str(idx)), timestamp=ts or now_iso())
        if extra:
            rec.update(extra)
        records.append(rec)
        parent = rec["uuid"]
        idx += 1
        dt = parse_ts(ts)
        if dt:
            last_dt = dt

    def emit_assistant(text, ts):
        emit("assistant", {
            "id": f"msg_import_{idx}", "type": "message", "role": "assistant",
            "model": model, "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }, ts)

    first_ts = lines[1].get("timestamp") if len(lines) > 1 else None
    emit("user", {"role": "user",
                  "content": f"[Imported from OpenAI Codex CLI session {session_id} "
                             f"(model {model}). Codex tool activity appears as "
                             f"[codex_tool_call]/[codex_tool_result] text blocks.]"},
         first_ts, extra={"isMeta": True})

    n_user = n_asst = 0
    for d in lines:
        if d.get("type") != "response_item":
            continue
        p = d.get("payload") or {}
        pt = p.get("type")
        ts = d.get("timestamp")
        if pt == "message":
            role = p.get("role")
            text = codex_texts(p.get("content"))
            if not text.strip():
                continue
            if role == "user":
                if text in prompt_pool:            # a real typed prompt
                    prompt_pool.remove(text)
                    emit("user", {"role": "user", "content": text}, ts)
                    n_user += 1
                # else: injected context (AGENTS.md, env) — skip
            elif role == "assistant":
                emit_assistant(text, ts)
                n_asst += 1
        elif pt in ("function_call", "custom_tool_call", "local_shell_call",
                    "web_search_call"):
            name = p.get("name") or pt
            arg = p.get("input") or p.get("arguments") or json.dumps(
                p.get("action", {}), ensure_ascii=False)
            emit_assistant(f"[codex_tool_call: {name}]\n{cap(str(arg))}\n[/codex_tool_call]", ts)
        elif pt in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            if isinstance(out, list):
                out = codex_texts(out)
            elif isinstance(out, dict):
                out = out.get("content") or json.dumps(out, ensure_ascii=False)
            emit_assistant(f"[codex_tool_result]\n{cap(str(out))}\n[/codex_tool_result]", ts)
        # reasoning (encrypted), agent_message (inter-agent), etc. — skip

    if n_user == 0 or n_asst == 0:
        return None
    return records, cwd, last_dt


def sync_codex_to_claude(state, args, guard):
    reg = state.setdefault("codex_to_claude", {})
    done = skipped = 0
    for src, sid in sorted(iter_codex_rollouts()):
        if src in guard:
            continue
        st = os.stat(src)
        entry = reg.get(src)
        if args.session and args.session not in src:
            continue
        if not args.session:
            if st.st_mtime < args.since_epoch:
                continue
            if entry and entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size:
                continue
        conv = convert_codex_to_claude(src, sid)
        if conv is None:
            reg[src] = {"mtime": st.st_mtime, "size": st.st_size, "target": None,
                        "skipped": True}
            continue
        records, cwd, last_dt = conv
        target = os.path.join(CLAUDE_PROJECTS, munge_cwd(cwd), f"{sid}.jsonl")
        if entry and entry.get("target") == target and os.path.exists(target):
            if entry.get("written_sha") and sha256_file(target) != entry["written_sha"]:
                log(f"SKIP (diverged in Claude): {target}")
                skipped += 1
                continue
        if args.dry_run:
            log(f"DRY codex->claude: {src} -> {target} ({len(records)} records)")
            done += 1
            continue
        sha = write_jsonl_atomic(target, records, mtime=last_dt.timestamp())
        reg[src] = {"mtime": st.st_mtime, "size": st.st_size,
                    "target": target, "written_sha": sha}
        log(f"codex->claude: {os.path.basename(src)} -> {target} ({len(records)} records)")
        done += 1
    return done, skipped


# ---------------------------------------------------------------------------
# Claude -> Codex
# ---------------------------------------------------------------------------

def iter_claude_sessions():
    if not os.path.isdir(CLAUDE_PROJECTS):
        return
    for proj in os.listdir(CLAUDE_PROJECTS):
        pdir = os.path.join(CLAUDE_PROJECTS, proj)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if UUID_RE.match(name):
                yield os.path.join(pdir, name), name[:-6]


def block_to_text(block):
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        bt = block.get("type")
        if bt == "text":
            return block.get("text", "")
        if bt == "tool_result":
            c = block.get("content")
            if isinstance(c, list):
                return "\n".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text")
            return str(c or "")
    return ""


def convert_claude_to_codex(src_path, claude_sid):
    """Claude session -> (rollout_lines, first_dt, last_dt, codex_id). None if empty."""
    recs = read_jsonl(src_path)
    # loop guard: this claude session was itself imported from codex
    for r in recs[:5]:
        msg = r.get("message") or {}
        if r.get("isMeta") and isinstance(msg.get("content"), str) \
                and msg["content"].startswith("[Imported from OpenAI Codex CLI session"):
            return None
    cwd = branch = None
    first_dt = last_dt = None
    items = []  # ("user"|"assistant", text, ts)

    for r in recs:
        if r.get("type") not in ("user", "assistant") or r.get("isSidechain"):
            continue
        ts = r.get("timestamp")
        dt = parse_ts(ts)
        if dt:
            first_dt = first_dt or dt
            last_dt = dt
        cwd = cwd or r.get("cwd")
        branch = branch or r.get("gitBranch")
        msg = r.get("message") or {}
        content = msg.get("content")
        if r["type"] == "user":
            if r.get("isMeta"):
                continue
            if isinstance(content, str):
                t = content.strip()
                if t and not t.startswith(("<command-name>", "<local-command",
                                           "Caveat:", "<system-reminder>")):
                    items.append(("user", t, ts))
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        t = block_to_text(b).strip()
                        items.append(("assistant",
                                      f"[external_agent_tool_result]\n{cap(t)}\n"
                                      f"[/external_agent_tool_result]", ts))
                    else:
                        t = block_to_text(b).strip()
                        if t and not t.startswith(("<command-name>", "<local-command",
                                                   "Caveat:", "<system-reminder>")):
                            items.append(("user", t, ts))
        else:  # assistant
            for b in (content if isinstance(content, list) else []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append(("assistant", b["text"], ts))
                elif b.get("type") == "tool_use":
                    arg = json.dumps(b.get("input", {}), ensure_ascii=False, indent=None)
                    items.append(("assistant",
                                  f"[external_agent_tool_call: {b.get('name', 'tool')}]\n"
                                  f"{cap(arg)}\n[/external_agent_tool_call]", ts))

    if not any(k == "user" for k, _, _ in items) or \
       not any(k == "assistant" for k, _, _ in items):
        return None
    first_dt = first_dt or datetime.now(timezone.utc)
    last_dt = last_dt or first_dt
    codex_id = det_uuid7(f"claude:{claude_sid}", first_dt)

    def line(ltype, payload, ts):
        return {"timestamp": ts or now_iso(), "type": ltype, "payload": payload}

    meta_ts = first_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    out = [line("session_meta", {
        "id": codex_id, "timestamp": meta_ts, "cwd": cwd or "/",
        "originator": "codex-tui", "cli_version": codex_cli_version(),
        "source": "cli", "model_provider": "openai",
        "base_instructions": {"text": f"External import of Claude Code session "
                                      f"{claude_sid}. Tool activity appears as "
                                      f"[external_agent_tool_call]/"
                                      f"[external_agent_tool_result] text blocks."},
        "git": {"branch": branch} if branch else None,
    }, meta_ts)]
    out.append(line("event_msg", {
        "type": "task_started", "turn_id": "external-import-turn-1",
        "started_at": int(first_dt.timestamp()), "model_context_window": None,
        "collaboration_mode_kind": "default"}, meta_ts))

    last_agent = None
    for kind, text, ts in items:
        if kind == "user":
            out.append(line("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}, ts))
            out.append(line("event_msg", {
                "type": "user_message", "message": text,
                "local_images": [], "text_elements": []}, ts))
        else:
            out.append(line("response_item", {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}, ts))
            out.append(line("event_msg", {
                "type": "agent_message", "message": text,
                "phase": None, "memory_citation": None}, ts))
            last_agent = text

    end_ts = last_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    out.append(line("event_msg", {"type": "agent_message", "message": IMPORT_MARKER,
                                  "phase": None, "memory_citation": None}, end_ts))
    zero = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "total_tokens": 0}
    out.append(line("event_msg", {"type": "token_count",
                                  "info": {"total_token_usage": zero,
                                           "last_token_usage": zero,
                                           "model_context_window": None},
                                  "rate_limits": None}, end_ts))
    out.append(line("event_msg", {"type": "task_complete",
                                  "turn_id": "external-import-turn-1",
                                  "last_agent_message": last_agent,
                                  "completed_at": int(last_dt.timestamp())}, end_ts))
    return out, first_dt, last_dt, codex_id


_codex_version = None


def codex_cli_version():
    global _codex_version
    if _codex_version is None:
        try:
            with open(os.path.join(CODEX_HOME, "version.json")) as fh:
                _codex_version = json.load(fh).get("latest_version", "0.144.0")
        except (OSError, json.JSONDecodeError, AttributeError):
            _codex_version = "0.144.0"
    return _codex_version


def sync_claude_to_codex(state, args, guard):
    reg = state.setdefault("claude_to_codex", {})
    done = skipped = 0
    for src, sid in sorted(iter_claude_sessions()):
        if src in guard:
            continue
        st = os.stat(src)
        entry = reg.get(src)
        if args.session and args.session not in src:
            continue
        if not args.session:
            if st.st_mtime < args.since_epoch:
                continue
            if entry and entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size:
                continue
        conv = convert_claude_to_codex(src, sid)
        if conv is None:
            reg[src] = {"mtime": st.st_mtime, "size": st.st_size, "target": None,
                        "skipped": True}
            continue
        rollout, first_dt, last_dt, codex_id = conv
        target = os.path.join(
            CODEX_HOME, "sessions", first_dt.strftime("%Y/%m/%d"),
            f"rollout-{first_dt.strftime('%Y-%m-%dT%H-%M-%S')}-{codex_id}.jsonl")
        if entry and entry.get("target") == target and os.path.exists(target):
            if entry.get("written_sha") and sha256_file(target) != entry["written_sha"]:
                log(f"SKIP (diverged in Codex): {target}")
                skipped += 1
                continue
        if args.dry_run:
            log(f"DRY claude->codex: {src} -> {target} ({len(rollout)} lines)")
            done += 1
            continue
        sha = write_jsonl_atomic(target, rollout, mtime=last_dt.timestamp())
        reg[src] = {"mtime": st.st_mtime, "size": st.st_size,
                    "target": target, "written_sha": sha}
        log(f"claude->codex: {src} -> {os.path.basename(target)} ({len(rollout)} lines)")
        done += 1
    return done, skipped


# ---------------------------------------------------------------------------
# install / uninstall
#
# Both are idempotent and additive: they only ever touch lines and JSON objects
# that name this tool, so a hand-rolled entry is recognised (never duplicated)
# and unrelated cron jobs / hooks are left exactly as they were.
# ---------------------------------------------------------------------------

MEMORY_NAME = "memory-sync"
MEMORY_SCRIPT = "memory-sync.py"


def script_path():
    return os.path.realpath(os.path.abspath(__file__))


def _pythonw():
    """On Windows prefer pythonw.exe so the scheduled task never flashes a console."""
    exe = sys.executable
    if os.name == "nt":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def _command(target, log_path=None):
    if os.name == "nt":
        cmd = f'"{_pythonw()}" "{target}" --quiet'
    else:
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(target)} --quiet"
    if log_path:
        cmd += f" >> {shlex.quote(log_path)} 2>&1"
    return cmd


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
    return {
        "name": MEMORY_NAME,
        "file": MEMORY_SCRIPT,
        "path": os.path.join(os.path.dirname(script_path()), MEMORY_SCRIPT),
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
    stripped = line.strip()
    if stripped.startswith("#"):
        return stripped.startswith(f"# {spec['name']}")
    return spec["file"] in line


def hook_is_ours(hook, spec):
    return spec["file"] in str(hook.get("command", ""))


def _say(dry, msg):
    print(("would " if dry else "") + msg)


# ---- scheduler: cron (POSIX) ----------------------------------------------

def _crontab_read():
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return p.stdout.splitlines() if p.returncode == 0 else []


def _crontab_write(lines):
    body = "\n".join(lines).rstrip("\n") + "\n"
    subprocess.run(["crontab", "-"], input=body, text=True, check=True)


def _ensure_log(path):
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").close()
    except OSError as e:
        print(f"scheduler: could not create {path} ({e}); the job will still run")


def _minutes(schedule):
    """'*/5 * * * *' -> 5, for the Windows scheduler and for messages."""
    m = re.match(r"\*/(\d+)", schedule)
    return int(m.group(1)) if m else 2


def install_cron(dry, spec):
    if os.name == "nt":
        return install_task(dry, spec)
    if not shutil.which("crontab"):
        print(f"{spec['name']}: no `crontab` on PATH — skipped")
        return False
    lines = _crontab_read()
    if any(line_is_ours(l, spec) for l in lines):
        print(f"{spec['name']}: cron entry already present")
        return False
    entry = f"{spec['schedule']} {_command(spec['path'], spec['log'])}"
    if dry:
        _say(True, f"{spec['name']}: add cron entry:\n    {entry}")
        return True
    _ensure_log(spec["log"])
    _crontab_write(lines + ["", f"# {spec['name']} — {spec['blurb']}", entry])
    print(f"{spec['name']}: cron entry added ({spec['schedule']}), logging to {spec['log']}")
    return True


def uninstall_cron(dry, spec):
    if os.name == "nt":
        return uninstall_task(dry, spec)
    if not shutil.which("crontab"):
        return False
    lines = _crontab_read()
    keep = [l for l in lines if not line_is_ours(l, spec)]
    if len(keep) == len(lines):
        print(f"{spec['name']}: no cron entry found")
        return False
    while keep and not keep[-1].strip():  # tidy the blank we inserted above it
        keep.pop()
    n = len(lines) - len(keep)
    if dry:
        _say(True, f"{spec['name']}: remove {n} cron line(s)")
        return True
    if keep:
        _crontab_write(keep)
    else:
        subprocess.run(["crontab", "-r"], check=False)  # our lines were the only ones
    print(f"{spec['name']}: removed {n} cron line(s)")
    return True


# ---- scheduler: Task Scheduler (Windows) -----------------------------------

def _task_exists(spec):
    p = subprocess.run(["schtasks", "/Query", "/TN", spec["name"]],
                       capture_output=True, text=True)
    return p.returncode == 0


def install_task(dry, spec):
    if _task_exists(spec):
        print(f"{spec['name']}: scheduled task already present")
        return False
    every = _minutes(spec["schedule"])
    if dry:
        _say(True, f"{spec['name']}: create scheduled task (every {every} minutes)")
        return True
    subprocess.run(["schtasks", "/Create", "/TN", spec["name"], "/SC", "MINUTE",
                    "/MO", str(every), "/F", "/TR", _command(spec["path"])], check=True)
    print(f"{spec['name']}: scheduled task created (every {every} minutes)")
    return True


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


# ---- Claude Code hooks -----------------------------------------------------

def _hook_obj(event, spec):
    h = {"type": "command", "command": _command(spec["path"]), "timeout": 120, "async": True}
    if event == "SessionStart" and spec["status"]:
        h["statusMessage"] = spec["status"]
    return h


def _load_settings():
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise SystemExit(f"{CLAUDE_SETTINGS} is not valid JSON ({e}) — refusing to edit it")


def _save_settings(cfg):
    if os.path.exists(CLAUDE_SETTINGS):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(CLAUDE_SETTINGS, f"{CLAUDE_SETTINGS}.bak-{stamp}")
    os.makedirs(os.path.dirname(CLAUDE_SETTINGS), exist_ok=True)
    tmp = CLAUDE_SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CLAUDE_SETTINGS)


def install_hooks(dry, spec):
    cfg = _load_settings()
    hooks = cfg.setdefault("hooks", {})
    added = []
    for event in spec["events"]:
        groups = hooks.setdefault(event, [])
        if any(hook_is_ours(h, spec) for g in groups for h in g.get("hooks", [])):
            continue
        group = next((g for g in groups if g.get("matcher", "") == ""), None)
        if group is None:
            group = {"matcher": "", "hooks": []}
            groups.append(group)
        group.setdefault("hooks", []).append(_hook_obj(event, spec))
        added.append(event)
    if not added:
        print(f"{spec['name']}: hooks already present")
        return False
    if dry:
        _say(True, f"{spec['name']}: add {' + '.join(added)} hook(s) to {CLAUDE_SETTINGS}")
        return True
    _save_settings(cfg)
    print(f"{spec['name']}: added {' + '.join(added)} hook(s) to {CLAUDE_SETTINGS}")
    return True


def uninstall_hooks(dry, spec):
    cfg = _load_settings()
    hooks = cfg.get("hooks") or {}
    removed = []
    for event in list(hooks):
        groups = hooks.get(event) or []
        kept_groups = []
        touched = False
        for g in groups:
            hs = g.get("hooks", [])
            keep = [h for h in hs if not hook_is_ours(h, spec)]
            if len(keep) != len(hs):
                touched = True
                removed.append(event)
                if not keep:
                    continue  # drop a group we emptied; leave others alone
                g = dict(g, hooks=keep)
            kept_groups.append(g)
        if kept_groups:
            hooks[event] = kept_groups
        elif touched:
            hooks.pop(event)
    if not removed:
        print(f"{spec['name']}: no hooks found")
        return False
    if not hooks:
        cfg.pop("hooks", None)
    if dry:
        _say(True, f"{spec['name']}: remove {' + '.join(removed)} hook(s) from {CLAUDE_SETTINGS}")
        return True
    _save_settings(cfg)
    print(f"{spec['name']}: removed {' + '.join(removed)} hook(s) from {CLAUDE_SETTINGS}")
    return True


# ---- self-update -----------------------------------------------------------

RAW_BASE = "https://raw.githubusercontent.com/faratech/agent-session-sync"


ENTRY_POINT = b"sys.exit(main())"


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": f"{TOOL_NAME}-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    declared = resp.headers.get("Content-Length")
    if declared is not None and len(data) != int(declared):
        raise OSError(f"short read: got {len(data)} of {declared} bytes")
    return data


def _vet(source, filename):
    """Refuse anything that isn't the whole script we asked for. A proxy, a login
    page, or a truncated body must never land on disk as an executable we then run.

    Truncation is the subtle one: the first few KB of either script is still valid
    Python, so it compiles, installs, and then silently does nothing on every cron
    tick. Hence the tail check — not just the head.
    """
    if not source.startswith(b"#!"):
        return "no shebang — not a script"
    stem = os.path.basename(filename).rsplit(".", 1)[0]
    if b'"""' + stem.encode() not in source:
        return f"docstring does not identify it as {stem}"
    if not source.rstrip().endswith(ENTRY_POINT):
        return "truncated — does not end with the entry point"
    try:
        compile(source.decode("utf-8"), filename, "exec")
    except (SyntaxError, UnicodeDecodeError) as e:
        return f"does not compile: {e}"
    return None


def _install_file(path, source, dry):
    """Replace `path` with `source`, atomically, keeping a .bak. Returns True if changed."""
    if os.path.exists(path) and sha256_file(path) == hashlib.sha256(source).hexdigest():
        print(f"update: {os.path.basename(path)} already up to date")
        return False
    if dry:
        _say(True, f"update: replace {path} ({len(source)} bytes)")
        return True
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".new"
    with open(tmp, "wb") as fh:
        fh.write(source)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)   # atomic: a concurrent cron tick sees one version or the other
    print(f"update: {os.path.basename(path)} updated (previous saved as .bak)")
    return True


def do_update(args):
    here = os.path.dirname(script_path())
    if os.path.isdir(os.path.join(here, ".git")):
        print(f"note: {here} is a git checkout — `git pull` is the better move there.\n")

    wanted = [script_path()]
    mem = os.path.join(here, MEMORY_SCRIPT)
    if args.memories or os.path.exists(mem):
        wanted.append(mem)

    print(f"{TOOL_NAME}: updating from {RAW_BASE} @ {args.ref}\n")
    fetched = []
    for path in wanted:                       # fetch and vet everything before writing anything
        name = os.path.basename(path)
        url = f"{RAW_BASE}/{args.ref}/{name}"
        try:
            source = _fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            print(f"update failed: cannot fetch {url}: {e}", file=sys.stderr)
            return 1
        bad = _vet(source, name)
        if bad:
            print(f"update failed: {url}: {bad}", file=sys.stderr)
            return 1
        fetched.append((path, source))

    changed = False
    for path, source in fetched:
        changed |= _install_file(path, source, args.dry_run)

    if args.dry_run:
        print("\n(dry run — nothing was written)")
        return 0
    if changed:
        print("\nThe new code takes effect on the next run; this process is still the old one.")
    print()
    return do_install(args)                   # idempotent: adds only what is missing


# ---- drivers ---------------------------------------------------------------

def _specs(args):
    specs = [session_spec()]
    if args.memories:
        mem = memory_spec()
        if not os.path.exists(mem["path"]):
            raise SystemExit(f"--memories: {mem['path']} not found "
                             f"(it ships alongside this script)")
        specs.append(mem)
    return specs


def do_install(args):
    dry = args.dry_run
    print(f"{TOOL_NAME}: installing {script_path()}\n")
    if not dry:
        os.makedirs(STATE_DIR, exist_ok=True)
    changed = False
    for spec in _specs(args):
        if not args.no_cron:
            changed |= install_cron(dry, spec)
        if not args.no_hooks:
            changed |= install_hooks(dry, spec)
    print(f"\nstate: {STATE_DIR}")
    if dry:
        print("\n(dry run — nothing was written)")
    elif changed:
        print("\nDone. The first sync runs on the next scheduler tick; run it now with:\n"
              f"    {shlex.quote(sys.executable)} {shlex.quote(script_path())} --dry-run -v")
        print("Claude Code loads hooks at startup — open /hooks or start a new session.")
    else:
        print("\nNothing to do — already installed.")
    return 0


def do_uninstall(args):
    dry = args.dry_run
    specs = _specs(args)
    print(f"{TOOL_NAME}: uninstalling\n")
    for spec in specs:
        if not args.no_cron:
            uninstall_cron(dry, spec)
        if not args.no_hooks:
            uninstall_hooks(dry, spec)
    if args.purge:
        for path in [STATE_DIR] + [s["log"] for s in specs]:
            if not os.path.exists(path):
                continue
            _say(dry, f"purge: remove {path}")
            if not dry:
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) \
                    else os.remove(path)
    else:
        print(f"\nstate kept at {STATE_DIR} (re-run with --purge to delete)")
    print("\nSynced sessions and memory files were left in place — "
          "this only removes the automation.")
    if dry:
        print("(dry run — nothing was written)")
    return 0


# ---------------------------------------------------------------------------

def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to-claude", action="store_true", help="only sync codex -> claude")
    ap.add_argument("--to-codex", action="store_true", help="only sync claude -> codex")
    ap.add_argument("--days", type=float, default=30, help="only sources modified in the last N days (default 30)")
    ap.add_argument("--session", help="force-sync any source whose path contains this id, ignoring age/state")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no summary line (for cron)")

    setup = ap.add_argument_group("setup")
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true",
                      help="add the scheduler entry and Claude Code hooks if absent")
    mode.add_argument("--uninstall", action="store_true",
                      help="remove them if present (synced sessions are kept)")
    mode.add_argument("--update", action="store_true",
                      help="fetch the latest scripts from GitHub, then --install")
    setup.add_argument("--ref", default="main", metavar="REF",
                       help="branch, tag or commit to update from (default: main)")
    setup.add_argument("--memories", action="store_true",
                       help="also un/install/update the opt-in memory-sync.py companion")
    setup.add_argument("--no-cron", action="store_true", help="skip the scheduler entry")
    setup.add_argument("--no-hooks", action="store_true", help="skip the Claude Code hooks")
    setup.add_argument("--purge", action="store_true",
                       help="with --uninstall: also delete the state dir and log")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if args.update:
        return do_update(args)
    if args.install:
        return do_install(args)
    if args.uninstall:
        return do_uninstall(args)
    if args.purge or args.no_cron or args.no_hooks or args.memories:
        ap.error("--memories/--purge/--no-cron/--no-hooks only apply to "
                 "--install, --uninstall or --update")
    if args.ref != "main":
        ap.error("--ref only applies to --update")

    args.since_epoch = datetime.now(timezone.utc).timestamp() - args.days * 86400

    os.makedirs(STATE_DIR, exist_ok=True)
    lock = acquire_lock(LOCK_FILE)
    if lock is None:
        return 0  # another run in progress

    state = load_state()
    c2c = cl2c = (0, 0)
    if not args.to_codex:
        c2c = sync_codex_to_claude(state, args, produced_targets(state))
    if not args.to_claude:
        # recompute the guard so files written by the pass above are excluded
        cl2c = sync_claude_to_codex(state, args, produced_targets(state))
    if not args.dry_run:
        save_state(state)
    if not args.quiet:
        print(f"codex->claude: {c2c[0]} synced, {c2c[1]} diverged-skip | "
              f"claude->codex: {cl2c[0]} synced, {cl2c[1]} diverged-skip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
