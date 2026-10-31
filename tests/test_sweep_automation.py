"""Regressions for the 2026-10-30 sweep: hooks, scheduler entries, and command ownership."""
import json
import os
import sys
import unittest
from unittest import mock

from test_sync import Workspace
from agent_session_sync import legacy
from agent_session_sync.automation import edit_hooks, setup, shell_command
from agent_session_sync.io import SyncError, json_bytes
from agent_session_sync.ownership import SESSION, MEMORY, command_owner, cron_owner


def command_line_to_argv(line):
    """CommandLineToArgvW / msvcrt (2008+) splitting, the oracle for Windows quoting."""
    args, i, n = [], 0, len(line)
    while i < n:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            break
        arg, quoted = "", False
        while i < n and (quoted or line[i] not in " \t"):
            if line[i] == "\\":
                j = i
                while j < n and line[j] == "\\":
                    j += 1
                if j < n and line[j] == '"':
                    arg += "\\" * ((j - i) // 2)
                    if (j - i) % 2:
                        arg += '"'
                        i = j + 1
                    else:
                        i = j
                else:
                    arg += "\\" * (j - i)
                    i = j
            elif line[i] == '"':
                if quoted and i + 1 < n and line[i + 1] == '"':
                    arg += '"'
                    i += 2
                else:
                    quoted = not quoted
                    i += 1
            else:
                arg += line[i]
                i += 1
        args.append(arg)
    return args


class SweepAutomationTests(Workspace):
    def test_malformed_antigravity_definition_is_refused(self):
        # Issue #91
        adapter = self.adapters["agy"]
        path = self.base / "agy-config" / "hooks.json"
        path.parent.mkdir()
        adapter.options["settings"] = str(path)
        for foreign in ({"SessionStart": "echo foreign-not-a-list"}, {"SessionStart": [{"type": "command", "command": "echo other"}]},
                        {"SessionStart": ["echo not-an-object"]}):
            with self.subTest(foreign=foreign):
                path.write_bytes(json_bytes({"agent-session-sync": foreign}))
                with self.assertRaises(SyncError):
                    edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"])
        # Our own definition is still replaced, and removed on uninstall.
        path.write_bytes(json_bytes({}))
        _, _, data, _ = edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"])
        path.write_bytes(data)
        _, _, again, changed = edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"])
        self.assertFalse(changed)
        _, _, removed, _ = edit_hooks(adapter, [], uninstall=True)
        self.assertNotIn("agent-session-sync", json.loads(removed))

    def test_env_options_and_unquoted_windows_commands_are_recognized(self):
        # Issue #92
        cases = {
            "env -i /usr/bin/python3 /opt/agent-session-sync.py sync": SESSION,
            "env -u FOO /usr/bin/python3 /opt/agent-session-sync.py sync": SESSION,
            "env --unset=FOO -C /tmp PATH=/bin python3 -m agent_session_sync sync": SESSION,
            "env -S 'python3 /opt/memory-sync.py' --quiet": MEMORY,
            "C:\\Python312\\python.exe C:\\tools\\agent-session-sync.py hook": SESSION,
            "C:\\Python312\\python.exe C:\\tools\\memory-sync.py --quiet": MEMORY,
            "env FOO=1 python3 /opt/agent-session-sync.py": SESSION,
            "env -- python3 /opt/agent-session-sync.py": SESSION,
            "env -i echo agent-session-sync.py": None,
            "C:\\Windows\\notepad.exe C:\\tools\\agent-session-sync.py": None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(command_owner(command), expected)
                self.assertEqual(cron_owner("*/2 * * * * " + command), expected)

    @unittest.skipIf(os.name == "nt", "cron is POSIX-only")
    def test_line_break_in_cron_paths_is_refused(self):
        # Issue #94
        writes = []
        with mock.patch("agent_session_sync.automation.shutil.which", return_value="/usr/bin/crontab"), \
             mock.patch.object(legacy, "_crontab_read", return_value=["10 * * * * /usr/bin/unrelated"]), \
             mock.patch.object(legacy, "_crontab_write", side_effect=writes.append):
            with self.assertRaises(SyncError):
                setup({}, self.base / "state\nX", {}, no_hooks=True)
        self.assertEqual(writes, [])
        self.assertFalse((self.base / "state\nX" / "automation-config.json").exists())

    def test_windows_quoting_round_trips_through_command_line_to_argv(self):
        # Issue #122
        argv = ["C:\\Python312\\python.exe", "D:\\", "--state-dir", "C:\\state dir\\", "--config", "C:\\a\\config.json",
                'say "hi"', 'x\\"y', "trailing\\\\", "", "plain"]
        line = shell_command(argv, windows=True)
        self.assertEqual(command_line_to_argv(line), argv)
        self.assertEqual(command_line_to_argv(shell_command(["D:\\", "sync"], windows=True)), ["D:\\", "sync"])


if __name__ == "__main__":
    unittest.main()
