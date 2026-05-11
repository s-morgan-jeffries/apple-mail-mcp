"""Tests for ImapConnector."""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import IMAPClientError
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


class TestFindThreadMembers:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_returns_empty_when_no_search_hits(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.return_value = [((b"\\HasNoChildren",), b"/", "INBOX")]
        mock_client.search.return_value = []

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@x",
            anchor_references=[],
        )
        assert result == []
        mock_client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_returns_anchor_and_reply_sorted_chronologically(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.return_value = [((b"\\HasNoChildren",), b"/", "INBOX")]
        # Every search returns the same UIDs — dedup by Message-ID in fetch.
        mock_client.search.return_value = [1, 2]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<anchor@x>",
                    subject=b"Original",
                    date=datetime(2026, 4, 20, 10, 0, 0),
                ),
                b"FLAGS": (),
            },
            2: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<reply@x>",
                    subject=b"Re: Original",
                    date=datetime(2026, 4, 21, 10, 0, 0),
                ),
                b"FLAGS": (),
            },
        }

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@x",
            anchor_references=[],
        )

        assert len(result) == 2
        assert [m["id"] for m in result] == ["anchor@x", "reply@x"]
        # Chronological sort
        assert result[0]["date_received"] < result[1]["date_received"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_iterates_all_mailboxes_in_account(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.return_value = [
            ((), b"/", "INBOX"),
            ((), b"/", "Archive"),
            ((), b"/", "Sent"),
        ]
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="a@x",
            anchor_references=[],
        )

        selected_folders = [
            call.args[0] for call in mock_client.select_folder.call_args_list
        ]
        assert selected_folders == ["INBOX", "Archive", "Sent"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_dedups_messages_found_in_multiple_mailboxes(self, mock_cls):
        """A Gmail-like account may surface the same message in INBOX and
        All Mail. Output must not duplicate it."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.return_value = [
            ((), b"/", "INBOX"),
            ((), b"/", "All Mail"),
        ]
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<anchor@x>", subject=b"Original"
                ),
                b"FLAGS": (),
            }
        }

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@x",
            anchor_references=[],
        )
        assert len(result) == 1
        assert result[0]["id"] == "anchor@x"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_skips_mailbox_that_fails_to_select(self, mock_cls):
        from imapclient.exceptions import IMAPClientError

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.return_value = [
            ((), b"/", "INBOX"),
            ((), b"/", "[Gmail]/Smart Label"),
        ]

        def select_side_effect(name, readonly=False):
            if "Smart Label" in name:
                raise IMAPClientError("cannot select this mailbox")

        mock_client.select_folder.side_effect = select_side_effect
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<anchor@x>"),
                b"FLAGS": (),
            }
        }

        # No exception — Smart Label skipped, INBOX still processed.
        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@x",
            anchor_references=[],
        )
        assert len(result) == 1
        mock_client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_searches_for_each_known_id(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.return_value = [((), b"/", "INBOX")]
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@x",
            anchor_references=["parent@x", "grandparent@x"],
        )

        # Collect all search criteria across all calls.
        searched_ids: set[str] = set()
        for call in mock_client.search.call_args_list:
            crit = call.args[0]
            # Last element is the header value, e.g. "<anchor@x>"
            val = crit[-1]
            if isinstance(val, str) and val.startswith("<") and val.endswith(">"):
                searched_ids.add(val.strip("<>"))

        assert "anchor@x" in searched_ids
        assert "parent@x" in searched_ids
        assert "grandparent@x" in searched_ids

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_logout_called_on_exception(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.list_folders.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
                anchor_rfc_message_id="a@x",
                anchor_references=[],
            )

        mock_client.logout.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #72: get_message
# ---------------------------------------------------------------------------


class TestGetMessage:
    """ImapConnector.get_message — Message-ID lookup + envelope/body fetch."""

    def _setup_client(
        self, mock_cls: MagicMock, *, uids: list[int] = None,
        body: bytes = b"plain text body",
        include_body_fetch: bool = True,
        include_header_fetch: bool = False,
    ) -> MagicMock:
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = uids if uids is not None else [42]

        fetched: dict[int, dict[bytes, Any]] = {}
        for uid in (uids or [42]):
            entry: dict[bytes, Any] = {
                b"ENVELOPE": _fake_envelope(
                    message_id=f"<{uid}@example.com>".encode(),
                    subject=b"Hello",
                ),
                b"FLAGS": (b"\\Seen",),
            }
            if include_body_fetch:
                entry[b"BODY[TEXT]"] = body
            if include_header_fetch:
                entry[b"BODY[HEADER]"] = (
                    b"From: alice@example.com\r\nSubject: Hello\r\n"
                )
            fetched[uid] = entry
        client.fetch.return_value = fetched
        return client

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_search_uses_bracketed_message_id_header_criteria(
        self, mock_cls: MagicMock
    ) -> None:
        """The Message-ID gets bracketed before SEARCH HEADER — RFC 5322
        canonical form is what IMAP servers compare the literal header
        against."""
        self._setup_client(mock_cls)

        ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "abc@example.com", mailbox="INBOX",
        )

        mock_cls.return_value.search.assert_called_once_with(
            ["HEADER", "Message-ID", "<abc@example.com>"]
        )

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_search_preserves_already_bracketed_id(
        self, mock_cls: MagicMock
    ) -> None:
        """If the caller already supplied an angle-bracketed ID, don't
        wrap it twice."""
        self._setup_client(mock_cls)

        ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "<abc@example.com>", mailbox="INBOX",
        )
        mock_cls.return_value.search.assert_called_once_with(
            ["HEADER", "Message-ID", "<abc@example.com>"]
        )

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_select_folder_honors_mailbox_param(
        self, mock_cls: MagicMock
    ) -> None:
        self._setup_client(mock_cls)
        ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "abc@x", mailbox="Archive",
        )
        mock_cls.return_value.select_folder.assert_called_once_with(
            "Archive", readonly=True
        )

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_returns_dict_with_applescript_compatible_keys(
        self, mock_cls: MagicMock
    ) -> None:
        """Return shape must match the AppleScript path so callers don't
        have to special-case which dispatch fired."""
        self._setup_client(mock_cls, uids=[7], body=b"hello world")

        result = ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "msg7@example.com", mailbox="INBOX",
        )

        assert set(result.keys()) >= {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged", "content",
        }
        assert result["content"] == "hello world"
        assert result["read_status"] is True

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_default_fetch_keys_include_body_text(
        self, mock_cls: MagicMock
    ) -> None:
        """Default include_content=True and headers_only=False → fetch
        ENVELOPE + FLAGS + BODY[TEXT]."""
        self._setup_client(mock_cls)

        ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "abc@x", mailbox="INBOX",
        )

        fetch_keys = mock_cls.return_value.fetch.call_args[0][1]
        assert b"ENVELOPE" in fetch_keys
        assert b"FLAGS" in fetch_keys
        assert b"BODY[TEXT]" in fetch_keys
        assert b"BODY[HEADER]" not in fetch_keys

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_include_content_false_skips_body_fetch(
        self, mock_cls: MagicMock
    ) -> None:
        self._setup_client(mock_cls, include_body_fetch=False)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "abc@x", mailbox="INBOX", include_content=False,
        )

        fetch_keys = mock_cls.return_value.fetch.call_args[0][1]
        assert b"BODY[TEXT]" not in fetch_keys
        assert b"BODY[HEADER]" not in fetch_keys
        # content empty when not requested.
        assert result["content"] == ""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_headers_only_uses_body_header_not_body_text(
        self, mock_cls: MagicMock
    ) -> None:
        """headers_only is the perf knob — fetch headers only, return
        empty content. The envelope already carries subject/sender/date,
        so the BODY[HEADER] fetch is for spec-correctness vs. servers
        that might still send the body without an explicit ask."""
        self._setup_client(
            mock_cls, include_body_fetch=False, include_header_fetch=True,
        )

        result = ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "abc@x", mailbox="INBOX", headers_only=True,
        )

        fetch_keys = mock_cls.return_value.fetch.call_args[0][1]
        assert b"BODY[HEADER]" in fetch_keys
        assert b"BODY[TEXT]" not in fetch_keys
        assert result["content"] == ""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_no_match_raises_message_not_found(
        self, mock_cls: MagicMock
    ) -> None:
        from apple_mail_mcp.exceptions import MailMessageNotFoundError

        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = []

        with pytest.raises(MailMessageNotFoundError, match="not found"):
            ImapConnector("h", 993, "u@e.com", "pw").get_message(
                "ghost@nowhere", mailbox="INBOX",
            )

        # Logout still called via the finally block.
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_logout_called_on_exception(
        self, mock_cls: MagicMock
    ) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        client.search.side_effect = RuntimeError("kaboom")

        with pytest.raises(RuntimeError):
            ImapConnector("h", 993, "u@e.com", "pw").get_message(
                "x@y", mailbox="INBOX",
            )
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_only_first_match_is_fetched(
        self, mock_cls: MagicMock
    ) -> None:
        """If the server (somehow) returns multiple UIDs for the same
        Message-ID — duplicate appends, server quirk — fetch only one to
        avoid pulling unbounded duplicates."""
        self._setup_client(mock_cls, uids=[1, 2, 3])

        ImapConnector("h", 993, "u@e.com", "pw").get_message(
            "abc@x", mailbox="INBOX",
        )

        fetch_args = mock_cls.return_value.fetch.call_args[0]
        # First positional arg is the UID list — must be exactly one.
        assert len(list(fetch_args[0])) == 1


# ---------------------------------------------------------------------------
# Issue #73: get_attachments
# ---------------------------------------------------------------------------

# BODYSTRUCTURE fixtures for the attachment extractor. Leaf shape is
# (type, subtype, params, id, desc, encoding, size, [extras...], [disposition]).
# Disposition tuple is (kind, params) where params is flat (k,v,k,v,...).

# Plain body — should never be reported as an attachment.
_BS_PLAIN_TEXT = (b"text", b"plain", (), None, None, b"7bit", 100, 5)

# PDF attachment — disposition = attachment, filename in disposition params.
_BS_PDF_ATTACHMENT = (
    b"application", b"pdf",
    (b"name", b"report.pdf"),
    None, None, b"base64", 524288,
    (b"attachment", (b"filename", b"report.pdf")),
)

# JPEG attachment — different mime + size.
_BS_JPEG_ATTACHMENT = (
    b"image", b"jpeg",
    (b"name", b"photo.jpg"),
    None, None, b"base64", 4096,
    (b"attachment", (b"filename", b"photo.jpg")),
)

# Inline image with filename — multipart/related signature image case.
_BS_INLINE_IMAGE_WITH_FILENAME = (
    b"image", b"png",
    (b"name", b"sig.png"),
    b"<sig@local>", None, b"base64", 2048,
    (b"inline", (b"filename", b"sig.png")),
)

# Inline body part WITHOUT a filename — a real body, not an attachment.
_BS_INLINE_BODY_NO_FILENAME = (
    b"text", b"html",
    (b"charset", b"utf-8"),
    None, None, b"7bit", 200, 10,
    (b"inline", ()),
)

# Forwarded email (message/rfc822). Per RFC 2046 §5.2.1, the leaf for
# message/rfc822 carries an envelope + body + lines after the size field;
# disposition may or may not be present. Without disposition, the
# AppleScript path silently drops these — IMAP must still surface them.
_BS_FORWARDED_EMAIL_NO_DISP = (
    b"message", b"rfc822",
    (), None, None, b"7bit", 8192,
    None, None, 250,  # envelope, body, lines (None'd; we don't inspect them)
)

# Legacy: filename in content-type's `name` param, no disposition at all.
_BS_LEGACY_NAME_PARAM_ONLY = (
    b"application", b"zip",
    (b"name", b"old.zip"),
    None, None, b"base64", 1024,
)

# Unicode filename via UTF-8 bytes.
_BS_UNICODE_FILENAME = (
    b"application", b"pdf",
    (),
    None, None, b"base64", 100,
    (b"attachment", (b"filename", b"r\xc3\xa9sum\xc3\xa9.pdf")),
)

# Mangled bytes that aren't valid UTF-8.
_BS_MANGLED_FILENAME = (
    b"application", b"pdf",
    (),
    None, None, b"base64", 100,
    (b"attachment", (b"filename", b"\xff\xfe\xff.pdf")),
)


class TestGetAttachments:
    """ImapConnector.get_attachments — Message-ID lookup + BODYSTRUCTURE walk."""

    def _setup_client(
        self, mock_cls: MagicMock, *,
        uids: list[int] | None = None,
        bodystructure: Any = None,
    ) -> MagicMock:
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = uids if uids is not None else [42]
        if bodystructure is None:
            bodystructure = _BS_PLAIN_TEXT
        client.fetch.return_value = {
            (uids or [42])[0]: {b"BODYSTRUCTURE": bodystructure},
        }
        return client

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_search_uses_bracketed_message_id(
        self, mock_cls: MagicMock
    ) -> None:
        """Same lookup path as get_message — bracket the ID for HEADER search."""
        self._setup_client(mock_cls)

        ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        mock_cls.return_value.search.assert_called_once_with(
            ["HEADER", "Message-ID", "<abc@x>"]
        )

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_fetch_only_requests_bodystructure(
        self, mock_cls: MagicMock
    ) -> None:
        """Single FETCH item — we don't need ENVELOPE/FLAGS/BODY here."""
        self._setup_client(mock_cls)

        ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        fetch_keys = mock_cls.return_value.fetch.call_args[0][1]
        assert list(fetch_keys) == [b"BODYSTRUCTURE"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_no_attachments_returns_empty_list(
        self, mock_cls: MagicMock
    ) -> None:
        """A plain text/plain body has no attachments."""
        self._setup_client(mock_cls, bodystructure=_BS_PLAIN_TEXT)
        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )
        assert result == []

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_single_pdf_attachment(
        self, mock_cls: MagicMock
    ) -> None:
        bs = (_BS_PLAIN_TEXT, _BS_PDF_ATTACHMENT, b"mixed")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert result == [{
            "name": "report.pdf",
            "mime_type": "application/pdf",
            "size": 524288,
            "downloaded": False,  # always False on IMAP path; documented divergence
        }]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_multiple_attachments(
        self, mock_cls: MagicMock
    ) -> None:
        bs = (_BS_PLAIN_TEXT, _BS_PDF_ATTACHMENT, _BS_JPEG_ATTACHMENT, b"mixed")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert len(result) == 2
        names = {a["name"] for a in result}
        assert names == {"report.pdf", "photo.jpg"}

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_inline_image_with_filename_is_surfaced(
        self, mock_cls: MagicMock
    ) -> None:
        """Multipart/related inline image (e.g. signature PNG) — the case
        where Mail.app's AppleScript surface drops it silently. IMAP must
        surface it."""
        bs = (_BS_PLAIN_TEXT, _BS_INLINE_IMAGE_WITH_FILENAME, b"related")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert len(result) == 1
        assert result[0]["name"] == "sig.png"
        assert result[0]["mime_type"] == "image/png"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_inline_body_without_filename_is_not_an_attachment(
        self, mock_cls: MagicMock
    ) -> None:
        """`Content-Disposition: inline` with no filename is a regular
        body part (e.g. the message's HTML body) — not an attachment."""
        bs = (_BS_PLAIN_TEXT, _BS_INLINE_BODY_NO_FILENAME, b"alternative")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert result == []

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_forwarded_email_surfaces_as_attachment(
        self, mock_cls: MagicMock
    ) -> None:
        """A `message/rfc822` part — i.e. a forwarded `.eml` — must be
        surfaced even when no Content-Disposition is set, per RFC 2046
        §5.2.1. This is the silent-failure case the issue calls out for
        the AppleScript path."""
        bs = (_BS_PLAIN_TEXT, _BS_FORWARDED_EMAIL_NO_DISP, b"mixed")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert len(result) == 1
        assert result[0]["mime_type"] == "message/rfc822"
        assert result[0]["size"] == 8192
        # No filename was provided → empty string. Caller can still see
        # the part exists and decide what to do with it.
        assert result[0]["name"] == ""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_legacy_name_param_without_disposition_is_not_an_attachment(
        self, mock_cls: MagicMock
    ) -> None:
        """A leaf with `name` in content-type params but no disposition
        and not message/rfc822: matches the existing has_attachment
        helper's behavior (which only triggers on disposition). Skipping
        this case keeps both helpers consistent — if we want to broaden
        attachment detection, we do it for both at once."""
        bs = (_BS_PLAIN_TEXT, _BS_LEGACY_NAME_PARAM_ONLY, b"mixed")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert result == []

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_unicode_filename_is_decoded(
        self, mock_cls: MagicMock
    ) -> None:
        bs = (_BS_PLAIN_TEXT, _BS_UNICODE_FILENAME, b"mixed")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert len(result) == 1
        assert result[0]["name"] == "résumé.pdf"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_mangled_filename_does_not_crash(
        self, mock_cls: MagicMock
    ) -> None:
        """Bytes that aren't valid UTF-8 must not raise; replacement-char
        decoding gives the user something they can see and rename."""
        bs = (_BS_PLAIN_TEXT, _BS_MANGLED_FILENAME, b"mixed")
        self._setup_client(mock_cls, bodystructure=bs)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert len(result) == 1
        # Replacement character means we got SOMETHING back, no crash.
        assert "�" in result[0]["name"]
        assert result[0]["name"].endswith(".pdf")

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_nested_multipart_attachments_are_surfaced(
        self, mock_cls: MagicMock
    ) -> None:
        """A multipart/alternative inside a multipart/mixed — common shape
        for "HTML + plain text + attachment" emails. The walk must
        recurse, not stop at the first multipart boundary."""
        inner = (_BS_PLAIN_TEXT, _BS_INLINE_BODY_NO_FILENAME, b"alternative")
        outer = (inner, _BS_PDF_ATTACHMENT, b"mixed")
        self._setup_client(mock_cls, bodystructure=outer)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x", mailbox="INBOX",
        )

        assert len(result) == 1
        assert result[0]["name"] == "report.pdf"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_no_match_raises_message_not_found(
        self, mock_cls: MagicMock
    ) -> None:
        from apple_mail_mcp.exceptions import MailMessageNotFoundError

        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = []

        with pytest.raises(MailMessageNotFoundError, match="not found"):
            ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
                "ghost@x", mailbox="INBOX",
            )
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_logout_called_on_exception(
        self, mock_cls: MagicMock
    ) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        client.search.side_effect = RuntimeError("kaboom")

        with pytest.raises(RuntimeError):
            ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
                "abc@x", mailbox="INBOX",
            )
        client.logout.assert_called_once()


