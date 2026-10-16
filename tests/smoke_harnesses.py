"""Opt-in live checks using isolated roots and no model provider.

Run with: python3 tests/smoke_harnesses.py -v
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_sync import Workspace, IDS
from agent_session_sync.automation import edit_hooks
from agent_session_sync.io import json_bytes


class OfflineMuseSmoke(Workspace):
    @unittest.skipUnless(shutil.which("muse"), "Muse CLI is not installed")
    def test_muse_echo_receives_startup_context_and_journal_is_readable(self):
        executable = shutil.which("muse")
        self.append(self.paths["codex"], {"timestamp": "2030-01-01T00:00:00Z", "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixture handoff context."}]}})
        config = self.base / "sync-config.json"
        config.write_bytes(json_bytes({"harnesses": {i.harness: {"enabled": i.harness in ("codex", "muse"), "home": i.home}
                                                    for i in self.installations}}))
        config_root = self.base / "config"
        adapter = self.adapters["muse"]
        adapter.options["settings"] = str(config_root / "muse/settings.json")
        entry = Path(__file__).resolve().parent.parent / "agent-session-sync.py"
        command = [sys.executable, str(entry), "--config", str(config), "--state-dir", str(self.state)]
        path, _, data, _ = edit_hooks(adapter, command)
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SHELL", "LANG", "TERM", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")}
        env.update(HOME=str(self.base), USERPROFILE=str(self.base), XDG_CONFIG_HOME=str(config_root),
                   XDG_DATA_HOME=str(self.base), MUSE_HOME=str(adapter.home), NO_COLOR="1")
        process = subprocess.run([executable, "exec", "--provider", "echo", "--workspace", str(self.project),
                                  "--json", "Fixture smoke prompt."], cwd=str(self.project), env=env,
                                 capture_output=True, text=True, timeout=45)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("malformed", process.stderr.lower())
        self.assertNotIn("unsupported", process.stderr.lower())
        self.assertIn("Fixture smoke prompt", process.stdout)
        connection = sqlite3.connect(str(self.state / "sessions.sqlite3"))
        try:
            self.assertGreater(connection.execute("SELECT count(*) FROM metadata WHERE key LIKE 'binding:muse:%'").fetchone()[0], 0,
                               "startup hook did not provide context")
        finally:
            connection.close()
        refs = [ref for ref in adapter.discover() if ref.native_id != IDS["muse"]]
        self.assertTrue(refs, "echo provider did not create a session journal")
        session = adapter.read(refs[-1])
        self.assertTrue(any("Fixture smoke prompt" in e.text for e in session.events if e.kind == "user"))
        self.assertTrue(any(e.kind == "assistant" for e in session.events))


class NativeResumeSmoke(Workspace):
    def run_native(self, name):
        executable = shutil.which(name)
        if not executable:
            self.skipTest(name + " is not installed")
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                self.send_error(403, "offline test blocks external traffic")

            def do_GET(self):
                body = json.dumps({"object": "list", "data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                requests.append(request)
                answer = "Fixture native resume accepted."
                if "count_tokens" in self.path:
                    events = None
                    body = json.dumps({"input_tokens": 10}).encode()
                elif "generatecontent" in self.path.lower():
                    response = {"candidates": [{"content": {"role": "model", "parts": [{"text": answer}]}, "finishReason": "STOP"}],
                                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}
                    events = None
                    body = (("data: " + json.dumps(response) + "\n\n") if "stream" in self.path.lower() else json.dumps(response)).encode()
                elif "responses" in self.path:
                    message = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": answer, "annotations": []}]}
                    response = {"id": "resp_fixture", "object": "response", "status": "completed", "model": "fixture", "output": [message],
                                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
                    events = [
                        ("response.created", {"type": "response.created", "response": dict(response, status="in_progress", output=[])}),
                        ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": dict(message, status="in_progress", content=[])}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "msg_fixture", "output_index": 0, "content_index": 0, "delta": answer}),
                        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": message}),
                        ("response.completed", {"type": "response.completed", "response": response}),
                    ]
                    body = json.dumps(response).encode()
                else:
                    message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture",
                               "content": [{"type": "text", "text": answer}], "stop_reason": "end_turn", "stop_sequence": None,
                               "usage": {"input_tokens": 10, "output_tokens": 5}}
                    events = [
                        ("message_start", {"type": "message_start", "message": dict(message, content=[], stop_reason=None)}),
                        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": answer}}),
                        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                    body = json.dumps(message).encode()
                streaming = events is not None and request.get("stream", False)
                if streaming:
                    body = "".join("event: {}\ndata: {}\n\n".format(event, json.dumps(value)) for event, value in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if streaming or "streamgeneratecontent" in self.path.lower() else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = "http://127.0.0.1:{}".format(server.server_port)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SHELL", "LANG", "TERM", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")}
        env.update(HOME=str(self.base), USERPROFILE=str(self.base), XDG_CONFIG_HOME=str(self.base / "config"),
                   XDG_DATA_HOME=str(self.base / "data"), CODEX_HOME=str(self.adapters["codex"].home),
                   CLAUDE_CONFIG_DIR=str(self.adapters["claude"].home), HTTP_PROXY=endpoint, HTTPS_PROXY=endpoint,
                   ALL_PROXY=endpoint, NO_PROXY="127.0.0.1,localhost", ANTHROPIC_BASE_URL=endpoint,
                   ANTHROPIC_API_KEY="offline-fixture", OPENAI_BASE_URL=endpoint + "/v1", OPENAI_API_KEY="offline-fixture",
                   GEMINI_API_KEY="offline-fixture", GOOGLE_GEMINI_BASE_URL=endpoint,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", NO_COLOR="1")
        source = self.read("muse")
        item = self.engine().handoff(source, name)
        if name == "codex":
            config = self.adapters["codex"].home / "config.toml"
            config.write_text('model="fixture"\nmodel_provider="fixture"\n[model_providers.fixture]\nname="Fixture"\nbase_url="' + endpoint + '/v1"\nwire_api="responses"\nrequires_openai_auth=false\n', encoding="utf-8")
            command = [executable, "exec", "resume", item["native_id"], "Fixture continuation.", "--skip-git-repo-check", "--json"]
        elif name == "claude":
            command = [executable, "--bare", "--resume", item["native_id"], "--model", "fixture", "-p", "Fixture continuation.", "--output-format", "json", "--tools", ""]
        else:
            config = self.base / "sync-config.json"
            config.write_bytes(json_bytes({"harnesses": {i.harness: {"home": i.home, "enabled": i.harness in ("muse", "agy")}
                                                        for i in self.installations}}))
            adapter = self.adapters["agy"]
            adapter.options["settings"] = str(self.base / ".gemini/config/hooks.json")
            # Antigravity's runtime home is fixed under HOME; route its reader there too.
            cfg = json.loads(config.read_text())
            cfg["harnesses"]["agy"]["home"] = str(self.base / ".gemini/antigravity-cli")
            config.write_bytes(json_bytes(cfg))
            entry = Path(__file__).resolve().parent.parent / "agent-session-sync.py"
            path, _, data, _ = edit_hooks(adapter, [sys.executable, str(entry), "--config", str(config), "--state-dir", str(self.state)])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            settings_path = self.base / ".gemini/antigravity-cli/settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_bytes(json_bytes({"modelProvider": "gemini"}))
            self.append(self.paths["muse"], {"schema_version": 1, "sequence": 99, "recorded_at": 1893456000000,
                    "payload": {"kind": "event", "run_id": "r1", "event": {"kind": "assistant_message_committed", "text": "Latest fixture context."}}})
            command = [executable, "--model", "gemini-3.8-flash", "--effort", "low", "-p", "Fixture continuation.", "--output-format", "json"]
        process = subprocess.run(command, cwd=str(self.project), env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(process.returncode, 0, process.stderr + "\n" + process.stdout)
        self.assertTrue(requests, "native resume never reached the local mock model")
        details = ""
        if name == "agy":
            connection = sqlite3.connect(str(self.state / "sessions.sqlite3"))
            details = json.dumps({"stderr": process.stderr, "request_keys": [list(r) for r in requests],
                                  "metadata_keys": [r[0] for r in connection.execute("SELECT key FROM metadata")]})
            connection.close()
        self.assertTrue(any("Fix the login bug" in json.dumps(r) for r in requests), "resumed request omitted imported history: " + details)
        self.assertIn("Fixture native resume accepted", process.stdout)
        if name != "agy":
            refs = [r for r in self.adapters[name].discover() if r.native_id == item["native_id"]]
            self.assertTrue(refs)
            latest = max(refs, key=lambda r: Path(r.path).stat().st_mtime_ns)
            continued = self.engine().lineage(self.adapters[name].read(latest))
            self.assertIsNotNone(continued)
            self.assertTrue(any("Fixture continuation" in e.text for e in continued.events if e.kind == "user"))
            self.assertTrue(any("Fix the login bug" in e.text for e in continued.events if e.kind == "user"))

    def test_codex_native_resume_with_local_mock(self):
        self.run_native("codex")

    def test_claude_native_resume_with_local_mock(self):
        self.run_native("claude")

    def test_antigravity_startup_context_with_local_mock(self):
        self.run_native("agy")


if __name__ == "__main__":
    unittest.main()
