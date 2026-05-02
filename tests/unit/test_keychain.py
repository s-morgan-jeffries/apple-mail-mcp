"""Tests for Keychain password retrieval."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)
from apple_mail_mcp.keychain import (
    SERVICE_NAME_PREFIX,
    delete_imap_password,
    get_imap_password,
    set_imap_password,
)


def _mock_security(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mock subprocess.CompletedProcess-like result."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestServiceNamePrefix:
    def test_prefix_matches_decision_doc(self):
        assert SERVICE_NAME_PREFIX == "apple-mail-mcp.imap."


class TestHappyPath:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_returns_password_stripped_of_trailing_newline(self, mock_run):
        mock_run.return_value = _mock_security(0, stdout="secret123\n")
        result = get_imap_password("iCloud", "user@icloud.com")
        assert result == "secret123"

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_invokes_security_with_correct_args(self, mock_run):
        mock_run.return_value = _mock_security(0, stdout="p\n")
        get_imap_password("iCloud", "user@icloud.com")
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            "apple-mail-mcp.imap.iCloud",
            "-a",
            "user@icloud.com",
        ]

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_preserves_internal_whitespace(self, mock_run):
        mock_run.return_value = _mock_security(0, stdout="with spaces\n")
        assert get_imap_password("iCloud", "u@i.com") == "with spaces"


class TestEntryNotFound:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_exit_44_raises_entry_not_found(self, mock_run):
        mock_run.return_value = _mock_security(
            44,
            stderr="security: SecKeychainSearchCopyNext: The specified item could "
            "not be found in the keychain.",
        )
        with pytest.raises(MailKeychainEntryNotFoundError):
            get_imap_password("iCloud", "u@i.com")


class TestAccessDenied:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_exit_128_raises_access_denied(self, mock_run):
        mock_run.return_value = _mock_security(
            128, stderr="User interaction is not allowed."
        )
        with pytest.raises(MailKeychainAccessDeniedError):
            get_imap_password("iCloud", "u@i.com")

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_interaction_not_allowed_code_raises_access_denied(self, mock_run):
        # errSecInteractionNotAllowed = -25308
        mock_run.return_value = _mock_security(
            1, stderr="security: SecKeychainItemCopyAccess: (-25308)"
        )
        with pytest.raises(MailKeychainAccessDeniedError):
            get_imap_password("iCloud", "u@i.com")


class TestOtherFailure:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_unknown_failure_raises_keychain_error(self, mock_run):
        mock_run.return_value = _mock_security(
            2, stderr="some other failure"
        )
        with pytest.raises(MailKeychainError) as exc_info:
            get_imap_password("iCloud", "u@i.com")
        # Must not be caught by more specific handlers.
        assert type(exc_info.value) is MailKeychainError
        assert "some other failure" in str(exc_info.value)

    @patch(
        "apple_mail_mcp.keychain.subprocess.run",
        side_effect=FileNotFoundError("security"),
    )
    def test_security_binary_missing_raises_keychain_error(self, mock_run):
        with pytest.raises(MailKeychainError):
            get_imap_password("iCloud", "u@i.com")


class TestSetImapPassword:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_writes_via_security_with_update_flag(self, mock_run):
        mock_run.return_value = _mock_security(0)
        set_imap_password("iCloud", "user@icloud.com", "appspecificpw")
        cmd = mock_run.call_args[0][0]
        # -U makes the command idempotent (overwrite existing entry).
        assert cmd == [
            "security",
            "add-generic-password",
            "-s",
            "apple-mail-mcp.imap.iCloud",
            "-a",
            "user@icloud.com",
            "-w",
            "appspecificpw",
            "-U",
        ]

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_zero_exit_returns_none(self, mock_run):
        mock_run.return_value = _mock_security(0)
        assert set_imap_password("iCloud", "u@i.com", "p") is None

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_access_denied_raises_access_denied(self, mock_run):
        mock_run.return_value = _mock_security(
            128, stderr="User interaction is not allowed."
        )
        with pytest.raises(MailKeychainAccessDeniedError):
            set_imap_password("iCloud", "u@i.com", "p")

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_other_failure_raises_keychain_error(self, mock_run):
        mock_run.return_value = _mock_security(
            2, stderr="duplicate entry without -U? unexpected"
        )
        with pytest.raises(MailKeychainError) as exc_info:
            set_imap_password("iCloud", "u@i.com", "p")
        assert type(exc_info.value) is MailKeychainError

    @patch(
        "apple_mail_mcp.keychain.subprocess.run",
        side_effect=FileNotFoundError("security"),
    )
    def test_security_binary_missing_raises_keychain_error(self, mock_run):
        with pytest.raises(MailKeychainError):
            set_imap_password("iCloud", "u@i.com", "p")


class TestDeleteImapPassword:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_invokes_security_delete_with_correct_args(self, mock_run):
        mock_run.return_value = _mock_security(0)
        delete_imap_password("iCloud", "user@icloud.com")
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "security",
            "delete-generic-password",
            "-s",
            "apple-mail-mcp.imap.iCloud",
            "-a",
            "user@icloud.com",
        ]

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_zero_exit_returns_none(self, mock_run):
        mock_run.return_value = _mock_security(0)
        assert delete_imap_password("iCloud", "u@i.com") is None

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_exit_44_raises_entry_not_found(self, mock_run):
        mock_run.return_value = _mock_security(
            44, stderr="The specified item could not be found in the keychain."
        )
        with pytest.raises(MailKeychainEntryNotFoundError):
            delete_imap_password("iCloud", "u@i.com")

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_access_denied_raises_access_denied(self, mock_run):
        mock_run.return_value = _mock_security(
            128, stderr="User interaction is not allowed."
        )
        with pytest.raises(MailKeychainAccessDeniedError):
            delete_imap_password("iCloud", "u@i.com")

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_other_failure_raises_keychain_error(self, mock_run):
        mock_run.return_value = _mock_security(2, stderr="other")
        with pytest.raises(MailKeychainError) as exc_info:
            delete_imap_password("iCloud", "u@i.com")
        assert type(exc_info.value) is MailKeychainError
