"""Recognize executed utility commands without matching unrelated arguments."""
import re
import shlex

SESSION = "agent-session-sync"
MEMORY = "memory-sync"


def _basename(value):
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _script_owner(value):
    name = _basename(value)
    if name in ("agent-session-sync", "agent-session-sync.exe", "agent-session-sync.py"):
        return SESSION
    if name in ("memory-sync", "memory-sync.exe", "memory-sync.py", "memory_sync.py"):
        return MEMORY
    if name == "compat.py" and value.replace("\\", "/").rsplit("/", 2)[-2:-1] == ["agent_session_sync"]:
        return SESSION
    return None


# env options that take a separate value (GNU and BSD spellings).
ENV_VALUE_OPTIONS = {"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"}


def _strip_env(args):
    """The command env runs, past its options and NAME=value assignments."""
    args = args[1:]
    while args:
        arg = args[0]
        if arg == "--":
            return args[1:]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            args = args[1:]
        elif arg in ("-S", "--split-string") or arg.startswith(("-S", "--split-string=")):
            # -S splits one string into the command and its leading arguments.
            if arg in ("-S", "--split-string"):
                value, rest = (args[1] if len(args) > 1 else ""), args[2:]
            else:
                value, rest = (arg.split("=", 1)[1] if arg.startswith("--") else arg[2:]), args[1:]
            try:
                return shlex.split(value) + rest
            except ValueError:
                return []
        elif arg in ENV_VALUE_OPTIONS:
            args = args[2:]
        elif arg.startswith("-") and arg != "-":
            args = args[1:]  # -i, -0, -v, --ignore-environment, -uNAME, --unset=NAME, ...
        else:
            break
    return args


def _words(command):
    """Candidate argument lists: POSIX words, plus Windows words when the command contains
    backslashes, which POSIX quoting consumes (C:\\tools becomes C:tools)."""
    candidates = []
    try:
        candidates.append(shlex.split(command))
    except ValueError:
        pass
    if "\\" in command:
        try:
            words = shlex.split(command, posix=False)
        except ValueError:
            words = command.split()
        candidates.append([w[1:-1] if len(w) >= 2 and w[0] == w[-1] == '"' else w for w in words])
    return candidates


def command_owner(command):
    if isinstance(command, str):
        for args in _words(command):
            owner = _args_owner(args)
            if owner:
                return owner
        return None
    if isinstance(command, list) and all(isinstance(arg, str) for arg in command):
        return _args_owner(list(command))
    return None


def _args_owner(args):
    while args and (args[0] == "&" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[0])):
        args.pop(0)
    if args and _basename(args[0]) in ("env", "env.exe"):
        args = _strip_env(args)
    if not args:
        return None
    direct = _script_owner(args[0])
    if direct:
        return direct
    if not re.fullmatch(r"(?:pythonw?(?:\d+(?:\.\d+)*)?|pypy\d*|py)(?:\.exe)?", _basename(args[0])):
        return None
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in ("-c", "-", "--help", "-h", "--version", "-V"):
            return None
        if arg == "--":
            return _script_owner(args[index + 1]) if index + 1 < len(args) else None
        if arg == "-m" or arg.startswith("-m"):
            module = args[index + 1] if arg == "-m" and index + 1 < len(args) else arg[2:]
            if module == "agent_session_sync.memory_sync":
                return MEMORY
            return SESSION if module in ("agent_session_sync", "agent_session_sync.__main__", "agent_session_sync.cli", "agent_session_sync.legacy") else None
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            return _script_owner(arg)
    return None


def cron_owner(line):
    stripped = line.strip()
    if stripped.startswith("#"):
        match = re.match(r"^#\s*(agent-session-sync|memory-sync)(?:\s|$)", stripped)
        return match.group(1) if match else None
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
        return None
    if stripped.startswith("@"):
        fields = stripped.split(None, 1)
        return command_owner(fields[1]) if len(fields) == 2 else None
    fields = stripped.split(None, 5)
    return command_owner(fields[5]) if len(fields) == 6 else None
