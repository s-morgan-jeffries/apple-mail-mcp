"""Tests for ImapConnector."""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from imapclient.response_types import Address, Envelope

from apple_mail_mcp.imap_connector import CONNECT_TIMEOUT_S, ImapConnector


def _fake_envelope(
    *,
    message_id: bytes = b"<msg-1@example.com>",
    subject: bytes = b"Hello",
    sender_name: bytes = b"Alice",
    sender_mailbox: bytes = b"alice",
    sender_host: bytes = b"example.com",
    date: datetime | None = None,
) -> Envelope:
    """Build an Envelope with reasonable defaults for envelope-shape tests."""
    date = date or datetime(2026, 4, 22, 10, 0, 0)
    from_addr = Address(sender_name, None, sender_mailbox, sender_host)
    return Envelope(
        date=date,
        subject=subject,
        from_=(from_addr,),
        sender=(from_addr,),
        reply_to=(from_addr,),
        to=(),
        cc=(),
        bcc=(),
        in_reply_to=None,
        message_id=message_id,
    )


def _fake_fetch_result(uids: list[int]) -> dict[int, dict[bytes, Any]]:
    """Build a FETCH-style dict with ENVELOPE + FLAGS for given UIDs."""
    return {
        uid: {
            b"ENVELOPE": _fake_envelope(
                message_id=f"<msg-{uid}@example.com>".encode(),
                subject=f"Subject {uid}".encode(),
            ),
            b"FLAGS": (b"\\Seen",),
        }
        for uid in uids
    }


class TestConstructor:
    def test_timeout_is_three_seconds_by_default(self):
        assert CONNECT_TIMEOUT_S == 3.0

    def test_default_timeout(self):
        conn = ImapConnector("host", 993, "u@i.com", "pw")
        assert conn._connect_timeout == CONNECT_TIMEOUT_S

    def test_custom_timeout(self):
        conn = ImapConnector("host", 993, "u@i.com", "pw", connect_timeout=10.0)
        assert conn._connect_timeout == 10.0

    def test_stores_credentials(self):
        conn = ImapConnector("imap.example.com", 993, "user@example.com", "secret")
        assert conn._host == "imap.example.com"
        assert conn._port == 993
        assert conn._email == "user@example.com"
        assert conn._password == "secret"


class TestSearchHappyPath:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_no_filters_opens_connection_and_searches_all(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1, 2, 3]
        mock_client.fetch.return_value = _fake_fetch_result([1, 2, 3])

        conn = ImapConnector("imap.example.com", 993, "u@e.com", "pw")
        result = conn.search_messages()

        # Connection setup
        mock_cls.assert_called_once_with(
            "imap.example.com", port=993, ssl=True, timeout=3.0
        )
        mock_client.login.assert_called_once_with("u@e.com", "pw")
        mock_client.select_folder.assert_called_once_with("INBOX", readonly=True)

        # SEARCH with no filters → ALL
        mock_client.search.assert_called_once_with(["ALL"])

        # FETCH with envelope + flags
        fetch_args = mock_client.fetch.call_args
        assert fetch_args[0][0] == [1, 2, 3]
        assert b"ENVELOPE" in fetch_args[0][1]
        assert b"FLAGS" in fetch_args[0][1]

        # LOGOUT
        mock_client.logout.assert_called_once()

        assert len(result) == 3

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_empty_search_result_skips_fetch(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages()

        mock_client.fetch.assert_not_called()
        mock_client.logout.assert_called_once()
        assert result == []

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_custom_mailbox(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        conn.search_messages(mailbox="Archive")

        mock_client.select_folder.assert_called_once_with("Archive", readonly=True)

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_logout_called_on_exception(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.side_effect = RuntimeError("boom")

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        with pytest.raises(RuntimeError, match="boom"):
            conn.search_messages()

        mock_client.logout.assert_called_once()


class TestTextFilters:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_sender_contains_maps_to_from(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            sender_contains="alice"
        )

        mock_client.search.assert_called_once_with(["FROM", "alice"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_subject_contains_maps_to_subject(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            subject_contains="invoice"
        )

        mock_client.search.assert_called_once_with(["SUBJECT", "invoice"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_sender_and_subject_combined(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            sender_contains="bob", subject_contains="report"
        )

        mock_client.search.assert_called_once_with(
            ["FROM", "bob", "SUBJECT", "report"]
        )


class TestFlagFilters:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_read_status_true_maps_to_seen(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(read_status=True)
        mock_client.search.assert_called_once_with(["SEEN"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_read_status_false_maps_to_unseen(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(read_status=False)
        mock_client.search.assert_called_once_with(["UNSEEN"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_is_flagged_true_maps_to_flagged(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(is_flagged=True)
        mock_client.search.assert_called_once_with(["FLAGGED"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_is_flagged_false_maps_to_unflagged(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(is_flagged=False)
        mock_client.search.assert_called_once_with(["UNFLAGGED"])
