"""
Performance benchmark tests for Apple Mail operations.

These tests measure operation timings against documented baselines
and detect performance regressions.

Run: make test-benchmark (or pytest tests/benchmarks/ -v)
Requires: Real Mail.app with configured account.

Thresholds are set at 5x the documented baseline to catch regressions
while tolerating normal variance.
"""

import statistics
import time

import pytest

pytestmark = pytest.mark.benchmark


class BenchmarkResult:
    """Statistical summary of benchmark iterations."""

    def __init__(self, name: str, times: list[float]) -> None:
        self.name = name
        self.times = times
        self.mean = statistics.mean(times)
        self.stdev = statistics.stdev(times) if len(times) > 1 else 0.0
        self.min = min(times)
        self.max = max(times)
        self.median = statistics.median(times)
        self.cv = (self.stdev / self.mean * 100) if self.mean > 0 else 0.0

        # Cold start detection: first run > 2x median of remaining
        if len(times) > 2:
            rest_median = statistics.median(times[1:])
            self.cold_start = times[0] > rest_median * 2
        else:
            self.cold_start = False

    def __str__(self) -> str:
        cold = " (cold start detected)" if self.cold_start else ""
        return (
            f"{self.name}: mean={self.mean:.2f}s, "
            f"median={self.median:.2f}s, "
            f"stdev={self.stdev:.2f}s, "
            f"CV={self.cv:.1f}%{cold}"
        )


def _benchmark(name: str, fn: callable, iterations: int = 5) -> BenchmarkResult:  # type: ignore[type-arg]
    """Run a function multiple times and return timing statistics."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    result = BenchmarkResult(name, times)
    print(f"  {result}")
    return result


class TestSearchPerformance:
    """Benchmark search_messages operation.

    Documented baselines:
    - Search typical mailbox: ~1-5s
    - Get single message: <1s
    """

    @pytest.mark.skip(reason="Requires real Mail.app - enable manually")
    def test_search_messages_baseline(self) -> None:
        """Search should complete within 5x baseline."""
        from apple_mail_mcp.mail_connector import AppleMailConnector

        connector = AppleMailConnector()
        br = _benchmark(
            "search_messages (INBOX, limit=10)",
            lambda: connector.search_messages("Gmail", "INBOX", limit=10),
        )
        # Baseline: ~2s, threshold: 10s
        assert br.mean < 10.0, f"Regression: {br.mean:.2f}s (threshold: 10s)"
