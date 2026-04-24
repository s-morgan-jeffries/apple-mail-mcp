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


class TestDateFilters:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_from_iso_converted_to_imap_format(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            date_from="2026-04-22"
        )
        mock_client.search.assert_called_once_with(["SINCE", "22-Apr-2026"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_to_is_inclusive_of_full_day(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            date_to="2026-04-22"
        )
        # Inclusive upper bound → BEFORE next day.
        mock_client.search.assert_called_once_with(["BEFORE", "23-Apr-2026"])

    def test_invalid_date_from_raises_value_error(self):
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        with pytest.raises(ValueError, match="ISO 8601"):
            conn.search_messages(date_from="04/22/2026")

    def test_invalid_date_to_raises_value_error(self):
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        with pytest.raises(ValueError, match="ISO 8601"):
            conn.search_messages(date_to="not-a-date")

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_range(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            date_from="2026-04-01", date_to="2026-04-22"
        )
        mock_client.search.assert_called_once_with(
            ["SINCE", "01-Apr-2026", "BEFORE", "23-Apr-2026"]
        )


class TestLimit:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_limit_slices_uids_from_end(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = list(range(1, 101))  # 100 UIDs
        mock_client.fetch.return_value = _fake_fetch_result(list(range(91, 101)))

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages(limit=10)

        fetch_uids = mock_client.fetch.call_args[0][0]
        assert fetch_uids == list(range(91, 101))
        assert len(result) == 10

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_limit_none_fetches_all(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = list(range(1, 11))
        mock_client.fetch.return_value = _fake_fetch_result(list(range(1, 11)))

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        conn.search_messages(limit=None)

        fetch_uids = mock_client.fetch.call_args[0][0]
        assert fetch_uids == list(range(1, 11))


# BODYSTRUCTURE shapes below match what IMAPClient returns: either a flat
# leaf tuple (type, subtype, params, id, desc, encoding, size, [type-specific], [disposition])
# or a multipart tuple ((child1,), (child2,), ..., subtype).
_LEAF_TEXT = (b"text", b"plain", (), None, None, b"7bit", 100, 5)
_LEAF_PDF_ATTACHMENT = (
    b"application",
    b"pdf",
    (b"name", b"x.pdf"),
    None,
    None,
    b"base64",
    2048,
    (b"attachment", (b"filename", b"x.pdf")),
)
_MULTIPART_WITH_ATTACHMENT = (_LEAF_TEXT, _LEAF_PDF_ATTACHMENT, b"mixed")


class TestHasAttachment:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_true_filters_to_messages_with_attachments(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1, 2, 3]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": _LEAF_TEXT,
            },
            2: {
                b"ENVELOPE": _fake_envelope(message_id=b"<2@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": _MULTIPART_WITH_ATTACHMENT,
            },
            3: {
                b"ENVELOPE": _fake_envelope(message_id=b"<3@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": (b"text", b"html", (), None, None, b"7bit", 456, 10),
            },
        }

        result = ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            has_attachment=True
        )

        ids = [m["id"] for m in result]
        assert ids == ["2@e.com"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_false_filters_to_messages_without(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1, 2]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": _LEAF_TEXT,
            },
            2: {
                b"ENVELOPE": _fake_envelope(message_id=b"<2@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": _MULTIPART_WITH_ATTACHMENT,
            },
        }

        result = ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            has_attachment=False
        )

        ids = [m["id"] for m in result]
        assert ids == ["1@e.com"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_none_does_not_fetch_bodystructure(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = _fake_fetch_result([1])

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(has_attachment=None)

        fetch_keys = mock_client.fetch.call_args[0][1]
        assert b"BODYSTRUCTURE" not in fetch_keys

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_set_includes_bodystructure_in_fetch(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": _LEAF_TEXT,
            }
        }

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(has_attachment=True)

        fetch_keys = mock_client.fetch.call_args[0][1]
        assert b"BODYSTRUCTURE" in fetch_keys


class TestEnvelopeTranslation:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_strips_angle_brackets_from_message_id(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<abc@example.com>"),
                b"FLAGS": (),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["id"] == "abc@example.com"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_empty_sender_returns_empty_string(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        env = Envelope(
            date=datetime(2026, 4, 22),
            subject=b"s",
            from_=(),
            sender=(),
            reply_to=(),
            to=(),
            cc=(),
            bcc=(),
            in_reply_to=None,
            message_id=b"<1@e.com>",
        )
        mock_client.fetch.return_value = {
            1: {b"ENVELOPE": env, b"FLAGS": ()},
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["sender"] == ""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_seen_flag_maps_to_read_status(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (b"\\Seen",),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["read_status"] is True
        assert msg["flagged"] is False

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_flagged_flag_maps_to_flagged(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (b"\\Flagged",),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["flagged"] is True
        assert msg["read_status"] is False

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_iso_format(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<1@e.com>",
                    date=datetime(2026, 4, 22, 14, 30, 0),
                ),
                b"FLAGS": (),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["date_received"] == "2026-04-22T14:30:00"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_subject_bytes_decoded_utf8(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<1@e.com>", subject="héllo ✓".encode()
                ),
                b"FLAGS": (),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["subject"] == "héllo ✓"
