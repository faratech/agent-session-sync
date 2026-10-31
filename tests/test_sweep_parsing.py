"""Regressions for the 2026-10-30 sweep: transcript parsing, scanning, and selection."""
import contextlib
import dataclasses
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from test_sync import Workspace, IDS
from agent_session_sync.adapters import Antigravity, Claude, Codex, Copilot, Muse, SessionRef, choose_title, get_adapters
from agent_session_sync.cli import main
from agent_session_sync.discovery import Installation
from agent_session_sync.engine import Engine
from agent_session_sync.io import SyncError, json_bytes, loads, read_records
from agent_session_sync.model import Event, Session, markdown


class SweepParsingTests(Workspace):
    def write_records(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def cli_config(self, only=None):
        harnesses = {i.harness: {"home": i.home, "executable": sys.executable, "settings": str(self.base / i.harness / "settings.json")}
                     for i in self.installations}
        if only:
            harnesses = {name: (options if name in only else {"enabled": False}) for name, options in harnesses.items()}
        config = self.base / "config.json"
        config.write_bytes(json_bytes({"harnesses": harnesses}))
        return ["--state-dir", str(self.state), "--config", str(config)]

    def adapter(self, cls, name, home, options=None):
        return cls(dataclasses.replace(self.adapters[name].installation, home=str(home)), options)

    def claude_message(self, uuid, parent, kind, content, **extra):
        record = {"uuid": uuid, "type": kind, "cwd": str(self.project), "sessionId": IDS["claude"],
                  "timestamp": "2026-10-15T10:00:00Z", "message": {"role": kind, "content": content}}
        if parent is not False:
            record["parentUuid"] = parent
        return dict(record, **extra)

    def test_imports_honor_the_recency_window(self):
        # Issue #89
        engine = self.engine()
        session = self.read("muse")
        for event in session.events:
            event.timestamp = "2020-01-01T00:00:00+00:00"
        imported = engine.store.remember(session, imported=True)
        self.assertNotIn(imported.key, [s.key for s in engine.scan(harness="muse", days=1)])
        self.assertIn(imported.key, [s.key for s in engine.scan(harness="muse", days=None)])
        recent = self.read("muse")
        for event in recent.events:
            event.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        recent.native_id = "recent-import"
        recent = engine.store.remember(recent, imported=True)
        self.assertIn(recent.key, [s.key for s in engine.scan(harness="muse", days=1)])

    def test_empty_first_prompt_does_not_blank_the_title(self):
        # Issue #90
        session = Session("muse", "sid-1", "", [Event("e1", "user", ""), Event("e2", "user", "Real title here")])
        self.assertEqual(session.title, "Real title here")
        self.assertTrue(markdown(session).startswith("# Real title here"))
        self.assertEqual(Session("muse", "sid-2", "", [Event("e1", "user", "  ")]).title, "sid-2")
        self.assertEqual(choose_title(["", "Fix it"], "fallback"), "Fix it")
        self.assertEqual(choose_title(["", " "], "fallback"), "fallback")

    def test_corrupt_archive_is_isolated_to_its_session(self):
        # Issue #97
        engine = self.engine()
        claude = next(s for s in engine.scan(days=None) if s.harness == "claude")
        archive = engine.archive_path(claude)
        for garbage in (b"\x80\x81\x82garbage", b"{not json"):
            with self.subTest(garbage=garbage):
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(garbage)
                engine.errors = []
                results = engine.sync(destination="codex", days=None)
                self.assertTrue(any("corrupt archive" in e and claude.key in e for e in engine.errors), engine.errors)
                # The other sources still reached the destination.
                self.assertTrue(any(r["source_key"].startswith("muse:") for r in results)
                                or engine.store.previous(self.read("muse").key, "codex"), results)

    def test_history_with_invalid_utf8_keeps_the_session(self):
        # Issue #99
        home, sid = self.base / "agy-history", "aaaaaaaa-0000-4000-8000-000000000099"
        self.write_records(home / "brain" / sid / ".system_generated/logs/transcript.jsonl", [
            {"type": "USER_INPUT", "status": "DONE", "step_index": 0, "content": "<USER_REQUEST>Hello</USER_REQUEST>"}])
        (home / "history.jsonl").write_bytes(b'\xff\xfe broken\n' + json.dumps({"conversationId": sid, "workspace": "/work/proj"}).encode() + b"\n")
        adapter = self.adapter(Antigravity, "agy", home)
        session = adapter.read(next(adapter.discover()))
        self.assertEqual(session.workspace, "/work/proj")
        self.assertEqual([e.text for e in session.events], ["Hello"])

    def test_claude_leaf_without_parent_field_excludes_abandoned_branch(self):
        # Issue #100
        sid = IDS["claude"]
        path = self.write_records(self.base / "missing-parent" / (sid + ".jsonl"), [
            self.claude_message("u1", None, "user", "Old prompt."),
            self.claude_message("a1", "u1", "assistant", "ABANDONED"),
            self.claude_message("u2", False, "user", "New prompt."),
        ])
        session = self.adapters["claude"].read(SessionRef(sid, str(path)))
        self.assertEqual([(e.kind, e.text) for e in session.events], [("user", "New prompt.")])
        # A file with no parent links at all is read in order, as before.
        flat = self.write_records(self.base / "flat" / (sid + ".jsonl"), [
            self.claude_message("u1", False, "user", "One."), self.claude_message("a1", False, "assistant", "Two.")])
        self.assertEqual([e.text for e in self.adapters["claude"].read(SessionRef(sid, str(flat))).events], ["One.", "Two."])

    def test_copilot_restart_without_context_keeps_the_workspace(self):
        # Issue #101
        home, sid = self.base / "copilot-restart", "cccccccc-0000-4000-8000-000000000101"
        self.write_records(home / "session-state" / sid / "events.jsonl", [
            {"type": "session.start", "timestamp": "2026-10-15T10:00:00Z", "data": {"context": {"cwd": "/work/proj", "branch": "main"}}},
            {"type": "user.message", "timestamp": "2026-10-15T10:00:01Z", "data": {"content": "Hello"}},
            {"type": "session.start", "timestamp": "2026-10-15T10:00:02Z", "data": {"context": {}}},
        ])
        adapter = self.adapter(Copilot, "copilot", home)
        session = adapter.read(next(adapter.discover()))
        self.assertEqual((session.workspace, session.branch), ("/work/proj", "main"))

    def test_wrongly_typed_fields_are_refused_as_malformed(self):
        # Issue #102
        cases = {
            "codex": [{"type": "session_meta", "payload": {"id": IDS["codex"], "cwd": "/w"}}, {"type": "response_item", "payload": "oops"}],
            "claude": [{"type": "user", "uuid": "u1", "parentUuid": None, "message": "hello"}],
            "muse": [{"schema_version": 1, "payload": "oops"}],
            "agy": [{"type": "USER_INPUT", "status": "DONE", "content": "x", "workspacePaths": None, "tool_calls": 5}],
            "copilot": [{"type": "user.message", "data": "hi"}],
        }
        for name, records in cases.items():
            with self.subTest(name=name):
                path = self.write_records(self.base / ("malformed-" + name) / "t.jsonl", records)
                with self.assertRaises(SyncError):
                    self.adapters[name].read(SessionRef(IDS[name], str(path)))
        # The CLI reports the refusal instead of a traceback.
        path = self.write_records(self.base / "malformed-cli" / "t.jsonl", cases["claude"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(self.cli_config() + ["import", str(path), "--harness", "claude"]), 1)
        self.assertIn("malformed", errors.getvalue())

    def test_muse_prompt_dedup_links_by_intent_and_ignores_missing_ids(self):
        # Issue #103
        sid = "01a13ea5-053b-7301-a70a-41f31f460103"
        stream = {"kind": "session", "id": sid}
        def rec(seq, payload, kind="runtime.session"):
            return {"schema_version": 1, "id": str(seq), "stream": stream, "sequence": seq, "recorded_at": 1792052385797831 + seq,
                    "payload_type": kind, "payload": payload}
        def parse(home, rows):
            self.write_records(home / "sessions/2026/10/15" / sid / "session.jsonl", rows)
            adapter = self.adapter(Muse, "muse", home)
            return [(e.kind, e.text) for e in adapter.read(next(adapter.discover())).events]
        linked = parse(self.base / "muse-intent-display", [
            rec(1, {"event": {"kind": "user_prompt_display", "prompt": "Hello task"}, "intent_id": "i1"}),
            rec(2, {"intent_id": "i1", "model_messages": [{"content": [{"kind": "text", "text": "Hello task"}]}]}, "runtime.user_intent.accepted"),
        ])
        self.assertEqual(linked, [("user", "Hello task")])
        unlinked = parse(self.base / "muse-no-ids", [
            rec(1, {"event": {"kind": "user_prompt_display", "prompt": "First"}}),
            rec(2, {"event": {"kind": "started", "prompt": "Second"}}),
            rec(3, {"model_messages": [{"content": [{"kind": "text", "text": "Third"}]}]}, "runtime.user_intent.accepted"),
        ])
        self.assertEqual(unlinked, [("user", "First"), ("user", "Second"), ("user", "Third")])

    def test_antigravity_string_workspace_paths_are_refused(self):
        # Issue #104
        home, sid = self.base / "agy-string", "bbbbbbbb-0000-4000-8000-000000000104"
        path = self.write_records(home / "brain" / sid / ".system_generated/logs/transcript.jsonl", [
            {"type": "USER_INPUT", "status": "DONE", "content": "x", "workspacePaths": "/work/proj"}])
        adapter = self.adapter(Antigravity, "agy", home)
        with self.assertRaises(SyncError):
            adapter.read(SessionRef(sid, str(path)))
        self.write_records(path, [{"type": "USER_INPUT", "status": "DONE", "content": "x", "workspacePaths": ["/work/proj"]}])
        self.assertEqual(adapter.read(SessionRef(sid, str(path))).workspace, "/work/proj")

    def test_lone_surrogates_are_refused_at_the_model_boundary(self):
        # Issue #106
        document = self.read("muse").to_dict()
        document["events"][0]["text"] = "bad \ud800"
        with self.assertRaises(SyncError):
            Session.from_dict(document)
        document = self.read("muse").to_dict()
        document["title"] = "bad \udfff"
        with self.assertRaises(SyncError):
            Session.from_dict(document)
        with self.assertRaises(SyncError):
            json_bytes({"data": "\ud800"})
        # Reading an archive through the scrubbing loader keeps it usable.
        raw = json.dumps(dict(self.read("muse").to_dict(), title="ok \\ud800")).replace("\\\\ud800", "\\ud800")
        self.assertEqual(Session.from_dict(loads(raw)).title, "ok �")

    def test_codex_import_uses_the_transcripts_own_session_id(self):
        # Issue #113
        renamed = self.base / "renamed.jsonl"
        renamed.write_bytes(self.paths["codex"].read_bytes())
        session = self.adapters["codex"].read(SessionRef("renamed", str(renamed)))
        self.assertEqual(session.native_id, IDS["codex"])
        self.assertEqual(session.key, self.read("codex").key)
        prefix = self.cli_config()
        keys = []
        for name in ("renamed.jsonl", "othername.jsonl"):
            copy = self.base / name
            copy.write_bytes(self.paths["codex"].read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(prefix + ["--json", "import", str(copy), "--harness", "codex"]), 0)
            keys.append(json.loads(output.getvalue())["session"])
        self.assertEqual(keys[0], keys[1])
        self.assertTrue(keys[0].endswith(":" + IDS["codex"]))

    def test_disabled_profiles_do_not_configure_live_adapters(self):
        # Issue #114
        work = Installation("codex", "work", str(self.base / "codex-work"))
        config = {"profiles": [
            {"harness": "codex", "name": "work", "home": str(self.base / "old"), "enabled": False,
             "workspace_map": {"x": "/disabled"}, "auto_handoff": False},
            {"harness": "codex", "name": "work", "home": str(self.base / "codex-work"), "workspace_map": {"x": "/live"}},
            {"harness": "claude", "name": "default", "enabled": False, "workspace_map": {"y": "/disabled"}},
        ]}
        adapters = get_adapters([work, self.installations[1]], config)
        self.assertEqual(adapters["codex@work"].options["workspace_map"], {"x": "/live"})
        self.assertNotIn("auto_handoff", adapters["codex@work"].options)
        self.assertNotIn("workspace_map", adapters["claude"].options)

    def test_empty_selector_names_no_session(self):
        # Issue #115
        # With a single session, "" was a prefix of it and selected it.
        engine = Engine({"claude": self.adapters["claude"]}, self.state)
        self.addCleanup(engine.close)
        self.assertEqual(engine.select(IDS["claude"][:4]).native_id, IDS["claude"])
        for selector in ("", "  "):
            with self.assertRaises(SyncError):
                engine.select(selector)
        engine.close()
        prefix = self.cli_config(only={"claude"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(prefix + ["show", IDS["claude"][:4]]), 0)
            self.assertEqual(main(prefix + ["show", ""]), 1)

    def test_deeply_nested_record_is_refused_without_aborting_the_scan(self):
        # Issue #118
        depth = 3000
        line = '{"type": "user", "uuid": "deep", "parentUuid": null, "x": ' + "[" * depth + '"\\ud800"' + "]" * depth + "}"
        with self.assertRaises(ValueError):
            loads(line)
        sid = "dddddddd-0000-4000-8000-000000000118"
        path = self.paths["claude"].with_name(sid + ".jsonl")
        path.write_text(line + "\n", encoding="utf-8")
        with self.assertRaises(SyncError):
            read_records(path)
        engine = self.engine()
        sessions = engine.scan(days=None)
        self.assertTrue(any(path.name in e for e in engine.errors), engine.errors)
        self.assertIn(self.read("claude").key, [s.key for s in sessions])
        # Recursive text extraction over deep content is refused by the adapter instead of crashing.
        with mock.patch.object(Claude, "parse", side_effect=RecursionError("maximum recursion depth exceeded")):
            with self.assertRaises(SyncError):
                self.read("claude")

    def test_duplicate_native_ids_resolve_to_one_session(self):
        # Issue #119
        sid = IDS["claude"]
        copy = self.paths["claude"].parent.parent / "moved-project" / (sid + ".jsonl")
        copy.parent.mkdir()
        copy.write_bytes(self.paths["claude"].read_bytes())
        self.append(copy, self.claude_message("u-new", "a2", "user", "Continued in the copy."))
        later = time.time() + 5
        os.utime(str(copy), (later, later))
        engine = self.engine()
        sessions = [s for s in engine.scan(days=None) if s.native_id == sid]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].source_path, str(copy.absolute()))
        self.assertTrue(any(str(self.paths["claude"]) in issue for issue in sessions[0].issues), sessions[0].issues)
        self.assertEqual(engine.select(sid).source_path, str(copy.absolute()))


if __name__ == "__main__":
    unittest.main()
