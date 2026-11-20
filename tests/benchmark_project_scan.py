"""Synthetic project-filter benchmark with an unrelated 8 MiB history."""
import isolation  # noqa: F401  (first: keeps tests off the real state and config)
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
        # The unrelated history keeps growing, as a session being written does (#128).
        samples = []
        adapter = fixture.adapters["muse"]
        with mock.patch.object(adapter, "read", wraps=adapter.read) as read:
            for index in range(20):
                fixture.append(source, {"schema_version": 1, "sequence": 1000 + index, "payload": {"kind": "event", "run_id": "r1",
                               "event": {"kind": "assistant_message_committed", "text": "more " + str(index)}}})
                start = time.perf_counter()
                engine.scan(project=str(fixture.base / "unrelated-project"), days=None)
                samples.append((time.perf_counter() - start) * 1000)
            print(json.dumps({"case": "growing", "iterations": len(samples), "history_mib": 8,
                              "median_ms": round(statistics.median(samples), 3), "transcript_reads": read.call_count}))
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
