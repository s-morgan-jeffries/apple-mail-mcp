"""Unit tests for attachment functionality."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailMessageNotFoundError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector


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

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_script_quotes_size_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Guard against NSJSONSerialization silently dropping the 'size' key.

        AppleScript record key `size:` collides with NSSize/NSObject selectors
        and gets stripped during NSDictionary conversion. Must be `|size|:`.
        """
        mock_run.return_value = "[]"
        connector.get_attachments("msg-1")
        script = mock_run.call_args[0][0]
        assert "|size|:(file size of att)" in script
        assert ", size:(file size of att)" not in script


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
        # First call enumerates names; second call performs the save.
        mock_run.side_effect = [
            '[{"name":"document.pdf","mime_type":"application/pdf",'
            '"size":1,"downloaded":true}]',
            "1",
        ]

        result = connector.save_attachments(
            message_id="12345",
            save_directory=tmp_path,
            attachment_indices=[0]
        )

        assert result == 1
        call_args = mock_run.call_args_list[-1][0][0]
        assert str(tmp_path) in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_all_attachments(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        """Test saving all attachments from a message."""
        mock_run.side_effect = [
            '[{"name":"a.pdf","mime_type":"application/pdf","size":1,"downloaded":true},'
            '{"name":"b.pdf","mime_type":"application/pdf","size":2,"downloaded":true},'
            '{"name":"c.pdf","mime_type":"application/pdf","size":3,"downloaded":true}]',
            "3",
        ]

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


class TestSaveAttachmentsPathTraversal:
    """save_attachments must not let an attacker-controlled attachment
    filename (``name of att`` — set by whoever sent the email) escape the
    chosen save directory. Path traversal here is an arbitrary file write."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_compute_targets_sanitizes_traversal_name(self, tmp_path: Path) -> None:
        from apple_mail_mcp.mail_connector import _compute_attachment_save_targets

        names = ["../../../../tmp/evil.sh", "report.pdf"]
        targets = _compute_attachment_save_targets(names, tmp_path.resolve(), None)

        # Every target stays strictly within the save directory.
        for _, p in targets:
            assert p.resolve().is_relative_to(tmp_path.resolve())
            assert p.resolve() != tmp_path.resolve()
        # The traversal name is reduced to a safe basename.
        assert targets[0][1].name == "evil.sh"
        # AppleScript indices are 1-based and preserve order.
        assert [i for i, _ in targets] == [1, 2]

    def test_compute_targets_absolute_name_contained(self, tmp_path: Path) -> None:
        from apple_mail_mcp.mail_connector import _compute_attachment_save_targets

        targets = _compute_attachment_save_targets(
            ["/etc/cron.d/evil"], tmp_path.resolve(), None
        )
        assert len(targets) == 1
        assert targets[0][1].resolve().is_relative_to(tmp_path.resolve())
        assert targets[0][1].name == "evil"

    def test_compute_targets_dedupes_collisions(self, tmp_path: Path) -> None:
        from apple_mail_mcp.mail_connector import _compute_attachment_save_targets

        # Two names that sanitize to the same basename must not collapse to
        # one path (which would silently overwrite/lose an attachment).
        targets = _compute_attachment_save_targets(
            ["a/report.pdf", "b/report.pdf"], tmp_path.resolve(), None
        )
        paths = {str(p) for _, p in targets}
        assert len(paths) == 2

    def test_compute_targets_respects_indices(self, tmp_path: Path) -> None:
        from apple_mail_mcp.mail_connector import _compute_attachment_save_targets

        targets = _compute_attachment_save_targets(
            ["a.pdf", "b.pdf", "c.pdf"], tmp_path.resolve(), [0, 2]
        )
        assert [i for i, _ in targets] == [1, 3]
        assert [p.name for _, p in targets] == ["a.pdf", "c.pdf"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_does_not_concatenate_raw_name(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Path
    ) -> None:
        # First call enumerates attachments (returns a malicious name);
        # the second call saves to the precomputed, sanitized paths.
        mock_run.side_effect = [
            '[{"name":"../../../../tmp/evil.sh","mime_type":"x",'
            '"size":1,"downloaded":true}]',
            "1",
        ]
        result = connector.save_attachments(message_id="123", save_directory=tmp_path)

        assert result == 1
        save_script = mock_run.call_args_list[-1][0][0]
        # The vulnerable runtime path concatenation must be gone.
        assert "& attName" not in save_script
        assert "name of att" not in save_script
        # Saves target a Python-sanitized POSIX path under the chosen dir.
        assert "POSIX file" in save_script
        assert str(tmp_path) in save_script
        # No traversal sequence survives into the script.
        assert "/tmp/evil.sh" not in save_script
        assert ".." not in save_script
