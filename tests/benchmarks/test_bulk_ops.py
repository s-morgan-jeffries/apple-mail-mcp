"""Benchmarks for bulk-mutation operations.

These benchmarks DO mutate Mail.app state, but they revert their changes
in a finally block so the suite is idempotent.

The setup story is honest: real benchmarks require real test data. Each
test fixture documents what state it expects and skips with a clear
message if the precondition isn't met.

v1 covers `mark_as_read` only. `move_messages` is intentionally deferred
to a follow-up — IMAP UIDs change on move, which makes the round-trip-
then-revert pattern fragile. A future PR can add it once a stable
fixture-mailbox setup is documented.
"""

from __future__ import annotations

import os

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import (
    BenchmarkResult,
    assert_within_baseline,
    measure_median,
)

BULK_SIZE = 50


@pytest.fixture(scope="module")
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=120)


@pytest.fixture(scope="module")
def test_account() -> str:
    return os.getenv("MAIL_TEST_ACCOUNT", "iCloud")


@pytest.fixture(scope="module")
def bulk_message_ids(
    connector: AppleMailConnector, test_account: str
) -> list[str]:
    """Return a list of at least BULK_SIZE message IDs from the test
    account. Used as the inputs for bulk benchmarks.

    Skips the module if fewer than BULK_SIZE messages are available — the
    benchmarks are about scaling behavior, so a small N defeats the point.
    """
    for mb in ("INBOX", "Archive", "Sent Messages"):
        try:
            results = connector.search_messages(
                account=test_account, mailbox=mb, limit=BULK_SIZE
            )
        except Exception:
            continue
        if len(results) >= BULK_SIZE:
            return [r["id"] for r in results]
    pytest.skip(
        f"Need at least {BULK_SIZE} messages in account {test_account!r} "
        f"(across INBOX/Archive/Sent Messages) for bulk benchmarks."
    )


def test_mark_as_read_50_msgs(
    connector: AppleMailConnector,
    bulk_message_ids: list[str],
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: bulk-mark-read against BULK_SIZE messages.

    Each iteration toggles read→unread→read on the same message set.
    The benchmark measures the full round-trip (both directions go
    through the same AppleScript path; the per-call cost is what we
    care about for scaling, not the direction).

    Final state: every message in `bulk_message_ids` ends up read.
    For messages that were already read before the test, this is a
    no-op net change. For any that were unread before the test, they
    end up read — a small accepted side effect of running a benchmark
    against real data.
    """
    name = "mark_as_read_50_msgs"

    def run() -> None:
        connector.mark_as_read(bulk_message_ids, read=False)
        connector.mark_as_read(bulk_message_ids, read=True)

    try:
        result: BenchmarkResult = measure_median(run, name=name)
        assert_within_baseline(name, result, baselines, capture_mode)
    finally:
        # Defensive: if a run crashed partway, force-mark-read to leave
        # the test account in a consistent state.
        try:
            connector.mark_as_read(bulk_message_ids, read=True)
        except Exception:
            pass
