import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_sync import legacy
from agent_session_sync.adapters import BUILTINS, FileAdapter, SessionRef, get_adapters
from agent_session_sync.automation import edit_hooks, setup, command_is_ours, shell_command
from agent_session_sync.cli import hook, main
from agent_session_sync.discovery import Installation, discover, executable_candidates
from agent_session_sync.engine import Engine
from agent_session_sync.io import SyncError, atomic_write, digest, json_bytes, read_records
from agent_session_sync.model import Event, Session, markdown, timestamp
from agent_session_sync.store import single_flight

FIXTURES = Path(__file__).parent / "fixtures"
IDS = {name: str(i) + "0000000-0000-4000-8000-00000000000" + str(i) for i, name in enumerate(BUILTINS, 1)}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project with spaces ü"
        self.project.mkdir()
        self.state = self.base / "state"
        self.installations = [Installation(name, "default", str(self.base / name), sys.executable, "fixture", True) for name in BUILTINS]
        self.adapters = get_adapters(self.installations, {"harnesses": {"agy": {"workspace_map": {IDS["agy"]: str(self.project)}}}})
        self.paths = {}
        for name, adapter in self.adapters.items():
            paths = {
                "codex": adapter.home / "sessions/2026/10/15" / ("rollout-2026-10-15T10-00-00-" + IDS[name] + ".jsonl"),
                "claude": adapter.home / "projects/project" / (IDS[name] + ".jsonl"),
                "muse": adapter.home / "sessions/2026/10/15" / IDS[name] / "session.jsonl",
                "agy": adapter.home / "brain" / IDS[name] / ".system_generated/logs/transcript_full.jsonl",
                "copilot": adapter.home / "session-state" / IDS[name] / "events.jsonl",
            }
            path = paths[name]
            path.parent.mkdir(parents=True)
            records = [json.loads(line) for line in (FIXTURES / (name + ".jsonl")).read_text(encoding="utf-8").splitlines()]
            for record in records:
                if name == "codex" and record.get("type") == "session_meta":
                    record["payload"].update(id=IDS[name], cwd=str(self.project))
                elif name == "claude":
                    record.update(sessionId=IDS[name], cwd=str(self.project))
                elif name == "muse" and record.get("payload_type") == "runtime.session.metadata":
                    record["payload"]["record"]["workspace_root"] = str(self.project)
                elif name == "copilot" and record.get("type") == "session.start":
                    record["data"]["context"]["cwd"] = str(self.project)
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
            self.paths[name] = path
        self.patch = mock.patch.object(legacy, "CODEX_HOME", str(self.base / "codex"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def engine(self, dry=False):
        engine = Engine(self.adapters, self.state, dry)
        self.addCleanup(engine.close)
        return engine

    def read(self, name):
        return self.adapters[name].read(SessionRef(IDS[name], str(self.paths[name])))

    def append(self, path, record):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


class AdapterTests(Workspace):
    def test_all_five_read_user_assistant_and_structured_tools(self):
        for name in BUILTINS:
            with self.subTest(name=name):
                session = self.read(name)
                self.assertEqual(session.workspace, str(self.project))
                self.assertEqual([e.kind for e in session.events], ["user", "tool_call", "tool_result", "assistant"])
                self.assertEqual(session.events[0].text, "Fix the login bug.")
                self.assertEqual(session.title, "Fix the login bug.")
                self.assertIn("login tests pass", session.events[-1].text)
                self.assertNotIn("excluded", markdown(session))

    def test_claude_follows_selected_parent_chain(self):
        self.assertNotIn("Abandoned branch", markdown(self.read("claude")))

    def test_muse_export_and_transaction_envelopes(self):
        records, _, _ = read_records(self.paths["muse"])
        prompt = records.pop(1)
        prompt["payload"]["event"] = {"kind": "user_prompt_display", "prompt": "Fix the login bug."}
        records.insert(1, {"retained_frame": "transaction", "children": [{"record_json": json.dumps(prompt)}]})
        path = self.base / "muse-export.json"
        path.write_text(json.dumps({"export_schema_version": 1, "events": [{"kind": "record", "envelope": r} for r in records]}), encoding="utf-8")
        session = self.adapters["muse"].read(SessionRef("export", str(path)))
        self.assertEqual(len(session.events), 4)
        self.assertEqual(session.events[0].kind, "user")

    def test_antigravity_compact_fallback_reports_source_truncation(self):
        path = self.paths["agy"]
        compact_path = path.with_name("transcript.jsonl")
        records, _, _ = read_records(path)
        records[-1]["truncated_fields"] = ["content"]
        compact_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        path.unlink()
        ref = list(self.adapters["agy"].discover())[0]
        session = self.adapters["agy"].read(ref)
        self.assertIn("source truncated", " ".join(session.issues))

    def test_sixth_file_adapter_without_engine_changes(self):
        home = self.base / "sixth"
        home.mkdir()
        (home / "session.json").write_bytes(json_bytes(self.read("muse").to_dict()))
        installation = Installation("sixth", "default", str(home))
        adapter = FileAdapter(installation, {"launch": ["some-cli", "--read", "{context}"]})
        self.adapters["sixth"] = adapter
        engine = self.engine()
        session = adapter.read(next(iter(adapter.discover())))
        result = engine.handoff(session, "muse")
        self.assertTrue(Path(result["context"]).is_file())
        result = engine.handoff(self.read("claude"), "sixth")
        self.assertEqual(result["command"][:2], ["some-cli", "--read"])

    def test_executable_adapter_protocol(self):
        source = self.base / "portable.json"
        source.write_bytes(json_bytes(self.read("claude").to_dict()))
        command = self.base / "adapter.py"
        command.write_text("""import json,sys
request=json.load(sys.stdin)
if request['operation']=='list':
    response={'sessions':[{'native_id':'custom','path':sys.argv[1]}]}
else:
    response={'session':json.load(open(sys.argv[1],encoding='utf-8'))}
print(json.dumps(dict(protocol_version=1,**response)))
""", encoding="utf-8")
        adapters = get_adapters([], {"adapters": {"custom": {"command": [sys.executable, str(command), str(source)]}}})
        session = adapters["custom"].read(next(iter(adapters["custom"].discover())))
        self.assertEqual(session.harness, "custom")
        self.assertEqual(session.events[0].text, "Fix the login bug.")


class TransferTests(Workspace):
    def test_route_restrictions_apply_to_automatic_startup_handoffs(self):
        engine = self.engine()
        engine.config = {"routes": []}
        self.assertEqual(engine.sync(days=None), [])
        self.assertIsNone(engine.incoming("muse", str(self.project)))
        self.assertFalse((self.state / "handoffs").exists())

    def test_purge_preserves_memory_state_and_unrelated_files(self):
        self.state.mkdir()
        for name in ("memory-state.json", "memory.lock", "unrelated.txt", "discovery.json"):
            (self.state / name).write_text("keep" if name != "discovery.json" else "{}", encoding="utf-8")
        setup(self.adapters, self.state, {}, uninstall=True, purge=True, no_cron=True, no_hooks=True)
        self.assertTrue((self.state / "memory-state.json").exists())
        self.assertTrue((self.state / "memory.lock").exists())
        self.assertTrue((self.state / "unrelated.txt").exists())
        self.assertFalse((self.state / "discovery.json").exists())

    def test_state_loss_preserves_continued_native_file_and_recovers_parent_archive(self):
        engine = self.engine()
        source = self.read("muse")
        result = engine.handoff(source, "claude")
        path = Path(result["target"])
        records, _, _ = read_records(path)
        self.append(path, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"], "cwd": str(self.project),
                           "message": {"role": "user", "content": "Keep this continuation."}})
        before = path.read_bytes()
        engine.store.db.execute("DELETE FROM transfers")
        engine.store.db.commit()
        with self.assertRaises(SyncError):
            engine.handoff(source, "claude")
        self.assertEqual(path.read_bytes(), before)
        recovered = engine.lineage(self.adapters["claude"].read(SessionRef(result["native_id"], str(path))))
        self.assertEqual(len(recovered.events), len(source.events) + 1)

    def test_all_twenty_directed_handoffs_and_native_loop_guard(self):
        engine = self.engine()
        originals = {name: path.read_bytes() for name, path in self.paths.items()}
        count = 0
        for source, destination in itertools.permutations(BUILTINS, 2):
            with self.subTest(source=source, destination=destination):
                result = engine.handoff(self.read(source), destination)
                self.assertIn("Fix the login bug", Path(result["context"]).read_text(encoding="utf-8"))
                archived = Session.from_dict(json.loads(Path(result["archive"]).read_text(encoding="utf-8")))
                self.assertEqual(len(archived.events), 4)
                self.assertTrue(Path(result["context"]).with_name("manifest.json").is_file())
                if destination in ("codex", "claude"):
                    imported = self.adapters[destination].read(SessionRef(result["native_id"], result["target"]))
                    self.assertIsNone(engine.lineage(imported))
                    self.assertIn(result["native_id"], result["command"])
                count += 1
        self.assertEqual(count, 20)
        for name, data in originals.items():
            self.assertEqual(self.paths[name].read_bytes(), data)

    def test_idempotent_artifacts_and_source_metadata_change(self):
        engine = self.engine()
        source = self.read("codex")
        first = engine.handoff(source, "muse")
        files = {str(p): p.read_bytes() for p in self.state.rglob("*") if p.is_file() and p.suffix != ".sqlite3"}
        source.source_sha = "metadata changed without conversation changes"
        second = engine.handoff(source, "muse")
        self.assertEqual(first["id"], second["id"])
        for path, data in files.items():
            self.assertEqual(Path(path).read_bytes(), data)

    def test_native_continuation_returns_as_linked_branch_without_rewriting(self):
        engine = self.engine()
        source = self.read("codex")
        outbound = engine.handoff(source, "claude")
        target = Path(outbound["target"])
        records, _, _ = read_records(target)
        self.append(target, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"],
                            "cwd": str(self.project), "timestamp": "2026-10-15T11:00:00Z",
                            "message": {"role": "user", "content": "Now add regression tests."}})
        continued_bytes = target.read_bytes()
        continued = engine.lineage(self.adapters["claude"].read(SessionRef(outbound["native_id"], str(target))))
        self.assertEqual(len(continued.events), len(source.events) + 1)
        self.assertIn(source.key, continued.parent)
        returned = engine.handoff(continued, "codex")
        self.assertNotEqual(returned["native_id"], source.native_id)
        self.assertIn("regression tests", Path(returned["context"]).read_text(encoding="utf-8"))
        engine.handoff(source, "claude")
        self.assertEqual(target.read_bytes(), continued_bytes)

    def test_unknown_native_destination_and_ledger_loss_never_clobber(self):
        engine = self.engine()
        source = self.read("muse")
        item, files = engine.plan(source, "claude")
        target = Path(item["target"])
        target.parent.mkdir(parents=True)
        target.write_text("unowned history", encoding="utf-8")
        with self.assertRaises(SyncError):
            engine.apply(item, files)
        self.assertEqual(target.read_text(encoding="utf-8"), "unowned history")

    def test_corrupt_middle_refuses_conversion_and_partial_tail_is_reported(self):
        engine = self.engine()
        path = self.paths["codex"]
        self.append(path, {"type": "unused"})
        with path.open("a", encoding="utf-8") as stream:
            stream.write('{"partial":')
        self.assertIn("incomplete trailing", " ".join(self.read("codex").issues))
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.assertEqual(engine.scan(harness="codex", days=None), [])
        self.assertIn("invalid JSON", engine.errors[0])

    def test_budget_is_utf8_bounded_and_archive_keeps_large_tool_text(self):
        engine = self.engine()
        source = self.read("muse")
        source.events.insert(-1, Event("large", "tool_result", "界" * 30000))
        result = engine.handoff(source, "agy", budget=4096)
        self.assertLessEqual(Path(result["context"]).stat().st_size, 4096)
        self.assertIn("界" * 30000, Path(result["archive"]).read_text(encoding="utf-8"))
        self.assertTrue(result["issues"])

    def test_dry_run_creates_nothing_even_with_missing_state(self):
        engine = self.engine(dry=True)
        result = engine.handoff(self.read("codex"), "claude")
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(result["target"]).exists())

    def test_ambiguous_identifier_writes_no_handoffs(self):
        engine = self.engine()
        with self.assertRaisesRegex(SyncError, "ambiguous"):
            engine.select("c")  # a key prefix shared by codex, claude, and copilot
        self.assertFalse((self.state / "handoffs").exists())

    def test_automatic_sync_detected_targets_is_idempotent_and_does_not_loop(self):
        engine = self.engine()
        results = engine.sync(project=str(self.project), days=None)
        self.assertTrue(results)
        self.assertFalse(engine.errors, engine.errors)
        self.assertEqual(engine.sync(project=str(self.project), days=None), [])
        self.assertFalse(engine.errors, engine.errors)

    def test_atomic_commit_detects_target_appearing_during_write(self):
        path = self.base / "target"
        original_link = os.link
        def racing_link(source, destination):
            Path(destination).write_text("continued", encoding="utf-8")
            return original_link(source, destination)
        with mock.patch("agent_session_sync.io.os.link", side_effect=racing_link):
            with self.assertRaises(SyncError):
                atomic_write(path, b"import")
        self.assertEqual(path.read_text(encoding="utf-8"), "continued")

    def test_lock_prevents_overlapping_runs(self):
        with single_flight(self.state):
            with self.assertRaises(SyncError):
                with single_flight(self.state):
                    self.fail("second lock acquired")

    def test_legacy_registry_backup_keeps_old_native_file_and_uses_new_snapshots(self):
        self.state.mkdir()
        target = self.base / "claude/projects/legacy/10000000-0000-4000-8000-000000000001.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text("previous owned import", encoding="utf-8")
        legacy_state = {"codex_to_claude": {str(self.paths["codex"]): {"target": str(target), "written_sha": digest(target.read_bytes())}}}
        (self.state / "state.json").write_bytes(json_bytes(legacy_state))
        engine = self.engine()
        self.assertEqual((self.state / "state-v1.json.bak").read_bytes(), (self.state / "state.json").read_bytes())
        result = engine.handoff(self.read("codex"), "claude")
        self.assertNotEqual(result["target"], str(target))
        self.assertNotEqual(result["native_id"], target.stem)
        self.assertEqual(result["legacy_target"], str(target))
        self.assertEqual(target.read_text(encoding="utf-8"), "previous owned import")
        source = self.read("codex")
        source.events.append(Event("next", "assistant", "A later source turn.", "2026-10-15T12:00:00Z"))
        updated = engine.handoff(source, "claude")
        self.assertNotEqual(updated["target"], result["target"])
        self.assertIn("later source turn", Path(updated["target"]).read_text(encoding="utf-8"))
        self.assertEqual(target.read_text(encoding="utf-8"), "previous owned import")


