"""Regressions reproduced during the iterative correctness/performance review."""
import json
import io
import contextlib
import copy
import os
import sqlite3
import subprocess
from argparse import Namespace
from pathlib import Path
from unittest import mock

from test_sync import Workspace, IDS
from agent_session_sync.adapters import Muse, SessionRef, CommandAdapter
from agent_session_sync.discovery import Installation, discover, load_config
from agent_session_sync.cli import hook, legacy_arguments, parser, main
from agent_session_sync.automation import setup
from agent_session_sync.io import SyncError, json_bytes, read_records, digest, resolved_workspace
from agent_session_sync.model import Event, Session, MARKER
from agent_session_sync.store import Store
from agent_session_sync.render import compact, DEFAULT_BUDGET
from agent_session_sync.model import identity


class ReviewRegressions(Workspace):
    def test_shared_startup_hook_resolves_profile_from_transcript(self):
        work = Muse(Installation("muse", "work", str(self.base / "muse-work")))
        self.adapters["muse@work"] = work
        path = work.home / "sessions/2030/01/01/new-thread/session.jsonl"
        path.parent.mkdir(parents=True)
        records = [{"schema_version": 1, "payload_type": "runtime.session.metadata", "payload": {"record": {"workspace_root": str(self.project)}}},
                   {"schema_version": 1, "recorded_at": 1893456000000, "payload": {"run_id": "new", "event": {"kind": "started", "prompt": "Continue work."}}}]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        engine = self.engine()
        payload = {"session_id": "new-thread", "cwd": str(self.project), "transcript_path": str(path), "source": "startup"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            result = hook(engine, Namespace(harness="muse", event="start"))
        self.assertIn("hookSpecificOutput", result)
        self.assertIsNotNone(engine.store.db.execute("SELECT value FROM metadata WHERE key='binding:muse@work:new-thread'").fetchone())
        self.assertIsNone(engine.store.db.execute("SELECT value FROM metadata WHERE key='binding:muse:new-thread'").fetchone())

    def test_ambiguous_shared_hook_does_not_assume_default_profile(self):
        self.adapters["muse@work"] = Muse(Installation("muse", "work", str(self.base / "muse-work")))
        engine = self.engine()
        payload = {"session_id": "unknown", "cwd": str(self.project), "source": "startup"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stderr(io.StringIO()) as diagnostics:
            self.assertEqual(hook(engine, Namespace(harness="muse", event="start")), {})
        self.assertIn("cannot identify", diagnostics.getvalue())
        self.assertFalse((self.state / "handoffs").exists())

    def test_sync_repairs_each_missing_completed_artifact(self):
        engine = self.engine()
        result = engine.handoff(self.read("muse"), "claude")
        folder = Path(result["context"]).parent
        for path in (Path(result["target"]), Path(result["context"]), Path(result["archive"]),
                     folder / "manifest.json", folder / "transcript.md"):
            with self.subTest(artifact=path.name):
                path.unlink()
                engine.sync(source="muse", destination="claude", days=None)
                self.assertTrue(path.is_file(), engine.errors)
                self.assertFalse(engine.errors)

    def test_repair_does_not_overwrite_continued_target(self):
        engine = self.engine()
        result = engine.handoff(self.read("muse"), "claude")
        path = Path(result["target"])
        records, _, _ = read_records(path)
        self.append(path, {"type": "user", "uuid": "new-turn", "parentUuid": records[-1]["uuid"],
                           "cwd": str(self.project), "message": {"role": "user", "content": "Keep my continuation."}})
        before = path.read_bytes()
        manifest = Path(result["context"]).with_name("manifest.json")
        manifest.unlink()
        engine.sync(source="muse", destination="claude", days=None)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(manifest.exists())

    def test_unknown_text_marker_preserves_every_event(self):
        path = self.paths["muse"]
        records, _, _ = read_records(path)
        records[1]["payload"]["event"]["prompt"] += " Quoted example " + MARKER + "11111111-1111-4111-8111-111111111111]"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        session = self.read("muse")
        events = list(session.events)
        result = self.engine().lineage(session)
        self.assertEqual(result.events, events)
        self.assertEqual(result.parent, "")

    def test_idle_sync_does_not_render_completed_transfers(self):
        engine = self.engine()
        engine.sync(days=None)
        engine.sync(days=None)  # warm generated-source discovery
        with mock.patch.object(engine, "plan", wraps=engine.plan) as plan, \
             mock.patch.object(engine.store, "sessions", wraps=engine.store.sessions) as sessions:
            self.assertEqual(engine.sync(days=None), [])
        plan.assert_not_called()
        sessions.assert_called_once_with(imported_only=True)

    def test_invalid_cache_shapes_are_ignored(self):
        directory = self.base / "discovery-state"
        directory.mkdir()
        for value in ([], "cache", 1, None, {"checked_at": []},
                      {"schema_version": 1, "checked_at": "today", "installations": [{}]}):
            with self.subTest(value=value):
                (directory / "discovery.json").write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(len(discover(dry_run=True, directory=directory)), 5)

    def test_bad_profiles_fail_before_version_probes(self):
        for profile in ({"harness": "muse", "name": "work"}, {"harness": "muse", "name": [], "home": "/tmp"},
                        {"harness": "muse", "name": "work", "home": []}):
            with self.subTest(profile=profile), mock.patch("agent_session_sync.discovery.subprocess.run") as probe:
                with self.assertRaises(SyncError):
                    discover({"profiles": [profile]}, directory=self.base / "absent-state")
                probe.assert_not_called()

    def test_invalid_nested_config_fields_are_actionable(self):
        cases = ({"routes": [{"from": []}]}, {"plugins": [{}]}, {"harnesses": {"muse": {"home": []}}},
                 {"adapters": {"custom": {"command": "echo"}}}, {"auto_handoff": "false"})
        config = self.base / "bad-config.json"
        for value in cases:
            with self.subTest(config=value):
                config.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(SyncError):
                    load_config(config)

    def test_profile_specific_binding_recovers_parent_and_isolates_default(self):
        engine = self.engine()
        transfer = engine.handoff(self.read("codex"), "muse")
        engine.store.db.execute("INSERT INTO metadata VALUES (?, ?)", ("binding:muse@work:same-id", transfer["id"]))
        engine.store.db.commit()
        child = Session("muse", "same-id", str(self.project), [Event("u", "user", "New work turn.")], profile="work")
        default = Session("muse", "same-id", str(self.project), [Event("u", "user", "Independent turn.")])
        self.assertEqual(len(engine.lineage(child).events), 5)
        self.assertEqual(len(engine.lineage(default).events), 1)
        self.assertEqual(default.parent, "")

    def test_different_profiles_of_one_harness_can_receive_handoffs(self):
        engine = self.engine()
        work = Muse(Installation("muse", "work", str(self.base / "muse-work")))
        self.adapters["muse@work"] = work
        source = self.read("muse")
        with mock.patch.object(engine, "scan", return_value=[source]):
            result = engine.incoming("muse@work", str(self.project), exclude_session=source.native_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["source_key"], source.key)

    def test_v1_index_migration_indexes_portable_imports(self):
        directory = self.base / "old-index"
        directory.mkdir()
        db = sqlite3.connect(str(directory / "sessions.sqlite3"))
        db.execute("CREATE TABLE sessions(key TEXT PRIMARY KEY, source_path TEXT, stat TEXT, document TEXT NOT NULL)")
        imported = self.read("muse")
        imported.provenance["portable_import"] = True
        for session in (imported, self.read("codex")):
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session.key, session.source_path, "null", json.dumps(session.to_dict())))
        db.execute("PRAGMA user_version=1")
        db.commit()
        db.close()
        store = Store(directory)
        self.addCleanup(store.close)
        self.assertEqual([s["harness"] for s in store.sessions(imported_only=True)], ["muse"])
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)


