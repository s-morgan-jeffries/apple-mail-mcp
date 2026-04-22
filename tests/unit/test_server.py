"""
Unit tests for the FastMCP server layer in apple_mail_mcp.server.

These tests exercise each @mcp.tool() function directly as a regular Python
callable with a mocked AppleMailConnector. They cover server-layer concerns
that the connector tests cannot: input validation, confirmation flows,
exception-to-error_type mapping, structured response shape, and
operation_logger calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    DeclinedElicitation,
)

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from apple_mail_mcp.server import (
    _build_forward_summary,
    _build_send_summary,
    create_mailbox,
    delete_messages,
    flag_message,
    forward_message,
    get_attachments,
    get_message,
    get_thread,
    list_accounts,
    list_mailboxes,
    list_rules,
    mark_as_read,
    move_messages,
    reply_to_message,
    save_attachments,
    search_messages,
    send_email,
    send_email_with_attachments,
)


@pytest.fixture
def mock_mail() -> Any:
    with patch("apple_mail_mcp.server.mail") as m:
        yield m


@pytest.fixture
def mock_logger() -> Any:
    with patch("apple_mail_mcp.server.operation_logger") as m:
        yield m


@pytest.fixture
def mock_ctx_accept() -> MagicMock:
    """Mock MCP Context that accepts elicitation."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    return ctx


