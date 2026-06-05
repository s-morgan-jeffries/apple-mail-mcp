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

        assert result["saved"] == 1
        assert result["rejected"] == []
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

        assert result["saved"] == 3
        assert result["rejected"] == []

    # --- Byte caps (#236) ---------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_per_attachment_cap_rejects_oversized(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """An attachment whose reported size exceeds the per-attachment cap is
        rejected before any save runs (only the enumerate AppleScript fires)."""
        connector = AppleMailConnector(timeout=30, max_attachment_bytes=10)
        mock_run.side_effect = [
            '[{"name":"huge.bin","mime_type":"application/octet-stream",'
            '"size":100,"downloaded":true}]',
        ]
        result = connector.save_attachments("12345", tmp_path)
        assert result["saved"] == 0
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["name"] == "huge.bin"
        assert result["rejected"][0]["reason"] == "per_attachment_cap"
        # No save script — enumerate only.
        assert mock_run.call_count == 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_aggregate_cap_rejects_when_total_exceeded(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        connector = AppleMailConnector(
            timeout=30, max_attachment_bytes=1000, max_total_attachment_bytes=150
        )
        mock_run.side_effect = [
            '[{"name":"a.bin","mime_type":"application/octet-stream",'
            '"size":100,"downloaded":true},'
            '{"name":"b.bin","mime_type":"application/octet-stream",'
            '"size":100,"downloaded":true}]',
            "1",
        ]
        result = connector.save_attachments("12345", tmp_path)
        assert result["saved"] == 1
        assert [r["reason"] for r in result["rejected"]] == ["aggregate_cap"]
        assert result["rejected"][0]["name"] == "b.bin"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_fail_open_on_unknown_size(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """A reported size of 0 is treated as under-cap and saved (the
        post-write net would still delete it if it turned out oversized)."""
        connector = AppleMailConnector(timeout=30, max_attachment_bytes=10)
        mock_run.side_effect = [
            '[{"name":"unknown.bin","mime_type":"application/octet-stream",'
            '"size":0,"downloaded":false}]',
            "1",
        ]
        result = connector.save_attachments("12345", tmp_path)
        assert result["saved"] == 1
        assert result["rejected"] == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_postwrite_net_deletes_oversized_written_file(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Even if the reported size passed the pre-check, a written file that
        exceeds the cap on disk is deleted and reported (covers Mail
        under-reporting size for a not-yet-downloaded attachment)."""
        connector = AppleMailConnector(timeout=30, max_attachment_bytes=10)
        # Reported size 5 passes the pre-check...
        mock_run.side_effect = [
            '[{"name":"big.bin","mime_type":"application/octet-stream",'
            '"size":5,"downloaded":false}]',
            "1",
        ]
        # ...but the actual written file is 100 bytes (> 10-byte cap).
        written = tmp_path / "big.bin"
        written.write_bytes(b"x" * 100)

        result = connector.save_attachments("12345", tmp_path)

        assert result["saved"] == 0
        assert not written.exists()  # post-write net deleted it
        assert [r["reason"] for r in result["rejected"]] == [
            "per_attachment_cap_postwrite"
        ]

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

        assert result["saved"] == 1
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


