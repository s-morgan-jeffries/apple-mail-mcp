"""Tests for get_selected_messages server tool."""

from unittest.mock import MagicMock, patch

from apple_mail_mcp.server import get_selected_messages


class TestGetSelectedMessagesTool:
    """Tests for the get_selected_messages MCP tool."""

    @patch("apple_mail_mcp.server.mail")
    def test_returns_selected_messages(self, mock_mail: MagicMock) -> None:
        """Test successful retrieval of selected messages."""
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "12345",
                "subject": "Hello",
                "sender": "alice@example.com",
                "date_received": "Mon Jan 1 2024",
                "read_status": True,
                "flagged": False,
                "content": "Hi there",
            }
        ]

        result = get_selected_messages()

        assert result["success"] is True
        assert result["count"] == 1
        assert result["messages"][0]["id"] == "12345"
        mock_mail.get_selected_messages.assert_called_once_with(include_content=True)

    @patch("apple_mail_mcp.server.mail")
    def test_returns_empty_when_nothing_selected(self, mock_mail: MagicMock) -> None:
        """Test response when no message is selected."""
        mock_mail.get_selected_messages.return_value = []

        result = get_selected_messages()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["messages"] == []

    @patch("apple_mail_mcp.server.mail")
    def test_without_content(self, mock_mail: MagicMock) -> None:
        """Test fetching selected messages without body content."""
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "99",
                "subject": "Test",
                "sender": "b@example.com",
                "date_received": "Tue Jan 2 2024",
                "read_status": False,
                "flagged": False,
                "content": "",
            }
        ]

        result = get_selected_messages(include_content=False)

        assert result["success"] is True
        mock_mail.get_selected_messages.assert_called_once_with(include_content=False)

    @patch("apple_mail_mcp.server.mail")
    def test_handles_applescript_error(self, mock_mail: MagicMock) -> None:
        """Test error handling when AppleScript fails."""
        from apple_mail_mcp.exceptions import MailAppleScriptError

        mock_mail.get_selected_messages.side_effect = MailAppleScriptError("timeout")

        result = get_selected_messages()

        assert result["success"] is False
        assert "error" in result
