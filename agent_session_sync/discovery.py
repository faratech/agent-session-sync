"""Cross-platform, cached executable and history discovery."""
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .io import SyncError, atomic_write, digest, json_bytes, resolved_workspace

NAMES = ("codex", "claude", "muse", "agy", "copilot")
CACHE_TTL = 86400


def env(name, default):
    """An environment value, treating set-but-empty as unset (per XDG)."""
    return os.environ.get(name) or default


def state_dir():
    if os.environ.get("AGENT_SESSION_SYNC_STATE_DIR"):
        return Path(os.environ["AGENT_SESSION_SYNC_STATE_DIR"]).expanduser()
    if os.name == "nt":
        return Path(env("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "agent-session-sync"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/var/lib/agent-session-sync")
    return Path(env("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-session-sync"


def config_file():
    if os.environ.get("AGENT_SESSION_SYNC_CONFIG"):
        return Path(os.environ["AGENT_SESSION_SYNC_CONFIG"]).expanduser()
    base = env("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    if os.name == "nt":
        base = env("LOCALAPPDATA", base)
    return Path(base) / "agent-session-sync/config.json"


def load_config(path=None):
    selected = path is not None or bool(os.environ.get("AGENT_SESSION_SYNC_CONFIG"))
    path = (Path(path).expanduser() if path is not None else config_file()).resolve()
    if not path.exists():
        if selected:
            raise SyncError("selected configuration file does not exist: {}".format(path))
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise SyncError("cannot read configuration {}: {}".format(path, exc))
    result = validate_config(result)
    # Resolve known path fields once at the configuration boundary. Executable
    # names without separators intentionally remain PATH lookups.
    options = list(result.get("harnesses", {}).values()) + list(result.get("adapters", {}).values()) + result.get("profiles", [])
    def resolve_value(value, follow_links=True):
        if value.lower().startswith("file:"):
            return resolved_workspace(value)
        target = Path(value).expanduser()
        target = target if target.is_absolute() else path.parent / target
        # Executables keep their configured link so self-updating harnesses stay reachable.
        return resolved_workspace(str(target)) if follow_links else os.path.abspath(str(target))
    for item in options:
        for field in ("home", "settings", "executable"):
            value = item.get(field)
            if value is None or field == "executable" and not any(c in value for c in ("/", "\\")):
                continue
            item[field] = resolve_value(value, follow_links=field != "executable")
        if item.get("workspace_map"):
            item["workspace_map"] = {key: resolve_value(value) if value else "" for key, value in item["workspace_map"].items()}
    for route in result.get("routes", []):
        if route.get("project"):
            route["project"] = resolve_value(route["project"])
    return result


def validate_config(result):
    """Reject malformed user input before probing executables or writing caches."""
    if not isinstance(result, dict):
        raise SyncError("configuration must be a JSON object")
    for key, expected in (("harnesses", dict), ("adapters", dict), ("profiles", list), ("plugins", list), ("routes", list)):
        if key in result and not isinstance(result[key], expected):
            raise SyncError("configuration {} must be a {}".format(key, expected.__name__))
    if any(not isinstance(item, dict) for key in ("profiles", "routes") for item in result.get(key, [])):
        raise SyncError("profiles and routes must contain objects")
    if any(not isinstance(v, dict) for key in ("harnesses", "adapters") for v in result.get(key, {}).values()):
        raise SyncError("harness and adapter options must be objects")
    def string(value, label):
        if not isinstance(value, str) or not value.strip():
            raise SyncError(label + " must be a nonempty string")

    def options(value, label):
        for field in ("home", "executable", "settings"):
            if field in value:
                string(value[field], label + "." + field)
        for field in ("enabled", "auto_handoff"):
            if field in value and not isinstance(value[field], bool):
                raise SyncError(label + "." + field + " must be a boolean")
        for field in ("command", "launch"):
            if field in value:
                args = value[field]
                if not isinstance(args, list) or not args or any(not isinstance(arg, str) for arg in args) or not args[0].strip():
                    raise SyncError(label + "." + field + " must be a nonempty argument list")
        if "timeout" in value and (isinstance(value["timeout"], bool) or not isinstance(value["timeout"], (int, float))
                                   or not 0 < value["timeout"] <= 2147483):
            raise SyncError(label + ".timeout must be a positive number no greater than 2147483 seconds")
        if "workspace_map" in value and (not isinstance(value["workspace_map"], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value["workspace_map"].items())):
            raise SyncError(label + ".workspace_map must map session IDs to workspace strings")

    if "auto_handoff" in result and not isinstance(result["auto_handoff"], bool):
        raise SyncError("auto_handoff must be a boolean")
    for group in ("harnesses", "adapters"):
        for name, value in result.get(group, {}).items():
            options(value, group + "." + name)
    for profile in result.get("profiles", []):
        options(profile, "profile")
        if not profile.get("enabled", True):
            continue
        if not isinstance(profile.get("harness"), str) or profile["harness"] not in NAMES:
            raise SyncError("profile.harness must name a built-in harness")
        string(profile.get("name"), "profile.name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", profile["name"]):
            raise SyncError("profile.name may contain letters, digits, underscores, and hyphens")
        if profile["name"].startswith("imported-"):
            raise SyncError("profile names beginning imported- are reserved for portable snapshots")
        string(profile.get("home"), "profile.home")
    for route in result.get("routes", []):
        string(route.get("from"), "route.from")
        string(route.get("to"), "route.to")
        if "project" in route:
            string(route["project"], "route.project")
    for name in result.get("plugins", []):
        string(name, "plugin name")
    return result


def default_homes():
    home = Path.home()
    data = Path(env("XDG_DATA_HOME", str(home / ".local/share")))
    muse = data / "muse"
    if not os.environ.get("XDG_DATA_HOME"):
        candidates = [muse, home / "Library/Application Support/muse"]
        for variable in ("LOCALAPPDATA", "APPDATA"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / "muse")
        muse = next((p for p in candidates if (p / "sessions").is_dir()), muse)
    return {
        "codex": Path(env("CODEX_HOME", str(home / ".codex"))),
        "claude": Path(env("CLAUDE_CONFIG_DIR", str(home / ".claude"))),
        "muse": Path(env("MUSE_HOME", str(muse))),
        "agy": Path(env("AGY_HOME", str(home / ".gemini/antigravity-cli"))),
        "copilot": Path(env("COPILOT_HOME", str(home / ".copilot"))),
    }


def executable_candidates(name, platform=None, environ=None, home=None):
    """Known user install locations supplement PATH, including scheduler environments."""
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    windows = (os.name == "nt") if platform is None else platform == "nt"
    dirs = [home / ".local/bin", home / "bin", home / ".cargo/bin",
            home / ".npm-global/bin", home / ".npm/bin"]
    for key in ("PNPM_HOME", "NPM_CONFIG_PREFIX"):
        if env.get(key):
            dirs.extend([Path(env[key]), Path(env[key]) / "bin"])
    if windows:
        local = Path(env.get("LOCALAPPDATA") or str(home / "AppData/Local"))
        roaming = Path(env.get("APPDATA") or str(home / "AppData/Roaming"))
        dirs += [roaming / "npm", local / "Microsoft/WinGet/Links", local / "Microsoft/WindowsApps",
                 local / "Programs" / name, local / name / "bin", home / "scoop/shims"]
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if env.get(key):
                dirs.extend([Path(env[key]) / name, Path(env[key]) / name / "bin"])
        extensions = (".exe", ".cmd", ".bat", ".com", "")
    else:
        dirs += [Path("/usr/local/bin"), Path("/usr/bin"), Path("/opt/homebrew/bin")]
        extensions = ("",)
    return [directory / (name + extension) for directory in dirs for extension in extensions]


def find_executable(name, explicit=None):
    if explicit:
        found = shutil.which(str(explicit))
        return str(Path(found).absolute()) if found else None
    found = shutil.which(name)
    if found:
        return str(Path(found).absolute())
    for candidate in executable_candidates(name):
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate.absolute())
    if os.name == "nt":
        # App Paths is an optional install location; do not enumerate unrelated programs.
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(hive, "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name + ".exe")
                with key:
                    candidate = Path(winreg.QueryValue(key, None))
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                pass
    return None


def signature(path):
    if not path:
        return None
    try:
        stat = Path(path).stat()
        return [stat.st_mtime_ns, stat.st_size]
    except OSError:
        return None


@dataclass
class Installation:
    harness: str
    profile: str
    home: str
    executable: str = ""
    version: str = "unknown"
    history_exists: bool = False

    @property
    def endpoint(self):
        return self.harness if self.profile == "default" else self.harness + "@" + self.profile


def discover(config=None, refresh=False, dry_run=False, directory=None):
    config = validate_config({} if config is None else config)
    path = Path(directory or state_dir()) / "discovery.json"
    specs = []
    for name, home in default_homes().items():
        override = config.get("harnesses", {}).get(name, {})
        if override.get("enabled", True):
            spec = dict(harness=name, profile="default", home=str(home))
            spec.update({k: v for k, v in override.items() if k in ("home", "executable")})
            specs.append(spec)
    for profile in config.get("profiles", []):
        if not profile.get("enabled", True):
            continue
        if profile.get("harness") not in NAMES or not profile.get("name"):
            raise SyncError("profiles require a built-in harness and a name")
        specs.append(dict(harness=profile["harness"], profile=profile["name"],
                          home=profile["home"], executable=profile.get("executable")))
    endpoints = [(s["harness"], s["profile"]) for s in specs]
    if len(endpoints) != len(set(endpoints)):
        raise SyncError("duplicate harness profile")
    # Resolve paths cheaply on every run. Version subprocesses are cached.
    for spec in specs:
        spec["home"] = str(Path(spec["home"]).expanduser().absolute())
        spec["executable"] = find_executable(spec["harness"], spec.get("executable")) or ""
        spec["signature"] = signature(spec["executable"])
    fingerprint = digest(json_bytes({"specs": specs, "path": os.environ.get("PATH", ""),
                                    "pathext": os.environ.get("PATHEXT", ""), "platform": os.name}))
    cache = {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    if not isinstance(cache, dict):
        cache = {}
    checked = cache.get("checked_at")
    cached_installations = cache.get("installations")
    if (cache.get("schema_version") != 1 or not isinstance(cache.get("fingerprint"), str)
            or isinstance(checked, bool) or not isinstance(checked, (int, float)) or not 0 <= checked <= 253402300799
            or not isinstance(cached_installations, list)
            or any(not isinstance(item, dict) or any(not isinstance(item.get(field), str)
                   for field in ("endpoint", "harness", "profile", "home", "executable", "version")) for item in cached_installations)):
        cache = {}
    fresh = (not refresh and cache.get("schema_version") == 1 and cache.get("fingerprint") == fingerprint
             and 0 <= time.time() - cache.get("checked_at", 0) < CACHE_TTL)
    old = {item["endpoint"]: item for item in cache.get("installations", []) if isinstance(item, dict) and "endpoint" in item}
    installations = []
    for spec in specs:
        item = Installation(**{k: v for k, v in spec.items() if k != "signature"})
        item.history_exists = Path(item.home).is_dir()
        previous = old.get(item.endpoint, {})
        if fresh:
            item.version = previous.get("version", "unknown")
        elif item.executable and not dry_run:
            try:
                process = subprocess.run([item.executable, "--version"], stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=3)
                if process.returncode == 0:
                    item.version = (process.stdout or process.stderr).strip().splitlines()[0][:200]
            except (OSError, subprocess.TimeoutExpired, IndexError):
                pass
        installations.append(item)
    if not dry_run and not fresh:
        atomic_write(path, json_bytes({"schema_version": 1, "fingerprint": fingerprint, "checked_at": time.time(),
                                     "installations": [dict(asdict(i), endpoint=i.endpoint) for i in installations]}), replace=True)
    return installations
