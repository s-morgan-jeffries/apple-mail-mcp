"""Unit tests for message management functionality."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailAppleScriptError,
    MailMailboxNotFoundError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector


class TestMoveMessages:
    """Tests for moving messages between mailboxes."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_single_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test moving a single message."""
        mock_run.return_value = "1"

        result = connector.move_messages(
            message_ids=["12345"],
            destination_mailbox="Archive",
            account="Gmail"
        )

        assert result == 1
        call_args = mock_run.call_args[0][0]
        assert "Archive" in call_args
        assert "12345" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_multiple_messages(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test moving multiple messages."""
        mock_run.return_value = "3"

        result = connector.move_messages(
            message_ids=["12345", "12346", "12347"],
            destination_mailbox="Archive",
            account="Gmail"
        )

        assert result == 3

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_to_nested_mailbox(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test moving to nested mailbox."""
        mock_run.return_value = "1"

        result = connector.move_messages(
            message_ids=["12345"],
            destination_mailbox="Projects/Client Work",
            account="Gmail"
        )

        assert result == 1
        call_args = mock_run.call_args[0][0]
        assert "Projects/Client Work" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_with_gmail_handling(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test Gmail-specific move handling (copy + delete)."""
        mock_run.return_value = "1"

        result = connector.move_messages(
            message_ids=["12345"],
            destination_mailbox="Archive",
            account="Gmail",
            gmail_mode=True
        )

        assert result == 1
        # Gmail mode uses copy+delete instead of native move
        mock_run.assert_called_once()

    def test_move_empty_list(self, connector: AppleMailConnector) -> None:
        """Test moving with empty message list."""
        result = connector.move_messages(
            message_ids=[],
            destination_mailbox="Archive",
            account="Gmail"
        )
        assert result == 0

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test error when destination mailbox doesn't exist."""
        mock_run.side_effect = MailMailboxNotFoundError("Mailbox not found")

        with pytest.raises(MailMailboxNotFoundError):
            connector.move_messages(
                message_ids=["12345"],
                destination_mailbox="NonExistent",
                account="Gmail"
            )


class TestFlagMessage:
    """Tests for flagging messages."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_with_red(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test flagging message with red flag."""
        mock_run.return_value = "1"

        result = connector.flag_message(
            message_ids=["12345"],
            flag_color="red"
        )

        assert result == 1
        call_args = mock_run.call_args[0][0]
        assert "flag index" in call_args
        assert "1" in call_args  # Red is index 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_with_none(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test removing flag from message."""
        mock_run.return_value = "1"

        result = connector.flag_message(
            message_ids=["12345"],
            flag_color="none"
        )

        assert result == 1
        call_args = mock_run.call_args[0][0]
        assert "-1" in call_args  # None is index -1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_multiple_messages(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test flagging multiple messages."""
        mock_run.return_value = "3"

        result = connector.flag_message(
            message_ids=["12345", "12346", "12347"],
            flag_color="blue"
        )

        assert result == 3

    def test_flag_invalid_color(self, connector: AppleMailConnector) -> None:
        """Test error with invalid flag color."""
        with pytest.raises(ValueError):
            connector.flag_message(
                message_ids=["12345"],
                flag_color="invalid"
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_all_colors(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test all valid flag colors."""
        valid_colors = ["none", "orange", "red", "yellow", "blue", "green", "purple", "gray"]

        for color in valid_colors:
            mock_run.return_value = "1"
            result = connector.flag_message(
                message_ids=["12345"],
                flag_color=color
            )
            assert result == 1


class TestCreateMailbox:
    """Tests for creating mailboxes."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_top_level_mailbox(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test creating a top-level mailbox."""
        mock_run.return_value = "success"

        result = connector.create_mailbox(
            account="Gmail",
            name="Archive"
        )

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert "Archive" in call_args
        assert "make new mailbox" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_nested_mailbox(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test creating a nested mailbox."""
        mock_run.return_value = "success"

        result = connector.create_mailbox(
            account="Gmail",
            name="Client Work",
            parent_mailbox="Projects"
        )

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert "Client Work" in call_args
        assert "Projects" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_mailbox_already_exists(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test error when mailbox already exists."""
        mock_run.side_effect = MailAppleScriptError("Mailbox already exists")

        with pytest.raises(MailAppleScriptError):
            connector.create_mailbox(
                account="Gmail",
                name="INBOX"  # Already exists
            )

    def test_create_mailbox_invalid_name(self, connector: AppleMailConnector) -> None:
        """Test error with invalid mailbox name."""
        with pytest.raises(ValueError):
            connector.create_mailbox(
                account="Gmail",
                name=""  # Empty name
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_mailbox_dangerous_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test sanitization of dangerous mailbox names."""
        mock_run.return_value = "success"

        # Path traversal attempt should be sanitized to just "etc"
        result = connector.create_mailbox(
            account="Gmail",
            name="../../../etc"  # Path traversal attempt gets sanitized
        )

        assert result is True
        call_args = mock_run.call_args[0][0]
        # Should not contain path traversal
        assert "../" not in call_args
        # Should contain sanitized name
        assert "etc" in call_args


class TestDeleteMessages:
    """Tests for deleting messages."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_single_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test deleting a single message (move to trash)."""
        mock_run.return_value = "1"

        result = connector.delete_messages(
            message_ids=["12345"],
            permanent=False
        )

        assert result == 1
        call_args = mock_run.call_args[0][0]
        assert "delete" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_multiple_messages(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test deleting multiple messages."""
        mock_run.return_value = "3"

        result = connector.delete_messages(
            message_ids=["12345", "12346", "12347"],
            permanent=False
        )

        assert result == 3

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_permanent_delete(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test permanent deletion (bypass trash)."""
        mock_run.return_value = "1"

        result = connector.delete_messages(
            message_ids=["12345"],
            permanent=True
        )

        assert result == 1
        # Permanent delete should have different script

    def test_delete_empty_list(self, connector: AppleMailConnector) -> None:
        """Test deleting with empty message list."""
        result = connector.delete_messages(
            message_ids=[],
            permanent=False
        )
        assert result == 0

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_validates_bulk_limit(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test that bulk delete operations are limited."""
        # Should enforce reasonable limits for safety
        large_list = [str(i) for i in range(200)]

        # Should not allow deleting too many at once without confirmation
        with pytest.raises((ValueError, MailAppleScriptError)):
            connector.delete_messages(
                message_ids=large_list,
                permanent=False,
                skip_bulk_check=False
            )


class TestMessageManagementSecurity:
    """Tests for message management security features."""

    def test_validates_mailbox_name(self) -> None:
        """Test mailbox name validation."""
        from apple_mail_mcp.utils import sanitize_mailbox_name

        # Should remove dangerous characters
        assert sanitize_mailbox_name("Valid Name") == "Valid Name"
        assert sanitize_mailbox_name("../../../") == ""
        assert sanitize_mailbox_name("Name<>:") == "Name"

    def test_flag_color_validation(self) -> None:
        """Test flag color validation."""
        from apple_mail_mcp.utils import validate_flag_color

        valid_colors = ["none", "orange", "red", "yellow", "blue", "green", "purple", "gray"]
        for color in valid_colors:
            assert validate_flag_color(color) is True

        assert validate_flag_color("invalid") is False
        assert validate_flag_color("") is False
