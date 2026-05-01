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
import os
import statistics
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.mail_connector import AppleMailConnector

REGRESSION_RATIO = 5.0
DEFAULT_RUNS = 5
BASELINE_PATH = Path(__file__).parent / "baseline.json"

BENCH_MAILBOX_NAME = "[apple-mail-mcp-bench]"
BULK_SIZE = 50


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


# ---------------------------------------------------------------------------
# Shared fixtures: connector, test account, bench mailbox
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def connector() -> AppleMailConnector:
    """Single connector reused across the entire benchmark session.

    Generous timeout (10 min) because some setup operations on full
    accounts can be slow — `move_messages` in particular scans every
    account×mailbox pair to find each message ID (see #32). The benchmarks
    themselves are much faster than this; the long timeout is for fixture
    setup and teardown."""
    return AppleMailConnector(timeout=600)


@pytest.fixture(scope="session")
def test_account() -> str:
    """Account name from MAIL_TEST_ACCOUNT (defaults to 'iCloud')."""
    return os.getenv("MAIL_TEST_ACCOUNT", "iCloud")


@pytest.fixture(scope="session")
def bench_source(
    connector: AppleMailConnector, test_account: str
) -> str:
    """First mailbox in the test account that has at least BULK_SIZE
    messages. Used as the source pool for bulk fixtures and as the
    move-back destination during teardown.

    Skips the entire benchmark session if no mailbox has enough
    messages — see BENCHMARKING.md for setup."""
    for mb in ("INBOX", "Archive", "Sent Messages"):
        try:
            results = connector.search_messages(
                account=test_account, mailbox=mb, limit=BULK_SIZE
            )
        except Exception:
            continue
        if len(results) >= BULK_SIZE:
            return mb
    pytest.skip(
        f"Need at least {BULK_SIZE} messages in account {test_account!r} "
        f"(across INBOX/Archive/Sent Messages). See "
        f"docs/guides/BENCHMARKING.md for setup."
    )


@pytest.fixture(scope="session")
def bench_mailbox(
    connector: AppleMailConnector, test_account: str
) -> str:
    """Ensure the [apple-mail-mcp-bench] mailbox exists in the test
    account; create it via create_mailbox if missing. Returns its name."""
    mailboxes = connector.list_mailboxes(test_account)
    names = {mb["name"] for mb in mailboxes}
    if BENCH_MAILBOX_NAME not in names:
        connector.create_mailbox(account=test_account, name=BENCH_MAILBOX_NAME)
    return BENCH_MAILBOX_NAME


@pytest.fixture
def bench_messages(
    connector: AppleMailConnector,
    test_account: str,
    bench_source: str,
    bench_mailbox: str,
) -> Iterator[list[str]]:
    """Populate bench_mailbox with BULK_SIZE messages from bench_source,
    yield their (post-move) IDs, then move every remaining message in
    bench_mailbox back to bench_source.

    The teardown searches bench_mailbox at the end (rather than tracking
    IDs through the test) so that benchmarks which move messages around
    still leave bench_mailbox empty when they're done.

    First-run safety: if bench_mailbox already has leftover messages
    from a previous crashed run, those are drained back to bench_source
    before the fresh BULK_SIZE messages are moved in. This makes the
    fixture idempotent."""

    def _drain_bench_to_source() -> None:
        """Move every message currently in bench_mailbox back to source."""
        # Drain in chunks of BULK_SIZE so we don't try to move 1000s in
        # one shot if something has gone wrong.
        while True:
            leftover = connector.search_messages(
                account=test_account, mailbox=bench_mailbox, limit=BULK_SIZE
            )
            if not leftover:
                break
            try:
                connector.move_messages(
                    [m["id"] for m in leftover],
                    destination_mailbox=bench_source,
                    account=test_account,
                    source_mailbox=bench_mailbox,
                )
            except Exception:
                # If move fails, break to avoid an infinite loop; the
                # teardown will surface the issue.
                break

    # Pre-clean any leftover from a prior failed run.
    _drain_bench_to_source()

    # Move BULK_SIZE fresh messages from source into bench.
    source_msgs = connector.search_messages(
        account=test_account, mailbox=bench_source, limit=BULK_SIZE
    )
    if len(source_msgs) < BULK_SIZE:
        pytest.skip(
            f"bench_source {bench_source!r} returned only {len(source_msgs)} "
            f"messages; need {BULK_SIZE}."
        )
    try:
        connector.move_messages(
            [m["id"] for m in source_msgs],
            destination_mailbox=bench_mailbox,
            account=test_account,
            source_mailbox=bench_source,
        )
    except MailAppleScriptError as e:
        # The bulk-operation cubic-loop bug (#103) makes move_messages
        # impractically slow on accounts with many mailboxes (e.g.,
        # Gmail with 90+ labels in the configuration). Once #103 is
        # fixed, this fixture (and the bulk benchmarks that depend on
        # it) will succeed automatically.
        pytest.skip(
            f"bench_messages setup timed out: {e}. Likely blocked by #103 "
            f"(bulk operations scan all accounts × all mailboxes). The "
            f"bulk benchmarks will activate once that perf bug is fixed."
        )

    # IDs change on move (IMAP UID semantics). Re-fetch.
    in_bench = connector.search_messages(
        account=test_account, mailbox=bench_mailbox, limit=BULK_SIZE
    )
    bench_ids = [m["id"] for m in in_bench[:BULK_SIZE]]

    try:
        yield bench_ids
    finally:
        _drain_bench_to_source()