# ---------------------------------------------------------------------------
# Mailbox write operations (#162, #163): delete_mailbox / rename_mailbox
# ---------------------------------------------------------------------------


class TestImapDeleteMailbox:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_delete_empty_mailbox_returns_zero(
        self, mock_cls: MagicMock
    ) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        client.select_folder.return_value = {b"EXISTS": 0}

        result = ImapConnector("h", 993, "u@e.com", "pw").delete_mailbox(
            "Empty"
        )
        assert result == 0
        client.delete_folder.assert_called_once_with("Empty")
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_delete_non_empty_refuses_by_default(
        self, mock_cls: MagicMock
    ) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        client.select_folder.return_value = {b"EXISTS": 5}

        with pytest.raises(ValueError, match="not empty"):
            ImapConnector("h", 993, "u@e.com", "pw").delete_mailbox(
                "Big"
            )
        # Did not invoke delete_folder.
        client.delete_folder.assert_not_called()
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_delete_non_empty_with_allow_non_empty_cascades(
        self, mock_cls: MagicMock
    ) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        client.select_folder.return_value = {b"EXISTS": 42}

        result = ImapConnector("h", 993, "u@e.com", "pw").delete_mailbox(
            "Big", allow_non_empty=True
        )
        assert result == 42
        client.delete_folder.assert_called_once_with("Big")


