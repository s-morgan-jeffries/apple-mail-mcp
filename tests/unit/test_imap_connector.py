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
