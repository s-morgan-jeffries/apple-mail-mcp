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
    """Baseline: filtered search with a permissive filter ('@' in sender) so
    most messages match — exercises the per-message AppleScript IF filter
    machinery (post-#32) plus property fetching for the limit-bounded result
    set. Pre-#32 this used `whose sender contains` and was 60s+ on a 200+
    msg folder; the reverse-iteration rewrite drops it to single-digit s."""
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


def test_search_messages_with_zero_matches(
    connector: AppleMailConnector,
    test_account: str,
    benchmark_mailbox: str,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: filtered search where NO messages match — exercises the
    full-scan worst case for the per-message IF-filter path.

    Pre-#32 the `whose <selective filter>` evaluator scanned every message
    in the mailbox before returning empty, just like the no-match case here
    does post-#32. The expectation is that this scales linearly with mailbox
    size and stays bounded; if it regresses dramatically that signals the
    iteration cost of `messages of mailboxRef` itself has changed."""
    name = "search_messages_with_zero_matches"
    # A subject substring that's vanishingly unlikely to appear in real mail.
    sentinel = "zzzz_apple_mail_mcp_no_match_sentinel_qqqq"
    result: BenchmarkResult = measure_median(
        lambda: connector.search_messages(
            account=test_account,
            mailbox=benchmark_mailbox,
            subject_contains=sentinel,
            limit=10,
        ),
        name=name,
    )
    assert_within_baseline(name, result, baselines, capture_mode)
