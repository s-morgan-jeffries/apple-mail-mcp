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

from apple_mail_mcp import __version__
from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.mail_connector import AppleMailConnector

REGRESSION_RATIO = 5.0
DEFAULT_RUNS = 5
BASELINE_PATH = Path(__file__).parent / "baseline.json"

BENCH_MAILBOX_NAME = "[apple-mail-mcp-bench]"
# Dedicated synthetic source for the generic bulk benchmarks (#287). Seeded
# via IMAP APPEND like the Gmail variant, so the bulk benchmarks never touch
# the user's real mail and don't depend on a real >=50-message mailbox.
BENCH_SOURCE_NAME = "[apple-mail-mcp-bench-source]"
BULK_SIZE = 50

# Gmail variant fixtures (#101): benchmarks against a Gmail account use a
# pair of dedicated mailboxes populated with synthetic IMAP APPEND data so
# the user's real INBOX is never touched. Source holds the 50-msg pool;
# bench is the move-target. Both persist across runs (fixture is idempotent).
GMAIL_BENCH_MAILBOX_NAME = "[apple-mail-mcp-bench-gmail]"
GMAIL_BENCH_SOURCE_NAME = "[apple-mail-mcp-bench-gmail-source]"


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

# baseline.json holds benchmark name -> median seconds (float), plus a
# `_version` string stamp recording the release the timings were captured for
# (the release-artifact gate, #356, checks it). Hence dict[str, Any].
def _load_baselines() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {}
    with BASELINE_PATH.open() as f:
        return json.load(f)


def _save_baselines(baselines: dict[str, Any]) -> None:
    BASELINE_PATH.write_text(
        json.dumps(baselines, indent=2, sort_keys=True) + "\n"
    )


