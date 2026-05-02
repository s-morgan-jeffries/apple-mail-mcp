"""Tests for ImapConnectionPool (issue #75).

The pool reuses IMAPClient sessions across calls keyed by (host, email).
These tests cover:

- Reuse: same key → same client; login() runs once across N calls.
- Per-account isolation: different keys → independent clients/locks.
- Idle reconnect: stale entries are dropped + reopened transparently.
- Error invalidation: LoginError / IMAPClientError / OSError drops the
  cached entry so the next call gets a fresh connection.
- Per-connection locking: one client serializes use across threads.
- close(): logs out every cached client.
- ImapConnector wiring: methods route through the pool when one is set.

The IMAPClient class itself is mocked at the module level — these are
unit tests, no real network.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from apple_mail_mcp.imap_connector import (
    POOL_IDLE_TIMEOUT_S,
    ImapConnectionPool,
    ImapConnector,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool() -> ImapConnectionPool:
    """Default pool — production idle threshold."""
    return ImapConnectionPool()


@pytest.fixture
def short_idle_pool() -> ImapConnectionPool:
    """Pool with a tiny idle window for the reconnect tests."""
    return ImapConnectionPool(idle_timeout_s=0.01)


# ---------------------------------------------------------------------------
# Reuse + per-account isolation
# ---------------------------------------------------------------------------


class TestSessionReuse:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_two_consecutive_calls_share_one_client(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """The headline win: two calls to the same (host, email) reuse one
        IMAPClient instance — IMAPClient() called once, login() called once."""
        client = MagicMock()
        mock_cls.return_value = client

        with pool.session("h", 993, "u@e.com", "pw", 3.0) as c1:
            assert c1 is client
        with pool.session("h", 993, "u@e.com", "pw", 3.0) as c2:
            assert c2 is client

        mock_cls.assert_called_once()
        client.login.assert_called_once_with("u@e.com", "pw")
        # No logout between calls — connection stays alive.
        client.logout.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_different_accounts_get_independent_clients(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """Two different (host, email) keys produce two separate clients,
        connected once each."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with pool.session("h1", 993, "a@e.com", "pw", 3.0) as c1:
            assert c1 is clients[0]
        with pool.session("h2", 993, "b@e.com", "pw", 3.0) as c2:
            assert c2 is clients[1]

        assert mock_cls.call_count == 2
        clients[0].login.assert_called_once_with("a@e.com", "pw")
        clients[1].login.assert_called_once_with("b@e.com", "pw")

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_same_host_different_email_are_separate(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """Pool key is (host, email), not just host. Two users on the
        same server need independent sessions."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with pool.session("h", 993, "alice@e.com", "pw", 3.0):
            pass
        with pool.session("h", 993, "bob@e.com", "pw", 3.0):
            pass

        assert mock_cls.call_count == 2


# ---------------------------------------------------------------------------
# Idle reconnect
# ---------------------------------------------------------------------------


class TestIdleReconnect:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_stale_entry_is_dropped_and_reopened(
        self, mock_cls: MagicMock,
        short_idle_pool: ImapConnectionPool,
    ) -> None:
        """If `now - last_used > idle_timeout`, the next session() call
        drops the cached client (logging it out politely) and opens a
        fresh one. Tests use a 10ms idle window so we don't sleep long."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with short_idle_pool.session("h", 993, "u@e.com", "pw", 3.0):
            pass

        # Wait long enough for the entry to be considered stale.
        time.sleep(0.05)

        with short_idle_pool.session("h", 993, "u@e.com", "pw", 3.0) as c:
            assert c is clients[1]  # second client, fresh connection

        assert mock_cls.call_count == 2
        # Old client was logged out before being replaced.
        clients[0].logout.assert_called_once()
        clients[1].logout.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_within_idle_window_reuses(
        self, mock_cls: MagicMock,
        pool: ImapConnectionPool,
    ) -> None:
        """The default 270s window comfortably covers any tight burst of
        calls a user / agent makes in succession."""
        client = MagicMock()
        mock_cls.return_value = client

        for _ in range(5):
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                pass

        mock_cls.assert_called_once()
        client.login.assert_called_once()


# ---------------------------------------------------------------------------
# Error invalidation
# ---------------------------------------------------------------------------


class TestErrorInvalidation:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_login_error_drops_entry(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """A LoginError raised inside the session block (e.g. server
        decided mid-session our auth is no longer valid) drops the
        cached entry. The exception still propagates so the orchestrator
        can fall back to AppleScript."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with pytest.raises(LoginError):
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                raise LoginError("rejected")

        # Next call must reconnect — the bad entry was dropped.
        with pool.session("h", 993, "u@e.com", "pw", 3.0):
            pass

        assert mock_cls.call_count == 2
        clients[0].logout.assert_called_once()  # invalidation tries to be polite

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_imap_client_error_drops_entry(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """Protocol-level error mid-session (e.g. server returned BAD,
        connection went away). Same invalidation path as LoginError."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with pytest.raises(IMAPClientError):
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                raise IMAPClientError("BAD command")

        with pool.session("h", 993, "u@e.com", "pw", 3.0):
            pass

        assert mock_cls.call_count == 2

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_oserror_drops_entry(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """Network drop mid-session (broken pipe, conn reset). Pool must
        invalidate so the next call reconnects rather than blindly
        reusing a dead socket."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with pytest.raises(OSError):
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                raise OSError("broken pipe")

        with pool.session("h", 993, "u@e.com", "pw", 3.0):
            pass

        assert mock_cls.call_count == 2

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_logout_failure_during_invalidation_is_swallowed(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """If the cached client's logout() ITSELF raises during
        invalidation (the connection is already dead, so logout would
        too), we swallow it. The original exception still propagates."""
        clients = [MagicMock(), MagicMock()]
        clients[0].logout.side_effect = OSError("logout: broken")
        mock_cls.side_effect = clients

        with pytest.raises(IMAPClientError, match="BAD"):
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                raise IMAPClientError("BAD")

        # Cache is empty; next call reconnects cleanly.
        with pool.session("h", 993, "u@e.com", "pw", 3.0):
            pass

        assert mock_cls.call_count == 2

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_non_invalidating_exception_keeps_entry_cached(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """An exception NOT in _POOL_INVALIDATE_EXCS — e.g. a clean
        MailMessageNotFoundError that the caller surfaces as a
        not-found result — should NOT drop the connection. The session
        is still valid; the message just wasn't there."""
        client = MagicMock()
        mock_cls.return_value = client

        with pytest.raises(ValueError):
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                raise ValueError("not a connection-level failure")

        # Same key reuses the same client — no invalidation happened.
        with pool.session("h", 993, "u@e.com", "pw", 3.0):
            pass

        mock_cls.assert_called_once()
        client.logout.assert_not_called()


# ---------------------------------------------------------------------------
# Per-connection locking
# ---------------------------------------------------------------------------


class TestPerConnectionLocking:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_same_key_serializes_across_threads(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """Two threads asking for the same (host, email) at once must
        not run inside `with session()` simultaneously — IMAPClient is
        not safe for concurrent use on one connection.

        Held by thread A: thread B blocks until A exits the with-block.
        """
        client = MagicMock()
        mock_cls.return_value = client

        a_inside = threading.Event()
        a_release = threading.Event()
        b_done = threading.Event()
        observed_overlap = threading.Event()

        def thread_a() -> None:
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                a_inside.set()
                a_release.wait(timeout=2)
                if b_done.is_set():
                    observed_overlap.set()

        def thread_b() -> None:
            a_inside.wait(timeout=2)
            with pool.session("h", 993, "u@e.com", "pw", 3.0):
                b_done.set()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()

        # Give B a moment — if it managed to enter while A is holding,
        # b_done would fire before a_release.
        time.sleep(0.05)
        b_done_during_a = b_done.is_set()
        a_release.set()
        ta.join(timeout=2)
        tb.join(timeout=2)

        # B must NOT have entered while A was holding the per-connection lock.
        assert not b_done_during_a, "Lock failed: B entered while A held"
        assert b_done.is_set()
        assert not observed_overlap.is_set()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_different_keys_run_concurrently(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """The cache lock is held only for dict lookup. Two threads
        accessing different (host, email) keys must NOT serialize."""
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        a_inside = threading.Event()
        b_inside = threading.Event()
        release = threading.Event()
        both_were_inside = threading.Event()

        def thread_a() -> None:
            with pool.session("h1", 993, "a@e.com", "pw", 3.0):
                a_inside.set()
                # If b also enters, both events will be set together.
                if b_inside.wait(timeout=1):
                    both_were_inside.set()
                release.wait(timeout=2)

        def thread_b() -> None:
            with pool.session("h2", 993, "b@e.com", "pw", 3.0):
                b_inside.set()
                a_inside.wait(timeout=1)
                release.wait(timeout=2)

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()

        # Allow them to ramp up; they should both be inside.
        time.sleep(0.05)
        both_inside_now = a_inside.is_set() and b_inside.is_set()
        release.set()
        ta.join(timeout=2)
        tb.join(timeout=2)

        assert both_inside_now or both_were_inside.is_set(), (
            "Different keys must run in parallel"
        )


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_close_logs_out_every_cached_client(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        clients = [MagicMock(), MagicMock()]
        mock_cls.side_effect = clients

        with pool.session("h1", 993, "a@e.com", "pw", 3.0):
            pass
        with pool.session("h2", 993, "b@e.com", "pw", 3.0):
            pass

        pool.close()

        clients[0].logout.assert_called_once()
        clients[1].logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_close_swallows_individual_logout_errors(
        self, mock_cls: MagicMock, pool: ImapConnectionPool
    ) -> None:
        """Bad cached client shouldn't prevent cleaning up the others."""
        clients = [MagicMock(), MagicMock()]
        clients[0].logout.side_effect = OSError("dead")
        mock_cls.side_effect = clients

        with pool.session("h1", 993, "a@e.com", "pw", 3.0):
            pass
        with pool.session("h2", 993, "b@e.com", "pw", 3.0):
            pass

        pool.close()  # must not raise

        clients[1].logout.assert_called_once()

    def test_close_is_idempotent(
        self, pool: ImapConnectionPool
    ) -> None:
        pool.close()
        pool.close()  # second call must not raise on empty cache


# ---------------------------------------------------------------------------
# ImapConnector wiring
# ---------------------------------------------------------------------------


class TestImapConnectorWithPool:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_no_pool_keeps_per_call_lifecycle(
        self, mock_cls: MagicMock
    ) -> None:
        """Default behavior (pool=None) is unchanged: open + login +
        op + logout per call. This is what existing tests rely on."""
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = []

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        conn.search_messages()
        conn.search_messages()

        # Each call did its own connect + login + logout.
        assert mock_cls.call_count == 2
        assert client.login.call_count == 2
        assert client.logout.call_count == 2

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_with_pool_amortizes_login_across_calls(
        self, mock_cls: MagicMock
    ) -> None:
        """The headline win: with a pool, two consecutive calls share
        one login + one logout (zero, technically — pool keeps it open
        for reuse). This test exercises the same observable change end-
        to-end through ImapConnector.search_messages."""
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = []

        pool = ImapConnectionPool()
        conn = ImapConnector("h", 993, "u@e.com", "pw", pool=pool)
        conn.search_messages()
        conn.search_messages()

        mock_cls.assert_called_once()
        client.login.assert_called_once()
        # No logout yet — pool still holds the entry.
        client.logout.assert_not_called()

        # Closing the pool finally logs the cached client out.
        pool.close()
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_pool_invalidates_on_imap_client_error_through_connector(
        self, mock_cls: MagicMock
    ) -> None:
        """When ImapConnector.search_messages raises an
        IMAPClientError mid-call (e.g. SEARCH failed), the pool
        invalidates the cached entry. The next call reconnects cleanly."""
        clients = [MagicMock(), MagicMock()]
        clients[0].search.side_effect = IMAPClientError("BAD")
        clients[1].search.return_value = []
        mock_cls.side_effect = clients

        pool = ImapConnectionPool()
        conn = ImapConnector("h", 993, "u@e.com", "pw", pool=pool)

        with pytest.raises(IMAPClientError):
            conn.search_messages()
        # Recovery: next call reconnects.
        result = conn.search_messages()

        assert result == []
        assert mock_cls.call_count == 2


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


class TestPoolDefaults:
    def test_default_idle_timeout_is_below_icloud_drop_threshold(self) -> None:
        """iCloud and most providers drop sessions ~30 min idle. The
        default must comfortably stay under that so we never hand out
        a half-dead connection."""
        # 30 min = 1800s. Default should be well under that.
        assert POOL_IDLE_TIMEOUT_S < 30 * 60
        # And large enough to amortize a typical interactive burst
        # (search → get_message → get_attachments within a minute).
        assert POOL_IDLE_TIMEOUT_S > 60

    def test_pool_constructor_accepts_custom_idle_timeout(self) -> None:
        p = ImapConnectionPool(idle_timeout_s=42.0)
        assert p._idle_timeout_s == 42.0  # noqa: SLF001 — internal check is the point
