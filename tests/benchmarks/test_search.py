"""Benchmarks for search_messages."""

from __future__ import annotations

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import (
    BenchmarkResult,
    assert_within_baseline,
    measure_median,
)


@pytest.fixture(scope="module")
def benchmark_mailbox(
    connector: AppleMailConnector, test_account: str
) -> str:
    """Pick a mailbox in the test account that has at least 1 message.

    Tries INBOX, Archive, Sent Messages in that order. Skips if none
    have messages — search benchmarks are meaningless without data.

    Note: this is intentionally separate from `bench_source` (which
    requires BULK_SIZE messages). Search benchmarks work on any size of
    mailbox."""
    for mb in ("INBOX", "Archive", "Sent Messages"):
        try:
            results = connector.search_messages(
                account=test_account, mailbox=mb, limit=1
            )
        except Exception:
            continue
        if results:
            return mb
    pytest.skip(
        f"No mailbox with messages in account {test_account!r}. "
        f"Set MAIL_TEST_ACCOUNT to an account with at least one message."
    )


def test_search_messages_no_filter(
    connector: AppleMailConnector,
    test_account: str,
    benchmark_mailbox: str,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: list-style search with no filters, just a limit.

    Exercises the unfiltered-`whose`-clause path (which we drop the
    `whose` for entirely — see the comment in _search_messages_applescript).
    """
    name = "search_messages_no_filter"
    result: BenchmarkResult = measure_median(
        lambda: connector.search_messages(
            account=test_account, mailbox=benchmark_mailbox, limit=10
        ),
        name=name,
    )
    assert_within_baseline(name, result, baselines, capture_mode)


def test_search_messages_with_sender_filter(
    connector: AppleMailConnector,
    test_account: str,
    benchmark_mailbox: str,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: filtered search — exercises the `whose sender contains`
    server-side filter path. Filter is intentionally permissive ("@") so
    most messages match; we want to time the filter machinery, not the
    "no results" fast-path."""
    name = "search_messages_with_sender_filter"
    result: BenchmarkResult = measure_median(
        lambda: connector.search_messages(
            account=test_account,
            mailbox=benchmark_mailbox,
            sender_contains="@",
            limit=10,
        ),
        name=name,
    )
    assert_within_baseline(name, result, baselines, capture_mode)