@pytest.fixture(scope="session")
def baselines() -> dict[str, Any]:
    """Loaded baseline timings, keyed by benchmark name (plus `_version`)."""
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
    baselines: dict[str, Any],
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
    # Stamp the release this baseline was captured for, so the release-artifact
    # gate (#356) can tell a fresh baseline from a stale one.
    merged["_version"] = __version__
    _save_baselines(merged)
    print(
        f"\nbaseline.json updated with {len(_captured)} entries "
        f"(_version={__version__}): {sorted(_captured.keys())}"
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
    """Dedicated synthetic source pool for the bulk fixtures, also the
    move-back destination during teardown (#287).

    Creates [apple-mail-mcp-bench-source] if missing and IMAP-APPENDs
    BULK_SIZE synthetic messages into it (idempotent — only appends the
    shortfall). Same approach as ``gmail_bench_source`` so the generic bulk
    benchmarks never touch the user's real mail and don't depend on a real
    >=50-message mailbox existing. Session-scoped; the populate cost is paid
    once per run.

    Skips cleanly if MAIL_TEST_ACCOUNT has no Keychain IMAP creds (the
    synthetic seeding needs the IMAP path). Run `apple-mail-fast-mcp setup-imap
    --account <acct>` first."""
    from imapclient import IMAPClient

    from apple_mail_mcp.exceptions import (
        MailKeychainAccessDeniedError,
        MailKeychainEntryNotFoundError,
    )
    from apple_mail_mcp.keychain import get_imap_password

    mailboxes = connector.list_mailboxes(test_account)
    names = {mb["name"] for mb in mailboxes}
    if BENCH_SOURCE_NAME not in names:
        connector.create_mailbox(account=test_account, name=BENCH_SOURCE_NAME)

    host, port, email = connector._resolve_imap_config(test_account)
    try:
        pw = get_imap_password(test_account, email)
    except (MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError):
        pytest.skip(
            f"No Keychain IMAP creds for {test_account!r}; the synthetic "
            f"bench source can't be seeded. Run `apple-mail-fast-mcp setup-imap "
            f"--account {test_account}`. See docs/guides/BENCHMARKING.md."
        )

    client = IMAPClient(host, port=port, ssl=True, timeout=60)
    client.login(email, pw)
    try:
        info = client.select_folder(BENCH_SOURCE_NAME)
        existing = int(info.get(b"EXISTS", 0))
        if existing < BULK_SIZE:
            client.unselect_folder()
            for i in range(existing, BULK_SIZE):
                client.append(BENCH_SOURCE_NAME, _make_synthetic_message(i))
    finally:
        client.logout()

    return BENCH_SOURCE_NAME


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


# ---------------------------------------------------------------------------
# Gmail-account fixtures (#101)
#
# Synthetic-data approach: instead of touching the user's real Gmail INBOX
# (the iCloud benchmarks do that for iCloud), Gmail benchmarks operate on
# a dedicated pair of mailboxes populated via IMAP APPEND with 50 synthetic
# messages. Real INBOX is never touched; mailboxes are clearly prefixed.
#
# Skip cleanly when MAIL_TEST_ACCOUNT_GMAIL is unset.
# ---------------------------------------------------------------------------


def _make_synthetic_message(i: int) -> bytes:
    """RFC 5322 synthetic message for IMAP APPEND. Distinguishable subject
    so a human inspecting Gmail can see what these are."""
    return (
        f"From: bench-sender-{i}@example.com\r\n"
        f"To: bench@example.com\r\n"
        f"Subject: ZZZ-AMM-BENCH Synthetic Message {i:03d}\r\n"
        f"Message-ID: <bench-{i}@example.invalid>\r\n"
        f"Date: Thu, 01 May 2026 12:00:00 +0000\r\n"
        f"\r\n"
        f"Synthetic benchmark message #{i} for issue #101. "
        f"If you see this in your inbox, the apple-mail-fast-mcp benchmark "
        f"fixture leaked.\r\n"
    ).encode()


@pytest.fixture(scope="session")
def test_account_gmail() -> str:
    """Gmail account name from MAIL_TEST_ACCOUNT_GMAIL. Skip when unset."""
    name = os.getenv("MAIL_TEST_ACCOUNT_GMAIL")
    if not name:
        pytest.skip(
            "MAIL_TEST_ACCOUNT_GMAIL not set. Configure to enable Gmail "
            "benchmarks. See docs/guides/BENCHMARKING.md."
        )
    return name


@pytest.fixture(scope="session")
def gmail_bench_source(
    connector: AppleMailConnector, test_account_gmail: str
) -> str:
    """Create [apple-mail-mcp-bench-gmail-source] (if missing) and ensure
    it holds at least BULK_SIZE synthetic messages via IMAP APPEND.

    Idempotent: only appends what's missing if the mailbox already has
    some messages from a prior run. Session-scoped so the populate cost
    is paid once per test run."""
    from imapclient import IMAPClient

    from apple_mail_mcp.keychain import get_imap_password

    mailboxes = connector.list_mailboxes(test_account_gmail)
    names = {mb["name"] for mb in mailboxes}
    if GMAIL_BENCH_SOURCE_NAME not in names:
        connector.create_mailbox(
            account=test_account_gmail, name=GMAIL_BENCH_SOURCE_NAME
        )

    host, port, email = connector._resolve_imap_config(test_account_gmail)
    pw = get_imap_password(test_account_gmail, email)
    client = IMAPClient(host, port=port, ssl=True, timeout=60)
    client.login(email, pw)
    try:
        info = client.select_folder(GMAIL_BENCH_SOURCE_NAME)
        existing = int(info.get(b"EXISTS", 0))
        if existing < BULK_SIZE:
            client.unselect_folder()
            for i in range(existing, BULK_SIZE):
                client.append(
                    GMAIL_BENCH_SOURCE_NAME, _make_synthetic_message(i)
                )
    finally:
        client.logout()

    return GMAIL_BENCH_SOURCE_NAME


@pytest.fixture(scope="session")
def gmail_bench_mailbox(
    connector: AppleMailConnector, test_account_gmail: str
) -> str:
    """Ensure [apple-mail-mcp-bench-gmail] exists in the Gmail account."""
    mailboxes = connector.list_mailboxes(test_account_gmail)
    names = {mb["name"] for mb in mailboxes}
    if GMAIL_BENCH_MAILBOX_NAME not in names:
        connector.create_mailbox(
            account=test_account_gmail, name=GMAIL_BENCH_MAILBOX_NAME
        )
    return GMAIL_BENCH_MAILBOX_NAME


@pytest.fixture
def gmail_bench_messages(
    connector: AppleMailConnector,
    test_account_gmail: str,
    gmail_bench_source: str,
    gmail_bench_mailbox: str,
) -> Iterator[list[str]]:
    """Move BULK_SIZE synthetic messages from gmail_bench_source to
    gmail_bench_mailbox; yield IDs; drain back on teardown.

    Mirror of bench_messages but for the Gmail account with synthetic
    data — uses ``gmail_mode=True`` for moves to exercise the copy+delete
    path that the benchmarks here measure."""

    def _drain_bench_to_source() -> None:
        while True:
            leftover = connector.search_messages(
                account=test_account_gmail,
                mailbox=gmail_bench_mailbox,
                limit=BULK_SIZE,
            )
            if not leftover:
                break
            try:
                connector.move_messages(
                    [m["id"] for m in leftover],
                    destination_mailbox=gmail_bench_source,
                    account=test_account_gmail,
                    source_mailbox=gmail_bench_mailbox,
                    gmail_mode=True,
                )
            except Exception:
                break

    _drain_bench_to_source()

    source_msgs = connector.search_messages(
        account=test_account_gmail,
        mailbox=gmail_bench_source,
        limit=BULK_SIZE,
    )
    if len(source_msgs) < BULK_SIZE:
        pytest.skip(
            f"gmail_bench_source has {len(source_msgs)} messages; need "
            f"{BULK_SIZE}. The synthetic-data setup may have failed mid-way."
        )
    try:
        connector.move_messages(
            [m["id"] for m in source_msgs],
            destination_mailbox=gmail_bench_mailbox,
            account=test_account_gmail,
            source_mailbox=gmail_bench_source,
            gmail_mode=True,
        )
    except MailAppleScriptError as e:
        pytest.skip(f"gmail_bench_messages setup failed: {e}")

    in_bench = connector.search_messages(
        account=test_account_gmail,
        mailbox=gmail_bench_mailbox,
        limit=BULK_SIZE,
    )
    bench_ids = [m["id"] for m in in_bench[:BULK_SIZE]]

    try:
        yield bench_ids
    finally:
        _drain_bench_to_source()
