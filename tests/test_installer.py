import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("installer", str(ROOT / "install.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

FAKE_CRONTAB = """#!/bin/sh
case "$1" in
 -l) [ -f "$CRONTAB_FILE" ] && cat "$CRONTAB_FILE" && exit 0; echo "no crontab for test" >&2; exit 1;;
 -r) rm -f "$CRONTAB_FILE";;
 -) cat > "$CRONTAB_FILE";;
esac
"""


class ArchiveTests(unittest.TestCase):
    def archive(self, names):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        return data.getvalue()

    def test_extract_strips_the_root_and_refuses_escapes(self):
        with tempfile.TemporaryDirectory() as directory:
            installer.extract(self.archive(["repo-sha/agent_session_sync/cli.py"]), directory)
            self.assertTrue((Path(directory) / "agent_session_sync/cli.py").is_file())
            for bad in (["repo-sha/../escape.py"], ["repo-sha/a.py", "other/b.py"]):
                with self.assertRaises(OSError):
                    installer.extract(self.archive(bad), directory)
            self.assertFalse((Path(directory).parent / "escape.py").exists())

    def test_vet_refuses_incomplete_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                installer.vet(directory)
            installer.stage_from(ROOT, Path(directory) / "tree")
            self.assertTrue(installer.vet(Path(directory) / "tree"))


@unittest.skipIf(os.name == "nt", "exercises cron and POSIX launchers")
class UpdateInPlaceTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.home, self.state = self.base / "home", self.base / "state"
        (self.home / ".claude/projects").mkdir(parents=True)
        (self.home / ".codex/sessions").mkdir(parents=True)
        self.state.mkdir()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "crontab").write_text(FAKE_CRONTAB)
        for name in ("claude", "codex"):
            (bin_dir / name).write_text("#!/bin/sh\necho '{} 1.0'\n".format(name))
        for path in bin_dir.iterdir():
            path.chmod(0o755)
        self.crontab = self.base / "crontab"
        self.env = {"HOME": str(self.home), "PATH": str(bin_dir) + os.pathsep + "/usr/bin:/bin",
                    "XDG_DATA_HOME": str(self.home / ".local/share"), "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "AGENT_SESSION_SYNC_STATE_DIR": str(self.state),
                    "CRONTAB_FILE": str(self.crontab)}

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.base), ignore_errors=True)

    def install(self, *extra):
        result = subprocess.run([sys.executable, str(ROOT / "install.py"), "--source", str(ROOT)] + list(extra),
                                env=self.env, cwd=str(self.base), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_v1_automation_is_moved_to_the_managed_install(self):
        old = self.base / "v1/agent-session-sync.py"
        command = "/usr/bin/python3 {} --quiet".format(old)
        self.crontab.write_text("# agent-session-sync — old\n*/2 * * * * {} >> /tmp/x.log 2>&1\n".format(command))
        settings = self.home / ".claude/settings.json"
        settings.write_text(json.dumps({"model": "opus", "hooks": {event: [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]
                                                                   for event in ("SessionStart", "Stop")}}))
        output = self.install()
        self.assertIn("scheduler=True hooks=True memories=False", output)
        current = self.home / ".local/share/agent-session-sync/current/agent-session-sync.py"
        cron = self.crontab.read_text()
        self.assertNotIn(str(old), cron)
        self.assertIn(str(current), cron)
        hooks = json.loads(settings.read_text())["hooks"]
        commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        self.assertTrue(commands and all(str(current) in c for c in commands), commands)
        self.assertEqual(json.loads(settings.read_text())["model"], "opus")
        self.assertIn("Automation already configured.", self.install())
        launcher = self.home / ".local/bin/agent-session-sync"
        self.assertEqual(subprocess.run([str(launcher), "--version"], env=self.env, capture_output=True, text=True).stdout.strip(),
                         installer.vet(ROOT))

    def test_cli_only_machines_get_no_new_automation(self):
        self.assertIn("no automation found", self.install())
        self.assertFalse(self.crontab.exists())
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertTrue((self.home / ".local/share/agent-session-sync/current/agent_session_sync/cli.py").is_file())

    def test_dry_run_changes_nothing(self):
        self.crontab.write_text("*/2 * * * * /usr/bin/python3 /old/agent-session-sync.py --quiet\n")
        before = self.crontab.read_text()
        self.assertIn("would refresh automation", self.install("--dry-run"))
        self.assertEqual(self.crontab.read_text(), before)
        self.assertFalse((self.home / ".local/share/agent-session-sync/current").exists())


if __name__ == "__main__":
    unittest.main()
