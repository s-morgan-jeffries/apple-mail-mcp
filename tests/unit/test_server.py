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
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from apple_mail_mcp.server import (
    create_mailbox,
    delete_messages,
    flag_message,
    forward_message,
    get_attachments,
    get_message,
    list_mailboxes,
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
def mock_confirm_yes() -> Any:
    with patch("apple_mail_mcp.server.require_confirmation", return_value=True) as m:
        yield m


@pytest.fixture
def mock_confirm_no() -> Any:
    with patch("apple_mail_mcp.server.require_confirmation", return_value=False) as m:
        yield m


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
    def test_success_logs_and_returns_details(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
    ) -> None:
        mock_mail.send_email.return_value = True

        result = send_email(
            subject="Hi",
            body="hello",
            to=["a@example.com"],
            cc=["b@example.com"],
        )

        assert result["success"] is True
        assert result["details"]["recipients"] == 2
        mock_confirm_yes.assert_called_once()
        mock_mail.send_email.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "success"

    def test_validation_failure_no_confirmation_or_send(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
    ) -> None:
        result = send_email(subject="Hi", body="b", to=[])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_confirm_yes.assert_not_called()
        mock_mail.send_email.assert_not_called()
        mock_logger.log_operation.assert_not_called()

    def test_confirmation_denied_logs_cancelled(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_no: MagicMock,
    ) -> None:
        result = send_email(
            subject="Hi", body="b", to=["a@example.com"]
        )

        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.send_email.assert_not_called()
        mock_logger.log_operation.assert_called_once()
        assert mock_logger.log_operation.call_args.args[0] == "send_email"
        assert mock_logger.log_operation.call_args.args[2] == "cancelled"

    def test_applescript_error_maps_to_send_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
    ) -> None:
        mock_mail.send_email.side_effect = MailAppleScriptError("fail")

        result = send_email(
            subject="Hi", body="b", to=["a@example.com"]
        )

        assert result["success"] is False
        assert result["error_type"] == "send_error"
        mock_logger.log_operation.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "failure"

    def test_unexpected_exception_maps_to_unknown(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
    ) -> None:
        mock_mail.send_email.side_effect = RuntimeError("boom")

        result = send_email(
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
    def test_success_returns_attachment_count(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "report.pdf"
        att.write_bytes(b"pdf")
        mock_mail.send_email_with_attachments.return_value = True

        result = send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is True
        assert result["details"]["attachments"] == 1
        mock_mail.send_email_with_attachments.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "success"

    def test_validation_failure_short_circuits(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
    ) -> None:
        result = send_email_with_attachments(
            subject="Hi", body="b", to=[], attachments=[]
        )

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.send_email_with_attachments.assert_not_called()
        mock_confirm_yes.assert_not_called()

    def test_missing_attachment_file(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
        tmp_path: Any,
    ) -> None:
        missing = tmp_path / "nope.pdf"

        result = send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(missing)],
        )

        assert result["success"] is False
        assert result["error_type"] == "file_not_found"
        mock_mail.send_email_with_attachments.assert_not_called()
        mock_confirm_yes.assert_not_called()

    def test_confirmation_denied_logs_cancelled(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_no: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")

        result = send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.send_email_with_attachments.assert_not_called()
        mock_logger.log_operation.assert_called_once()
        assert mock_logger.log_operation.call_args.args[2] == "cancelled"

    def test_connector_value_error_maps_to_validation_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")
        mock_mail.send_email_with_attachments.side_effect = ValueError("bad size")

        result = send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert mock_logger.log_operation.call_args.args[2] == "failure"

    def test_applescript_error_maps_to_send_error(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")
        mock_mail.send_email_with_attachments.side_effect = MailAppleScriptError("fail")

        result = send_email_with_attachments(
            subject="Hi",
            body="b",
            to=["a@example.com"],
            attachments=[str(att)],
        )

        assert result["success"] is False
        assert result["error_type"] == "send_error"
        assert mock_logger.log_operation.call_args.args[2] == "failure"

    def test_unexpected_exception_maps_to_unknown(
        self,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_confirm_yes: MagicMock,
        tmp_path: Any,
    ) -> None:
        att = tmp_path / "r.pdf"
        att.write_bytes(b"x")
        mock_mail.send_email_with_attachments.side_effect = RuntimeError("boom")

        result = send_email_with_attachments(
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
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.return_value = "fwd-7"

        result = forward_message(
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

    def test_empty_to_validation_error(self, mock_mail: MagicMock) -> None:
        result = forward_message("1", to=[])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.forward_message.assert_not_called()

    def test_message_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.side_effect = MailMessageNotFoundError("x")

        result = forward_message("999", to=["c@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    def test_value_error_from_connector(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.side_effect = ValueError("bad")

        result = forward_message("1", to=["c@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.forward_message.side_effect = RuntimeError("boom")

        result = forward_message("1", to=["c@example.com"])

        assert result["success"] is False
        assert result["error_type"] == "unknown"
