"""Repeatable synthetic idle-sync benchmark: python3 tests/benchmark_idle_sync.py."""
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
        engine = fixture.engine()
        engine.sync(days=None)
        engine.sync(days=None)
        samples = []
        with mock.patch.object(engine, "plan", wraps=engine.plan) as plan:
            for _ in range(30):
                start = time.perf_counter()
                engine.sync(days=None)
                samples.append((time.perf_counter() - start) * 1000)
            print(json.dumps({"iterations": len(samples), "median_ms": round(statistics.median(samples), 3),
                              "render_calls": plan.call_count, "max_ms": round(max(samples), 3)}))
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
