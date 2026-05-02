"""Benchmarks for the IMAP connection pool (issue #75).

Per #75's acceptance criterion: a benchmark must demonstrate measurable
speedup on a realistic repeat-call workflow. The shape is the natural
interactive burst — search → get_message → get_attachments → search →
get_message — five sequential IMAP calls against the same account.

Skips gracefully when:
- No Keychain entry for the test account (IMAP path can't run).
- The test account's INBOX is empty (no message id to feed get_message
  / get_attachments).

Two benchmarks captured side by side:
- ``imap_repeat_5_calls_no_pool`` — current behavior, one TCP+TLS+LOGIN
  per call (~5 × ~400 ms overhead).
- ``imap_repeat_5_calls_pooled`` — one connection reused across all
  five calls (~1 × ~400 ms overhead).

The delta is the pool's headline win.
"""

from __future__ import annotations

import pytest

from apple_mail_mcp.imap_connector import ImapConnectionPool
from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import (
    BenchmarkResult,
    assert_within_baseline,
    measure_median,
)


def _skip_if_no_imap(
    connector: AppleMailConnector, test_account: str
) -> tuple[str, str]:
    """Verify IMAP is configured for the test account; skip if not.

    Returns (mailbox, message_id) for the burst. Tries common mailbox
    names in order; uses whichever has at least one message. Matches
    the `benchmark_mailbox` fixture pattern in test_search.py.
    """
    from apple_mail_mcp.exceptions import (
        MailKeychainAccessDeniedError,
        MailKeychainEntryNotFoundError,
    )
    from apple_mail_mcp.keychain import get_imap_password

    try:
        _, _, email = connector._resolve_imap_config(test_account)
        get_imap_password(test_account, email)
    except (MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError):
        pytest.skip(
            f"No Keychain entry for {test_account!r} — pool can't be "
            f"exercised. Run `apple-mail-mcp setup-imap` first."
        )

    for mb in ("INBOX", "Archive", "Sent Messages"):
        try:
            matches = connector.search_messages(
                account=test_account, mailbox=mb, limit=1,
            )
        except Exception:
            continue
        if matches:
            return (mb, matches[0]["id"])

    pytest.skip(
        f"No mailbox in {test_account!r} has any messages "
        f"(checked INBOX, Archive, Sent Messages)."
    )


def _five_call_burst(
    connector: AppleMailConnector,
    test_account: str,
    mailbox: str,
    message_id: str,
) -> None:
    """The realistic interactive burst: two searches plus message and
    attachment fetches against the same account/mailbox. Designed to
    mirror what an agent does when narrowing down a query and inspecting
    a hit."""
    connector.search_messages(account=test_account, mailbox=mailbox, limit=5)
    connector.get_message(message_id, account=test_account, mailbox=mailbox)
    connector.get_attachments(message_id, account=test_account, mailbox=mailbox)
    connector.search_messages(
        account=test_account, mailbox=mailbox, sender_contains="@", limit=5,
    )
    connector.get_message(message_id, account=test_account, mailbox=mailbox)


def test_imap_repeat_5_calls_no_pool(
    connector: AppleMailConnector,
    test_account: str,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: per-call lifecycle (the current default). Each of the
    five calls eats its own TCP + TLS + LOGIN handshake."""
    mailbox, message_id = _skip_if_no_imap(connector, test_account)
    name = "imap_repeat_5_calls_no_pool"

    result: BenchmarkResult = measure_median(
        lambda: _five_call_burst(connector, test_account, mailbox, message_id),
        name=name,
    )
    assert_within_baseline(name, result, baselines, capture_mode)


def test_imap_repeat_5_calls_pooled(
    test_account: str,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """The same five-call burst with the pool enabled. One TCP + TLS +
    LOGIN amortized across all five calls.

    Uses a fresh AppleMailConnector with `imap_pool=ImapConnectionPool()`
    rather than the session-scoped fixture, so the pool starts empty
    for each repeat in `measure_median` and reuses the connection only
    within a single iteration. This isolates the per-iteration savings
    cleanly: each iteration pays exactly one connect + login, then five
    operations.

    A real production server would keep the pool alive across many
    iterations (an even bigger win), but pretending the pool is fresh
    each time gives a conservative lower-bound estimate of the speedup
    that doesn't depend on how stale `last_used` happens to be at
    measurement time.
    """
    pool = ImapConnectionPool()
    pooled_connector = AppleMailConnector(timeout=600, imap_pool=pool)
    mailbox, message_id = _skip_if_no_imap(pooled_connector, test_account)
    name = "imap_repeat_5_calls_pooled"

    def run() -> None:
        # Drop any cached connection from the previous iteration so each
        # iteration's amortization is observable independently.
        pool.close()
        _five_call_burst(pooled_connector, test_account, mailbox, message_id)

    try:
        result: BenchmarkResult = measure_median(run, name=name)
        assert_within_baseline(name, result, baselines, capture_mode)
    finally:
        pool.close()
