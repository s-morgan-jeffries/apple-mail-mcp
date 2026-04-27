"""Pytest harness for the benchmark suite.

The benchmark suite is opt-in:

- `pytest tests/benchmarks/` (default): tests are *collected* but skipped with
  a clear "use --run-benchmark to enable" message.
- `pytest tests/benchmarks/ --run-benchmark`: runs benchmarks against real
  Mail.app, asserting each is within 5x of the committed baseline.
- `pytest tests/benchmarks/ --run-benchmark --capture-baseline`: re-captures
  observed timings into baseline.json instead of asserting. Use after an
  intentional perf change.

The 5x threshold is calibrated for real-machine noise (one slow outlier
shouldn't fail the suite). For tighter regression detection, the median of
five runs is used as the headline number — see `measure_median`.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REGRESSION_RATIO = 5.0
DEFAULT_RUNS = 5
BASELINE_PATH = Path(__file__).parent / "baseline.json"


# ---------------------------------------------------------------------------
# Skip-unless-flag gate
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every test in this directory unless --run-benchmark is given."""
    if config.getoption("--run-benchmark"):
        return
    skip_marker = pytest.mark.skip(
        reason="Benchmarks are opt-in. Use --run-benchmark to enable."
    )
    for item in items:
        # Only mark items in this directory; the hook fires globally.
        if "tests/benchmarks/" in str(item.fspath):
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Timing harness
# ---------------------------------------------------------------------------

class BenchmarkResult:
    """Statistical summary of N timed runs of an operation.

    Cold-start detection: the first run is flagged if it's more than 2x the
    median of the remaining runs. Common with operations that warm up Mail.app
    state (e.g., the first IMAP connection or the first AppleScript call after
    a long idle).
    """

    def __init__(self, name: str, times: list[float]) -> None:
        self.name = name
        self.times = times
        self.mean = statistics.mean(times)
        self.median = statistics.median(times)
        self.stdev = statistics.stdev(times) if len(times) > 1 else 0.0
        self.min = min(times)
        self.max = max(times)
        self.cv = (self.stdev / self.mean * 100) if self.mean > 0 else 0.0
        if len(times) > 2:
            rest_median = statistics.median(times[1:])
            self.cold_start = times[0] > rest_median * 2
        else:
            self.cold_start = False

    def __str__(self) -> str:
        cold = " [cold-start]" if self.cold_start else ""
        return (
            f"{self.name}: median={self.median:.2f}s "
            f"(mean={self.mean:.2f}s, stdev={self.stdev:.2f}s, "
            f"min={self.min:.2f}s, max={self.max:.2f}s, CV={self.cv:.1f}%)"
            f"{cold}"
        )


def measure_median(
    fn: Callable[[], Any], *, runs: int = DEFAULT_RUNS, name: str = ""
) -> BenchmarkResult:
    """Run `fn` `runs` times, return the timing summary.

    Median (not mean) is the headline number because it tolerates a single
    slow outlier (e.g., a transient network hiccup or Mail.app GC pause)
    without skewing the comparison against baseline.
    """
    times: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    result = BenchmarkResult(name, times)
    print(f"  {result}")
    return result


# ---------------------------------------------------------------------------
# Baseline I/O + assertion
# ---------------------------------------------------------------------------

def _load_baselines() -> dict[str, float]:
    if not BASELINE_PATH.exists():
        return {}
    with BASELINE_PATH.open() as f:
        return json.load(f)


def _save_baselines(baselines: dict[str, float]) -> None:
    BASELINE_PATH.write_text(
        json.dumps(baselines, indent=2, sort_keys=True) + "\n"
    )


@pytest.fixture(scope="session")
def baselines() -> dict[str, float]:
    """Loaded baseline timings, keyed by benchmark name."""
    return _load_baselines()


# Mutable session-scoped collector for capture mode. Tests append observed
# results here and the session-finalizer writes baseline.json once at the end.
_captured: dict[str, float] = {}


@pytest.fixture(scope="session")
def capture_mode(request: pytest.FixtureRequest) -> bool:
    """True when the user passed --capture-baseline; recording mode is on."""
    return bool(request.config.getoption("--capture-baseline"))


def assert_within_baseline(
    name: str,
    result: BenchmarkResult,
    baselines: dict[str, float],
    capture_mode: bool,
    *,
    ratio: float = REGRESSION_RATIO,
) -> None:
    """In compare mode: fail if median > ratio * baseline.
    In capture mode: stash the observed value for end-of-session writeout."""
    if capture_mode:
        _captured[name] = round(result.median, 3)
        return
    if name not in baselines:
        pytest.skip(
            f"No baseline for {name!r}. "
            f"Run with --capture-baseline to create one."
        )
    baseline = baselines[name]
    threshold = baseline * ratio
    assert result.median <= threshold, (
        f"Regression: {name} median={result.median:.2f}s "
        f"exceeds {ratio}x baseline ({baseline:.2f}s, threshold "
        f"{threshold:.2f}s). If this is an intentional change, "
        f"refresh with `make benchmark-baseline`."
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """When --capture-baseline was set, write the collected timings."""
    if not session.config.getoption("--capture-baseline"):
        return
    if not _captured:
        return
    # Merge with any existing baselines so partial runs don't wipe other
    # entries (e.g., capturing only search benchmarks shouldn't clobber
    # bulk_ops baselines).
    merged = _load_baselines()
    merged.update(_captured)
    _save_baselines(merged)
    print(
        f"\nbaseline.json updated with {len(_captured)} entries: "
        f"{sorted(_captured.keys())}"
    )
