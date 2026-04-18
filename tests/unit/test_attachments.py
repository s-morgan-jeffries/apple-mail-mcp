"""Unit tests for attachment functionality."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailAppleScriptError,
    MailMessageNotFoundError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector


class TestSendWithAttachments:
    """Tests for sending emails with attachments."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @pytest.fixture
    def test_file(self, tmp_path: Path) -> Path:
        """Create a test file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content")
        return test_file

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_with_single_attachment(
        self, mock_run: MagicMock, connector: AppleMailConnector, test_file: Path
    ) -> None:
        """Test sending email with single attachment."""
        mock_run.return_value = "sent"

        result = connector.send_email_with_attachments(
            subject="Test",
            body="Test body",
            to=["recipient@example.com"],
            attachments=[test_file]
        )

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert str(test_file) in call_args
        assert "make new attachment" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_with_multiple_attachments(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Test sending email with multiple attachments."""
        mock_run.return_value = "sent"

        # Create multiple test files
        file1 = tmp_path / "file1.pdf"
        file2 = tmp_path / "file2.txt"
        file1.write_bytes(b"PDF content")
        file2.write_text("Text content")

        result = connector.send_email_with_attachments(
            subject="Test",
            body="Test body",
            to=["recipient@example.com"],
            attachments=[file1, file2]
        )

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert str(file1) in call_args
        assert str(file2) in call_args

    def test_send_with_nonexistent_file(self, connector: AppleMailConnector) -> None:
        """Test error when attachment file doesn't exist."""
        from apple_mail_mcp.exceptions import MailAppleScriptError

        with pytest.raises((MailAppleScriptError, FileNotFoundError)):
            connector.send_email_with_attachments(
                subject="Test",
                body="Test body",
                to=["recipient@example.com"],
                attachments=[Path("/nonexistent/file.txt")]
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_validates_attachment_size(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Test that large attachments are validated."""
        # Create a file larger than the limit
        large_file = tmp_path / "large.bin"
        # We'll implement size checking in the connector
        large_file.write_bytes(b"x" * (26 * 1024 * 1024))  # 26MB

        # Should raise error about file size
        with pytest.raises((ValueError, MailAppleScriptError)):
            connector.send_email_with_attachments(
                subject="Test",
                body="Test",
                to=["test@example.com"],
                attachments=[large_file],
                max_attachment_size=25 * 1024 * 1024  # 25MB limit
            )


class TestGetAttachments:
    """Tests for getting attachment information."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_list(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test listing attachments from a message."""
        mock_run.return_value = (
            '[{"name":"document.pdf","mime_type":"application/pdf","size":524288,"downloaded":true},'
            '{"name":"image.jpg","mime_type":"image/jpeg","size":102400,"downloaded":true}]'
        )

        result = connector.get_attachments("12345")

        assert len(result) == 2
        assert result[0]["name"] == "document.pdf"
        assert result[0]["mime_type"] == "application/pdf"
        assert result[0]["size"] == 524288
        assert result[0]["downloaded"] is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting attachments from message with none."""
        mock_run.return_value = "[]"

        result = connector.get_attachments("12345")

        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_handles_pipe_in_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"q1|q2.pdf","mime_type":"application/pdf","size":1000,"downloaded":true}]'
        )
        result = connector.get_attachments("12345")
        assert result[0]["name"] == "q1|q2.pdf"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_message_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test error when message doesn't exist."""
        mock_run.side_effect = MailMessageNotFoundError("Message not found")

        with pytest.raises(MailMessageNotFoundError):
            connector.get_attachments("99999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| so NSJSONSerialization preserves it."""
        mock_run.return_value = "[]"
        connector.get_attachments("12345")
        script = mock_run.call_args[0][0]
        assert "|name|:(name of att)" in script


class TestSaveAttachments:
    """Tests for saving attachments."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_single_attachment(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Test saving a single attachment."""
        mock_run.return_value = "1"

        result = connector.save_attachments(
            message_id="12345",
            save_directory=tmp_path,
            attachment_indices=[0]
        )

        assert result == 1
        call_args = mock_run.call_args[0][0]
        assert str(tmp_path) in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_all_attachments(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Test saving all attachments from a message."""
        mock_run.return_value = "3"

        result = connector.save_attachments(
            message_id="12345",
            save_directory=tmp_path
        )

        assert result == 3

    def test_save_to_invalid_directory(self, connector: AppleMailConnector) -> None:
        """Test error when save directory is invalid."""
        with pytest.raises((ValueError, FileNotFoundError)):
            connector.save_attachments(
                message_id="12345",
                save_directory=Path("/nonexistent/directory")
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_validates_path_traversal(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test that path traversal is prevented."""
        # Attempting path traversal should be blocked
        # Will fail with FileNotFoundError or ValueError depending on path
        with pytest.raises((ValueError, FileNotFoundError)):
            connector.save_attachments(
                message_id="12345",
                save_directory=Path("../../etc")
            )


class TestAttachmentSecurity:
    """Tests for attachment security features."""

    def test_validates_file_type_restrictions(self) -> None:
        """Test that dangerous file types are restricted."""
        from apple_mail_mcp.security import validate_attachment_type

        # Dangerous types should be rejected by default
        assert validate_attachment_type("malware.exe") is False
        assert validate_attachment_type("script.bat") is False
        assert validate_attachment_type("script.sh") is False
        assert validate_attachment_type("document.scr") is False

        # Safe types should be allowed
        assert validate_attachment_type("document.pdf") is True
        assert validate_attachment_type("image.jpg") is True
        assert validate_attachment_type("data.csv") is True

    def test_validates_file_size(self) -> None:
        """Test file size validation."""
        from apple_mail_mcp.security import validate_attachment_size

        # Within limit
        assert validate_attachment_size(1024 * 1024, max_size=10 * 1024 * 1024) is True

        # Exceeds limit
        assert validate_attachment_size(30 * 1024 * 1024, max_size=25 * 1024 * 1024) is False

    def test_sanitizes_filename(self) -> None:
        """Test filename sanitization."""
        from apple_mail_mcp.utils import sanitize_filename

        # Remove dangerous characters and path components
        # Path.name extracts just the filename, so "../../../etc/passwd" -> "passwd"
        assert sanitize_filename("../../../etc/passwd") == "passwd"
        assert sanitize_filename("file:name.txt") == "file_name.txt"
        assert sanitize_filename("file\x00name.txt") == "filename.txt"

        # Preserve safe names
        assert sanitize_filename("document.pdf") == "document.pdf"
        assert sanitize_filename("my-file_v2.txt") == "my-file_v2.txt"
