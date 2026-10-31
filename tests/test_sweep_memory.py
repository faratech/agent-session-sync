"""Regressions for the 2026-10-30 sweep: the memory companion."""
import contextlib
import io
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from agent_session_sync import memory_sync as memory


class SweepMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        names = ("CODEX_HOME", "CLAUDE_HOME", "CLAUDE_PROJECTS", "CODEX_MEM", "CODEX_SUMMARY", "CODEX_OWNED", "CODEX_AGENTS",
                 "STATE_DIR", "STATE_FILE", "LOCK_FILE", "POINTER", "VERBOSE")
        patch = mock.patch.multiple(memory, **{name: getattr(memory, name) for name in names})
        patch.start()
        self.addCleanup(patch.stop)
        memory.configure_paths(str(self.base / "codex"), str(self.base / "claude"), str(self.base / "state"))
        self.memory_args = ["--codex-home", str(self.base / "codex"), "--claude-home", str(self.base / "claude"),
                            "--state-dir", str(self.base / "state"), "--project", "project"]

    def note(self, name, body):
        path = Path(memory.CLAUDE_PROJECTS) / "project/memory" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = memory.main(argv)
        return status, out.getvalue(), err.getvalue()

    def agents(self):
        path = Path(memory.CODEX_AGENTS)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_auto_index_over_budget_is_still_pointed_at(self):
        # Issue #108
        for i in range(40):
            self.note("note-%02d.md" % i, "---\ndescription: %s\n---\n%s\n" % ("d" * 90, "x" * 200))
        status, _, err = self.run_main(self.memory_args + ["--to-codex", "--warn-bytes", "2542"])
        self.assertEqual(status, 0, err)
        bundle = Path(memory.CODEX_OWNED).read_text(encoding="utf-8")
        self.assertIn("index only", bundle)
        self.assertGreater(len(bundle.encode("utf-8")), 2542)
        self.assertIn(memory.CODEX_OWNED, self.agents())
        self.assertNotIn("use index mode", err)
        # A full-text bundle over budget is still never pointed at.
        Path(memory.CODEX_AGENTS).unlink()
        status, _, err = self.run_main(self.memory_args + ["--to-codex", "--mode", "full", "--warn-bytes", "2542"])
        self.assertEqual(status, 0, err)
        self.assertNotIn(memory.CODEX_OWNED, self.agents())

    def test_auto_full_bundles_fit_the_budget_with_their_header(self):
        # Issue #108: auto mode accounts for the complete bundle.
        self.note("a.md", "---\ndescription: short\n---\n" + "body " * 40 + "\n")
        for budget in range(300, 900, 25):
            with self.subTest(budget=budget):
                Path(memory.CODEX_OWNED).unlink() if Path(memory.CODEX_OWNED).exists() else None
                status, _, err = self.run_main(self.memory_args + ["--to-codex", "--warn-bytes", str(budget)])
                self.assertEqual(status, 0, err)
                bundle = Path(memory.CODEX_OWNED).read_text(encoding="utf-8")
                if "index only" not in bundle:
                    self.assertLessEqual(len(bundle.encode("utf-8")), budget)
                self.assertIn(memory.CODEX_OWNED, self.agents())

    def test_one_failing_direction_does_not_stop_the_other(self):
        # Issue #109
        summary = Path(memory.CODEX_SUMMARY)
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_bytes(b"\xff\xfe not utf-8")
        self.note("a.md", "---\ndescription: A fixture\n---\nA user memory note.\n")
        status, _, err = self.run_main(self.memory_args)
        self.assertEqual(status, 1)
        self.assertIn("codex->claude", err)
        self.assertTrue(Path(memory.CODEX_OWNED).is_file())
        self.assertIn(memory.CODEX_OWNED, self.agents())

    def test_mentions_do_not_count_as_pointers(self):
        # Issue #110
        self.note("a.md", "---\ndescription: A fixture\n---\nA user memory note.\n")
        agents = Path(memory.CODEX_AGENTS)
        agents.parent.mkdir(parents=True, exist_ok=True)
        agents.write_text("Please do NOT read claude_code_sync.md from /old/home/.codex/memories/claude_code_sync.md\n", encoding="utf-8")
        summary = Path(memory.CODEX_SUMMARY)
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text("A codex note.\n", encoding="utf-8")
        index = Path(memory.CLAUDE_PROJECTS) / "project/memory/MEMORY.md"
        index.write_text("Ignore codex_memory_sync.md for now.\n", encoding="utf-8")
        status, _, err = self.run_main(self.memory_args)
        self.assertEqual(status, 0, err)
        self.assertIn(memory.POINTER, self.agents())
        self.assertIn(memory.INDEX_LINE, index.read_text(encoding="utf-8"))
        # An installed pointer, even reworded by the user, is not duplicated.
        agents.write_text("At session start, read " + memory.CODEX_OWNED + " for memory.\n", encoding="utf-8")
        index.write_text("- [codex_memory_sync.md](codex_memory_sync.md) - Codex memory\n", encoding="utf-8")
        self.run_main(self.memory_args)
        self.assertEqual(self.agents().count(memory.CODEX_OWNED), 1)
        self.assertEqual(index.read_text(encoding="utf-8").count("](codex_memory_sync.md)"), 1)

    def test_only_regular_files_are_memories(self):
        # Issue #111
        self.note("real.md", "---\ndescription: Real\n---\nReal note.\n")
        memdir = Path(memory.CLAUDE_PROJECTS) / "project/memory"
        (memdir / "archive.md").mkdir()
        symlinks = True
        try:
            os.symlink(str(memdir / "missing-target.md"), str(memdir / "broken.md"))
        except (OSError, NotImplementedError):
            symlinks = False
        groups = memory.memory_groups()
        self.assertEqual([files for _, _, files in groups], [["real.md"]])
        status, _, err = self.run_main(self.memory_args + ["--to-codex"])
        self.assertEqual(status, 0, err)
        bundle = Path(memory.CODEX_OWNED).read_text(encoding="utf-8")
        self.assertIn("1 files", bundle)
        self.assertNotIn("archive.md", bundle)
        if symlinks:
            self.assertNotIn("broken.md", bundle)


if __name__ == "__main__":
    unittest.main()
