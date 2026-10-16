import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from agent_session_sync import memory_sync as memory


class MemoryReviewTests(unittest.TestCase):
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
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def summary(self):
        path = Path(memory.CODEX_SUMMARY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("A user memory note.\n", encoding="utf-8")
        return path

    def note(self, name="a.md", body="---\ndescription: A fixture\n---\nA user memory note.\n"):
        path = Path(memory.CLAUDE_PROJECTS) / "project/memory" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_project_changes_and_deleted_codex_outputs_are_repaired(self):
        self.summary()
        state = {}
        args = Namespace(project="one", dry_run=False)
        self.assertEqual(memory.codex_to_claude(state, args), 1)
        args.project = "two"
        self.assertEqual(memory.codex_to_claude(state, args), 1)
        target = Path(memory.CLAUDE_PROJECTS) / "two/memory" / memory.CLAUDE_OWNED_NAME
        target.unlink()
        self.assertEqual(memory.codex_to_claude(state, args), 1)
        self.assertTrue(target.is_file())
        pointer = target.with_name("MEMORY.md")
        pointer.unlink()
        self.assertEqual(memory.codex_to_claude(state, args), 1)
        self.assertTrue(pointer.is_file())
        before = target.stat().st_mtime_ns
        with mock.patch.object(memory, "atomic_write", wraps=memory.atomic_write) as write:
            self.assertEqual(memory.codex_to_claude(state, args), 0)
        write.assert_not_called()
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_deleted_claude_outputs_and_pointer_are_repaired_without_rewriting_idle_bundles(self):
        self.note()
        state, args = {}, Namespace(mode="index", warn_bytes=65536, dry_run=False)
        self.assertEqual(memory.claude_to_codex(state, args), 1)
        Path(memory.CODEX_OWNED).unlink()
        self.assertEqual(memory.claude_to_codex(state, args), 1)
        before = Path(memory.CODEX_OWNED).stat().st_mtime_ns
        Path(memory.CODEX_AGENTS).unlink()
        self.assertEqual(memory.claude_to_codex(state, args), 1)
        self.assertEqual(Path(memory.CODEX_OWNED).stat().st_mtime_ns, before)
        with mock.patch.object(memory, "atomic_write", wraps=memory.atomic_write) as write:
            self.assertEqual(memory.claude_to_codex(state, args), 0)
        write.assert_not_called()

    def test_absent_sources_do_not_create_targets_or_destroy_last_known_snapshots(self):
        args = Namespace(project="one", dry_run=False)
        state = {}
        self.assertEqual(memory.codex_to_claude(state, args), 0)
        self.assertFalse(Path(memory.CLAUDE_PROJECTS).exists())
        summary = self.summary()
        memory.codex_to_claude(state, args)
        summary.unlink()
        self.assertEqual(memory.codex_to_claude(state, args), 0)
        target = Path(memory.CLAUDE_PROJECTS) / "one/memory" / memory.CLAUDE_OWNED_NAME
        self.assertIn("A user memory note", target.read_text(encoding="utf-8"))
        note = self.note()
        other_args = Namespace(mode="auto", warn_bytes=65536, dry_run=False)
        memory.claude_to_codex(state, other_args)
        previous = Path(memory.CODEX_OWNED).read_bytes()
        note.unlink()
        self.assertEqual(memory.claude_to_codex(state, other_args), 0)
        self.assertEqual(Path(memory.CODEX_OWNED).read_bytes(), previous)
        self.assertEqual(memory.claude_to_codex(state, other_args), 0)

    def test_unknown_memory_output_remains_untouched_with_matching_old_checksum(self):
        summary = self.summary()
        target = Path(memory.CLAUDE_PROJECTS) / "one/memory" / memory.CLAUDE_OWNED_NAME
        target.parent.mkdir(parents=True)
        target.write_text("My own notes.", encoding="utf-8")
        state = {"codex_summary_sha": memory.sha(summary.read_text(encoding="utf-8"))}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(memory.codex_to_claude(state, Namespace(project="one", dry_run=False)), 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "My own notes.")
        self.assertFalse(target.with_name("MEMORY.md").exists())

    def test_index_mode_never_builds_a_full_bundle_and_reads_sources_once(self):
        source = self.note(body="Large note.\n" * 10000)
        with mock.patch.object(memory, "build_full", side_effect=AssertionError("index mode built full bundle")), \
             mock.patch.object(memory, "read", wraps=memory.read) as read:
            memory.claude_to_codex({}, Namespace(mode="index", warn_bytes=65536, dry_run=True))
        self.assertEqual(sum(call.args[0] == str(source) for call in read.call_args_list), 1)

    def test_auto_reads_once_and_matches_full_or_index_bytes_at_exact_boundary(self):
        sources = [self.note("a.md", "---\ndescription: First\n---\n界\n"), self.note("b.md", "\n\n")]
        groups = memory.memory_groups()
        full, index = memory.build_full(groups), memory.build_index(groups)
        for budget in (len(full.encode("utf-8")), len(full.encode("utf-8")) - 1):
            with mock.patch.object(memory, "read", wraps=memory.read) as read:
                mode, content = memory.build_auto(groups, budget)
            self.assertEqual(content, full if mode == "full" else index)
            self.assertEqual(mode, "full" if budget >= len(full.encode("utf-8")) else "index")
            self.assertEqual(sum(call.args[0] in [str(path) for path in sources] for call in read.call_args_list), 2)

    def test_auto_budget_includes_generated_header(self):
        self.note(body="---\ndescription: Short description\n---\n" + "Content " * 100)
        core_size = len(memory.build_full(memory.memory_groups()).encode("utf-8"))
        memory.claude_to_codex({}, Namespace(mode="auto", warn_bytes=core_size + 5, dry_run=False))
        body = Path(memory.CODEX_OWNED).read_text(encoding="utf-8")
        self.assertIn("index only", body)
        self.assertLessEqual(len(body.encode("utf-8")), core_size + 5)
        self.assertTrue(Path(memory.CODEX_AGENTS).is_file())

    def test_invalid_utf8_aborts_without_creating_generated_context(self):
        source = self.note()
        source.write_bytes(b"invalid \xff text")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(memory.main(["--to-codex", "--quiet"]), 1)
        self.assertIn("cannot read memory file", errors.getvalue())
        self.assertFalse(Path(memory.CODEX_OWNED).exists())
        handle = memory.acquire_lock(memory.LOCK_FILE)
        self.assertIsNotNone(handle)
        handle.close()

    def test_malformed_memory_registry_is_recoverable(self):
        path = Path(memory.STATE_FILE)
        path.parent.mkdir(parents=True)
        path.write_text("[]", encoding="utf-8")
        self.assertEqual(memory.load_state(), {})

    def test_bom_frontmatter_and_owned_crlf_output_can_be_updated(self):
        source = self.note()
        source.write_bytes(b"\xef\xbb\xbf---\r\ndescription: A BOM description\r\n---\r\nOld note.\r\n")
        self.assertEqual(memory.describe(memory.read(str(source))), "A BOM description")
        state, args = {}, Namespace(mode="index", warn_bytes=65536, dry_run=False)
        memory.claude_to_codex(state, args)
        target = Path(memory.CODEX_OWNED)
        content = target.read_text(encoding="utf-8")
        target.write_bytes(b"\xef\xbb\xbf" + content.replace("\n", "\r\n").encode("utf-8"))
        source.write_text("---\ndescription: Changed description\n---\nNew note.\n", encoding="utf-8")
        self.assertEqual(memory.claude_to_codex(state, args), 1)
        self.assertIn("Changed description", target.read_text(encoding="utf-8"))
