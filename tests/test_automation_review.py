import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock

from test_sync import Workspace
from agent_session_sync import legacy, memory_sync
from agent_session_sync.adapters import Muse
from agent_session_sync.automation import setup, edit_hooks, command_is_ours
from agent_session_sync.cli import main
from agent_session_sync.discovery import Installation, load_config
from agent_session_sync.io import SyncError, json_bytes
from agent_session_sync.ownership import command_owner, cron_owner, SESSION, MEMORY
from agent_session_sync.store import single_flight


class AutomationReviewTests(Workspace):
    def setUp(self):
        super().setUp()
        for adapter in self.adapters.values():
            adapter.options["settings"] = str(self.base / adapter.endpoint / "settings.json")

    def test_memory_install_json_has_no_legacy_output(self):
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {name: {"home": str(a.home), "settings": a.options["settings"], "executable": sys.executable} for name, a in self.adapters.items()}}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["install", "--memories", "--no-cron", "--dry-run", "--json", "--config", str(config), "--state-dir", str(self.state)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])

    def test_memory_hook_captures_roots_and_works_without_terminal_environment(self):
        setup(self.adapters, self.state, {}, no_cron=True, memories=True)
        settings = Path(self.adapters["claude"].options["settings"])
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        commands = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
        command = next(shlex.split(c) for c in commands if command_owner(c) == MEMORY)
        self.assertEqual(command[command.index("--state-dir") + 1], str(self.state))
        self.assertEqual(command[command.index("--codex-home") + 1], str(self.adapters["codex"].home))
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")}
        env.update(HOME=str(self.base / "sanitized-home"), CODEX_HOME=str(self.base / "wrong-codex"),
                   CLAUDE_CONFIG_DIR=str(self.base / "wrong-claude"), AGENT_SESSION_SYNC_STATE_DIR=str(self.base / "wrong-state"))
        result = subprocess.run(command, cwd=str(self.project), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / "memory-state.json").is_file())
        self.assertFalse((self.base / "wrong-state").exists())

    def test_plain_session_removal_preserves_memory_and_unrelated_mentions(self):
        adapter = self.adapters["claude"]
        path = Path(adapter.options["settings"])
        path.parent.mkdir(parents=True, exist_ok=True)
        memory = {"type": "command", "command": "python /site-packages/agent_session_sync/memory_sync.py --quiet"}
        unrelated = {"type": "command", "command": "echo 'agent_session_sync hook --quiet'"}
        cfg = {"hooks": {"Stop": [{"matcher": "", "hooks": [memory, unrelated]}]}}
        path.write_bytes(json_bytes(cfg))
        _, _, data, _ = edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"], uninstall=True)
        self.assertEqual(json.loads(data)["hooks"]["Stop"][0]["hooks"], [memory, unrelated])
        self.assertFalse(command_is_ours(memory))
        self.assertFalse(command_is_ours(unrelated))

    def test_command_and_cron_ownership_match_execution_not_mentions(self):
        cases = {
            "python3 /tmp/agent-session-sync.py --quiet": SESSION,
            "python3 -m agent_session_sync sync": SESSION,
            "python -m agent_session_sync.memory_sync --quiet": MEMORY,
            "python /site-packages/agent_session_sync/memory_sync.py --quiet": MEMORY,
            "echo 'python -m agent_session_sync --quiet'": None,
            "rg agent-session-sync.py /workspace": None,
            "python -c 'print(\"agent_session_sync\")' --quiet": None,
            "python /other/compat.py --quiet": None,
            '"C:\\Program Files\\Python\\python.exe" "C:\\tools\\agent-session-sync.py" --quiet': SESSION,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(command_owner(command), expected)
                self.assertEqual(cron_owner("*/2 * * * * " + command), expected)
        self.assertIsNone(cron_owner("# agent-session-sync-unrelated"))
        self.assertEqual(cron_owner("@reboot python -m agent_session_sync"), SESSION)

    def test_legacy_entry_point_never_uses_old_network_updater(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network access")), \
             mock.patch("agent_session_sync.cli.load_config", return_value={}), \
             mock.patch("sys.argv", ["legacy", "--update", "--dry-run"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(legacy.main(), 0)
        self.assertIn("uv tool upgrade", output.getvalue())

    def test_uninstall_cleans_recorded_harnesses_even_when_current_config_disables_them(self):
        setup(self.adapters, self.state, {}, no_cron=True)
        paths = [Path(a.options["settings"]) for a in self.adapters.values() if a.installation.harness != "codex"]
        setup({"codex": self.adapters["codex"]}, self.state, {"harnesses": {"claude": {"enabled": False}}}, uninstall=True, no_cron=True)
        for path in paths:
            self.assertNotIn("agent_session_sync", path.read_text(encoding="utf-8"))
            self.assertNotIn("agent-session-sync.py", path.read_text(encoding="utf-8"))

    def test_reinstall_retires_removed_profile_hooks_and_keeps_cleanup_metadata(self):
        work = Muse(Installation("muse", "work", str(self.base / "work-data"), sys.executable), {"settings": str(self.base / "work-settings.json")})
        profile = {"harness": "muse", "name": "work", "home": str(work.home), "settings": work.options["settings"]}
        setup(dict(self.adapters, **{"muse@work": work}), self.state, {"profiles": [profile]}, no_cron=True)
        setup(self.adapters, self.state, {}, no_cron=True)
        self.assertNotIn("agent-session-sync.py", Path(work.options["settings"]).read_text(encoding="utf-8"))
        saved = load_config(self.state / "automation-config.json")
        self.assertTrue(any(r["profile"] == "work" for r in saved["installed_hooks"]))

    def test_settings_symlinks_remain_symlinks(self):
        adapter = self.adapters["claude"]
        actual = self.base / "dotfiles/claude.json"
        actual.parent.mkdir()
        actual.write_bytes(json_bytes({"unrelated": 42}))
        alias = Path(adapter.options["settings"])
        alias.parent.mkdir(parents=True, exist_ok=True)
        try:
            alias.symlink_to(actual)
        except OSError:
            self.skipTest("symlinks unavailable")
        setup({"claude": adapter}, self.state, {}, no_cron=True)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(json.loads(actual.read_text(encoding="utf-8"))["unrelated"], 42)
        setup({}, self.state, {}, uninstall=True, no_cron=True)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(json.loads(actual.read_text(encoding="utf-8")), {"unrelated": 42})

    def test_config_relative_roots_are_independent_of_runtime_cwd(self):
        folder = self.base / "configuration"
        folder.mkdir()
        path = folder / "config.json"
        path.write_bytes(json_bytes({"harnesses": {"muse": {"home": "data/muse", "settings": "muse.json", "executable": "bin/muse"}},
                                    "routes": [{"from": "muse", "to": "codex", "project": "project"}]}))
        loaded = load_config(path)
        self.assertEqual(loaded["harnesses"]["muse"]["home"], str(folder / "data/muse"))
        self.assertEqual(loaded["harnesses"]["muse"]["settings"], str(folder / "muse.json"))
        self.assertEqual(loaded["harnesses"]["muse"]["executable"], str(folder / "bin/muse"))
        self.assertEqual(loaded["routes"][0]["project"], str(folder / "project"))

    def test_purge_refuses_live_session_and_memory_writers(self):
        marker = self.state / "sessions.sqlite3"
        self.state.mkdir()
        marker.write_bytes(b"active database")
        with single_flight(self.state):
            with self.assertRaises(SyncError):
                setup(self.adapters, self.state, {}, uninstall=True, purge=True, no_cron=True, no_hooks=True)
        self.assertEqual(marker.read_bytes(), b"active database")
        lock = memory_sync.acquire_lock(str(self.state / "memory.lock"))
        try:
            with self.assertRaises(SyncError):
                setup(self.adapters, self.state, {}, uninstall=True, purge=True, memories=True, no_cron=True, no_hooks=True)
        finally:
            lock.close()
        self.assertEqual(marker.read_bytes(), b"active database")

    def test_partial_purge_keeps_stable_locks_and_automation_configuration(self):
        setup(self.adapters, self.state, {}, no_cron=True)
        config = (self.state / "automation-config.json").read_bytes()
        setup(self.adapters, self.state, {}, uninstall=True, purge=True, no_cron=True, no_hooks=True)
        self.assertEqual((self.state / "automation-config.json").read_bytes(), config)
        self.assertTrue((self.state / "lock").exists())
