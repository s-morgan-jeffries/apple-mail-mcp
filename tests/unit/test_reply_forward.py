"""Tests for reply and forward functionality."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import MailMessageNotFoundError
from apple_mail_mcp.mail_connector import AppleMailConnector


@pytest.fixture
def connector() -> AppleMailConnector:
    """Create a mail connector instance."""
    return AppleMailConnector()


class TestReplyToMessage:
    """Tests for replying to messages."""

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_basic(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test basic reply to a message."""
        mock_run.return_value = "67890"

        result = connector.reply_to_message(
            message_id="12345",
            body="Thanks for your email!",
            reply_all=False,
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "12345" in call_args
        assert "Thanks for your email!" in call_args
        assert "reply" in call_args.lower()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_all(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test reply all to a message."""
        mock_run.return_value = "67890"

        result = connector.reply_to_message(
            message_id="12345",
            body="Thanks everyone!",
            reply_all=True,
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "12345" in call_args
        assert "reply to all" in call_args.lower() or "reply all" in call_args.lower()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_with_quote(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test reply with original message quoted."""
        mock_run.return_value = "67890"

        result = connector.reply_to_message(
            message_id="12345",
            body="See my comments below.",
            reply_all=False,
            quote_original=True,
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "12345" in call_args
        # AppleScript should handle quoting via reply command

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_without_quote(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test reply without quoting original message."""
        mock_run.return_value = "67890"

        result = connector.reply_to_message(
            message_id="12345",
            body="Quick response!",
            reply_all=False,
            quote_original=False,
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "12345" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_message_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test reply when message doesn't exist."""
        mock_run.side_effect = MailMessageNotFoundError("Message not found")

        with pytest.raises(MailMessageNotFoundError):
            connector.reply_to_message(
                message_id="99999",
                body="This should fail",
                reply_all=False,
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_empty_body(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test reply with empty body."""
        mock_run.return_value = "67890"

        # Should work - some replies might have no text
        result = connector.reply_to_message(
            message_id="12345",
            body="",
            reply_all=False,
        )

        assert result == "67890"


class TestForwardMessage:
    """Tests for forwarding messages."""

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_single_recipient(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding to a single recipient."""
        mock_run.return_value = "67890"

        result = connector.forward_message(
            message_id="12345",
            to=["colleague@example.com"],
            body="FYI - see below.",
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "12345" in call_args
        assert "colleague@example.com" in call_args
        assert "forward" in call_args.lower()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_multiple_recipients(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding to multiple recipients."""
        mock_run.return_value = "67890"

        result = connector.forward_message(
            message_id="12345",
            to=["colleague1@example.com", "colleague2@example.com"],
            body="Sharing this with the team.",
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "colleague1@example.com" in call_args
        assert "colleague2@example.com" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_with_cc(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding with CC recipients."""
        mock_run.return_value = "67890"

        result = connector.forward_message(
            message_id="12345",
            to=["colleague@example.com"],
            cc=["manager@example.com"],
            body="For your information.",
        )

        assert result == "67890"
        call_args = mock_run.call_args[0][0]
        assert "colleague@example.com" in call_args
        assert "manager@example.com" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_with_attachments(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding with attachments included."""
        mock_run.return_value = "67890"

        result = connector.forward_message(
            message_id="12345",
            to=["colleague@example.com"],
            body="See attached documents.",
            include_attachments=True,
        )

        assert result == "67890"
        # AppleScript forward should preserve attachments by default
        mock_run.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_without_attachments(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding without attachments."""
        mock_run.return_value = "67890"

        result = connector.forward_message(
            message_id="12345",
            to=["colleague@example.com"],
            body="Just the message content.",
            include_attachments=False,
        )

        assert result == "67890"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_empty_recipient_list(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding with no recipients raises error."""
        with pytest.raises(ValueError):
            connector.forward_message(
                message_id="12345",
                to=[],
                body="This should fail",
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_message_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test forwarding non-existent message."""
        mock_run.side_effect = MailMessageNotFoundError("Message not found")

        with pytest.raises(MailMessageNotFoundError):
            connector.forward_message(
                message_id="99999",
                to=["someone@example.com"],
                body="This should fail",
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_validates_emails(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test email validation for forward recipients."""
        with pytest.raises(ValueError):
            connector.forward_message(
                message_id="12345",
                to=["invalid-email"],
                body="This should fail",
            )


class TestReplyForwardSecurity:
    """Security tests for reply and forward operations."""

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_sanitizes_body(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test that reply body is sanitized."""
        mock_run.return_value = "67890"

        connector.reply_to_message(
            message_id="12345",
            body='Dangerous "quotes" and \\backslashes\\',
            reply_all=False,
        )

        call_args = mock_run.call_args[0][0]
        # Should have escaped special characters
        assert '\\"' in call_args or "\\'" in call_args or "\\\\" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_sanitizes_body(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test that forward body is sanitized."""
        mock_run.return_value = "67890"

        connector.forward_message(
            message_id="12345",
            to=["safe@example.com"],
            body='Dangerous "quotes" and \\backslashes\\',
        )

        call_args = mock_run.call_args[0][0]
        # Should have escaped special characters
        assert '\\"' in call_args or "\\'" in call_args or "\\\\" in call_args
