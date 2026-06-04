"""Tests for Keychain password retrieval."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)
from apple_mail_mcp.keychain import (
    IMAP_PASSWORD_ENV_PREFIX,
    SERVICE_NAME_PREFIX,
    _env_var_name,
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


@pytest.fixture(autouse=True)
def _clear_imap_password_env(monkeypatch):
    """Keep the env-var fallback (#248) from leaking into the Keychain-path
    tests: strip any APPLE_MAIL_MCP_IMAP_PASSWORD_* set in the runner's
    shell. Individual env-fallback tests set their own with monkeypatch."""
    import os

    for key in list(os.environ):
        if key.startswith(IMAP_PASSWORD_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


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


class TestEnvVarName:
    """#248: account name -> APPLE_MAIL_MCP_IMAP_PASSWORD_<SUFFIX>."""

    def test_prefix(self):
        assert IMAP_PASSWORD_ENV_PREFIX == "APPLE_MAIL_MCP_IMAP_PASSWORD_"

    @pytest.mark.parametrize(
        "account, expected",
        [
            ("iCloud", "APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD"),
            ("Gmail", "APPLE_MAIL_MCP_IMAP_PASSWORD_GMAIL"),
            ("MobileMe", "APPLE_MAIL_MCP_IMAP_PASSWORD_MOBILEME"),
            ("Yahoo!", "APPLE_MAIL_MCP_IMAP_PASSWORD_YAHOO"),
            ("My Gmail", "APPLE_MAIL_MCP_IMAP_PASSWORD_MY_GMAIL"),
            ("iCloud (Work)", "APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD_WORK"),
            ("a.b-c__d", "APPLE_MAIL_MCP_IMAP_PASSWORD_A_B_C_D"),
            ("  spaced  ", "APPLE_MAIL_MCP_IMAP_PASSWORD_SPACED"),
        ],
    )
    def test_normalization_round_trips(self, account, expected):
        assert _env_var_name(account) == expected

    @pytest.mark.parametrize("account", ["日本語", "!!!", "   ", ""])
    def test_empty_suffix_returns_none(self, account):
        # No ASCII alphanumerics -> no usable env var name; caller skips
        # the env path and uses Keychain.
        assert _env_var_name(account) is None


class TestEnvVarFallback:
    """#248: env-var password fallback for uvx / headless / CI."""

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_env_var_present_returns_value_without_shellout(
        self, mock_run, monkeypatch
    ):
        monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD", "envpw")
        assert get_imap_password("iCloud", "u@icloud.com") == "envpw"
        mock_run.assert_not_called()

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_env_var_takes_precedence_over_keychain(self, mock_run, monkeypatch):
        # Even when Keychain would succeed, a present env var wins.
        mock_run.return_value = _mock_security(0, stdout="keychainpw\n")
        monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD", "envpw")
        assert get_imap_password("iCloud", "u@icloud.com") == "envpw"
        mock_run.assert_not_called()

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_empty_env_var_falls_through_to_keychain(self, mock_run, monkeypatch):
        mock_run.return_value = _mock_security(0, stdout="keychainpw\n")
        monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD", "")
        assert get_imap_password("iCloud", "u@icloud.com") == "keychainpw"
        mock_run.assert_called_once()

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_whitespace_only_env_var_falls_through_to_keychain(
        self, mock_run, monkeypatch
    ):
        mock_run.return_value = _mock_security(0, stdout="keychainpw\n")
        monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD", "   ")
        assert get_imap_password("iCloud", "u@icloud.com") == "keychainpw"
        mock_run.assert_called_once()

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_absent_env_var_uses_keychain(self, mock_run, monkeypatch):
        monkeypatch.delenv(
            "APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD", raising=False
        )
        mock_run.return_value = _mock_security(0, stdout="keychainpw\n")
        assert get_imap_password("iCloud", "u@icloud.com") == "keychainpw"
        mock_run.assert_called_once()

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_env_var_preserves_internal_whitespace(self, mock_run, monkeypatch):
        # A non-empty value with internal spaces is returned verbatim (the
        # value isn't a name we normalize — it's the password).
        monkeypatch.setenv(
            "APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD", "pw with spaces"
        )
        assert get_imap_password("iCloud", "u@i.com") == "pw with spaces"
        mock_run.assert_not_called()