class SecondPassRegressions(Workspace):
    def test_unicode_line_separators_inside_json_are_not_record_boundaries(self):
        path = self.base / "unicode.jsonl"
        for separator in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(separator=ord(separator)):
                value = {"text": "first" + separator + "second"}
                path.write_bytes((json.dumps(value, ensure_ascii=False) + "\r\n").encode("utf-8"))
                self.assertEqual(read_records(path)[0], [value])
                with path.open("ab") as stream:
                    stream.write(b'{"incomplete":')
                records, issues, _ = read_records(path)
                self.assertEqual(records, [value])
                self.assertIn("incomplete", issues[0])

    def test_recency_and_inbox_compare_instants_without_rewriting_events(self):
        engine = self.engine()
        earlier = Session("external", "earlier", str(self.project), [Event("a", "user", "Earlier", "2026-10-15T12:00:00+02:00")], provenance={"portable_import": True})
        later = Session("external", "later", str(self.project), [Event("b", "user", "Later", "2026-10-15T10:30:00Z")], provenance={"portable_import": True})
        earlier = engine.store.remember(earlier, imported=True)
        later = engine.store.remember(later, imported=True)
        self.assertEqual(engine.scan(days=None)[0].key, later.key)
        selected = engine.incoming("muse", str(self.project))
        self.assertEqual(selected["source_key"], later.key)
        engine.handoff(earlier, "muse")
        pointer = json.loads(engine.inbox_path("muse", str(self.project)).read_text(encoding="utf-8"))
        self.assertEqual(pointer["source_key"], later.key)
        self.assertEqual(earlier.events[0].timestamp, "2026-10-15T12:00:00+02:00")

    def test_timestamp_precision_does_not_reverse_recency(self):
        session = Session("muse", "precision", str(self.project), [Event("a", "user", "earlier", "2026-10-15T10:00:00Z"),
                          Event("b", "assistant", "later", "2026-10-15T10:00:00.001Z")])
        self.assertEqual(session.updated, "2026-10-15T10:00:00.001Z")
        with self.assertRaises(SyncError):
            Event("bad", "user", "bad timestamp", "tomorrow")

    def test_dispatch_ignores_command_like_option_values(self):
        for value in ("list", "install", "hook", "sync"):
            with self.subTest(value=value):
                args = parser().parse_args(legacy_arguments(["--config", value, "--dry-run"]))
                self.assertEqual(args.command, "sync")
                self.assertEqual(args.config, value)
                args = parser().parse_args(legacy_arguments(["--session", value, "--dry-run"]))
                self.assertEqual(args.session, value)
        args = parser().parse_args(legacy_arguments(["--config", "list", "--install", "--dry-run"]))
        self.assertEqual(args.command, "install")
        args = parser().parse_args(legacy_arguments(["handoff", "session", "--to", "muse", "--note", "--install"][:-2] + ["--note=--install"]))
        self.assertEqual(args.command, "handoff")
        self.assertEqual(args.note, "--install")

    def test_missing_selected_config_is_an_error_but_missing_default_is_optional(self):
        missing = self.base / "absent.json"
        with self.assertRaises(SyncError):
            load_config(missing)
        with mock.patch.dict(os.environ, {"AGENT_SESSION_SYNC_CONFIG": str(missing)}):
            with self.assertRaises(SyncError):
                load_config()
        with mock.patch.dict(os.environ, {"AGENT_SESSION_SYNC_CONFIG": ""}), mock.patch("agent_session_sync.discovery.config_file", return_value=missing):
            self.assertEqual(load_config(), {})

    def test_relative_automation_config_is_absolute_and_works_from_another_cwd(self):
        config = self.base / "relative-config.json"
        config.write_bytes(json_bytes({"routes": [], "harnesses": {i.harness: {"home": i.home} for i in self.installations}}))
        try:
            relative = os.path.relpath(str(config), os.getcwd())
        except ValueError:
            self.skipTest("the temporary directory and the working directory are on different drives")
        for adapter in self.adapters.values():
            adapter.options["settings"] = str(self.base / adapter.endpoint / "settings.json")
        with mock.patch("agent_session_sync.automation.edit_hooks", wraps=__import__("agent_session_sync.automation", fromlist=["edit_hooks"]).edit_hooks) as edit:
            setup(self.adapters, self.state, load_config(relative), config_path=relative, no_cron=True)
        command = edit.call_args_list[0].args[1]
        selected = command[command.index("--config") + 1]
        # Hooks read the captured copy so data roots resolved at install time persist.
        self.assertEqual(selected, str((self.state / "automation-config.json").absolute()))
        result = subprocess.run(command + ["doctor", "--dry-run", "--json"], cwd=str(self.project), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["installations"][0]["home"], resolved_workspace(str(self.adapters["codex"].home)))

    def test_exact_selection_reads_only_its_transcript(self):
        engine = self.engine()
        with contextlib.ExitStack() as stack:
            readers = [stack.enter_context(mock.patch.object(a, "read", wraps=a.read)) for a in self.adapters.values()]
            self.assertEqual(engine.select(IDS["muse"]).native_id, IDS["muse"])
        self.assertEqual(sum(r.call_count for r in readers), 1)

    def test_unrelated_corrupt_transcript_does_not_fail_exact_selection(self):
        self.paths["codex"].write_text("invalid JSON\n", encoding="utf-8")
        engine = self.engine()
        self.assertEqual(engine.select(IDS["muse"]).native_id, IDS["muse"])
        self.assertEqual(engine.errors, [])

    def test_command_adapter_normalizes_identity_and_refetches_remote_stream(self):
        ref = SessionRef("stable-ref", str(self.base / "nonexistent-stream"))
        adapter = CommandAdapter(Installation("external", "default", str(self.base / "external")), {"command": ["fixture"]})
        document = self.read("muse").to_dict()
        self.adapters.clear()
        self.adapters["external"] = adapter
        engine = self.engine()
        def response(operation, **kwargs):
            return {"sessions": [vars(ref)]} if operation == "list" else {"session": copy.deepcopy(document)}
        with mock.patch.object(adapter, "request", side_effect=response) as requests:
            first = engine.scan(days=None)[0]
            document["events"][-1]["text"] = "Remote stream changed."
            second = engine.scan(days=None)[0]
        self.assertEqual(first.native_id, ref.native_id)
        self.assertEqual(first.source_path, ref.path)
        self.assertEqual(second.events[-1].text, "Remote stream changed.")
        self.assertEqual(sum(c.args[0] == "read" for c in requests.call_args_list), 2)

    def test_command_adapter_local_file_uses_its_filesystem_fingerprint(self):
        path = self.base / "external.json"
        path.write_text("fingerprint", encoding="utf-8")
        ref = SessionRef("stable-ref", str(path))
        adapter = CommandAdapter(Installation("external", "default", str(self.base / "external")), {"command": ["fixture"]})
        document = self.read("muse").to_dict()
        self.adapters.clear()
        self.adapters["external"] = adapter
        engine = self.engine()
        with mock.patch.object(adapter, "request", side_effect=lambda op, **kw: {"sessions": [vars(ref)]} if op == "list" else {"session": copy.deepcopy(document)}) as requests:
            engine.scan(days=None)
            engine.scan(days=None)
        self.assertEqual(sum(c.args[0] == "read" for c in requests.call_args_list), 1)

    def test_malformed_external_references_are_actionable(self):
        adapter = CommandAdapter(Installation("external", "default", str(self.base)), {"command": ["fixture"]})
        for response in ({}, {"sessions": None}, {"sessions": [None]}, {"sessions": [{"native_id": [], "path": "id"}]}):
            with self.subTest(response=response), mock.patch.object(adapter, "request", return_value=response):
                with self.assertRaises(SyncError):
                    list(adapter.discover())

    def test_native_migration_never_replaces_a_concurrent_legacy_writer(self):
        self.state.mkdir()
        old = self.base / "claude/projects/legacy/10000000-0000-4000-8000-000000000001.jsonl"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"legacy history")
        registry = {"codex_to_claude": {str(self.paths["codex"]): {"target": str(old), "written_sha": digest(old.read_bytes())}}}
        (self.state / "state.json").write_bytes(json_bytes(registry))
        engine = self.engine()
        link = os.link
        def concurrent_writer(source, destination):
            if Path(destination).suffix == ".jsonl":
                old.write_bytes(old.read_bytes() + b" plus continuation")
            return link(source, destination)
        with mock.patch("agent_session_sync.io.os.link", side_effect=concurrent_writer):
            result = engine.handoff(self.read("codex"), "claude")
        self.assertNotEqual(result["target"], str(old))
        self.assertEqual(old.read_bytes(), b"legacy history plus continuation")

    def test_legacy_continuations_remain_discoverable_after_migration(self):
        self.state.mkdir()
        old = self.base / "claude/projects/legacy/10000000-0000-4000-8000-000000000001.jsonl"
        old.parent.mkdir(parents=True)
        records, _, _ = read_records(self.paths["claude"])
        records[0]["parentUuid"] = "legacy-meta"
        records.insert(0, {"type": "user", "uuid": "legacy-meta", "parentUuid": None, "isMeta": True,
                           "message": {"role": "user", "content": "[Imported from OpenAI Codex CLI session legacy]"}})
        old.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        (self.state / "state.json").write_bytes(json_bytes({"codex_to_claude": {str(self.paths["codex"]): {"target": str(old), "written_sha": digest(old.read_bytes())}}}))
        engine = self.engine()
        self.append(old, {"type": "user", "uuid": "continued", "parentUuid": records[-1]["uuid"], "cwd": str(self.project), "message": {"role": "user", "content": "Continue the legacy task."}})
        continued = engine.lineage(self.adapters["claude"].read(SessionRef(old.stem, str(old))))
        self.assertTrue(continued.parent.startswith("legacy:"))
        self.assertEqual(continued.events[-1].text, "Continue the legacy task.")


