"""Benchmarks for bulk-mutation operations.

These benchmarks DO mutate Mail.app state, but the `bench_messages`
fixture in conftest.py handles setup (move BULK_SIZE messages into
[apple-mail-mcp-bench]) and teardown (move them all back to source).
The benchmarks themselves operate on the bench mailbox so test data
is isolated from real mail.

Two benchmarks here:
- `mark_as_read_50_msgs` — bulk read-state toggle (single AppleScript
  call covering all N messages; the key scaling-pattern signal)
- `move_messages_50_msgs` — bulk move (round-trip: bench → source → bench
  per iteration, leaving state unchanged at iteration end)
"""

from __future__ import annotations

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import (
    BenchmarkResult,
    assert_within_baseline,
    measure_median,
)


def test_mark_as_read_50_msgs(
    connector: AppleMailConnector,
    bench_messages: list[str],
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: bulk-mark-read against BULK_SIZE messages in the bench
    mailbox.

    Each iteration toggles read→unread→read on the same message set.
    Final state of each iteration matches the message's starting state
    in the bench mailbox.
    """
    name = "mark_as_read_50_msgs"

    def run() -> None:
        connector.mark_as_read(bench_messages, read=False)
        connector.mark_as_read(bench_messages, read=True)

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
    """Baseline: bulk-move BULK_SIZE messages.

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
        # Move bench → source
        connector.move_messages(
            current_ids,
            destination_mailbox=bench_source,
            account=test_account,
        )
        # IDs are now stale. Find the BULK_SIZE most recent in source —
        # those are the ones we just moved.
        in_source = connector.search_messages(
            account=test_account, mailbox=bench_source, limit=len(current_ids)
        )
        moved_ids = [m["id"] for m in in_source[: len(current_ids)]]

        # Move source → bench
        connector.move_messages(
            moved_ids,
            destination_mailbox=bench_mailbox,
            account=test_account,
        )
        # Re-fetch bench IDs for the next iteration.
        in_bench = connector.search_messages(
            account=test_account, mailbox=bench_mailbox, limit=len(current_ids)
        )
        current_ids[:] = [m["id"] for m in in_bench[: len(current_ids)]]

    # 3 runs (not 5) — each run is two moves on 50 messages, so the
    # benchmark is already slow.
    result: BenchmarkResult = measure_median(run, name=name, runs=3)
    assert_within_baseline(name, result, baselines, capture_mode)
