"""Synthetic project-filter benchmark with an unrelated 8 MiB history."""
import json
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_sync import Workspace


def main():
    fixture = Workspace()
    fixture.setUp()
    try:
        source = fixture.paths["muse"]
        records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        records[-1]["payload"]["event"]["text"] = "x" * (8 * 1024 * 1024)
        source.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        engine = fixture.engine()
        engine.scan(days=None)
        samples = []
        with mock.patch.object(engine.store, "cached", wraps=engine.store.cached) as cached:
            for _ in range(20):
                start = time.perf_counter()
                engine.scan(project=str(fixture.base / "unrelated-project"), days=None)
                samples.append((time.perf_counter() - start) * 1000)
            print(json.dumps({"iterations": len(samples), "history_mib": 8,
                              "median_ms": round(statistics.median(samples), 3), "document_reads": cached.call_count}))
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