class TestGetAttachmentContent:
    """#250: read a single attachment's bytes inline (no caller-facing disk)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @staticmethod
    def _raw_with_attachments(tmp_path: Path) -> bytes:
        """Build a real RFC822 message with a text + a binary attachment."""
        from apple_mail_mcp.draft_builder import build_draft_mime

        txt = tmp_path / "notes.txt"
        txt.write_text("hello from a text attachment\n")
        binf = tmp_path / "blob.bin"
        binf.write_bytes(b"\x00\x01\x02\xff\xfe")
        _mid, raw = build_draft_mime(
            sender="me@example.invalid",
            to=["you@example.invalid"],
            subject="with attachments",
            body="see attached",
            attachments=[txt, binf],
        )
        return raw

    def _imap_patches(self, connector, mock_imap):
        from unittest.mock import patch
        return [
            patch("apple_mail_mcp.mail_connector.ImapConnector",
                  return_value=mock_imap),
            patch.object(AppleMailConnector, "_get_imap_password_with_fallback",
                         return_value="pw"),
            patch.object(AppleMailConnector, "_resolve_imap_config",
                         return_value=("h", 993, "me@example.invalid")),
        ]

    def test_imap_path_text_attachment(self, connector, tmp_path):
        import contextlib
        raw = self._raw_with_attachments(tmp_path)
        mock_imap = MagicMock()
        mock_imap.fetch_raw_message.return_value = raw
        with contextlib.ExitStack() as stack:
            for p in self._imap_patches(connector, mock_imap):
                stack.enter_context(p)
            result = connector.get_attachment_content(
                "<m@x>", 0, account="iCloud", mailbox="INBOX"
            )
        assert result["name"] == "notes.txt"
        assert result["mime_type"] == "text/plain"
        assert result["payload"] == b"hello from a text attachment\n"
        mock_imap.fetch_raw_message.assert_called_once_with("<m@x>", "INBOX")

    def test_imap_path_binary_attachment(self, connector, tmp_path):
        import contextlib
        raw = self._raw_with_attachments(tmp_path)
        mock_imap = MagicMock()
        mock_imap.fetch_raw_message.return_value = raw
        with contextlib.ExitStack() as stack:
            for p in self._imap_patches(connector, mock_imap):
                stack.enter_context(p)
            result = connector.get_attachment_content(
                "<m@x>", 1, account="iCloud", mailbox="INBOX"
            )
        assert result["name"] == "blob.bin"
        assert result["payload"] == b"\x00\x01\x02\xff\xfe"

    def test_imap_index_out_of_range_raises(self, connector, tmp_path):
        import contextlib

        from apple_mail_mcp.exceptions import MailAttachmentIndexError
        raw = self._raw_with_attachments(tmp_path)
        mock_imap = MagicMock()
        mock_imap.fetch_raw_message.return_value = raw
        with contextlib.ExitStack() as stack:
            for p in self._imap_patches(connector, mock_imap):
                stack.enter_context(p)
            with pytest.raises(MailAttachmentIndexError):
                connector.get_attachment_content(
                    "<m@x>", 9, account="iCloud", mailbox="INBOX"
                )

    def test_imap_oversize_raises_before_returning(self, tmp_path):
        import contextlib

        from apple_mail_mcp.exceptions import MailAttachmentTooLargeError
        connector = AppleMailConnector(timeout=30, max_inline_attachment_bytes=4)
        raw = self._raw_with_attachments(tmp_path)
        mock_imap = MagicMock()
        mock_imap.fetch_raw_message.return_value = raw
        with contextlib.ExitStack() as stack:
            for p in self._imap_patches(connector, mock_imap):
                stack.enter_context(p)
            with pytest.raises(MailAttachmentTooLargeError):
                connector.get_attachment_content(
                    "<m@x>", 0, account="iCloud", mailbox="INBOX"
                )

    @patch.object(AppleMailConnector, "_get_attachments_applescript")
    def test_applescript_path_reads_saved_bytes(
        self, mock_meta, connector, monkeypatch
    ):
        mock_meta.return_value = [
            {"name": "notes.txt", "mime_type": "text/plain",
             "size": 12, "downloaded": True},
        ]

        def fake_save(message_id, one_based_index, dest_path):
            assert one_based_index == 1
            Path(dest_path).write_bytes(b"on-disk text")
            return True

        monkeypatch.setattr(
            connector, "_save_one_attachment_applescript", fake_save
        )
        result = connector.get_attachment_content("12345", 0)
        assert result["name"] == "notes.txt"
        assert result["mime_type"] == "text/plain"
        assert result["payload"] == b"on-disk text"

    @patch.object(AppleMailConnector, "_get_attachments_applescript")
    def test_applescript_oversize_rejected_without_save(
        self, mock_meta, monkeypatch
    ):
        from apple_mail_mcp.exceptions import MailAttachmentTooLargeError
        connector = AppleMailConnector(timeout=30, max_inline_attachment_bytes=10)
        mock_meta.return_value = [
            {"name": "big.bin", "mime_type": "application/octet-stream",
             "size": 100, "downloaded": True},
        ]
        save_calls = []
        monkeypatch.setattr(
            connector, "_save_one_attachment_applescript",
            lambda *a, **k: save_calls.append(1),
        )
        with pytest.raises(MailAttachmentTooLargeError):
            connector.get_attachment_content("12345", 0)
        assert save_calls == [], "must not save when over the inline cap"

    @patch.object(AppleMailConnector, "_get_attachments_applescript")
    def test_applescript_index_out_of_range_raises(self, mock_meta, connector):
        from apple_mail_mcp.exceptions import MailAttachmentIndexError
        mock_meta.return_value = [
            {"name": "only.txt", "mime_type": "text/plain",
             "size": 3, "downloaded": True},
        ]
        with pytest.raises(MailAttachmentIndexError):
            connector.get_attachment_content("12345", 5)
