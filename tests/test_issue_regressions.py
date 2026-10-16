import contextlib
import dataclasses
import io
import json
import os
import re
import shlex
import signal
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from test_sync import Workspace, IDS
from agent_session_sync import automation, legacy, memory_sync
from agent_session_sync.adapters import Antigravity, Muse, SessionRef, get_adapters
from agent_session_sync.automation import setup
from agent_session_sync.cli import main
from agent_session_sync.discovery import Installation, config_file, executable_candidates, find_executable, load_config, state_dir
from agent_session_sync.engine import Engine
from agent_session_sync.io import SyncError, atomic_write, digest, json_bytes, read_records, resolved_workspace
from agent_session_sync.model import Event, instant

REPO = Path(__file__).resolve().parent.parent


def js_hash_base36(value):
    """Claude Code's `(h<<5)-h+code|0` over UTF-16 code units, then Math.abs(h).toString(36)."""
    data = value.encode("utf-16-le", "surrogatepass")
    h = 0
    for i in range(0, len(data), 2):
        h = (h << 5) - h + int.from_bytes(data[i:i + 2], "little")
        h = (h + 2 ** 31) % 2 ** 32 - 2 ** 31
    h = abs(h)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        h, r = divmod(h, 36)
        out = digits[r] + out
        if not h:
            return out


