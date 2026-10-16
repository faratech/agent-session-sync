#!/usr/bin/env python3
"""Example protocol-v1 adapter for a directory of portable JSON exports."""
import json
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1]).expanduser().resolve()
    request = json.load(sys.stdin)
    if request.get("protocol_version") != 1:
        raise ValueError("unsupported protocol_version")
    if request.get("operation") == "list":
        result = {"sessions": [{"native_id": p.stem, "path": str(p)} for p in sorted(root.glob("*.json"))]}
    elif request.get("operation") == "read":
        path = Path(request["path"]).resolve()
        if path.parent != root or path.suffix != ".json":
            raise ValueError("export is outside the configured directory")
        result = {"session": json.loads(path.read_text(encoding="utf-8-sig"))}
    else:
        raise ValueError("unsupported operation")
    print(json.dumps(dict(protocol_version=1, **result)))


if __name__ == "__main__":
    main()