class TestImapRenameMailbox:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_rename_calls_imap_rename(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client

        ImapConnector("h", 993, "u@e.com", "pw").rename_mailbox(
            "Old", "New"
        )
        client.rename_folder.assert_called_once_with("Old", "New")
        client.logout.assert_called_once()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_rename_with_path_change_passes_through(
        self, mock_cls: MagicMock
    ) -> None:
        """Move semantics: dest path with a different parent."""
        client = MagicMock()
        mock_cls.return_value = client

        ImapConnector("h", 993, "u@e.com", "pw").rename_mailbox(
            "OldParent/Child", "NewParent/Child"
        )
        client.rename_folder.assert_called_once_with(
            "OldParent/Child", "NewParent/Child"
        )


class TestImapMoveMessages:
    """Issue #149: server-side message move via UID MOVE (RFC 6851)
    or UID COPY + STORE \\Deleted + UID EXPUNGE (RFC 4315 UIDPLUS)."""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_uses_uid_move_when_capability_present(
        self, mock_cls: MagicMock
    ) -> None:
        """Headline path: server advertises MOVE → one round-trip after
        the per-id Message-ID resolution."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"IMAP4REV1", b"MOVE", b"UIDPLUS"}
        client.search.side_effect = [[101], [102]]

        moved = ImapConnector("h", 993, "u@e.com", "pw").move_messages(
            ["msg-a@example.com", "<msg-b@example.com>"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        assert moved == 2
        client.select_folder.assert_called_once_with("INBOX", readonly=False)
        client.move.assert_called_once_with([101, 102], "Archive")
        # The UIDPLUS fallback path must not have fired.
        client.copy.assert_not_called()
        client.uid_expunge.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_falls_back_to_copy_store_expunge_when_only_uidplus(
        self, mock_cls: MagicMock
    ) -> None:
        """No MOVE capability but UIDPLUS present: COPY + STORE \\Deleted
        + scoped UID EXPUNGE. Scoped expunge keeps it safe."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"IMAP4REV1", b"UIDPLUS"}
        client.search.side_effect = [[201], [202]]

        moved = ImapConnector("h", 993, "u@e.com", "pw").move_messages(
            ["a@example.com", "b@example.com"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        assert moved == 2
        client.move.assert_not_called()
        client.copy.assert_called_once_with([201, 202], "Archive")
        client.add_flags.assert_called_once_with(
            [201, 202], [b"\\Deleted"], silent=True
        )
        client.uid_expunge.assert_called_once_with([201, 202])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_raises_when_neither_move_nor_uidplus(
        self, mock_cls: MagicMock
    ) -> None:
        """Without MOVE or UIDPLUS we'd have to do an unscoped EXPUNGE
        (which removes ALL \\Deleted-flagged messages). Refuse instead;
        orchestrator handles the AppleScript fallback."""
        from apple_mail_mcp.exceptions import MailImapMoveUnsupportedError

        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"IMAP4REV1"}

        with pytest.raises(MailImapMoveUnsupportedError, match="MOVE"):
            ImapConnector("h", 993, "u@e.com", "pw").move_messages(
                ["a@example.com"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )
        client.move.assert_not_called()
        client.copy.assert_not_called()
        client.uid_expunge.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_returns_zero_when_no_uids_resolve(
        self, mock_cls: MagicMock
    ) -> None:
        """All Message-IDs miss in the source mailbox → no-op, return 0."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"MOVE"}
        client.search.return_value = []

        moved = ImapConnector("h", 993, "u@e.com", "pw").move_messages(
            ["missing-1@example.com", "missing-2@example.com"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        assert moved == 0
        client.move.assert_not_called()
        client.copy.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_strips_and_re_adds_brackets_for_search(
        self, mock_cls: MagicMock
    ) -> None:
        """Mix of bracketless and bracketed Message-IDs as input. Both
        arrive at SEARCH HEADER as the bracketed RFC 5322 form."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"MOVE"}
        client.search.side_effect = [[1], [2]]

        ImapConnector("h", 993, "u@e.com", "pw").move_messages(
            ["bare@example.com", "<wrapped@example.com>"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        searches = [c[0][0] for c in client.search.call_args_list]
        assert searches == [
            ["HEADER", "Message-ID", "<bare@example.com>"],
            ["HEADER", "Message-ID", "<wrapped@example.com>"],
        ]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_skips_message_ids_that_dont_resolve(
        self, mock_cls: MagicMock
    ) -> None:
        """Best-effort partial-success: 3 ids in, 2 resolve → MOVE on
        the 2; return 2. Matches the AppleScript path's behavior."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"MOVE"}
        client.search.side_effect = [[10], [], [11]]

        moved = ImapConnector("h", 993, "u@e.com", "pw").move_messages(
            ["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        assert moved == 2
        client.move.assert_called_once_with([10, 11], "Archive")


# ---------------------------------------------------------------------------
# Issue #122: Gmail X-GM-THRID dispatch in find_thread_members
# ---------------------------------------------------------------------------


def _gmail_caps_with_xgm() -> set[bytes]:
    """Capability list as Gmail returns it post-login (live-probed
    2026-05-02 — see docs/research/imap-thread-strategies.md addendum)."""
    return {
        b"IMAP4REV1", b"UNSELECT", b"IDLE", b"NAMESPACE", b"QUOTA",
        b"ID", b"XLIST", b"CHILDREN", b"X-GM-EXT-1", b"UIDPLUS",
        b"COMPRESS=DEFLATE", b"ENABLE", b"MOVE", b"CONDSTORE", b"ESEARCH",
        b"UTF8=ACCEPT", b"LIST-EXTENDED", b"LIST-STATUS", b"LITERAL-",
        b"SPECIAL-USE", b"APPENDLIMIT=35651584",
    }


def _generic_caps_no_xgm() -> set[bytes]:
    """Capability list for a non-Gmail server (e.g. iCloud, Fastmail).
    No X-GM-EXT-1 → Tier 1 must skip."""
    return {
        b"IMAP4REV1", b"UNSELECT", b"IDLE", b"NAMESPACE", b"QUOTA",
        b"ID", b"UIDPLUS", b"ENABLE", b"CONDSTORE", b"ESEARCH",
        b"SPECIAL-USE",
    }


def _gmail_folder_listing_with_all() -> list:
    """A Gmail-style folder listing with the \\All SPECIAL-USE flag."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Drafts"), b"/", "[Gmail]/Drafts"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
        ((b"\\HasNoChildren", b"\\All"), b"/", "[Gmail]/All Mail"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "[Gmail]/Trash"),
        ((b"\\HasNoChildren", b"\\Junk"), b"/", "[Gmail]/Spam"),
    ]


def _localized_gmail_folder_listing() -> list:
    """A localized Gmail (Italian) listing — \\All flag present, name
    differs. Hardcoding `[Gmail]/All Mail` would miss this; SPECIAL-USE
    is the robust answer."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\All"), b"/", "[Google Mail]/Tutta la posta"),
    ]


def _generic_folder_listing_no_all() -> list:
    """A non-Gmail listing with no \\All flag — Tier 1 must skip even
    when the capability is advertised."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "Trash"),
    ]


class TestFindThreadMembersGmail:
    """Tier 1 (Gmail X-GM-THRID) dispatch in find_thread_members.

    The dispatcher itself lives in find_thread_members; these tests
    drive it via mocked IMAPClient and assert the right strategy fired
    based on the server's advertised capabilities."""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_uses_xgm_thrid_when_capability_advertised(
        self, mock_cls: MagicMock
    ) -> None:
        """The headline path: X-GM-EXT-1 advertised, anchor in All Mail
        → 5 round trips, mailbox-count-independent. BFS path NOT hit."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_with_all()
        # SEARCH HEADER Message-ID → anchor's UID
        # SEARCH X-GM-THRID → all UIDs in conversation
        client.search.side_effect = [[100], [100, 101, 102]]
        # FETCH X-GM-THRID → conversation ID; FETCH ENVELOPE FLAGS → members
        client.fetch.side_effect = [
            {100: {b"X-GM-THRID": 1234567890123456789}},
            _fake_fetch_result([100, 101, 102]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        # Tier 1 SELECTed the All Mail folder (resolved via SPECIAL-USE).
        client.select_folder.assert_called_once_with(
            "[Gmail]/All Mail", readonly=True,
        )
        # Two SEARCHes (anchor lookup + thread expansion); two FETCHes.
        assert client.search.call_count == 2
        assert client.fetch.call_count == 2
        # The X-GM-THRID search uses the conversation ID returned by FETCH.
        thrid_search = client.search.call_args_list[1][0][0]
        assert thrid_search == ["X-GM-THRID", "1234567890123456789"]
        # All three thread members surfaced (deduped by Message-ID).
        assert len(result) == 3

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_falls_back_to_bfs_when_xgm_ext_not_advertised(
        self, mock_cls: MagicMock
    ) -> None:
        """Non-Gmail server (no X-GM-EXT-1) skips Tier 1 entirely and
        runs the BFS — the Tier-1 helpers are never invoked."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _generic_caps_no_xgm()
        client.list_folders.return_value = _generic_folder_listing_no_all()
        client.search.return_value = []  # BFS finds nothing → fast exit

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        # BFS iterates folders. No SEARCH for X-GM-THRID was ever issued.
        for call in client.search.call_args_list:
            args = call[0][0]
            assert args[0] != "X-GM-THRID", (
                "X-GM-THRID query must not run when X-GM-EXT-1 is absent"
            )

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_falls_back_to_bfs_when_no_all_mail_folder(
        self, mock_cls: MagicMock
    ) -> None:
        """X-GM-EXT-1 advertised but no \\All folder in the listing —
        unusual but possible (e.g. SPECIAL-USE not set up). Tier 1
        returns None; BFS runs in the same session."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _generic_folder_listing_no_all()
        client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        # No SELECT of any "All Mail" folder.
        for call in client.select_folder.call_args_list:
            assert "All Mail" not in call[0][0]
        # BFS path SELECTed the regular folders.
        assert client.select_folder.called

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_falls_back_to_bfs_when_anchor_not_in_all_mail(
        self, mock_cls: MagicMock
    ) -> None:
        """SEARCH HEADER returns no UID (anchor not present in All Mail).
        Could happen during sync lag or if the message was hard-deleted
        from All Mail. Fall through to BFS."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_with_all()
        # First search is anchor-in-All-Mail → empty. Subsequent searches
        # are the BFS per-folder per-id-per-header sequence — return empty.
        client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="missing@gmail.com",
            anchor_references=[],
        )

        # Tier 1 attempted: All Mail SELECTed first.
        first_select = client.select_folder.call_args_list[0]
        assert first_select[0][0] == "[Gmail]/All Mail"
        # Then BFS fired: many more SELECTs followed.
        assert client.select_folder.call_count > 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_falls_back_to_bfs_on_xgm_thrid_query_rejection(
        self, mock_cls: MagicMock
    ) -> None:
        """Server advertised X-GM-EXT-1 but rejects the X-GM-THRID query
        anyway (quirky server / partial extension support). Tier 1
        returns None; BFS picks up cleanly."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_with_all()
        # Anchor lookup succeeds (Tier 1 SEARCH HEADER returns [100]);
        # subsequent BFS searches find nothing.
        client.search.side_effect = [[100]] + [[]] * 100
        # First FETCH (Tier 1's X-GM-THRID query) is rejected; any later
        # FETCH (BFS only fetches when SEARCH found something — it
        # didn't, above — but be defensive) returns empty.
        client.fetch.side_effect = [IMAPClientError("BAD X-GM-THRID"), {}, {}]

        # Should NOT raise — IMAPClientError inside Tier 1 maps to None,
        # not a re-raise. (Connection is still healthy; only the
        # optimization path is broken.)
        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        # BFS ran after the Tier 1 fall-through (multiple SELECTs).
        assert client.select_folder.call_count > 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_uses_localized_all_mail_via_special_use(
        self, mock_cls: MagicMock
    ) -> None:
        """Italian Gmail: All Mail is named `[Google Mail]/Tutta la posta`.
        SPECIAL-USE flag is the only safe way to find it."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _localized_gmail_folder_listing()
        client.search.side_effect = [[1], [1]]
        client.fetch.side_effect = [
            {1: {b"X-GM-THRID": 999}},
            _fake_fetch_result([1]),
        ]

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        client.select_folder.assert_called_once_with(
            "[Google Mail]/Tutta la posta", readonly=True,
        )

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_returns_thread_members_in_chronological_order(
        self, mock_cls: MagicMock
    ) -> None:
        """The Tier 1 path must match Tier 3's existing sort: ascending
        date_received. Callers depend on this — get_thread surfaces
        results in chronological order."""
        from datetime import datetime as _dt

        # Three messages with intentionally OUT-OF-ORDER fetch UIDs vs dates.
        e_old = _fake_envelope(
            message_id=b"<old@gmail.com>", subject=b"Original",
            date=_dt(2026, 1, 1, 12, 0, 0),
        )
        e_mid = _fake_envelope(
            message_id=b"<mid@gmail.com>", subject=b"Re: Original",
            date=_dt(2026, 1, 5, 12, 0, 0),
        )
        e_new = _fake_envelope(
            message_id=b"<new@gmail.com>", subject=b"Re: Re: Original",
            date=_dt(2026, 1, 10, 12, 0, 0),
        )
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_with_all()
        client.search.side_effect = [[100], [102, 100, 101]]
        client.fetch.side_effect = [
            {100: {b"X-GM-THRID": 555}},
            {
                100: {b"ENVELOPE": e_old, b"FLAGS": (b"\\Seen",)},
                101: {b"ENVELOPE": e_mid, b"FLAGS": (b"\\Seen",)},
                102: {b"ENVELOPE": e_new, b"FLAGS": ()},
            },
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="old@gmail.com",
            anchor_references=[],
        )

        ids = [r["id"] for r in result]
        assert ids == ["old@gmail.com", "mid@gmail.com", "new@gmail.com"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_dedups_by_message_id(
        self, mock_cls: MagicMock
    ) -> None:
        """Defensive: even if the FETCH response somehow includes the
        same Message-ID twice (unusual but observed in practice with
        some servers when a message appears under multiple labels but
        has the same RFC 5322 Message-ID), the result has unique IDs."""
        e1 = _fake_envelope(message_id=b"<dup@gmail.com>", subject=b"S")
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_with_all()
        client.search.side_effect = [[1], [1, 2]]
        client.fetch.side_effect = [
            {1: {b"X-GM-THRID": 7}},
            {
                1: {b"ENVELOPE": e1, b"FLAGS": ()},
                2: {b"ENVELOPE": e1, b"FLAGS": ()},  # same Message-ID
            },
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="dup@gmail.com",
            anchor_references=[],
        )

        assert len(result) == 1


# ---------------------------------------------------------------------------
# Issue #125: Gmail X-GM-THRID per-mailbox iteration (Tier 1.5)
# ---------------------------------------------------------------------------


def _gmail_folder_listing_no_all_with_sent() -> list:
    """Gmail-style listing where [Gmail]/All Mail is hidden (per-folder
    IMAP opt-out). \\Sent is still visible. This is the configuration
    Tier 1.5 (#125) is designed for."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Drafts"), b"/", "[Gmail]/Drafts"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "[Gmail]/Trash"),
        ((b"\\HasNoChildren",), b"/", "Receipts"),
        ((b"\\HasNoChildren",), b"/", "Newsletters"),
    ]


class TestFindThreadMembersGmailPerMailbox:
    """Tier 1.5 (Gmail X-GM-THRID per-mailbox, #125) dispatch.

    Triggered when X-GM-EXT-1 is advertised but \\All is not in the
    folder listing. Anchor lookup tries INBOX then \\Sent; per-mailbox
    SEARCH X-GM-THRID collects siblings."""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_uses_per_mailbox_when_all_mail_hidden(
        self, mock_cls: MagicMock
    ) -> None:
        """X-GM-EXT-1 advertised, no \\All folder → Tier 1 returns None,
        Tier 1.5 fires: SELECT INBOX, SEARCH for anchor, FETCH X-GM-THRID,
        then iterate all selectable folders SEARCHing X-GM-THRID."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        # Searches in order:
        # 1. INBOX HEADER Message-ID anchor → [42]
        # 2-7. Per-folder X-GM-THRID search (6 folders)
        client.search.side_effect = [
            [42],         # anchor in INBOX
            [42, 43],     # INBOX X-GM-THRID
            [],           # Drafts: empty
            [101],        # Sent Mail: 1 hit
            [],           # Trash: empty
            [],           # Receipts: empty
            [],           # Newsletters: empty
        ]
        # FETCHes:
        # 1. X-GM-THRID for the anchor UID
        # 2. ENVELOPE+FLAGS for INBOX hits
        # 3. ENVELOPE+FLAGS for Sent hit
        client.fetch.side_effect = [
            {42: {b"X-GM-THRID": 9876543210987654321}},
            _fake_fetch_result([42, 43]),
            _fake_fetch_result([101]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        # Anchor SELECTed INBOX, then per-mailbox iteration SELECTed each.
        select_paths = [
            call.args[0] for call in client.select_folder.call_args_list
        ]
        assert select_paths[0] == "INBOX"  # anchor lookup
        assert "[Gmail]/Sent Mail" in select_paths
        # Three messages collected (deduped: anchor's UID 42 appears once).
        assert len(result) == 3

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_falls_back_to_sent_when_anchor_not_in_inbox(
        self, mock_cls: MagicMock
    ) -> None:
        """If INBOX SEARCH returns no anchor UID, Tier 1.5 SELECTs the
        \\Sent folder and tries again. Anchor found there → continue."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.side_effect = [
            [],         # INBOX anchor SEARCH: not here
            [55],       # Sent anchor SEARCH: found
            [55],       # INBOX X-GM-THRID
            [],         # Drafts
            [55],       # Sent X-GM-THRID
            [],         # Trash
            [],         # Receipts
            [],         # Newsletters
        ]
        client.fetch.side_effect = [
            {55: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([55]),  # INBOX
            _fake_fetch_result([55]),  # Sent (deduped by msg-id)
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        select_paths = [
            call.args[0] for call in client.select_folder.call_args_list
        ]
        assert select_paths[0] == "INBOX"
        assert select_paths[1] == "[Gmail]/Sent Mail"
        # Single result (deduped from INBOX+Sent).
        assert len(result) == 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_returns_none_when_anchor_in_neither_inbox_nor_sent(
        self, mock_cls: MagicMock
    ) -> None:
        """Anchor SEARCH returns [] in both INBOX and Sent → Tier 1.5
        returns None → dispatcher falls through to BFS (Tier 3)."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.return_value = []  # blanket empty

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        # No FETCH X-GM-THRID was issued (anchor never found).
        for call in client.fetch.call_args_list:
            data = call[0][1]
            assert b"X-GM-THRID" not in data, (
                "Tier 1.5 should not FETCH X-GM-THRID after failed anchor lookup"
            )
        # Tier 3 (BFS) ran with empty results.
        assert result == []

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_xgm_thrid_query_rejection_per_mailbox_continues(
        self, mock_cls: MagicMock
    ) -> None:
        """A single folder's X-GM-THRID search rejection is logged DEBUG
        and skipped; doesn't tank the whole strategy."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.side_effect = [
            [42],                            # anchor in INBOX
            [42],                            # INBOX X-GM-THRID OK
            IMAPClientError("rejected"),     # Drafts rejects
            [101],                           # Sent Mail OK
            [],                              # Trash
            [],                              # Receipts
            [],                              # Newsletters
        ]
        client.fetch.side_effect = [
            {42: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([42]),
            _fake_fetch_result([101]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        assert len(result) == 2  # INBOX + Sent (Drafts skipped)

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_anchor_select_rejection_in_inbox_falls_through_to_sent(
        self, mock_cls: MagicMock
    ) -> None:
        """SELECT INBOX raises during anchor lookup → Tier 1.5 moves on
        to \\Sent. Same SELECT failure during the per-mailbox loop is
        also caught and the folder is skipped silently."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        def select(folder: str, **_kw: Any) -> dict[bytes, int]:
            if folder == "INBOX":
                raise IMAPClientError("INBOX SELECT rejected")
            return {b"EXISTS": 0}
        client.select_folder.side_effect = select

        # Anchor SEARCH only runs once — in Sent, after INBOX SELECT failed.
        # Then the per-mailbox loop SELECTs each folder; INBOX raises again
        # and is skipped before SEARCH would have run for it.
        client.search.side_effect = [
            [55],   # Sent anchor SEARCH
            [],     # [Gmail]/Drafts X-GM-THRID
            [55],   # [Gmail]/Sent Mail X-GM-THRID
            [],     # [Gmail]/Trash
            [],     # Receipts
            [],     # Newsletters
        ]
        client.fetch.side_effect = [
            {55: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([55]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        select_paths = [c.args[0] for c in client.select_folder.call_args_list]
        assert select_paths[0] == "INBOX"          # tried first, rejected
        assert "[Gmail]/Sent Mail" in select_paths  # fall-through worked
        assert len(result) == 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_anchor_search_rejection_in_inbox_falls_through_to_sent(
        self, mock_cls: MagicMock
    ) -> None:
        """SELECT INBOX succeeds but the anchor SEARCH raises — Tier 1.5
        treats the folder as a miss and tries \\Sent."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.side_effect = [
            IMAPClientError("INBOX search rejected"),  # anchor INBOX
            [55],                                       # anchor Sent: found
            [],                                         # INBOX X-GM-THRID
            [],                                         # Drafts
            [55],                                       # Sent
            [],                                         # Trash
            [],                                         # Receipts
            [],                                         # Newsletters
        ]
        client.fetch.side_effect = [
            {55: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([55]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        assert len(result) == 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_thrid_fetch_rejection_falls_through_to_bfs(
        self, mock_cls: MagicMock
    ) -> None:
        """FETCH X-GM-THRID raises after the anchor was located → Tier 1.5
        returns None → BFS picks up."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.side_effect = [[55]] + [[]] * 100
        client.fetch.side_effect = [IMAPClientError("BAD X-GM-THRID")] + [{}] * 10

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        assert result == []
        # BFS ran (more SELECTs than the single INBOX anchor SELECT).
        assert client.select_folder.call_count > 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_thrid_missing_from_fetch_falls_through_to_bfs(
        self, mock_cls: MagicMock
    ) -> None:
        """FETCH succeeds but the response dict lacks the X-GM-THRID key
        (server inconsistency) → Tier 1.5 returns None → BFS runs."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.side_effect = [[55]] + [[]] * 100
        client.fetch.side_effect = [{55: {}}] + [{}] * 10  # no X-GM-THRID key

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        assert result == []
        assert client.select_folder.call_count > 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_noselect_folders_skipped_in_per_mailbox_loop(
        self, mock_cls: MagicMock
    ) -> None:
        """A folder marked \\Noselect (e.g. Gmail's `[Gmail]` category
        parent) is skipped without a SELECT attempt."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\Noselect", b"\\HasChildren"), b"/", "[Gmail]"),
            ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
        ]
        client.search.side_effect = [
            [42],   # anchor INBOX
            [42],   # INBOX X-GM-THRID
            [42],   # Sent X-GM-THRID
        ]
        client.fetch.side_effect = [
            {42: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([42]),
            _fake_fetch_result([42]),
        ]

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        select_paths = [c.args[0] for c in client.select_folder.call_args_list]
        assert "[Gmail]" not in select_paths

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_bytes_folder_name_decoded_in_per_mailbox_loop(
        self, mock_cls: MagicMock
    ) -> None:
        """`list_folders()` returning a bytes folder name (some servers do
        this when names contain non-ASCII) is decoded before SELECT."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", b"Receipts"),  # bytes name
        ]
        client.search.side_effect = [
            [42],   # anchor INBOX
            [42],   # INBOX X-GM-THRID
            [],     # Receipts X-GM-THRID
        ]
        client.fetch.side_effect = [
            {42: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([42]),
        ]

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        select_paths = [c.args[0] for c in client.select_folder.call_args_list]
        assert "Receipts" in select_paths

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_envelope_fetch_rejection_in_loop_skips_mailbox(
        self, mock_cls: MagicMock
    ) -> None:
        """A FETCH ENVELOPE+FLAGS rejection on a single folder is silently
        skipped — other folders' results still surface."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = _gmail_folder_listing_no_all_with_sent()

        client.search.side_effect = [
            [42],   # anchor INBOX
            [42],   # INBOX X-GM-THRID OK
            [99],   # Drafts X-GM-THRID OK (FETCH fails below)
            [],     # Sent
            [],     # Trash
            [],     # Receipts
            [],     # Newsletters
        ]
        client.fetch.side_effect = [
            {42: {b"X-GM-THRID": 1111}},
            _fake_fetch_result([42]),                   # INBOX OK
            IMAPClientError("Drafts FETCH rejected"),   # Drafts FETCH fails
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@gmail.com",
            anchor_references=[],
        )

        assert len(result) == 1  # INBOX surfaces, Drafts skipped

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_envelope_none_or_missing_msgid_is_skipped(
        self, mock_cls: MagicMock
    ) -> None:
        """Defensive: FETCH entries lacking ENVELOPE, or whose envelope has
        no Message-ID, are skipped rather than crashing the loop."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _gmail_caps_with_xgm()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
        ]
        client.search.side_effect = [
            [42],            # anchor INBOX
            [42, 43, 44],    # INBOX X-GM-THRID — 43 has no envelope, 44 has no msgid
        ]
        client.fetch.side_effect = [
            {42: {b"X-GM-THRID": 1111}},
            {
                42: {
                    b"ENVELOPE": _fake_envelope(message_id=b"<a@gmail.com>"),
                    b"FLAGS": (),
                },
                43: {b"ENVELOPE": None, b"FLAGS": ()},
                44: {b"ENVELOPE": _fake_envelope(message_id=b""), b"FLAGS": ()},
            },
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="a@gmail.com",
            anchor_references=[],
        )

        assert len(result) == 1
        assert result[0]["id"] == "a@gmail.com"


# ---------------------------------------------------------------------------
# Issue #123: RFC 5256 THREAD dispatch (Tier 2)
# ---------------------------------------------------------------------------


def _fastmail_caps_with_thread() -> set[bytes]:
    """Capability list for a Fastmail-like server: THREAD=REFERENCES
    advertised, no X-GM-EXT-1."""
    return {
        b"IMAP4REV1", b"UNSELECT", b"IDLE", b"NAMESPACE", b"QUOTA",
        b"ID", b"UIDPLUS", b"ENABLE", b"CONDSTORE", b"ESEARCH",
        b"SPECIAL-USE", b"THREAD=REFERENCES",
    }


def _caps_with_thread_refs_alias() -> set[bytes]:
    """Capability set advertising the THREAD=REFS alias (RFC 5256)."""
    return {
        b"IMAP4REV1", b"UNSELECT", b"IDLE", b"NAMESPACE",
        b"UIDPLUS", b"ENABLE", b"THREAD=REFS",
    }


def _fastmail_folder_listing() -> list:
    """A small folder listing for THREAD-dispatch tests."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
        ((b"\\HasNoChildren",), b"/", "Archive"),
    ]


class TestFindThreadMembersImapThread:
    """Tier 2 (RFC 5256 THREAD, #123) dispatch."""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_uses_imap_thread_when_capability_advertised(
        self, mock_cls: MagicMock
    ) -> None:
        """THREAD=REFERENCES advertised → Tier 2 fires: per mailbox, narrow
        SEARCH for anchor + sibling refs, then THREAD command, then walk
        tree for matching clusters."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = _fastmail_folder_listing()

        # Per-mailbox: 2 SEARCHes (anchor MsgID, References) + (if any
        # match) THREAD + FETCH.
        client.search.side_effect = [
            [7],   # INBOX HEADER Message-ID
            [],    # INBOX HEADER References
            [],    # Sent HEADER Message-ID
            [],    # Sent HEADER References
            [],    # Archive HEADER Message-ID
            [99],  # Archive HEADER References (sibling reply)
        ]
        # THREAD response: nested tuples per RFC 5256.
        client.thread.side_effect = [
            ((7, 8, 9),),                  # INBOX cluster contains anchor
            ((99, (100,)),),               # Archive cluster contains sibling
        ]
        client.fetch.side_effect = [
            _fake_fetch_result([7, 8, 9]),
            _fake_fetch_result([99, 100]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        # THREAD called twice (INBOX + Archive); not on Sent.
        assert client.thread.call_count == 2
        for thread_call in client.thread.call_args_list:
            assert thread_call.kwargs.get("algorithm") == "REFERENCES"
        assert len(result) == 5

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_accepts_thread_refs_alias(
        self, mock_cls: MagicMock
    ) -> None:
        """THREAD=REFS (Gmail's shorter form) also triggers Tier 2;
        algorithm passed to client.thread() is 'REFS' not 'REFERENCES'."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _caps_with_thread_refs_alias()
        client.list_folders.return_value = _fastmail_folder_listing()
        client.search.side_effect = [
            [1],   # INBOX MsgID
            [],    # INBOX References
            [],    # Sent MsgID
            [],    # Sent References
            [],    # Archive MsgID
            [],    # Archive References
        ]
        client.thread.return_value = ((1,),)
        client.fetch.return_value = _fake_fetch_result([1])

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        algos = [
            c.kwargs.get("algorithm") for c in client.thread.call_args_list
        ]
        assert all(a == "REFS" for a in algos)

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_skips_when_capability_missing(
        self, mock_cls: MagicMock
    ) -> None:
        """No THREAD=* in caps → Tier 2 helper never invoked."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _generic_caps_no_xgm()
        client.list_folders.return_value = _generic_folder_listing_no_all()
        client.search.return_value = []  # BFS finds nothing → fast exit

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        assert client.thread.call_count == 0

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_thread_command_rejection_falls_through_to_bfs(
        self, mock_cls: MagicMock
    ) -> None:
        """If client.thread() raises mid-flight (server lied about
        THREAD capability), Tier 2 returns None → BFS runs."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = _fastmail_folder_listing()

        client.search.side_effect = [
            [1],   # INBOX MsgID
            [],    # INBOX References
        ] + [[]] * 100  # plenty for BFS to fast-exit
        client.thread.side_effect = IMAPClientError("THREAD rejected")
        client.fetch.return_value = {}

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        # Tier 2 only made 2 SEARCHes (first folder, until THREAD raised).
        # BFS then ran more SEARCHes — assert the count exceeds Tier 2's.
        assert client.search.call_count > 2

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_returns_none_when_called_without_thread_capability(
        self, mock_cls: MagicMock
    ) -> None:
        """Defensive: if Tier 2 is invoked when neither THREAD=REFERENCES
        nor THREAD=REFS is advertised, return None rather than guessing
        an algorithm name. (Dispatcher gates on capability, but the helper
        defends against direct misuse.)"""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _generic_caps_no_xgm()  # no THREAD

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn._thread_via_imap_thread(
            client,
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        assert result is None
        # No THREAD command was issued.
        assert client.thread.call_count == 0

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_noselect_folders_skipped_in_tier2_loop(
        self, mock_cls: MagicMock
    ) -> None:
        """\\Noselect folders are skipped before any SELECT attempt."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\Noselect", b"\\HasChildren"), b"/", "Folders"),
            ((b"\\HasNoChildren",), b"/", "Archive"),
        ]
        client.search.side_effect = [
            [7],   # INBOX MsgID
            [],    # INBOX References
            [],    # Archive MsgID
            [],    # Archive References
        ]
        client.thread.return_value = ((7,),)
        client.fetch.return_value = _fake_fetch_result([7])

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        select_paths = [c.args[0] for c in client.select_folder.call_args_list]
        assert "Folders" not in select_paths
        assert "INBOX" in select_paths and "Archive" in select_paths

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_bytes_folder_name_decoded_in_tier2_loop(
        self, mock_cls: MagicMock
    ) -> None:
        """Bytes folder names from list_folders() are decoded before SELECT."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", b"Archive"),  # bytes name
        ]
        client.search.side_effect = [
            [7],   # INBOX MsgID
            [],    # INBOX References
            [],    # Archive MsgID
            [],    # Archive References
        ]
        client.thread.return_value = ((7,),)
        client.fetch.return_value = _fake_fetch_result([7])

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        select_paths = [c.args[0] for c in client.select_folder.call_args_list]
        assert "Archive" in select_paths

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_select_rejection_in_tier2_loop_continues(
        self, mock_cls: MagicMock
    ) -> None:
        """A single folder's SELECT rejection is logged DEBUG and the loop
        moves on — non-empty results from other folders still surface."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = _fastmail_folder_listing()

        def select(folder: str, **_kw: Any) -> dict[bytes, int]:
            if folder == "Sent":
                raise IMAPClientError("Sent SELECT rejected")
            return {b"EXISTS": 0}
        client.select_folder.side_effect = select

        client.search.side_effect = [
            [7],   # INBOX MsgID
            [],    # INBOX References
            [],    # Archive MsgID
            [],    # Archive References
        ]
        client.thread.return_value = ((7,),)
        client.fetch.return_value = _fake_fetch_result([7])

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        # Tier 2 still returned a non-empty result (so BFS didn't run);
        # Sent was attempted but skipped.
        assert len(result) == 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_search_rejection_in_tier2_loop_continues(
        self, mock_cls: MagicMock
    ) -> None:
        """An IMAPClientError on either narrow SEARCH (anchor MsgID or
        References) skips the folder and lets others contribute."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = _fastmail_folder_listing()

        client.search.side_effect = [
            IMAPClientError("INBOX MsgID search rejected"),
            # next iteration: Sent
            [],   # Sent MsgID
            [],   # Sent References
            [7],  # Archive MsgID
            [],   # Archive References
        ]
        client.thread.return_value = ((7,),)
        client.fetch.return_value = _fake_fetch_result([7])

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        assert len(result) == 1

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_thread_with_no_intersecting_clusters_continues(
        self, mock_cls: MagicMock
    ) -> None:
        """SEARCH found UIDs but THREAD response's clusters don't actually
        contain them (server inconsistency) — skip the folder, don't fetch."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Archive"),
        ]
        client.search.side_effect = [
            [7],    # INBOX MsgID
            [],     # INBOX References
            [],     # Archive MsgID
            [99],   # Archive References (sibling reply)
        ]
        client.thread.side_effect = [
            ((1, 2, 3),),       # INBOX clusters: no overlap with {7}
            ((99, (100,)),),    # Archive cluster overlaps {99}
        ]
        client.fetch.return_value = _fake_fetch_result([99, 100])

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        assert client.fetch.call_count == 1  # INBOX skipped pre-FETCH
        assert len(result) == 2

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_envelope_fetch_rejection_in_tier2_loop_continues(
        self, mock_cls: MagicMock
    ) -> None:
        """A FETCH ENVELOPE+FLAGS rejection on a single folder is silently
        skipped; other folders still contribute."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Archive"),
        ]
        client.search.side_effect = [
            [7],    # INBOX MsgID
            [],     # INBOX References
            [],     # Archive MsgID
            [99],   # Archive References
        ]
        client.thread.side_effect = [
            ((7,),),
            ((99,),),
        ]
        client.fetch.side_effect = [
            IMAPClientError("INBOX FETCH rejected"),
            _fake_fetch_result([99]),
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        assert len(result) == 1  # Archive surfaced; INBOX skipped

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_envelope_none_or_missing_msgid_is_skipped(
        self, mock_cls: MagicMock
    ) -> None:
        """Defensive: FETCH entries with ENVELOPE=None or empty Message-ID
        are skipped without crashing the loop. Duplicate Message-IDs across
        folders are deduped by the collected dict."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Archive"),
        ]
        client.search.side_effect = [
            [7],   # INBOX MsgID
            [],    # INBOX References
            [],    # Archive MsgID
            [99],  # Archive References
        ]
        client.thread.side_effect = [
            ((7, 8, 9),),
            ((99,),),
        ]
        client.fetch.side_effect = [
            {
                7: {
                    b"ENVELOPE": _fake_envelope(message_id=b"<a@example.com>"),
                    b"FLAGS": (),
                },
                8: {b"ENVELOPE": None, b"FLAGS": ()},
                9: {b"ENVELOPE": _fake_envelope(message_id=b""), b"FLAGS": ()},
            },
            {
                # Duplicate Message-ID across folders → dedup.
                99: {
                    b"ENVELOPE": _fake_envelope(message_id=b"<a@example.com>"),
                    b"FLAGS": (),
                },
            },
        ]

        result = ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="a@example.com",
            anchor_references=[],
        )

        # Three messages total in fetches but only one unique Message-ID
        # (and two skipped for missing data) → 1 result.
        assert len(result) == 1
        assert result[0]["id"] == "a@example.com"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_empty_collected_falls_through_to_bfs(
        self, mock_cls: MagicMock
    ) -> None:
        """If no folder contributed any members (all narrow SEARCHes
        empty), Tier 2 returns None and BFS runs."""
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = _fastmail_folder_listing()

        # All SEARCHes (Tier 2 narrow + BFS per-id-per-header) return [].
        client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        # Tier 2 never invoked THREAD (no UIDs to intersect against).
        assert client.thread.call_count == 0
        # Tier 2 made 2 SEARCHes per folder × 3 folders = 6.
        # BFS adds 3 SEARCHes (1 id × 3 headers) per folder.
        assert client.search.call_count > 6
