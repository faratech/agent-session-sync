"""Regressions for the 2026-10-30 sweep: CLI contracts."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_sync import cli
from agent_session_sync.adapters import BUILTINS
from agent_session_sync.cli import main
from agent_session_sync.discovery import discover
from agent_session_sync.io import json_bytes

REPO = Path(__file__).resolve().parent.parent
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SweepCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        # Every harness points into the temporary directory; nothing real is read.
        harnesses = {name: {"home": str(self.base / name), "executable": sys.executable} for name in BUILTINS}
        self.config = self.base / "config.json"
        self.config.write_bytes(json_bytes({"harnesses": harnesses}))
        self.prefix = ["--state-dir", str(self.state), "--config", str(self.config)]

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_update_memories_is_refused_not_ignored(self):
        # Issue #95
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            status, _, err = self.run_main(self.prefix + ["update", "--memories"])
        self.assertEqual(status, 1)
        self.assertIn("install --memories", err)

    def test_update_honors_json_and_quiet(self):
        # Issue #96
        installer = b'"""agent-session-sync installer: test"""\nprint("x")\n'
        narration = "source: test\nagent-session-sync 9.9.9 is installed.\n"
        def urlopen(request, timeout=None):
            return FakeResponse(installer)
        completed = subprocess.CompletedProcess([], 0, narration, None)
        with mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch.object(cli.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(cli.subprocess, "call", side_effect=AssertionError("inherits stdout")):
            status, out, _ = self.run_main(self.prefix + ["--json", "update", "--ref", SHA])
            self.assertEqual(status, 0)
            document = json.loads(out)
            self.assertEqual((document["commit"], document["status"]), (SHA, 0))
            self.assertIn("agent-session-sync 9.9.9 is installed.", document["output"])
            status, out, err = self.run_main(self.prefix + ["update", "--quiet", "--ref", SHA])
            self.assertEqual((status, out, err), (0, "", ""))
            self.assertIn("--ref", run.call_args[0][0])
            run.return_value = subprocess.CompletedProcess([], 3, "install step failed\n", None)
            status, out, err = self.run_main(self.prefix + ["update", "--quiet", "--ref", SHA])
            self.assertEqual((status, out), (3, ""))
            self.assertIn("install step failed", err)

    @unittest.skipIf(os.name == "nt", "uses a POSIX shell script as the harness")
    def test_undecodable_version_banner_falls_back(self):
        # Issue #112
        fake = self.base / "bin" / "codex"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nprintf '\\377\\376 codex\\n'\n", encoding="utf-8")
        fake.chmod(0o755)
        config = {"harnesses": {name: {"enabled": False} for name in BUILTINS}}
        config["harnesses"]["codex"] = {"home": str(self.base / "codex"), "executable": str(fake)}
        installations = discover(config, refresh=True, directory=self.state)
        self.assertEqual(len(installations), 1)
        self.assertIn("codex", installations[0].version)
        self.config.write_bytes(json_bytes(config))
        status, out, _ = self.run_main(self.prefix + ["--refresh", "doctor"])
        self.assertEqual(status, 0)
        self.assertIn("codex", out)

    @unittest.skipIf(os.name == "nt", "closed-pipe semantics differ on Windows")
    def test_closed_stdout_exits_quietly(self):
        # Issue #116
        read, write = os.pipe()
        os.close(read)
        try:
            process = subprocess.run([sys.executable, str(REPO / "agent-session-sync.py")] + self.prefix + ["doctor"],
                                     stdout=write, stderr=subprocess.PIPE, timeout=60)
        finally:
            os.close(write)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stderr, b"")

    def test_json_usage_errors_are_json(self):
        # Issue #117
        for argv in (["--json", "badcmd"], ["--json", "show"], ["show", "--json"], ["--json", "list", "--days", "x"]):
            with self.subTest(argv=argv):
                status, out, err = self.run_main(self.prefix + argv)
                self.assertEqual(status, 2)
                self.assertIn("error", json.loads(out))
                self.assertEqual(err, "")
        status, out, err = self.run_main(self.prefix + ["show"])
        self.assertEqual((status, out), (2, ""))
        self.assertIn("usage:", err)
        self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