class ThirdPassRegressions(Workspace):
    def test_quoting_a_known_transfer_does_not_remove_a_real_prompt(self):
        engine = self.engine()
        transfer = engine.handoff(self.read("codex"), "muse", mode="context")
        source = self.read("muse")
        source.events[0].text = "Explain this marker: " + MARKER + transfer["id"] + "]"
        source.provenance = {"transfer_id": transfer["id"], "context": True}
        result = engine.lineage(source)
        self.assertEqual(result.events, source.events)
        self.assertEqual(result.parent, "")

    def test_authoritative_binding_preserves_quoted_markers_and_recovers_parent(self):
        engine = self.engine()
        parent = self.read("codex")
        transfer = engine.handoff(parent, "muse", mode="context")
        source = self.read("muse")
        source.events[0].text = "Explain " + MARKER + "11111111-1111-4111-8111-111111111111]"
        source.provenance = {"transfer_id": "11111111-1111-4111-8111-111111111111", "context": True}
        engine.store.db.execute("INSERT INTO metadata VALUES (?, ?)", ("binding:muse:" + source.native_id, transfer["id"]))
        engine.store.db.commit()
        result = engine.lineage(source)
        self.assertEqual(len(result.events), len(parent.events) + len(source.events))
        self.assertEqual(result.events[-len(source.events)].text, source.events[0].text)
        self.assertIn(parent.key, result.parent)

    def test_small_context_budgets_handle_long_prompts_and_unicode(self):
        source = self.read("muse")
        source.events[0].text = "Original request. " + "界😀" * 5000
        source.events[-1].text = "LATEST CONCLUSION: " + "界😀" * 5000 + " Keep the final instruction."
        for budget in (1024, 1536, 2048, 4096, 32768):
            for note in ("", "A handoff note. " + "界" * 500):
                with self.subTest(budget=budget, note=bool(note)):
                    body, issues = compact(source, self.base / "archive.json", "11111111-1111-4111-8111-111111111111", budget, note,
                                           self.base / "manifest.json")
                    self.assertLessEqual(len(body.encode("utf-8")), budget)
                    self.assertIn("LATEST CONCLUSION", body)
                    self.assertTrue(issues)

    def test_long_tool_turn_keeps_latest_request_and_conclusion(self):
        source = self.read("muse")
        source.events = source.events[:1] + [Event(str(i), "tool_result", "Old diagnostic. " * 1000) for i in range(20)] + [
            Event("final", "assistant", "LATEST CONCLUSION: the regression failed; investigate pagination.")]
        body, _ = compact(source, self.base / "archive.json", "11111111-1111-4111-8111-111111111111", 4096)
        self.assertIn("Fix the login bug", body)
        self.assertIn("LATEST CONCLUSION", body)
        self.assertLess(body.index("Fix the login bug"), body.index("LATEST CONCLUSION"))

    def test_full_handoff_note_survives_manifest_and_repeated_lineage_recovery(self):
        engine = self.engine()
        source = self.read("muse")
        note = "Switch to the new requirements. " + "界" * 3000
        result = engine.handoff(source, "claude", note=note, budget=1024)
        manifest = json.loads(Path(result["context"]).with_name("manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["note"], note)
        path = Path(result["target"])
        records, _, _ = read_records(path)
        self.append(path, {"type": "user", "uuid": "continuation", "parentUuid": records[-1]["uuid"], "cwd": str(self.project),
                           "message": {"role": "user", "content": "Continue with those requirements."}})
        raw = self.adapters["claude"].read(SessionRef(result["native_id"], str(path)))
        before = raw.to_dict()
        first, second = engine.lineage(raw), engine.lineage(raw)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(raw.to_dict(), before)
        self.assertEqual(sum(e.text == note for e in first.events), 1)
        engine.store.db.execute("DELETE FROM transfers")
        engine.store.db.commit()
        recovered = engine.lineage(raw)
        self.assertEqual(sum(e.text == note for e in recovered.events), 1)

    def test_renderer_upgrade_uses_new_artifacts_instead_of_overwriting_v1(self):
        engine = self.engine()
        source = self.read("muse")
        old_id = identity(source.key, source.revision, "claude", "native", str(DEFAULT_BUDGET), "")
        old = self.state / "handoffs" / old_id / "context.md"
        old.parent.mkdir(parents=True)
        old.write_text("Previous renderer output.", encoding="utf-8")
        result = engine.handoff(source, "claude")
        self.assertNotEqual(result["id"], old_id)
        self.assertEqual(old.read_text(encoding="utf-8"), "Previous renderer output.")

    def test_changed_parent_archive_is_rejected_without_forwarding_its_events(self):
        engine = self.engine()
        result = engine.handoff(self.read("muse"), "claude")
        archive = Path(result["archive"])
        document = json.loads(archive.read_text(encoding="utf-8"))
        document["events"][0]["text"] = "CORRUPTED PARENT"
        archive.write_bytes(json_bytes(document))
        path = Path(result["target"])
        records, _, _ = read_records(path)
        self.append(path, {"type": "user", "uuid": "continuation", "parentUuid": records[-1]["uuid"],
                           "cwd": str(self.project), "message": {"role": "user", "content": "Keep this continuation."}})
        raw = self.adapters["claude"].read(SessionRef(result["native_id"], str(path)))
        recovered = engine.lineage(raw)
        self.assertFalse(any(event.text == "CORRUPTED PARENT" for event in recovered.events))
        self.assertIn("parent archive unavailable or invalid", " ".join(recovered.issues))
        self.assertEqual(recovered.events[-1].text, "Keep this continuation.")

    def test_portable_metadata_types_fail_before_storage_or_rendering(self):
        baseline = self.read("muse").to_dict()
        changes = ({"title": 123}, {"branch": []}, {"parent": None}, {"source_path": {}},
                   {"issues": [{}]}, {"profile": "work:other"}, {"schema_version": True},
                   {"provenance": {"baseline_count": True}}, {"provenance": {"transfer_id": []}},
                   {"provenance": {"source_revision": "bad"}}, {"provenance": {"portable_import": "yes"}})
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(SyncError):
                    Session.from_dict(dict(baseline, **change))
        document = copy.deepcopy(baseline)
        document["events"][0]["data"] = []
        with self.assertRaises(SyncError):
            Session.from_dict(document)

    def test_custom_provenance_is_not_mistaken_for_an_import(self):
        engine = self.engine()
        session = self.read("muse")
        session.provenance = {"provider": "custom provenance"}
        self.assertIs(engine.lineage(session), session)
        self.assertEqual(session.issues, [])

    def test_claude_content_block_order_is_preserved(self):
        ref = SessionRef("mixed", str(self.base / "mixed.jsonl"))
        assistant = {"type": "assistant", "cwd": str(self.project), "message": {"content": [
            {"type": "tool_use", "id": "call", "name": "Read", "input": {}},
            {"type": "text", "text": "After the call."}, {"type": "text", "text": "Adjacent text."},
            {"type": "image", "source": {"type": "base64", "data": "fixture"}},
            {"type": "text", "text": "After the image."}]}}
        parsed = self.adapters["claude"].parse([assistant], ref)
        self.assertEqual([e.kind for e in parsed.events], ["tool_call", "assistant", "attachment", "assistant"])
        self.assertEqual(parsed.events[1].text, "After the call.\nAdjacent text.")
        user = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call", "content": "Result."},
                                                          {"type": "text", "text": "Then my follow-up."}]}}
        self.assertEqual([e.kind for e in self.adapters["claude"].parse([user], ref).events], ["tool_result", "user"])

    def test_hook_unexpected_exceptions_fail_open_but_normal_commands_do_not_hide_them(self):
        with mock.patch("agent_session_sync.cli.execute", side_effect=RuntimeError("unexpected adapter failure")):
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["hook", "--harness", "muse", "--event", "start"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {})
            self.assertIn("unexpected adapter failure", errors.getvalue())
            with self.assertRaises(RuntimeError):
                main(["list"])

    def test_bad_hook_fields_are_actionable(self):
        engine = self.engine()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"workspacePaths": [123]}))):
            with self.assertRaises(SyncError):
                hook(engine, Namespace(harness="muse", event="start"))