class IssueRegressionTests(Workspace):
    def write_records(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def cli_config(self, **overrides):
        harnesses = {i.harness: {"home": i.home, "executable": sys.executable, "settings": str(self.base / i.harness / "settings.json")}
                     for i in self.installations}
        for name, options in overrides.items():
            harnesses[name].update(options)
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": harnesses}))
        return ["--state-dir", str(self.state), "--config", str(config)]

    def test_codex_prompt_with_image_attachment_is_kept(self):
        # Issue #36
        sid = IDS["codex"]
        path = self.write_records(self.base / "images" / ("rollout-x-" + sid + ".jsonl"), [
            {"timestamp": "2026-10-15T10:00:00Z", "type": "session_meta", "payload": {"id": sid, "cwd": str(self.project)}},
            {"timestamp": "2026-10-15T10:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "What is in this screenshot?",
                                                                               "images": ["data:image/png;base64,AAAA"]}},
            {"timestamp": "2026-10-15T10:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "What is in this screenshot?"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}},
            {"timestamp": "2026-10-15T10:00:06Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "A login form."}]}},
        ])
        session = self.adapters["codex"].read(SessionRef(sid, str(path)))
        users = [e.text for e in session.events if e.kind == "user"]
        self.assertEqual(users, ["What is in this screenshot?"])
        self.assertEqual(session.events[-1].text, "A login form.")

    def test_claude_new_root_branch_excludes_abandoned_history(self):
        # Issue #37
        sid = IDS["claude"]
        def message(uuid, parent, kind, content):
            return {"uuid": uuid, "parentUuid": parent, "type": kind, "cwd": str(self.project), "sessionId": sid,
                    "timestamp": "2026-10-15T10:00:00Z", "message": {"role": kind, "content": content}}
        path = self.write_records(self.base / "rewind" / (sid + ".jsonl"), [
            message("a1", None, "user", "Delete the production database."),
            message("a2", "a1", "assistant", "Done, dropped prod."),
            message("b1", None, "user", "List the tables in the staging database."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events], [("user", "List the tables in the staging database.")])

    def test_antigravity_explicit_unknown_mapping_overrides_recorded_workspace(self):
        # Issue #38
        sid = IDS["agy"]
        home = self.base / "agy-explicit"
        path = self.write_records(home / "brain" / sid / ".system_generated/logs/transcript_full.jsonl", [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-10-15T10:03:00Z",
             "workspacePaths": [str(self.base / "wrong-project")], "content": "<USER_REQUEST>hi</USER_REQUEST>"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-10-15T10:03:01Z", "content": "ok"},
        ])
        installation = Installation("agy", "default", str(home))
        mapped = Antigravity(installation, {"workspace_map": {sid: str(self.project)}}).read(SessionRef(sid, str(path)))
        self.assertEqual(mapped.workspace, str(self.project))
        unknown = Antigravity(installation, {"workspace_map": {sid: ""}}).read(SessionRef(sid, str(path)))
        self.assertFalse(unknown.workspace)

    def test_muse_export_is_titled_by_first_prompt_not_filename(self):
        # Issue #39
        path = self.base / "my-export.json"
        path.write_text(json.dumps({"export_schema_version": 1, "events": [
            {"envelope": {"schema_version": 1, "sequence": 1, "recorded_at": 1792058521000, "stream": {"kind": "session", "id": "sess-abc"},
                          "payload": {"kind": "run", "run_id": "r1", "event": {"kind": "started", "prompt": "Fix the login bug."}}}},
            {"envelope": {"schema_version": 1, "sequence": 2, "recorded_at": 1792058522000,
                          "payload": {"kind": "event", "run_id": "r1", "event": {"kind": "assistant_message_committed", "text": "Fixed."}}}},
        ]}), encoding="utf-8")
        session = self.adapters["muse"].read(SessionRef(path.stem, str(path)))
        self.assertEqual(session.native_id, "sess-abc")
        self.assertEqual(session.title, "Fix the login bug.")

    @unittest.skipIf(os.name == "nt", "extensionless executables are not found through PATHEXT on Windows")
    def test_configured_executable_symlink_is_kept_across_harness_updates(self):
        # Issue #40
        versions = self.base / "versions"
        old = versions / "1.0/claude"
        new = versions / "2.0/claude"
        for target in (old, new):
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            target.chmod(0o755)
        link = self.base / "bin/claude"
        link.parent.mkdir()
        try:
            link.symlink_to(old)
        except OSError:
            self.skipTest("symlinks unavailable")
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {"claude": {"executable": str(link)}}}))
        loaded = load_config(config)
        self.assertEqual(loaded["harnesses"]["claude"]["executable"], str(link))
        setup({}, self.state, loaded, no_cron=True, no_hooks=True)
        saved = load_config(self.state / "automation-config.json")["harnesses"]["claude"]["executable"]
        self.assertEqual(saved, str(link))
        # The harness self-updates: the launcher is retargeted and the old version removed.
        link.unlink()
        link.symlink_to(new)
        old.unlink()
        self.assertIsNotNone(find_executable("claude", saved))

    def test_munge_cwd_matches_claude_for_astral_and_long_paths(self):
        # Issue #41
        for munge in (legacy.munge_cwd, memory_sync.munge_cwd):
            with self.subTest(module=munge.__module__):
                self.assertEqual(munge("/home/me/proj_x"), "-home-me-proj-x")
                self.assertEqual(munge("/home/me/\U0001F680rocket"), "-home-me---rocket")
                long_path = "/srv/" + "/".join(["deeply-nested-directory-name"] * 8)
                suffix = js_hash_base36(long_path)
                expected = re.sub(r"[^a-zA-Z0-9]", "-", long_path)[:200] + "-" + suffix
                self.assertEqual(munge(long_path), expected)
                self.assertEqual(len(munge(long_path)), 200 + 1 + len(suffix))

    def test_rehandoff_and_repair_survive_codex_version_and_executable_changes(self):
        # Issue #42
        self.addCleanup(setattr, legacy, "_codex_version", None)
        engine = self.engine()
        source = self.read("muse")
        first = engine.handoff(source, "codex")
        Path(legacy.CODEX_HOME, "version.json").write_text(json.dumps({"latest_version": "9.9.9"}), encoding="utf-8")
        legacy._codex_version = None
        self.assertEqual(engine.handoff(source, "codex")["id"], first["id"])
        # Mirror repair after the Codex update leaves the ledger matching the file on disk.
        Path(first["target"]).unlink()
        engine.sync(source="muse", destination="codex", days=None)
        self.assertFalse(engine.errors, engine.errors)
        self.assertEqual(engine.store.transfer(first["id"])["target_sha"], digest(Path(first["target"]).read_bytes()))
        # Discovery finds the destination executable at a new path on a later run.
        claude = engine.handoff(source, "claude")
        moved = [dataclasses.replace(i, executable=str(self.base / "opt/claude")) if i.harness == "claude" else i for i in self.installations]
        later = Engine(get_adapters(moved, {}), self.state)
        self.addCleanup(later.close)
        self.assertEqual(later.handoff(source, "claude")["id"], claude["id"])

    def test_lone_surrogate_escape_does_not_block_handoff(self):
        # Issue #43
        path = self.paths["claude"]
        last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        line = json.dumps({"type": "user", "uuid": "u-surrogate", "parentUuid": last["uuid"], "cwd": str(self.project),
                           "sessionId": IDS["claude"], "timestamp": "2026-10-15T12:00:00Z",
                           "message": {"role": "user", "content": "truncated emoji: \ud83d"}})
        self.assertIn("\\ud83d", line)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        session = self.read("claude")
        self.assertTrue(session.revision)
        self.assertTrue(any(e.text.startswith("truncated emoji:") for e in session.events))
        engine = self.engine()
        self.assertTrue(engine.sync(source="claude", days=None))
        self.assertFalse(engine.errors, engine.errors)

    def test_timestamps_with_any_fraction_length_parse(self):
        # Issue #44: fromisoformat before 3.11 only accepts 3- or 6-digit fractions.
        cases = {
            "2026-10-15T10:00:00.1Z": 100000,
            "2026-10-15T10:00:00.12Z": 120000,
            "2026-10-15T10:00:00.1234Z": 123400,
            "2026-10-15T10:00:00.12345+00:00": 123450,
            "2026-10-15T10:00:00.1234567+00:00": 123456,
            "2026-10-15T10:00:00.123456789Z": 123456,
            "2026-10-15T12:00:00.12+02:00": 120000,
        }
        for value, microsecond in cases.items():
            with self.subTest(value=value):
                Event("1", "user", "hi", value)
                self.assertEqual(instant(value), datetime(2026, 10, 15, 10, 0, 0, microsecond, timezone.utc))

    @unittest.skipIf(os.name == "nt", "fake harness is a POSIX script")
    def test_open_releases_sync_lock_before_launching_harness(self):
        # Issue #45
        marker = self.base / "launched"
        fake = self.base / "bin/muse"
        fake.parent.mkdir()
        fake.write_text("""#!{python}
import sys
if "--version" in sys.argv:
    print("muse 1.0")
    sys.exit(0)
sys.path.insert(0, {repo!r})
from agent_session_sync.io import SyncError
from agent_session_sync.store import single_flight
try:
    with single_flight({state!r}):
        status = "acquired"
except SyncError as exc:
    status = str(exc)
open({marker!r}, "w").write(status)
""".format(python=sys.executable, repo=str(REPO), state=str(self.state), marker=str(marker)), encoding="utf-8")
        fake.chmod(0o755)
        prefix = self.cli_config(muse={"executable": str(fake)})
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(prefix + ["open", "muse", "--project", str(self.project)])
        self.assertEqual(code, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "acquired")

    def test_install_with_explicit_config_points_automation_at_captured_roots(self):
        # Issue #46
        for adapter in self.adapters.values():
            adapter.options["settings"] = str(self.base / adapter.endpoint / "settings.json")
        user_config = self.base / "user-config.json"
        user_config.write_text("{}", encoding="utf-8")
        setup(self.adapters, self.state, {}, config_path=user_config, no_cron=True)
        settings = json.loads(Path(self.adapters["claude"].options["settings"]).read_text(encoding="utf-8"))
        command = shlex.split(settings["hooks"]["SessionStart"][-1]["hooks"][0]["command"])
        captured = Path(command[command.index("--config") + 1])
        self.assertEqual(captured, self.state / "automation-config.json")
        self.assertEqual(load_config(captured)["harnesses"]["claude"]["home"], str(self.adapters["claude"].home.resolve()))

    def test_session_selector_matches_id_prefix_not_substring(self):
        # Issue #47
        template = self.paths["claude"].read_text(encoding="utf-8").splitlines()
        def claude_session(sid):
            records = [dict(json.loads(line), sessionId=sid) for line in template]
            self.write_records(self.paths["claude"].with_name(sid + ".jsonl"), records)
        first, contains, defaults = "abcd1234-0000-4000-8000-000000000001", "99abcd12-3400-4000-8000-000000000002", "defa0000-0000-4000-8000-000000000003"
        for sid in (first, contains, defaults):
            claude_session(sid)
        engine = self.engine()
        self.assertEqual(engine.select("abcd1").native_id, first)
        self.assertEqual(engine.select("defa").native_id, defaults)

    @unittest.skipIf(os.name == "nt", "XDG paths are POSIX-only")
    def test_empty_xdg_variables_are_treated_as_unset(self):
        # Issue #48
        home = self.base / "home"
        environ = {k: v for k, v in os.environ.items() if k not in ("AGENT_SESSION_SYNC_STATE_DIR", "AGENT_SESSION_SYNC_CONFIG")}
        environ.update(HOME=str(home), XDG_STATE_HOME="", XDG_CONFIG_HOME="")
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch("os.geteuid", return_value=1000, create=True):
            self.assertEqual(state_dir(), home / ".local/state/agent-session-sync")
            self.assertEqual(config_file(), home / ".config/agent-session-sync/config.json")

    @unittest.skipIf(os.name == "nt", "cron is POSIX-only")
    def test_percent_in_cron_command_is_escaped(self):
        # Issue #49
        lines = []
        state = self.base / "state%Y"
        with mock.patch("agent_session_sync.automation.shutil.which", return_value="/usr/bin/crontab"), \
             mock.patch.object(legacy, "_crontab_read", return_value=[]), \
             mock.patch.object(legacy, "_crontab_write", side_effect=lambda value: lines.extend(value)):
            setup({}, state, {}, no_hooks=True)
        jobs = [line for line in lines if not line.startswith("#")]
        self.assertEqual(len(jobs), 1)
        self.assertIn("state\\%Y", jobs[0])
        self.assertNotIn("%", jobs[0].replace("\\%", ""))

    def test_export_refreshes_explicit_output_path(self):
        # Issue #50
        prefix = self.cli_config()
        output = self.base / "conversation.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(prefix + ["export", IDS["claude"], "--output", str(output)]), 0)
        last = json.loads(self.paths["claude"].read_text(encoding="utf-8").splitlines()[-1])
        self.append(self.paths["claude"], {"type": "user", "uuid": "u-new", "parentUuid": last["uuid"], "sessionId": IDS["claude"],
                                           "cwd": str(self.project), "timestamp": "2026-10-15T11:00:00Z",
                                           "message": {"role": "user", "content": "One more question."}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(prefix + ["export", IDS["claude"], "--output", str(output)]), 0)
        self.assertIn("One more question.", output.read_text(encoding="utf-8"))

    def test_uninstall_with_nothing_installed_says_nothing_to_remove(self):
        # Issue #51
        prefix = self.cli_config()
        home = self.base / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(prefix + ["uninstall", "--no-cron"]), 0)
        self.assertEqual(output.getvalue().strip(), "Nothing to remove.")


    def claude_message(self, uuid, parent, kind, content, **extra):
        return dict({"uuid": uuid, "parentUuid": parent, "type": kind, "cwd": str(self.project), "sessionId": IDS["claude"],
                     "timestamp": "2026-10-15T10:00:00Z", "message": {"role": kind, "content": content}}, **extra)

    def test_claude_compact_boundary_follows_logical_parent(self):
        # Issue #52
        sid = IDS["claude"]
        boundary = {"uuid": "b1", "parentUuid": None, "logicalParentUuid": "a1", "type": "system", "subtype": "compact_boundary",
                    "content": "Conversation compacted", "cwd": str(self.project), "sessionId": sid, "timestamp": "2026-10-15T10:00:00Z"}
        summary = self.claude_message("s1", "b1", "user", "This session is being continued from a previous conversation...", isCompactSummary=True)
        tail = [summary, self.claude_message("u2", "s1", "user", "Now add tests."),
                self.claude_message("a2", "u2", "assistant", "Tests added.")]
        path = self.write_records(self.base / "compact" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Fix the login bug."),
            self.claude_message("a1", "u1", "assistant", "Fixed it."),
            boundary] + tail)
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events], [
            ("user", "Fix the login bug."), ("assistant", "Fixed it."), ("user", "Now add tests."), ("assistant", "Tests added.")])
        self.assertEqual(session.title, "Fix the login bug.")
        # Issue #67: a logical link into the already walked chain (as real Claude Code writes it)
        # is not a cycle; history resumes at the last conversation record before the boundary.
        rewritten = self.write_records(self.base / "compact-rewritten" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Fix the login bug."),
            self.claude_message("a1", "u1", "assistant", "Fixed it."),
            dict(boundary, logicalParentUuid="u2")] + tail)
        session = self.adapters["claude"].read(SessionRef(sid, str(rewritten)))
        self.assertEqual([e.text for e in session.events], ["Fix the login bug.", "Fixed it.", "Now add tests.", "Tests added."])
        # Without earlier history in the file, the summary is all that remains.
        resumed = self.write_records(self.base / "compact-resumed" / (sid + ".jsonl"), [dict(boundary, logicalParentUuid="u2")] + tail)
        session = self.adapters["claude"].read(SessionRef(sid, str(resumed)))
        self.assertEqual([e.text for e in session.events],
                         ["This session is being continued from a previous conversation...", "Now add tests.", "Tests added."])

    def test_portable_import_accepts_lone_surrogate_escape(self):
        # Issue #53
        prefix = self.cli_config()
        bundle = self.base / "bundle.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(prefix + ["export", IDS["claude"], "--output", str(bundle)]), 0)
        document = json.loads(bundle.read_text(encoding="utf-8"))
        document["events"][0]["text"] = "truncated emoji: \ud83d"
        bundle.write_text(json.dumps(document), encoding="ascii")
        self.assertIn("\\ud83d", bundle.read_text(encoding="ascii"))
        # The second import re-reads the archive written by the first.
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(prefix + ["import", str(bundle)]), 0, errors.getvalue())
        archives = list((self.state / "archives").rglob("*.json"))
        self.assertTrue(any("truncated emoji: \ufffd" in p.read_text(encoding="utf-8") for p in archives))

    def test_cached_document_with_lone_surrogate_is_scrubbed(self):
        # Issue #54
        engine = self.engine()
        self.assertTrue(any(s.harness == "claude" for s in engine.scan(days=None)))
        engine.close()
        database = sqlite3.connect(str(self.state / "sessions.sqlite3"))
        with database:
            rows = database.execute("SELECT key, document FROM sessions WHERE key LIKE 'claude:%'").fetchall()
            self.assertEqual(len(rows), 1)
            key, document = rows[0]
            self.assertIn("Fix the login bug.", document)
            # An older build cached the unscrubbed escape.
            database.execute("UPDATE sessions SET document=? WHERE key=?", (document.replace("Fix the login bug.", "Fix the login bug. \\ud83d"), key))
        database.close()
        engine = self.engine()
        session = next(s for s in engine.scan(days=None) if s.harness == "claude")
        self.assertEqual(session.events[0].text, "Fix the login bug. \ufffd")
        self.assertTrue(session.revision)
        engine.sync(source="claude", destination="codex", days=None)
        self.assertFalse(engine.errors, engine.errors)

    def test_crash_recovery_replaces_target_matching_planned_sha(self):
        # Issue #55
        self.addCleanup(setattr, legacy, "_codex_version", None)
        engine = self.engine()
        source = self.read("muse")
        item, files = engine.plan(source, "codex")
        # The run dies after saving its intent and writing the native target.
        engine.store.save_transfer(item)
        atomic_write(item["target"], files[item["target"]])
        Path(legacy.CODEX_HOME, "version.json").write_text(json.dumps({"latest_version": "9.9.9"}), encoding="utf-8")
        legacy._codex_version = None
        retry, retry_files = engine.plan(source, "codex")
        self.assertEqual(retry["id"], item["id"])
        self.assertNotEqual(retry_files[retry["target"]], files[item["target"]])
        result = engine.handoff(source, "codex")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(Path(result["target"]).read_bytes(), retry_files[retry["target"]])
        self.assertEqual(engine.store.transfer(item["id"])["target_sha"], digest(retry_files[retry["target"]]))

    def test_empty_windows_appdata_variables_yield_no_relative_candidates(self):
        # Issue #56
        home = self.base / "home"
        environ = {"LOCALAPPDATA": "", "APPDATA": "", "ProgramFiles": "", "PNPM_HOME": ""}
        candidates = executable_candidates("codex", platform="nt", environ=environ, home=home)
        self.assertTrue(candidates)
        self.assertEqual([str(c) for c in candidates if not c.is_absolute()], [])
        self.assertIn(home / "AppData/Roaming/npm/codex.exe", candidates)
        self.assertIn(home / "AppData/Local/Microsoft/WindowsApps/codex.exe", candidates)

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "filesystem paths must be valid Unicode")
    def test_non_utf8_workspace_path_raises_clear_error(self):
        # Issue #57
        path = str(self.base / "proj\udcff")
        with self.assertRaises(SyncError) as raised:
            resolved_workspace(path)
        self.assertIn("UTF-8", str(raised.exception))
        self.assertIn("proj", str(raised.exception))

    def test_uninstall_leaves_settings_without_our_hooks_unchanged(self):
        # Issue #58
        prefix = self.cli_config()
        home = self.base / "home"
        settings = Path(self.base / "claude" / "settings.json")
        for content in ({"model": "opus", "hooks": {}}, {"hooks": {"Stop": []}}):
            with self.subTest(content=content):
                settings.write_text(json.dumps(content), encoding="utf-8")
                before = settings.read_bytes()
                with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(prefix + ["uninstall", "--no-cron"]), 0)
                self.assertEqual(output.getvalue().strip(), "Nothing to remove.")
                self.assertEqual(settings.read_bytes(), before)
                self.assertEqual(list(settings.parent.glob("settings.json.bak-*")), [])

    def test_uninstall_without_crontab_does_not_claim_hooks_still_sync(self):
        # Issue #59
        prefix = self.cli_config()
        home = self.base / "home"
        which = __import__("shutil").which
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}), \
             mock.patch("agent_session_sync.automation.shutil.which", side_effect=lambda name, *a, **k: None if name == "crontab" else which(name, *a, **k)), \
             mock.patch.object(legacy, "_crontab_read", side_effect=AssertionError("crontab read")), \
             mock.patch.object(legacy, "_crontab_write", side_effect=AssertionError("crontab write")), \
             mock.patch.object(legacy, "_task_exists", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(prefix + ["uninstall", "--no-hooks"]), 0)
        self.assertNotIn("crontab unavailable", output.getvalue())
        self.assertEqual(output.getvalue().strip(), "Nothing to remove.")

    def test_launch_ignores_ctrl_c_while_harness_runs(self):
        # Issue #60
        prefix = self.cli_config()
        before = signal.getsignal(signal.SIGINT)
        during = []
        def call(command, **kwargs):
            handler = signal.getsignal(signal.SIGINT)
            during.append(handler)
            try:
                if callable(handler):
                    handler(signal.SIGINT, None)  # Ctrl-C reaching the wrapper must not raise.
            except KeyboardInterrupt:
                during.append("KeyboardInterrupt")
            return 7
        with mock.patch("agent_session_sync.cli.subprocess.call", side_effect=call), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(prefix + ["open", "muse", "--project", str(self.project)])
        self.assertEqual(code, 7)
        self.assertNotIn("KeyboardInterrupt", during)
        self.assertEqual(len(during), 1)
        self.assertNotIn(during[0], (signal.SIG_IGN, signal.SIG_DFL, signal.default_int_handler, None))
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_install_reports_captured_configuration_changes(self):
        # Issue #61
        def install(config):
            return setup({}, self.state, config, no_cron=True, no_hooks=True)
        captured = str(self.state / "automation-config.json")
        self.assertIn("capture configuration: " + captured, install({"harnesses": {"claude": {"budget": 1}}}))
        self.assertEqual([c for c in install({"harnesses": {"claude": {"budget": 1}}}) if c.startswith("capture configuration")], [])
        self.assertIn("capture configuration: " + captured, install({"harnesses": {"claude": {"budget": 2}}}))

    def test_schtasks_uses_task_xml_for_long_commands(self):
        # Issue #62
        short = ["C:\\Python\\pythonw.exe", "C:\\sync.py", "sync", "--quiet"]
        arguments, xml = automation.schtasks_create("agent-session-sync", short, 15)
        self.assertIsNone(xml)
        self.assertEqual(arguments[arguments.index("/TR") + 1], automation.shell_command(short, windows=True))
        base = "C:\\Users\\a.long.user.name\\AppData\\Local\\Programs\\Python\\Python312"
        state = "C:\\Users\\a.long.user.name\\AppData\\Local\\agent-session-sync\\state & <odd> \"name\""
        long = [base + "\\pythonw.exe", base + "\\Lib\\site-packages\\agent-session-sync.py",
                "--state-dir", state, "--config", state + "\\automation-config.json", "sync", "--quiet"]
        self.assertGreater(len(automation.shell_command(long, windows=True)), automation.SCHTASKS_TR_LIMIT)
        arguments, xml = automation.schtasks_create("agent-session-sync", long, 15)
        self.assertIn("/XML", arguments)
        self.assertNotIn("/TR", arguments)
        root = ElementTree.fromstring(xml.encode("utf-16"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, long[0])
        parsed = [m.replace('""', '"') for m in re.findall(r'"((?:[^"]|"")*)"', root.find(".//t:Exec/t:Arguments", ns).text)]
        self.assertEqual(parsed, long[1:])
        self.assertEqual(root.find(".//t:Repetition/t:Interval", ns).text, "PT15M")

    def test_torn_mid_file_jsonl_line_keeps_trailing_record(self):
        # Issue #63
        path = self.base / "torn.jsonl"
        path.write_text('{"type":"user","uuid":"u1"}\n{"type":"x","ses{"type":"user","uuid":"u2"}\n{"type":"user","uuid":"u3"}\n', encoding="utf-8")
        records, issues, _ = read_records(path)
        self.assertEqual([r["uuid"] for r in records], ["u1", "u2", "u3"])
        self.assertTrue(any("torn" in issue for issue in issues), issues)
        # Issue #66: a record cut short is dropped once the writer is seen to continue.
        path.write_text('{"type":"user","uuid":"u1"}\n{"type":"x","ses\n{"type":"user","uuid":"u3"}\n', encoding="utf-8")
        records, issues, _ = read_records(path)
        self.assertEqual([r["uuid"] for r in records], ["u1", "u3"])
        self.assertTrue(any("cut short at line 2" in issue for issue in issues), issues)
        # Malformed content, and a cut-off final record, still refuse conversion.
        for body in ('{"type":"user","uuid":"u1"}\nnot json\n{"type":"user","uuid":"u3"}\n', '{"type":"user","uuid":"u1"}\n{"type":"x","ses\n'):
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(SyncError):
                read_records(path)

    def test_claude_task_notifications_are_not_user_prompts(self):
        # Issue #64
        sid = IDS["claude"]
        notice = "<task-notification>\n<task-id>bx1</task-id>\n<status>completed</status>\n</task-notification>"
        path = self.write_records(self.base / "notify" / (sid + ".jsonl"), [
            self.claude_message("n1", None, "user", notice, origin={"kind": "task-notification"}, promptSource="system"),
            self.claude_message("a1", "n1", "assistant", "The build finished."),
            self.claude_message("n2", "a1", "user", [{"type": "text", "text": "Background task bx2 failed."}], origin={"kind": "task-notification"}),
            self.claude_message("u1", "n2", "user", "Now fix the tests."),
            self.claude_message("a2", "u1", "assistant", "Done."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([e.text for e in session.events if e.kind == "user"], ["Now fix the tests."])
        self.assertEqual(session.title, "Now fix the tests.")

    def test_codex_user_message_items_are_prompts(self):
        # Issue #65: current Codex builds record prompts as completed UserMessage items.
        sid = IDS["codex"]
        path = self.write_records(self.base / "codex-new" / ("rollout-2026-10-15T10-00-00-" + sid + ".jsonl"), [
            {"type": "session_meta", "payload": {"id": sid, "cwd": str(self.project)}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "Fix the login bug."}]}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "Fix the login bug.", "text_elements": []}]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Fixed."}]}},
        ])
        session = self.adapters["codex"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events], [("user", "Fix the login bug."), ("assistant", "Fixed.")])
        self.assertEqual(session.title, "Fix the login bug.")

    def test_crash_nul_padding_is_a_cut_short_record(self):
        # Issue #66: power loss leaves a cut record padded with NULs before the writer resumes.
        path = self.base / "nul.jsonl"
        path.write_bytes(b'{"type":"user","uuid":"u1"}\n{"type":"x","thread_id":"0' + b"\x00" * 64 + b'\n{"type":"user","uuid":"u3"}\n')
        records, issues, _ = read_records(path)
        self.assertEqual([r["uuid"] for r in records], ["u1", "u3"])
        self.assertTrue(any("cut short at line 2" in issue for issue in issues), issues)

    def test_claude_queued_human_prompts_are_user_events(self):
        # Issue #68: prompts typed mid-turn exist only as queued_command attachments.
        sid = IDS["claude"]
        def queued(uuid, parent, prompt, origin, mode="prompt"):
            return {"uuid": uuid, "parentUuid": parent, "type": "attachment", "cwd": str(self.project), "sessionId": sid,
                    "timestamp": "2026-10-15T10:00:00Z", "attachment": {"type": "queued_command", "prompt": prompt,
                                                                         "commandMode": mode, "origin": origin}}
        path = self.write_records(self.base / "queued" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Audit the repo."),
            queued("q1", "u1", "Also file issues.", {"kind": "human"}),
            queued("q2", "q1", "<task-notification>done</task-notification>", {"kind": "task-notification"}, "task-notification"),
            self.claude_message("a1", "q2", "assistant", "Auditing and filing."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events],
                         [("user", "Audit the repo."), ("user", "Also file issues."), ("assistant", "Auditing and filing.")])

    def test_raw_surrogate_bytes_are_scrubbed(self):
        # Issue #69: json.loads decodes bytes with surrogatepass, bypassing the escape check.
        from agent_session_sync.io import loads
        self.assertEqual(loads(b'{"t": "a\xed\xa0\x80b"}'), {"t": "a\ufffdb"})
        self.assertEqual(loads(b'{"t": "\xed\x9f\xbf"}'), {"t": "\ud7ff"})

    def test_empty_codex_rollout_is_not_an_error(self):
        # Issue #70: a rollout exists before its first record is written.
        sid = "01a130f2-9e78-7d40-b953-b3b84be9ae45"
        path = self.paths["codex"].with_name("rollout-2026-10-15T11-00-00-" + sid + ".jsonl")
        path.write_bytes(b"")
        engine = self.engine()
        sessions = engine.scan(harness="codex", days=None)
        self.assertEqual(engine.errors, [])
        self.assertNotIn(sid, [s.native_id for s in sessions])

    def test_muse_accepted_intent_is_not_duplicated_by_its_run(self):
        # Issue #71: current journals link the accepted intent to its run by intent_id.
        sid, run = "01a13ea5-053b-7301-a70a-41f31f460992", "01a13ea5-57ff-78e3-b20a-fcf4701b3d6c"
        home = self.base / "muse-intent"
        stream = {"kind": "session", "id": sid}
        self.write_records(home / "sessions/2026/10/15" / sid / "session.jsonl", [
            {"schema_version": 1, "id": "a", "stream": stream, "sequence": 1, "recorded_at": 1792052385797831, "causation_id": run,
             "payload_type": "runtime.user_intent.accepted",
             "payload": {"intent_id": run, "model_messages": [{"content": [{"kind": "text", "text": "Pull down the repo"}]}]}},
            {"schema_version": 1, "id": "b", "stream": stream, "sequence": 2, "recorded_at": 1792052385804626, "payload_type": "runtime.session",
             "payload": {"event": {"kind": "started", "prompt": "Pull down the repo"}, "kind": "run", "run_id": run}},
            {"schema_version": 1, "id": "c", "stream": stream, "sequence": 3, "recorded_at": 1792052385904626, "payload_type": "runtime.session",
             "payload": {"event": {"kind": "assistant_message_committed", "text": "Done."}, "kind": "run", "run_id": run}},
        ])
        adapter = Muse(dataclasses.replace(self.adapters["muse"].installation, home=str(home)))
        session = adapter.read(next(adapter.discover()))
        self.assertEqual([(e.kind, e.text) for e in session.events], [("user", "Pull down the repo"), ("assistant", "Done.")])

    def test_unbridged_compaction_keeps_its_summary(self):
        # Issue #72: only summaries of bridged boundaries restate history present in the file.
        sid = IDS["claude"]
        def boundary(uuid, logical):
            return {"uuid": uuid, "parentUuid": None, "logicalParentUuid": logical, "type": "system", "subtype": "compact_boundary",
                    "cwd": str(self.project), "sessionId": sid, "timestamp": "2026-10-15T10:00:00Z"}
        path = self.write_records(self.base / "compact-twice" / (sid + ".jsonl"), [
            boundary("b1", "x1"),
            self.claude_message("s1", "b1", "user", "Summary: build the importer.", isCompactSummary=True),
            self.claude_message("x1", "s1", "user", "Now add tests."),
            self.claude_message("a2", "x1", "assistant", "Added tests."),
            boundary("b2", "x2"),
            self.claude_message("s2", "b2", "user", "Summary: importer built, tests added.", isCompactSummary=True),
            self.claude_message("x2", "s2", "user", "Ship it."),
            self.claude_message("a3", "x2", "assistant", "Shipped."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([e.text for e in session.events],
                         ["Summary: build the importer.", "Now add tests.", "Added tests.", "Ship it.", "Shipped."])

    def test_utf16_input_lone_surrogates_are_scrubbed(self):
        # Issue #73: json.loads detects UTF-16/32, so surrogate checks run on decoded text.
        from agent_session_sync.io import loads
        for encoding in ("utf-16", "utf-16-le", "utf-32"):
            self.assertEqual(loads('{"t": "a\\ud800b"}'.encode(encoding)), {"t": "a\ufffdb"}, encoding)
        self.assertEqual(loads(b'\xef\xbb\xbf{"a": 1}'), {"a": 1})

    def test_muse_pasted_placeholder_uses_full_started_prompt(self):
        # Issue #74: the display form collapses pastes; the started prompt is complete.
        sid, run = "01a13e10-053b-7301-a70a-41f31f460992", "aef2d3bc-57ff-78e3-b20a-fcf4701b3d6c"
        home, stream = self.base / "muse-paste", {"kind": "session", "id": sid}
        full = "Turn off github actions: " + "x" * 1146
        def runtime(seq, event):
            return {"schema_version": 1, "id": str(seq), "stream": stream, "sequence": seq, "recorded_at": 1792052385797831 + seq,
                    "payload_type": "runtime.session", "payload": {"event": event, "kind": "run", "run_id": run}}
        self.write_records(home / "sessions/2026/10/15" / sid / "session.jsonl", [
            runtime(1, {"kind": "user_prompt_display", "prompt": "Turn off github actions: [Pasted Content 1146 chars]"}),
            runtime(2, {"kind": "started", "prompt": full}),
            runtime(3, {"kind": "assistant_message_committed", "text": "Done."}),
        ])
        adapter = Muse(dataclasses.replace(self.adapters["muse"].installation, home=str(home)))
        session = adapter.read(next(adapter.discover()))
        self.assertEqual([(e.kind, e.text) for e in session.events], [("user", full), ("assistant", "Done.")])

    def test_claude_slash_command_arguments_are_user_words(self):
        # Issue #75: /goal <objective> states the task; argument-less commands stay hidden.
        sid = IDS["claude"]
        path = self.write_records(self.base / "slash" / (sid + ".jsonl"), [
            self.claude_message("c0", None, "user", "<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>"),
            self.claude_message("c1", "c0", "user", "<command-name>/effort</command-name>\n<command-args>medium</command-args>"),
            self.claude_message("c2", "c1", "user", "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n"
                                                    "<command-args>Fix every bug.</command-args>"),
            self.claude_message("o1", "c2", "user", "<local-command-stdout>Goal set</local-command-stdout>"),
            self.claude_message("a1", "o1", "assistant", "Starting."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events],
                         [("user", "/effort medium"), ("user", "/goal Fix every bug."), ("assistant", "Starting.")])
        self.assertEqual(session.title, "/goal Fix every bug.")

    def test_claude_parallel_tool_results_are_kept(self):
        # Issue #76: each parallel result hangs off its own call, beside the active chain.
        sid = IDS["claude"]
        def call(uuid, parent, tool):
            return self.claude_message(uuid, parent, "assistant", [{"type": "tool_use", "id": tool, "name": "Bash", "input": {}}])
        def result(uuid, parent, tool):
            return self.claude_message(uuid, parent, "user", [{"type": "tool_result", "tool_use_id": tool, "content": "out " + tool}])
        path = self.write_records(self.base / "parallel" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "List two dirs."),
            call("a1", "u1", "t1"), call("a2", "a1", "t2"),
            result("r1", "a1", "t1"), result("r2", "a2", "t2"),
            self.claude_message("a3", "r2", "assistant", "Done."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.call_id) for e in session.events], [
            ("user", ""), ("tool_call", "t1"), ("tool_result", "t1"), ("tool_call", "t2"), ("tool_result", "t2"), ("assistant", "")])

    def test_muse_goal_continuations_are_not_user_prompts(self):
        # Issue #77: harness-written goal turns are skipped; the typed objective is the user's prompt.
        sid, objective = "01a13e90-b93b-7830-a3f9-001843133fea", "Analyze this codebase for bugs and fix them."
        home, stream, seq = self.base / "muse-goal", {"kind": "session", "id": sid}, [0]
        def rec(payload, kind="runtime.session"):
            seq[0] += 1
            return {"schema_version": 1, "id": "r%d" % seq[0], "stream": stream, "sequence": seq[0],
                    "recorded_at": 1792051054249323 + seq[0], "payload_type": kind, "payload": payload}
        nudge = "Continue working toward the active session goal.\n\n<objective>\n" + objective + "\n</objective>"
        rows = [rec({"event": {"kind": "inbox_item_queued", "source": {"action": "set", "goal_id": "g1", "source": "goal_control"},
                               "payload": {"prompt": "", "model_user_messages": [
                                   "<goal_control action=\"set\">\n<objective>" + objective + "</objective>\n</goal_control>"]}}}),
                rec({"event": {"kind": "started", "prompt": ""}, "kind": "run", "run_id": "run1"}),
                rec({"event": {"kind": "assistant_message_committed", "text": "Working."}, "kind": "run", "run_id": "run1"}),
                rec({"kind": "command_intake", "record": {"client_id": "muse-runtime-goal",
                                                          "command": {"payload": {"prompt": nudge}}}}, "runtime.command_intake.received"),
                rec({"event": {"kind": "goal_run_binding", "record": {"authority_kind": "continuation", "run_id": "run2"}},
                     "kind": "run", "run_id": "run2"}),
                rec({"event": {"kind": "started", "prompt": nudge}, "kind": "run", "run_id": "run2"}),
                rec({"event": {"kind": "assistant_message_committed", "text": "Progress."}, "kind": "run", "run_id": "run2"})]
        self.write_records(home / "sessions/2026/10/15" / sid / "session.jsonl", rows)
        adapter = Muse(dataclasses.replace(self.adapters["muse"].installation, home=str(home)))
        session = adapter.read(next(adapter.discover()))
        self.assertEqual([(e.kind, e.text) for e in session.events],
                         [("user", "/goal " + objective), ("assistant", "Working."), ("assistant", "Progress.")])
        self.assertEqual(session.title, "/goal " + objective)

    def test_parser_version_invalidates_cached_parses(self):
        # Issue #78: a parser change must reach transcripts that no longer change.
        from agent_session_sync import engine as engine_module
        engine = self.engine()
        first = {s.key: s for s in engine.scan(harness="claude", days=None)}
        original = type(self.adapters["claude"]).parse
        def changed(adapter, records, ref):
            session = original(adapter, records, ref)
            session.title = "reparsed"
            return session
        with mock.patch.object(type(self.adapters["claude"]), "parse", changed):
            self.assertNotEqual([s.title for s in engine.scan(harness="claude", days=None)], ["reparsed"])
            with mock.patch.object(engine_module, "PARSER_VERSION", engine_module.PARSER_VERSION + 1):
                self.assertEqual([s.title for s in engine.scan(harness="claude", days=None)], ["reparsed"] * len(first))

    def test_antigravity_reads_per_conversation_workspace(self):
        # Issue #79: conversation_metadata.json records each conversation's workspace.
        sid = IDS["agy"]
        home = self.base / "agy-metadata"
        path = self.write_records(home / "brain" / sid / ".system_generated/logs/transcript_full.jsonl", [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-10-15T10:03:00Z",
             "content": "<USER_REQUEST>hi</USER_REQUEST>"},
        ])
        (home / "cache").mkdir(parents=True)
        (home / "cache/conversation_metadata.json").write_text(json.dumps({"conversations": {
            sid: {"summary": {"ID": sid, "WorkspaceURIs": [self.project.as_uri()]}}}}), encoding="utf-8")
        session = Antigravity(Installation("agy", "default", str(home))).read(SessionRef(sid, str(path)))
        self.assertEqual(resolved_workspace(session.workspace), resolved_workspace(str(self.project)))

    def test_antigravity_reads_workspace_from_prompt_history(self):
        # Issue #80: history.jsonl names each conversation's workspace when caches are stale.
        sid = IDS["agy"]
        home = self.base / "agy-history"
        path = self.write_records(home / "brain" / sid / ".system_generated/logs/transcript_full.jsonl", [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-10-15T10:03:00Z",
             "content": "<USER_REQUEST>hi</USER_REQUEST>"},
        ])
        self.write_records(home / "history.jsonl", [
            {"display": "/effort medium", "timestamp": 1790213690604, "workspace": "/elsewhere", "type": "slash_command"},
            {"display": "hi", "timestamp": 1790213695694, "workspace": str(self.project), "conversationId": sid},
        ])
        session = Antigravity(Installation("agy", "default", str(home))).read(SessionRef(sid, str(path)))
        self.assertEqual(session.workspace, str(self.project))

    def test_title_keeps_slash_task_statement_but_skips_settings(self):
        # Issue #81: /goal <objective> states the task; /effort medium is a setting.
        from agent_session_sync.adapters import choose_title
        self.assertEqual(choose_title(["/goal Fix the workspace mapping", "yes"], "id"), "/goal Fix the workspace mapping")
        self.assertEqual(choose_title(["/effort medium", "Fix the bug."], "id"), "Fix the bug.")
        self.assertEqual(choose_title(["/effort medium"], "id"), "/effort medium")
        self.assertEqual(choose_title([], "id"), "id")

    def test_claude_titles_prefer_named_titles_and_skip_summaries(self):
        # Issue #82: compaction summaries never title a session; Claude's own titles win.
        sid = IDS["claude"]
        boundary = {"uuid": "b1", "parentUuid": None, "logicalParentUuid": "x1", "type": "system", "subtype": "compact_boundary",
                    "cwd": str(self.project), "sessionId": sid, "timestamp": "2026-10-15T10:00:00Z"}
        rows = [boundary,
                self.claude_message("s1", "b1", "user", "This session is being continued from a previous conversation.", isCompactSummary=True),
                self.claude_message("x1", "s1", "user", "Deploy the admin preview fix."),
                self.claude_message("a1", "x1", "assistant", "Deployed.")]
        path = self.write_records(self.base / "summary-title" / (sid + ".jsonl"), rows)
        self.assertEqual(self.adapters["claude"].read(SessionRef(sid, str(path))).title, "Deploy the admin preview fix.")
        named = rows + [{"type": "ai-title", "aiTitle": "admin-preview", "sessionId": sid},
                        {"type": "custom-title", "customTitle": "my rename", "sessionId": sid}]
        path = self.write_records(self.base / "named-title" / (sid + ".jsonl"), named)
        self.assertEqual(self.adapters["claude"].read(SessionRef(sid, str(path))).title, "my rename")

    def test_unrelated_history_does_not_invalidate_cached_parses(self):
        # Issue #83: Claude and Codex history.jsonl grow with every prompt.
        engine = self.engine()
        engine.scan(harness="claude", days=None)
        history = Path(self.adapters["claude"].home) / "history.jsonl"
        history.write_text('{"display": "hello"}\n', encoding="utf-8")
        with mock.patch.object(type(self.adapters["claude"]), "read", side_effect=AssertionError("re-read")):
            self.assertTrue(engine.scan(harness="claude", days=None))

    def test_claude_queued_and_local_goal_commands_are_user_words(self):
        # Issue #84: /goal is also recorded as a queued confirmation or a local command.
        sid = IDS["claude"]
        path = self.write_records(self.base / "goal-forms" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Start the migration."),
            {"uuid": "q1", "parentUuid": "u1", "type": "attachment", "sessionId": sid, "timestamp": "2026-10-15T10:00:00Z",
             "attachment": {"type": "queued_command", "prompt": "Goal set: Close all issues", "commandMode": "prompt",
                            "origin": {"kind": "auto-continuation"}}},
            {"uuid": "l1", "parentUuid": "q1", "type": "system", "subtype": "local_command", "sessionId": sid,
             "timestamp": "2026-10-15T10:00:01Z",
             "content": "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n<command-args>Ship it.</command-args>"},
            {"uuid": "l2", "parentUuid": "l1", "type": "system", "subtype": "local_command", "sessionId": sid,
             "timestamp": "2026-10-15T10:00:02Z", "content": "<local-command-stdout>Goal set: Ship it.</local-command-stdout>"},
            self.claude_message("a1", "l2", "assistant", "Working."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events], [
            ("user", "Start the migration."), ("user", "/goal Close all issues"), ("user", "/goal Ship it."), ("assistant", "Working.")])

    def test_summary_only_session_is_titled_by_its_id(self):
        # Issue #85: a compaction summary never titles a session, even when it is the only user text.
        sid = IDS["claude"]
        path = self.write_records(self.base / "summary-only" / (sid + ".jsonl"), [
            {"uuid": "b1", "parentUuid": None, "logicalParentUuid": "zz", "type": "system", "subtype": "compact_boundary",
             "sessionId": sid, "timestamp": "2026-10-15T10:00:00Z"},
            self.claude_message("s1", "b1", "user", "This session is being continued from a previous conversation.", isCompactSummary=True),
            self.claude_message("a1", "s1", "assistant", "Continuing."),
        ])
        self.assertEqual(self.adapters["claude"].read(SessionRef(sid, str(path))).title, sid)

    def test_claude_goal_typed_during_tool_run_is_kept(self):
        # Issue #86: the goal branch hangs off the running tool call, beside the chain.
        sid = IDS["claude"]
        path = self.write_records(self.base / "goal-side" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Deploy the release."),
            self.claude_message("a1", "u1", "assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
            {"uuid": "g1", "parentUuid": "a1", "type": "attachment", "sessionId": sid, "timestamp": "2026-10-15T10:00:00Z",
             "attachment": {"type": "goal_status"}},
            {"uuid": "l1", "parentUuid": "g1", "type": "system", "subtype": "local_command", "sessionId": sid, "timestamp": "2026-10-15T10:00:01Z",
             "content": "<command-name>/goal</command-name>\n<command-args>Release is live.</command-args>"},
            self.claude_message("r1", "a1", "user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            self.claude_message("a2", "r1", "assistant", "Deployed."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([e.text for e in session.events if e.kind == "user"], ["Deploy the release.", "/goal Release is live."])

    def test_claude_out_of_order_parallel_results_keep_every_call(self):
        # Issue #87: the later call sits on a side branch when its result returns first.
        sid = IDS["claude"]
        def call(uuid, parent, tool):
            return self.claude_message(uuid, parent, "assistant", [{"type": "tool_use", "id": tool, "name": "Read", "input": {}}])
        def result(uuid, parent, tool):
            return self.claude_message(uuid, parent, "user", [{"type": "tool_result", "tool_use_id": tool, "content": tool}])
        retry = self.claude_message("x1", "u1", "assistant", [{"type": "tool_use", "id": "tX", "name": "Read", "input": {}}])
        path = self.write_records(self.base / "parallel-order" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Read both files."),
            retry,  # an abandoned retry from the prompt stays excluded
            call("a1", "u1", "tA"), call("a2", "a1", "tB"),
            result("rB", "a2", "tB"), result("rA", "a1", "tA"),
            self.claude_message("a3", "rA", "assistant", "Both read."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.call_id) for e in session.events], [
            ("user", ""), ("tool_call", "tA"), ("tool_call", "tB"), ("tool_result", "tB"), ("tool_result", "tA"), ("assistant", "")])

    def test_muse_tool_task_output_is_not_an_assistant_reply(self):
        # Issue #88: task output duplicates the tool result; only run output is a reply.
        sid, home = "01a13e90-b93b-7830-a3f9-001843133fea", self.base / "muse-task-output"
        stream, rows = {"kind": "session", "id": sid}, []
        def rec(payload):
            rows.append({"schema_version": 1, "id": str(len(rows)), "stream": stream, "sequence": len(rows) + 1,
                         "recorded_at": 1792051054249323 + len(rows), "payload_type": "runtime.session", "payload": payload})
        out = '{"command":"pytest","exit_code":1}'
        rec({"event": {"kind": "started", "prompt": "Run the tests."}, "kind": "run", "run_id": "r1"})
        rec({"event": {"kind": "assistant_tool_calls_committed", "tool_calls": [{"name": "exec", "call_id": "c1", "args": {}}]},
             "kind": "run", "run_id": "r1"})
        rec({"event": {"kind": "output", "chunk": out, "final_result": True}, "kind": "task", "run_id": "r1", "task_id": "t1"})
        rec({"event": {"kind": "tool_result_batch_committed", "results": [{"tool_call_id": "c1", "text": out}]}, "kind": "run", "run_id": "r1"})
        rec({"event": {"kind": "started", "prompt": "Summarize."}, "kind": "run", "run_id": "r2"})
        rec({"event": {"kind": "output", "chunk": "One test fails.", "final_result": True}, "kind": "run", "run_id": "r2"})
        self.write_records(home / "sessions/2026/10/15" / sid / "session.jsonl", rows)
        adapter = Muse(dataclasses.replace(self.adapters["muse"].installation, home=str(home)))
        session = adapter.read(next(adapter.discover()))
        self.assertEqual([(e.kind, e.text) for e in session.events], [
            ("user", "Run the tests."), ("tool_call", ""), ("tool_result", out), ("user", "Summarize."), ("assistant", "One test fails.")])

if __name__ == "__main__":
    unittest.main()
