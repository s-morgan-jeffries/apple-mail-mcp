"""Security regression tests: IMAP CRLF command injection (CWE-93).

imapclient quotes string arguments but does NOT promote CR/LF-bearing
values to IMAP literals (its 8-bit detector only fires on bytes > 127),
so a raw CRLF embedded in a SEARCH term, a Message-ID, or a mailbox name
is sent inline on the authenticated control channel and splits the
command — letting subsequent bytes be parsed as a new tagged command.

The connector must reject control characters in every value that becomes
an IMAP command argument, before it reaches the wire.
"""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.imap_connector import ImapConnector, _build_search_criteria

# A representative injection payload and a grab-bag of control characters.
_INJECTION = 'x\r\nA1 DELETE "INBOX"'
_CONTROL_CHARS = ["\r", "\n", "\x00", "\x07", "\x1f"]


def _conn() -> ImapConnector:
    return ImapConnector("imap.example.com", 993, "u@example.com", "pw")


class TestSearchCriteriaRejectControlChars:
    """Free-text SEARCH criteria must reject control characters."""

    @pytest.mark.parametrize(
        "field",
        ["sender_contains", "subject_contains", "body_contains", "text_contains"],
    )
    @pytest.mark.parametrize("ctrl", _CONTROL_CHARS)
    def test_build_search_criteria_rejects_control_chars(self, field: str, ctrl: str) -> None:
        kwargs: dict[str, object] = {
            "sender_contains": None,
            "subject_contains": None,
            "read_status": None,
            "is_flagged": None,
            field: f"a{ctrl}b",
        }
        with pytest.raises(ValueError):
            _build_search_criteria(**kwargs)  # type: ignore[arg-type]

    def test_build_search_criteria_allows_clean_text(self) -> None:
        criteria = _build_search_criteria(None, "hello world", None, None)
        assert "SUBJECT" in criteria
        assert "hello world" in criteria

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_search_messages_never_reaches_wire_with_crlf(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().search_messages(subject_contains=_INJECTION)
        client.search.assert_not_called()
        client.select_folder.assert_not_called()


class TestMessageIdRejectControlChars:
    """Message-ID HEADER searches must reject control characters."""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_get_message_rejects_crlf(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().get_message(_INJECTION, mailbox="INBOX")
        client.search.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_delete_messages_rejects_crlf(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().delete_messages([_INJECTION], source_mailbox="INBOX")
        client.search.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_move_messages_rejects_crlf(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().move_messages(
                [_INJECTION], source_mailbox="INBOX", destination_mailbox="Archive"
            )
        client.search.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_find_thread_members_rejects_crlf_anchor(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().find_thread_members(_INJECTION, anchor_references=[])
        client.search.assert_not_called()


class TestMailboxNameRejectControlChars:
    """Mailbox/folder names become IMAP command args too (SELECT/DELETE/RENAME)."""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_search_rejects_crlf_mailbox(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().search_messages(mailbox="INBOX\r\nA1 DELETE x")
        client.select_folder.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_delete_mailbox_rejects_crlf(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().delete_mailbox("Junk\r\nA1 DELETE INBOX")
        # Must fail fast — before the SELECT pre-flight reaches the wire.
        client.select_folder.assert_not_called()
        client.delete_folder.assert_not_called()

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_rename_mailbox_rejects_crlf(self, mock_cls: MagicMock) -> None:
        client = MagicMock()
        mock_cls.return_value = client
        with pytest.raises(ValueError):
            _conn().rename_mailbox("Old", "New\r\nA1 DELETE INBOX")
        client.rename_folder.assert_not_called()