@pytest.fixture
def mock_ctx_decline() -> MagicMock:
    """Mock MCP Context that declines elicitation."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=DeclinedElicitation())
    return ctx


# ---------------------------------------------------------------------------
# 0. list_accounts
# ---------------------------------------------------------------------------


class TestListAccounts:
    def test_success_returns_accounts_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_accounts.return_value = [
            {"id": "UUID-1", "name": "Gmail",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            {"id": "UUID-2", "name": "iCloud",
             "email_addresses": ["me@icloud.com"],
             "account_type": "iCloud", "enabled": True},
        ]

        result = list_accounts()

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["accounts"]) == 2
        assert result["accounts"][0]["id"] == "UUID-1"
        mock_mail.list_accounts.assert_called_once_with()
        mock_logger.log_operation.assert_called_once_with(
            "list_accounts", {}, "success"
        )

    def test_empty_returns_empty_list(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_accounts.return_value = []

        result = list_accounts()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["accounts"] == []

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_accounts.side_effect = RuntimeError("boom")

        result = list_accounts()

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 0b. list_rules
# ---------------------------------------------------------------------------


class TestListRules:
    def test_success_returns_rules_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"name": "Junk filter", "enabled": True},
            {"name": "News", "enabled": False},
        ]

        result = list_rules()

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["rules"]) == 2
        mock_mail.list_rules.assert_called_once_with()
        mock_logger.log_operation.assert_called_once_with(
            "list_rules", {}, "success"
        )

    def test_empty_returns_empty_list(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = []

        result = list_rules()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["rules"] == []

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_rules.side_effect = RuntimeError("boom")

        result = list_rules()

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 1. list_mailboxes
# ---------------------------------------------------------------------------


class TestListMailboxes:
    def test_success_returns_mailboxes_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_mailboxes.return_value = [
            {"name": "INBOX", "unread_count": 3},
            {"name": "Sent", "unread_count": 0},
        ]

        result = list_mailboxes("Gmail")

        assert result["success"] is True
        assert result["account"] == "Gmail"
        assert len(result["mailboxes"]) == 2
        mock_mail.list_mailboxes.assert_called_once_with("Gmail")
        mock_logger.log_operation.assert_called_once_with(
            "list_mailboxes", {"account": "Gmail"}, "success"
        )

    def test_account_not_found_maps_to_error_type(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_mailboxes.side_effect = MailAccountNotFoundError("nope")

        result = list_mailboxes("Bogus")

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"
        assert "Bogus" in result["error"]
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_mailboxes.side_effect = RuntimeError("boom")

        result = list_mailboxes("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 2. search_messages
# ---------------------------------------------------------------------------


class TestSearchMessages:
    def test_success_returns_messages_with_count(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = [
            {"id": "1"},
            {"id": "2"},
        ]

        result = search_messages(
            "Gmail",
            mailbox="INBOX",
            sender_contains="alice@example.com",
            read_status=False,
            limit=10,
        )

        assert result["success"] is True
        assert result["account"] == "Gmail"
        assert result["mailbox"] == "INBOX"
        assert result["count"] == 2
        assert len(result["messages"]) == 2
        mock_mail.search_messages.assert_called_once_with(
            account="Gmail",
            mailbox="INBOX",
            sender_contains="alice@example.com",
            subject_contains=None,
            read_status=False,
            is_flagged=None,
            date_from=None,
            date_to=None,
            has_attachment=None,
            limit=10,
        )
        mock_logger.log_operation.assert_called_once()
        logged_op, logged_params, logged_status = mock_logger.log_operation.call_args.args
        assert logged_op == "search_messages"
        assert logged_status == "success"
        assert logged_params["filters"]["sender"] == "alice@example.com"

    def test_account_not_found_maps_to_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.side_effect = MailAccountNotFoundError("x")

        result = search_messages("Bogus")

        assert result["success"] is False
        assert result["error_type"] == "not_found"
        mock_logger.log_operation.assert_not_called()

    def test_mailbox_not_found_maps_to_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.side_effect = MailMailboxNotFoundError("x")

        result = search_messages("Gmail", mailbox="Missing")

        assert result["success"] is False
        assert result["error_type"] == "not_found"

    def test_advanced_filters_propagate_to_connector(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """New in #28: is_flagged, date_from, date_to, has_attachment must
        pass through to the connector and appear in the audit log."""
        mock_mail.search_messages.return_value = []

        result = search_messages(
            "Gmail",
            mailbox="INBOX",
            is_flagged=True,
            date_from="2026-04-01",
            date_to="2026-04-15",
            has_attachment=True,
            limit=25,
        )

        assert result["success"] is True
        mock_mail.search_messages.assert_called_once_with(
            account="Gmail",
            mailbox="INBOX",
            sender_contains=None,
            subject_contains=None,
            read_status=None,
            is_flagged=True,
            date_from="2026-04-01",
            date_to="2026-04-15",
            has_attachment=True,
            limit=25,
        )
        logged_params = mock_logger.log_operation.call_args.args[1]
        assert logged_params["filters"] == {
            "sender": None,
            "subject": None,
            "read_status": None,
            "is_flagged": True,
            "date_from": "2026-04-01",
            "date_to": "2026-04-15",
            "has_attachment": True,
        }

    def test_malformed_date_maps_to_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Connector raises ValueError on bad date; server surfaces
        error_type: validation_error (not generic unknown)."""
        mock_mail.search_messages.side_effect = ValueError(
            "date_from must be ISO 8601 YYYY-MM-DD, got: 'nope'"
        )

        result = search_messages("Gmail", date_from="nope")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "date_from" in result["error"]
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.side_effect = RuntimeError("boom")

        result = search_messages("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 3. get_message
# ---------------------------------------------------------------------------


class TestGetMessage:
    def test_success_passes_include_content(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_message.return_value = {"id": "1", "subject": "Hi"}

        result = get_message("1", include_content=False)

        assert result["success"] is True
        assert result["message"]["id"] == "1"
        mock_mail.get_message.assert_called_once_with("1", include_content=False)
        mock_logger.log_operation.assert_called_once_with(
            "get_message", {"message_id": "1"}, "success"
        )

    def test_message_not_found_maps_to_message_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_message.side_effect = MailMessageNotFoundError("x")

        result = get_message("999")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        assert "999" in result["error"]

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_message.side_effect = RuntimeError("boom")

        result = get_message("1")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 4. send_email
# ---------------------------------------------------------------------------


class TestSendEmail:
    async def test_success_logs_and_returns_details(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        mock_mail.send_email.return_value = True

        result = await send_email(
            subject="Hi",
            body="hello",
            to=["a@example.com"],
            cc=["b@example.com"],
        )

        assert result["success"] is True
        assert result["details"]["recipients"] == 2
        mock_mail.send_email.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "success"

    async def test_validation_failure_no_send(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        result = await send_email(subject="Hi", body="b", to=[])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.send_email.assert_not_called()
        mock_logger.log_operation.assert_not_called()

    async def test_elicitation_declined_logs_cancelled(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_decline: MagicMock,
    ) -> None:
        result = await send_email(
            subject="Hi", body="b", to=["a@example.com"], ctx=mock_ctx_decline
        )

        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.send_email.assert_not_called()
        mock_logger.log_operation.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "cancelled"

    async def test_applescript_error_maps_to_send_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        mock_mail.send_email.side_effect = MailAppleScriptError("fail")

        result = await send_email(
            subject="Hi", body="b", to=["a@example.com"]
        )

        assert result["success"] is False
        assert result["error_type"] == "send_error"
        mock_logger.log_operation.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "failure"

    async def test_unexpected_exception_maps_to_unknown(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        mock_mail.send_email.side_effect = RuntimeError("boom")

        result = await send_email(
            subject="Hi", body="b", to=["a@example.com"]
        )

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 5. mark_as_read
# ---------------------------------------------------------------------------


class TestMarkAsRead:
    def test_success_returns_updated_count(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.mark_as_read.return_value = 3

        result = mark_as_read(["1", "2", "3"], read=True)

        assert result["success"] is True
        assert result["updated"] == 3
        assert result["requested"] == 3
        mock_mail.mark_as_read.assert_called_once_with(["1", "2", "3"], read=True)
        mock_logger.log_operation.assert_called_once()

    def test_empty_list_fails_bulk_validation(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        result = mark_as_read([], read=True)

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.mark_as_read.assert_not_called()

    def test_over_limit_fails_validation(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        result = mark_as_read([str(i) for i in range(101)])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.mark_as_read.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.mark_as_read.side_effect = RuntimeError("boom")

        result = mark_as_read(["1"])

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 6. send_email_with_attachments
# ---------------------------------------------------------------------------


class TestSendEmailWithAttachments:
    async def test_success_returns_attachment_count(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "report.pdf"
        att.write_bytes(b"pdf")
        mock_mail.send_email_with_attachments.return_value = True

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is True
        assert result["details"]["attachments"] == 1
        mock_mail.send_email_with_attachments.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "success"

    async def test_validation_failure_short_circuits(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        result = await send_email_with_attachments(
            subject="Hi", body="b", to=[], attachments=[]
        )

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.send_email_with_attachments.assert_not_called()

    async def test_missing_attachment_file(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
    ) -> None:
        missing = tmp_path / "nope.pdf"

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(missing)],
        )

        assert result["success"] is False
        assert result["error_type"] == "file_not_found"
        mock_mail.send_email_with_attachments.assert_not_called()

    async def test_elicitation_declined_logs_cancelled(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_decline: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
            ctx=mock_ctx_decline,
        )

        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.send_email_with_attachments.assert_not_called()
        mock_logger.log_operation.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "cancelled"

    async def test_connector_value_error_maps_to_validation_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")
        mock_mail.send_email_with_attachments.side_effect = ValueError("bad size")

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert mock_logger.log_operation.call_args.args[2] == "failure"

    async def test_applescript_error_maps_to_send_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")
        mock_mail.send_email_with_attachments.side_effect = MailAppleScriptError("fail")

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "send_error"
        assert mock_logger.log_operation.call_args.args[2] == "failure"

    async def test_unexpected_exception_maps_to_unknown(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")
        mock_mail.send_email_with_attachments.side_effect = RuntimeError("boom")

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 7. get_attachments
# ---------------------------------------------------------------------------


class TestGetAttachments:
    def test_success_returns_attachments_and_count(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_attachments.return_value = [
            {"name": "a.pdf", "size": 10},
            {"name": "b.pdf", "size": 20},
        ]

        result = get_attachments("1")

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["attachments"]) == 2
        mock_logger.log_operation.assert_called_once()

    def test_message_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_attachments.side_effect = MailMessageNotFoundError("x")

        result = get_attachments("999")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        assert "999" in result["error"]

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_attachments.side_effect = RuntimeError("boom")

        result = get_attachments("1")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 7b. get_thread
# ---------------------------------------------------------------------------


class TestGetThread:
    def test_success_returns_thread_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.return_value = [
            {"id": "1", "subject": "Q3", "sender": "a@b", "date_received": "Mon", "read_status": True, "flagged": False},
            {"id": "2", "subject": "Re: Q3", "sender": "c@d", "date_received": "Tue", "read_status": False, "flagged": False},
        ]

        result = get_thread("1")

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["thread"]) == 2
        mock_mail.get_thread.assert_called_once_with("1")
        mock_logger.log_operation.assert_called_once_with(
            "get_thread", {"message_id": "1"}, "success"
        )

    def test_message_not_found_maps_to_message_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.side_effect = MailMessageNotFoundError("nope")

        result = get_thread("nope")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        assert "nope" in result["error"]
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.side_effect = RuntimeError("boom")

        result = get_thread("1")

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 8. save_attachments
# ---------------------------------------------------------------------------


class TestSaveAttachments:
    def test_success_returns_saved_count(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.return_value = 2

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is True
        assert result["saved"] == 2
        assert result["directory"] == str(tmp_path)
        mock_logger.log_operation.assert_called_once()

    def test_directory_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        missing = tmp_path / "does_not_exist"

        result = save_attachments("1", str(missing))

        assert result["success"] is False
        assert result["error_type"] == "directory_not_found"
        mock_mail.save_attachments.assert_not_called()

    def test_path_is_file_not_directory(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        file_path = tmp_path / "a.txt"
        file_path.write_text("x")

        result = save_attachments("1", str(file_path))

        assert result["success"] is False
        assert result["error_type"] == "invalid_directory"
        mock_mail.save_attachments.assert_not_called()

    def test_connector_value_error_maps_to_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.side_effect = ValueError("bad index")

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_message_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.side_effect = MailMessageNotFoundError("x")

        result = save_attachments("999", str(tmp_path))

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.side_effect = RuntimeError("boom")

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 9. move_messages
# ---------------------------------------------------------------------------


class TestMoveMessages:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.move_messages.return_value = 2

        result = move_messages(["1", "2"], "Archive", "Gmail")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["destination"] == "Archive"
        assert result["account"] == "Gmail"
        mock_mail.move_messages.assert_called_once_with(
            message_ids=["1", "2"],
            destination_mailbox="Archive",
            account="Gmail",
            gmail_mode=False,
        )

    def test_empty_list_early_exit(self, mock_mail: MagicMock) -> None:
        result = move_messages([], "Archive", "Gmail")

        assert result["success"] is True
        assert result["count"] == 0
        mock_mail.move_messages.assert_not_called()

    def test_mailbox_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.move_messages.side_effect = MailMailboxNotFoundError("x")

        result = move_messages(["1"], "Missing", "Gmail")

        assert result["success"] is False
        assert result["error_type"] == "mailbox_not_found"

    def test_account_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.move_messages.side_effect = MailAccountNotFoundError("x")

        result = move_messages(["1"], "Archive", "Bogus")

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"

    def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.move_messages.side_effect = RuntimeError("boom")

        result = move_messages(["1"], "Archive", "Gmail")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 10. flag_message
# ---------------------------------------------------------------------------


class TestFlagMessage:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.flag_message.return_value = 1

        result = flag_message(["1"], "red")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["flag_color"] == "red"
        mock_mail.flag_message.assert_called_once_with(
            message_ids=["1"], flag_color="red"
        )

    def test_empty_list_early_exit(self, mock_mail: MagicMock) -> None:
        result = flag_message([], "red")

        assert result["success"] is True
        assert result["count"] == 0
        mock_mail.flag_message.assert_not_called()

    def test_invalid_color_value_error(self, mock_mail: MagicMock) -> None:
        mock_mail.flag_message.side_effect = ValueError("bad color")

        result = flag_message(["1"], "chartreuse")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_message_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.flag_message.side_effect = MailMessageNotFoundError("x")

        result = flag_message(["999"], "red")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.flag_message.side_effect = RuntimeError("boom")

        result = flag_message(["1"], "red")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 11. create_mailbox
# ---------------------------------------------------------------------------


class TestCreateMailbox:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.create_mailbox.return_value = True

        result = create_mailbox("Gmail", "Projects", parent_mailbox="Work")

        assert result["success"] is True
        assert result["account"] == "Gmail"
        assert result["mailbox"] == "Projects"
        assert result["parent"] == "Work"
        mock_mail.create_mailbox.assert_called_once_with(
            account="Gmail", name="Projects", parent_mailbox="Work"
        )

    def test_empty_name_validation_error(self, mock_mail: MagicMock) -> None:
        result = create_mailbox("Gmail", "")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_mailbox.assert_not_called()

    def test_whitespace_only_name_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        result = create_mailbox("Gmail", "   ")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_mailbox.assert_not_called()

    def test_account_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.create_mailbox.side_effect = MailAccountNotFoundError("x")

        result = create_mailbox("Bogus", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"

    def test_connector_value_error_maps_to_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.create_mailbox.side_effect = ValueError("bad name")

        result = create_mailbox("Gmail", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_applescript_error(self, mock_mail: MagicMock) -> None:
        mock_mail.create_mailbox.side_effect = MailAppleScriptError("fail")

        result = create_mailbox("Gmail", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "applescript_error"

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.create_mailbox.side_effect = RuntimeError("boom")

        result = create_mailbox("Gmail", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 12. delete_messages
# ---------------------------------------------------------------------------


class TestDeleteMessages:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.return_value = 2

        result = delete_messages(["1", "2"], permanent=False)

        assert result["success"] is True
        assert result["count"] == 2
        assert result["permanent"] is False
        mock_mail.delete_messages.assert_called_once_with(
            message_ids=["1", "2"], permanent=False, skip_bulk_check=False
        )

    def test_empty_list_early_exit(self, mock_mail: MagicMock) -> None:
        result = delete_messages([])

        assert result["success"] is True
        assert result["count"] == 0
        mock_mail.delete_messages.assert_not_called()

    def test_over_limit_validation_error(self, mock_mail: MagicMock) -> None:
        result = delete_messages([str(i) for i in range(101)])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.delete_messages.assert_not_called()

    def test_value_error_from_connector(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.side_effect = ValueError("bad")

        result = delete_messages(["1"])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_message_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.side_effect = MailMessageNotFoundError("x")

        result = delete_messages(["999"])

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.side_effect = RuntimeError("boom")

        result = delete_messages(["1"])

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 13. reply_to_message
# ---------------------------------------------------------------------------


class TestReplyToMessage:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.reply_to_message.return_value = "reply-42"

        result = reply_to_message("1", "thanks", reply_all=True)

        assert result["success"] is True
        assert result["reply_id"] == "reply-42"
        assert result["original_message_id"] == "1"
        assert result["reply_all"] is True
        mock_mail.reply_to_message.assert_called_once_with(
            message_id="1", body="thanks", reply_all=True
        )

    def test_message_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.reply_to_message.side_effect = MailMessageNotFoundError("x")

        result = reply_to_message("999", "hi")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        assert "999" in result["error"]

    def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.reply_to_message.side_effect = RuntimeError("boom")

        result = reply_to_message("1", "hi")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 14. forward_message
# ---------------------------------------------------------------------------


class TestForwardMessage:
    async def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.return_value = "fwd-7"

        result = await forward_message(
            "1",
            to=["c@example.com"],
            body="fyi",
            cc=["d@example.com"],
        )

        assert result["success"] is True
        assert result["forward_id"] == "fwd-7"
        assert result["original_message_id"] == "1"
        assert result["recipients"] == ["c@example.com"]
        assert result["cc"] == ["d@example.com"]

    async def test_empty_to_validation_error(self, mock_mail: MagicMock) -> None:
        result = await forward_message("1", to=[])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.forward_message.assert_not_called()

    async def test_elicitation_declined_cancels(
        self, mock_mail: MagicMock, mock_ctx_decline: MagicMock
    ) -> None:
        result = await forward_message("1", to=["c@example.com"], ctx=mock_ctx_decline)

        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.forward_message.assert_not_called()

    async def test_message_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.side_effect = MailMessageNotFoundError("x")

        result = await forward_message("999", to=["c@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    async def test_value_error_from_connector(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.side_effect = ValueError("bad")

        result = await forward_message("1", to=["c@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    async def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.side_effect = RuntimeError("boom")

        result = await forward_message("1", to=["c@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# Rate limiting integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tight_limits() -> Any:
    """Monkeypatch TIER_LIMITS down to 2 calls/60s so we can trip them easily."""
    import apple_mail_mcp.security as sec
    original = sec.TIER_LIMITS.copy()
    sec.TIER_LIMITS.update({
        "cheap_reads": (2, 60.0),
        "expensive_ops": (2, 60.0),
        "sends": (2, 60.0),
    })
    yield
    sec.TIER_LIMITS.update(original)


class TestRateLimitingIntegration:
    """Verify rate limiting fires before connector calls in each tool."""

    def test_cheap_read_rate_limited(
        self, mock_mail: MagicMock, tight_limits: Any
    ) -> None:
        mock_mail.list_mailboxes.return_value = []

        list_mailboxes("Gmail")
        list_mailboxes("Gmail")
        result = list_mailboxes("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert mock_mail.list_mailboxes.call_count == 2

    def test_expensive_op_rate_limited(
        self, mock_mail: MagicMock, tight_limits: Any
    ) -> None:
        mock_mail.search_messages.return_value = []

        search_messages("Gmail")
        search_messages("Gmail")
        result = search_messages("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert mock_mail.search_messages.call_count == 2

    async def test_sends_rate_limited(
        self, mock_mail: MagicMock, tight_limits: Any
    ) -> None:
        mock_mail.send_email.return_value = True

        await send_email(subject="a", body="b", to=["x@example.com"])
        await send_email(subject="a", body="b", to=["x@example.com"])
        result = await send_email(subject="a", body="b", to=["x@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert mock_mail.send_email.call_count == 2

    def test_rate_limit_fires_before_connector(
        self, mock_mail: MagicMock, tight_limits: Any
    ) -> None:
        """Prove the connector is never called once rate-limited."""
        mock_mail.get_message.return_value = {"id": "1"}

        get_message("1")
        get_message("1")
        result = get_message("1")

        assert result["error_type"] == "rate_limited"
        assert mock_mail.get_message.call_count == 2

    async def test_tiers_are_independent_in_server(
        self, mock_mail: MagicMock, tight_limits: Any
    ) -> None:
        mock_mail.send_email.return_value = True
        mock_mail.list_mailboxes.return_value = []

        await send_email(subject="a", body="b", to=["x@example.com"])
        await send_email(subject="a", body="b", to=["x@example.com"])

        result = list_mailboxes("Gmail")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Elicitation / confirmation tests
# ---------------------------------------------------------------------------


class TestBuildSendSummary:
    def test_basic_summary(self) -> None:
        result = _build_send_summary("Hi", ["a@example.com"], None, None, "body")
        assert "Send this email?" in result
        assert "To: a@example.com" in result
        assert "Subject: Hi" in result
        assert "body" in result

    def test_includes_cc_bcc(self) -> None:
        result = _build_send_summary(
            "Hi", ["a@example.com"], ["b@example.com"], ["c@example.com"], "x"
        )
        assert "CC: b@example.com" in result
        assert "BCC: c@example.com" in result

    def test_truncates_long_body(self) -> None:
        long_body = "x" * 300
        result = _build_send_summary("Hi", ["a@example.com"], None, None, long_body)
        assert "..." in result
        assert len(result) < 400


class TestBuildForwardSummary:
    def test_basic_summary(self) -> None:
        result = _build_forward_summary("msg-1", ["a@example.com"], None, None, "fyi")
        assert "Forward this message?" in result
        assert "msg-1" in result
        assert "To: a@example.com" in result
        assert "fyi" in result


class TestElicitationFlow:
    async def test_send_email_with_accepted_ctx(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.send_email.return_value = True

        result = await send_email(
            subject="Hi", body="b", to=["a@example.com"], ctx=mock_ctx_accept
        )

        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()
        mock_mail.send_email.assert_called_once()

    async def test_send_email_without_ctx_skips_elicitation(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.send_email.return_value = True

        result = await send_email(
            subject="Hi", body="b", to=["a@example.com"], ctx=None
        )

        assert result["success"] is True
        mock_mail.send_email.assert_called_once()

    async def test_forward_with_accepted_ctx(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.forward_message.return_value = "fwd-1"

        result = await forward_message(
            "1", to=["a@example.com"], ctx=mock_ctx_accept
        )

        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()

    async def test_elicitation_exception_falls_open(
        self, mock_mail: MagicMock
    ) -> None:
        ctx = MagicMock()
        ctx.elicit = AsyncMock(side_effect=RuntimeError("unsupported"))
        mock_mail.send_email.return_value = True

        result = await send_email(
            subject="Hi", body="b", to=["a@example.com"], ctx=ctx
        )

        assert result["success"] is True
        mock_mail.send_email.assert_called_once()


# ---------------------------------------------------------------------------
# Test-mode safety gate (MAIL_TEST_MODE)
# ---------------------------------------------------------------------------


class TestSafetyGate:
    """Verify test-mode safety gate fires before other checks in each tool."""

    async def test_send_email_blocked_by_real_recipient(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = await send_email(
            subject="Hi", body="b", to=["real@person.com"]
        )

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.send_email.assert_not_called()

    async def test_send_email_allowed_with_reserved_recipient(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        mock_mail.send_email.return_value = True

        result = await send_email(
            subject="Hi", body="b", to=["test@example.com"]
        )

        assert result["success"] is True

    async def test_send_email_with_attachments_blocked_by_real_recipient(
        self, mock_mail: MagicMock, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        att = tmp_path / "a.pdf"
        att.write_bytes(b"x")

        result = await send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["real@person.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.send_email_with_attachments.assert_not_called()

    async def test_forward_message_blocked_by_real_recipient(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = await forward_message("1", to=["real@person.com"])

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.forward_message.assert_not_called()

    def test_reply_to_message_blocked_entirely(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")

        result = reply_to_message("1", "hi")

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.reply_to_message.assert_not_called()

    def test_list_mailboxes_blocked_by_wrong_account(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = list_mailboxes("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.list_mailboxes.assert_not_called()

    def test_list_mailboxes_allowed_with_test_account(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        mock_mail.list_mailboxes.return_value = []

        result = list_mailboxes("TestAccount")

        assert result["success"] is True

    def test_search_messages_blocked_by_wrong_account(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = search_messages("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.search_messages.assert_not_called()

    def test_move_messages_blocked_by_wrong_account(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = move_messages(["1"], "Archive", "Gmail")

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.move_messages.assert_not_called()

    def test_create_mailbox_blocked_by_wrong_account(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = create_mailbox("Gmail", "NewBox")

        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_mail.create_mailbox.assert_not_called()

    def test_safety_fires_before_rate_limit(
        self, mock_mail: MagicMock, monkeypatch: Any, tight_limits: Any
    ) -> None:
        """When both tight rate limits and bad account, safety error wins."""
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")

        result = list_mailboxes("Gmail")

        assert result["error_type"] == "safety_violation"
        mock_mail.list_mailboxes.assert_not_called()

    def test_non_test_mode_production_unaffected(
        self, mock_mail: MagicMock, monkeypatch: Any
    ) -> None:
        """Without MAIL_TEST_MODE, all operations work normally."""
        monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
        mock_mail.list_mailboxes.return_value = []

        result = list_mailboxes("Gmail")

        assert result["success"] is True
