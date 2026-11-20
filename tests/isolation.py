"""Imported first by every test module, before the package: tests never use the real state
directory (/var/lib/agent-session-sync when run as root) or the user's configuration."""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("AGENT_SESSION_SYNC_TEST_ISOLATION"):
    ROOT = tempfile.mkdtemp(prefix="agent-session-sync-tests-")
    atexit.register(shutil.rmtree, ROOT, True)
    os.environ["AGENT_SESSION_SYNC_TEST_ISOLATION"] = ROOT
    os.environ["AGENT_SESSION_SYNC_STATE_DIR"] = os.path.join(ROOT, "state")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(ROOT, "config")
    os.environ.pop("AGENT_SESSION_SYNC_CONFIG", None)