class DiscoveryTests(unittest.TestCase):
    def test_timestamp_units(self):
        expected = timestamp(1792058520)
        for value in (1792058520000, 1792058520000000, 1792058520000000000):
            self.assertEqual(timestamp(value), expected)
    def test_cache_reuse_refresh_and_executable_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "codex"
            executable.write_text("one", encoding="utf-8")
            config = {"harnesses": {name: {"home": str(root / name)} for name in BUILTINS}}
            with mock.patch("agent_session_sync.discovery.find_executable", side_effect=lambda name, explicit=None: str(executable) if name == "codex" else None), \
                 mock.patch("agent_session_sync.discovery.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "codex 1.0\n", "")) as run:
                first = discover(config, directory=root / "state")
                self.assertEqual(first[0].version, "codex 1.0")
                discover(config, directory=root / "state")
                self.assertEqual(run.call_count, 1)
                executable.write_text("changed binary", encoding="utf-8")
                discover(config, directory=root / "state")
                self.assertEqual(run.call_count, 2)
                discover(config, refresh=True, directory=root / "state")
                self.assertEqual(run.call_count, 3)

    def test_dry_discovery_does_not_probe_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "absent"
            with mock.patch("agent_session_sync.discovery.subprocess.run") as run:
                discover(dry_run=True, directory=state)
                run.assert_not_called()
            self.assertFalse(state.exists())

    def test_windows_user_install_candidates(self):
        candidates = executable_candidates("copilot", platform="nt", environ={"LOCALAPPDATA": "Local", "APPDATA": "Roaming"}, home="User")
        self.assertTrue(any(p.name == "copilot.cmd" and "npm" in str(p) for p in candidates))
        self.assertTrue(any(p.name == "copilot.exe" and "WinGet" in str(p) for p in candidates))


class HookAndCliTests(Workspace):
    def test_memory_companion_dry_run_does_not_create_its_state(self):
        state = self.base / "memory-dry-state"
        env = dict(os.environ, CODEX_HOME=str(self.adapters["codex"].home), CLAUDE_CONFIG_DIR=str(self.adapters["claude"].home),
                   AGENT_SESSION_SYNC_STATE_DIR=str(state))
        entry = Path(__file__).resolve().parent.parent / "memory-sync.py"
        result = subprocess.run([sys.executable, str(entry), "--dry-run", "--quiet"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state.exists())
    @unittest.skipIf(os.name == "nt", "cron is POSIX-only")
    def test_installed_module_cron_is_idempotent_and_uninstalls_surgically(self):
        lines = ["10 * * * * /usr/bin/unrelated-task"]
        def write(value):
            lines[:] = value
        with mock.patch("agent_session_sync.automation.entry_command", return_value=[sys.executable, "-m", "agent_session_sync"]), \
             mock.patch("agent_session_sync.automation.shutil.which", return_value="/usr/bin/crontab"), \
             mock.patch.object(legacy, "_crontab_read", side_effect=lambda: list(lines)), \
             mock.patch.object(legacy, "_crontab_write", side_effect=write):
            setup(self.adapters, self.state, {}, no_hooks=True)
            setup(self.adapters, self.state, {}, no_hooks=True)
            self.assertEqual(sum("-m agent_session_sync" in line for line in lines), 1)
            setup(self.adapters, self.state, {}, uninstall=True, no_hooks=True)
        self.assertEqual(lines, ["10 * * * * /usr/bin/unrelated-task"])

    def test_windows_quoted_hook_is_recognized(self):
        command = shell_command(["C:\\Program Files\\Python\\python.exe", "-m", "agent_session_sync", "hook", "--harness", "muse", "--event", "start"], windows=True)
        self.assertTrue(command_is_ours({"command": command}))

    def test_crontab_read_failure_is_not_treated_as_an_empty_crontab(self):
        with mock.patch.object(legacy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaises(SyncError):
                legacy._crontab_read()

    def test_antigravity_first_prompt_still_receives_automatic_context(self):
        path = self.paths["agy"]
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        first["created_at"] = "2030-01-01T00:00:00Z"
        path.write_text(json.dumps(first) + "\n", encoding="utf-8")
        engine = self.engine()
        payload = {"conversationId": IDS["agy"], "workspacePaths": [str(self.project)], "transcriptPath": str(path)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            result = hook(engine, argparse_args(harness="agy", event="start"))
        self.assertIn("injectSteps", result)

    def test_startup_context_protocol_for_three_context_harnesses(self):
        engine = self.engine()
        self.append(self.paths["codex"], {"timestamp": "2026-10-15T12:00:00Z", "type": "response_item",
                                        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Latest task context."}]}})
        for name, output_key in (("muse", "hookSpecificOutput"), ("agy", "injectSteps"), ("copilot", "additionalContext")):
            with self.subTest(name=name):
                args = argparse_args(harness=name, event="start")
                payload = {"cwd": str(self.project), "session_id": "new-" + name, "source": "startup"}
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                    result = hook(engine, args)
                self.assertIn(output_key, result)
                self.assertIn("Latest task context", json.dumps(result))
                with mock.patch("sys.stdin", io.StringIO(json.dumps(dict(payload, source="resume")))):
                    self.assertEqual(hook(engine, args), {})

    def test_hook_install_is_idempotent_and_preserves_unrelated_entries(self):
        for name in ("claude", "muse", "agy", "copilot"):
            with self.subTest(name=name):
                adapter = self.adapters[name]
                adapter.options["settings"] = str(self.base / name / "settings.json")
                path, _, data, changed = edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"])
                self.assertTrue(changed)
                path.parent.mkdir(parents=True, exist_ok=True)
                cfg = json.loads(data)
                cfg["unrelated"] = {"value": 42}
                path.write_bytes(json_bytes(cfg))
                _, _, data, changed = edit_hooks(adapter, [sys.executable, "-m", "agent_session_sync"])
                self.assertFalse(changed)
                _, _, data, changed = edit_hooks(adapter, [], uninstall=True)
                self.assertTrue(changed)
                self.assertEqual(json.loads(data)["unrelated"], {"value": 42})

    def test_install_dry_run_has_no_side_effects(self):
        for name, adapter in self.adapters.items():
            adapter.options["settings"] = str(self.base / name / "settings.json")
        result = setup(self.adapters, self.state, {}, dry_run=True, no_cron=True)
        self.assertTrue(result)
        self.assertFalse(self.state.exists())
        self.assertFalse(any(self.base.glob("*/settings.json")))

    def test_cli_export_import_and_machine_output(self):
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {i.harness: {"home": i.home, "executable": sys.executable} for i in self.installations}}))
        prefix = ["--state-dir", str(self.state), "--config", str(config)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(prefix + ["export", IDS["codex"], "--json"])
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["schema_version"], 1)
        exported = self.base / "exported.json"
        exported.write_text(output.getvalue(), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as imported_output:
            self.assertEqual(main(prefix + ["import", str(exported)]), 0)
        engine = self.engine()
        imported_key = json.loads(imported_output.getvalue())["session"]
        self.assertEqual(engine.select(imported_key).native_id, IDS["codex"])

    def test_legacy_flag_entrypoint_and_cli_dry_run_write_nothing(self):
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": {i.harness: {"home": i.home, "executable": sys.executable} for i in self.installations}}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--state-dir", str(self.state), "--config", str(config), "--dry-run", "--to-claude", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        self.assertFalse(self.state.exists())

    def test_schema_rejects_unknown_version(self):
        doc = self.read("codex").to_dict()
        doc["schema_version"] = 100
        with self.assertRaises(SyncError):
            Session.from_dict(doc)


def argparse_args(**kwargs):
    from argparse import Namespace
    return Namespace(**kwargs)


if __name__ == "__main__":
    unittest.main()
