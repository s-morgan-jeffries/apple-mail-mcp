"""Benchmarks for bulk-mutation operations.

These benchmarks DO mutate Mail.app state, but the `bench_messages`
fixture in conftest.py handles setup (move BULK_SIZE messages into
[apple-mail-mcp-bench]) and teardown (move them all back to source).
The benchmarks themselves operate on the bench mailbox so test data
is isolated from real mail.

Three benchmarks here:
- `mark_as_read_50_msgs` — bulk read-state toggle (single AppleScript
  call covering all N messages; the key scaling-pattern signal)
- `move_messages_50_msgs` — bulk move via the legacy connector method
  (AppleScript-only baseline: round-trip bench → source → bench)
- `update_message_move_50_msgs_imap` — bulk move via update_message's
  IMAP fast path (#149); side-by-side companion to the AppleScript
  baseline above.
"""

from __future__ import annotations

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import (
    BenchmarkResult,
    assert_within_baseline,
    measure_median,
)


def test_mark_as_read_50_msgs(
    connector: AppleMailConnector,
    test_account: str,
    bench_mailbox: str,
    bench_messages: list[str],
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: bulk-mark-read against BULK_SIZE messages in the bench
    mailbox, using the narrow-path source_mailbox parameter from #103.

    Each iteration toggles read→unread→read on the same message set.
    Final state of each iteration matches the message's starting state.
    """
    name = "mark_as_read_50_msgs"

    def run() -> None:
        connector.mark_as_read(
            bench_messages,
            read=False,
            account=test_account,
            source_mailbox=bench_mailbox,
        )
        connector.mark_as_read(
            bench_messages,
            read=True,
            account=test_account,
            source_mailbox=bench_mailbox,
        )

    result: BenchmarkResult = measure_median(run, name=name)
    assert_within_baseline(name, result, baselines, capture_mode)


def test_move_messages_50_msgs(
    connector: AppleMailConnector,
    test_account: str,
    bench_source: str,
    bench_mailbox: str,
    bench_messages: list[str],
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: bulk-move BULK_SIZE messages, using narrow-path
    source_mailbox in both directions.

    Each iteration moves bench → source → bench. Two move calls per
    iteration; we measure the round-trip and report it as
    move_messages_50_msgs (the per-direction time is half this median).

    IDs change on each move (IMAP UID semantics), so the test re-fetches
    after each direction. The fixture's teardown drains whatever's left
    in bench_mailbox back to source, which handles a partial-failure
    iteration cleanly.
    """
    name = "move_messages_50_msgs"

    # Mutable list so we can update IDs across iterations.
    current_ids = list(bench_messages)

    def run() -> None:
        # Move bench → source (narrow source-scan)
        connector.move_messages(
            current_ids,
            destination_mailbox=bench_source,
            account=test_account,
            source_mailbox=bench_mailbox,
        )
        # IDs are now stale. Find the BULK_SIZE most recent in source —
        # those are the ones we just moved.
        in_source = connector.search_messages(
            account=test_account, mailbox=bench_source, limit=len(current_ids)
        )
        moved_ids = [m["id"] for m in in_source[: len(current_ids)]]

        # Move source → bench (narrow source-scan)
        connector.move_messages(
            moved_ids,
            destination_mailbox=bench_mailbox,
            account=test_account,
            source_mailbox=bench_source,
        )
        # Re-fetch bench IDs for the next iteration.
        in_bench = connector.search_messages(
            account=test_account, mailbox=bench_mailbox, limit=len(current_ids)
        )
        current_ids[:] = [m["id"] for m in in_bench[: len(current_ids)]]

    # 3 runs (not 5) — each run is two moves on 50 messages.
    result: BenchmarkResult = measure_median(run, name=name, runs=3)
    assert_within_baseline(name, result, baselines, capture_mode)


def test_update_message_move_50_msgs_imap(
    connector: AppleMailConnector,
    test_account: str,
    bench_source: str,
    bench_mailbox: str,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """#149: bulk-move BULK_SIZE messages via update_message's move-only
    IMAP fast path. Side-by-side with ``move_messages_50_msgs`` (which
    measures the AppleScript baseline).

    Skipped when IMAP isn't configured for the test account — the IMAP
    fast path needs Keychain credentials and falls back to AppleScript
    silently otherwise, which would mask the comparison.

    The IDs handed to update_message are RFC 5322 Message-IDs from the
    IMAP search path; passing AppleScript internal numeric IDs would
    silently no-op the IMAP MOVE (server can't resolve them via SEARCH
    HEADER Message-ID)."""
    from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
    from apple_mail_mcp.keychain import get_imap_password

    # Skip cleanly when IMAP isn't configured.
    try:
        _, _, email = connector._resolve_imap_config(test_account)
        get_imap_password(test_account, email)
    except MailKeychainEntryNotFoundError:
        pytest.skip(
            f"IMAP not configured for {test_account!r}; nothing to "
            f"benchmark. Run `apple-mail-mcp setup-imap --account "
            f"{test_account}` to enable."
        )

    name = "update_message_move_50_msgs_imap"

    # Force IMAP search to get RFC 5322 Message-IDs. Falling back to
    # AppleScript would yield internal numeric IDs which the IMAP MOVE
    # path can't resolve via SEARCH HEADER Message-ID.
    rfc_ids = [
        m["id"]
        for m in connector._imap_search(
            account=test_account, mailbox=bench_source, limit=50,
        )
    ]
    if len(rfc_ids) < 50:
        pytest.skip(
            f"bench_source {bench_source!r} returned only {len(rfc_ids)} "
            f"RFC ids via IMAP search; need 50 for the benchmark."
        )

    # Move bench_source → bench, then bench → bench_source. After each
    # leg, re-fetch IDs via IMAP search since UIDs change on move.
    current = list(rfc_ids)
    src = bench_source
    dst = bench_mailbox

    def run() -> None:
        nonlocal current, src, dst
        connector.update_message(
            current,
            destination_mailbox=dst,
            account=test_account,
            source_mailbox=src,
        )
        # Re-fetch from the new source (was the destination).
        current = [
            m["id"]
            for m in connector._imap_search(
                account=test_account, mailbox=dst, limit=50,
            )
        ][:50]
        src, dst = dst, src

    # 3 runs (each is one move on 50 messages — half the round-trip cost
    # of move_messages_50_msgs).
    result: BenchmarkResult = measure_median(run, name=name, runs=3)
    assert_within_baseline(name, result, baselines, capture_mode)


def test_move_messages_50_msgs_gmail(
    connector: AppleMailConnector,
    test_account_gmail: str,
    gmail_bench_source: str,
    gmail_bench_mailbox: str,
    gmail_bench_messages: list[str],
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Gmail variant (#101): bulk-move 50 synthetic messages with
    ``gmail_mode=True`` (Gmail's IMAP doesn't natively support MOVE for
    label-backed folders, so the connector falls back to copy+delete in
    two AppleScript steps — that path is what this baseline measures).

    Same shape as ``test_move_messages_50_msgs`` but the source and
    destination mailboxes are dedicated synthetic-data fixtures, never
    the user's real INBOX."""
    name = "move_messages_50_msgs_gmail"

    current_ids = list(gmail_bench_messages)

    def run() -> None:
        connector.move_messages(
            current_ids,
            destination_mailbox=gmail_bench_source,
            account=test_account_gmail,
            source_mailbox=gmail_bench_mailbox,
            gmail_mode=True,
        )
        in_source = connector.search_messages(
            account=test_account_gmail,
            mailbox=gmail_bench_source,
            limit=len(current_ids),
        )
        moved_ids = [m["id"] for m in in_source[: len(current_ids)]]

        connector.move_messages(
            moved_ids,
            destination_mailbox=gmail_bench_mailbox,
            account=test_account_gmail,
            source_mailbox=gmail_bench_source,
            gmail_mode=True,
        )
        in_bench = connector.search_messages(
            account=test_account_gmail,
            mailbox=gmail_bench_mailbox,
            limit=len(current_ids),
        )
        current_ids[:] = [m["id"] for m in in_bench[: len(current_ids)]]

    result: BenchmarkResult = measure_median(run, name=name, runs=3)
    assert_within_baseline(name, result, baselines, capture_mode)
