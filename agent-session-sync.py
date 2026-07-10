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
import sys
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


STATE_DIR = default_state_dir()
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_FILE = os.path.join(STATE_DIR, "lock")

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
    args = ap.parse_args()
    VERBOSE = args.verbose
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
