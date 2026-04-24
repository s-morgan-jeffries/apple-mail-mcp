"""Unit tests for mail connector."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script


class TestAppleMailConnector:
    """Tests for AppleMailConnector."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch("subprocess.run")
    def test_run_applescript_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test successful AppleScript execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="result",
            stderr=""
        )

        result = connector._run_applescript("test script")
        assert result == "result"

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["/usr/bin/osascript", "-"]

    @patch("subprocess.run")
    def test_run_applescript_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test account not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get account \"NonExistent\""
        )

        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test mailbox not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get mailbox \"NonExistent\""
        )

        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_timeout(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test timeout handling."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)

        with pytest.raises(MailAppleScriptError, match="timeout"):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_curly_apostrophe_still_maps_to_typed_error(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Real macOS stderr uses curly apostrophes — must still dispatch typed errors.

        Regression guard for a bug where `Can\u2019t get account "X"` (curly
        apostrophe, as emitted by Mail.app) bypassed the typed-exception
        mapping and surfaced as a generic MailAppleScriptError, defeating the
        server-layer not-found routing.
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get account \"NonExistent\"",
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get mailbox \"NonExistent\"",
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true},'
            '{"id":"UUID-2","name":"Work","email_addresses":["me@work.com","alt@work.com"],'
            '"account_type":"iCloud","enabled":false}]'
        )
        result = connector.list_accounts()
        assert result == [
            {"id": "UUID-1", "name": "Gmail",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            {"id": "UUID-2", "name": "Work",
             "email_addresses": ["me@work.com", "alt@work.com"],
             "account_type": "iCloud", "enabled": False},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_accounts()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_handles_empty_email_addresses(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """An account with no email addresses must return email_addresses as []."""
        mock_run.return_value = (
            '[{"id":"UUID-3","name":"LocalOnly","email_addresses":[],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result == [{
            "id": "UUID-3", "name": "LocalOnly", "email_addresses": [],
            "account_type": "imap", "enabled": True,
        }]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_includes_type_and_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Generated AppleScript must extract account_type (as text) and enabled."""
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|account_type|:((account type of acc) as text)" in script
        assert "|enabled|:(enabled of acc)" in script
        assert "|id|:(id of acc as text)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"News From Apple","enabled":false},'
            '{"name":"Junk filter","enabled":true}]'
        )
        result = connector.list_rules()
        assert result == [
            {"name": "News From Apple", "enabled": False},
            {"name": "Junk filter", "enabled": True},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_rules()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_allows_duplicate_names(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail allows multiple rules with the same name — connector returns both."""
        mock_run.return_value = (
            '[{"name":"Send to OmniFocus","enabled":false},'
            '{"name":"Send to OmniFocus","enabled":true}]'
        )
        result = connector.list_rules()
        assert len(result) == 2
        assert result[0]["name"] == result[1]["name"]
        assert result[0]["enabled"] != result[1]["enabled"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_script_quotes_keys(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Record keys must be |quoted| per the v0.4.1 selector-collision rule."""
        mock_run.return_value = "[]"
        connector.list_rules()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of r)" in script
        assert "|enabled|:(enabled of r)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| (quoted) so NSJSONSerialization keeps it.

        Unquoted `name:` in the record literal causes the key to be silently
        dropped during ASObjC -> NSDictionary conversion because `name` collides
        with NSObject's `name` property. Regression guard for real Mail.app bug.
        """
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of acc)" in script
        assert "{name:(name of acc)" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"INBOX","unread_count":5},'
            '{"name":"Sent","unread_count":0},'
            '{"name":"Projects/Client A","unread_count":3}]'
        )
        result = connector.list_mailboxes("Gmail")
        assert result == [
            {"name": "INBOX", "unread_count": 5},
            {"name": "Sent", "unread_count": 0},
            {"name": "Projects/Client A", "unread_count": 3},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAccountNotFoundError("Can't get account \"NoSuch\".")
        with pytest.raises(MailAccountNotFoundError):
            connector.list_mailboxes("NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| so NSJSONSerialization preserves it."""
        mock_run.return_value = "[]"
        connector.list_mailboxes("Gmail")
        script = mock_run.call_args[0][0]
        assert "|name|:(name of mb)" in script

    # --- _resolve_imap_config --------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_returns_tuple(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"host":"imap.mail.me.com",'
            '"port":993,'
            '"email":"s_morgan_jeffries@yahoo.com"}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("imap.mail.me.com", 993, "s_morgan_jeffries@yahoo.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAccountNotFoundError(
            "Can't get account \"NoSuch\"."
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_imap_config("NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_script_has_quoted_keys(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """NSJSONSerialization requires |key| form for record keys."""
        mock_run.return_value = (
            '{"host":"h","port":993,"email":"e@e.com"}'
        )
        connector._resolve_imap_config("iCloud")
        script = mock_run.call_args[0][0]
        assert "|host|:(server name of acctRef)" in script
        assert "|port|:(port of acctRef)" in script
        assert "|email|:(user name of acctRef)" in script
        # Must assign to resultData for _wrap_as_json_script to serialize.
        assert "set resultData to" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_escapes_account_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"host":"h","port":993,"email":"e@e.com"}'
        )
        connector._resolve_imap_config('Weird "Name" Acct')
        script = mock_run.call_args[0][0]
        # The quote must be escaped; raw quotes would break the script.
        assert 'Weird \\"Name\\" Acct' in script

    # --- _imap_failures state + _log_imap_fallback -----------------------

    def test_imap_failures_starts_empty(
        self, connector: AppleMailConnector
    ) -> None:
        assert connector._imap_failures == set()

    def test_log_imap_fallback_keychain_entry_not_found_is_silent(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing Keychain entry is a benign opt-out signal — DEBUG only."""
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback(
                "iCloud", MailKeychainEntryNotFoundError("missing")
            )
        # Not in the failures set — benign signals don't count as failures.
        assert "iCloud" not in connector._imap_failures
        # Should log at DEBUG, never WARNING.
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_records) == 1
        assert "iCloud" in debug_records[0].getMessage()

    def test_log_imap_fallback_first_failure_logs_warning(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("iCloud", OSError("network down"))
        assert "iCloud" in connector._imap_failures
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "iCloud" in msg
        assert "OSError" in msg

    def test_log_imap_fallback_subsequent_failure_same_account_is_debug(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Seed: first failure.
        connector._log_imap_fallback("iCloud", OSError("first"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("iCloud", OSError("second"))
        # Set unchanged (already contains iCloud).
        assert connector._imap_failures == {"iCloud"}
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_records == []
        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_records) == 1

    def test_log_imap_fallback_failure_new_account_logs_warning(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        connector._log_imap_fallback("iCloud", OSError("iCloud first"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("Gmail", OSError("Gmail first"))
        assert connector._imap_failures == {"iCloud", "Gmail"}
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "Gmail" in warning_records[0].getMessage()

    def test_log_imap_fallback_access_denied_counts_as_failure(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Access denied is a misconfiguration worth surfacing, unlike missing entry."""
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback(
                "iCloud", MailKeychainAccessDeniedError("ACL refused")
            )
        assert "iCloud" in connector._imap_failures
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1

    # --- _imap_search helper ---------------------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.return_value = [{"id": "1", "subject": "S"}]

        result = connector._imap_search("iCloud", "INBOX", limit=5)

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password"
        )
        # Parameters forwarded 1:1 to the IMAP connector (minus `account`).
        mock_imap.search_messages.assert_called_once_with(
            mailbox="INBOX",
            sender_contains=None,
            subject_contains=None,
            read_status=None,
            is_flagged=None,
            date_from=None,
            date_to=None,
            has_attachment=None,
            limit=5,
        )
        assert result == [{"id": "1", "subject": "S"}]

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_search("iCloud", "INBOX")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_search("iCloud", "INBOX")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_search("iCloud", "INBOX")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_basic(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test basic message search."""
        mock_run.return_value = (
            '[{"id":"12345","subject":"Test Subject",'
            '"sender":"sender@example.com","date_received":"Mon Jan 1 2024",'
            '"read_status":false}]'
        )

        result = connector.search_messages("Gmail", "INBOX")

        assert len(result) == 1
        assert result[0]["id"] == "12345"
        assert result[0]["subject"] == "Test Subject"
        assert result[0]["sender"] == "sender@example.com"
        assert result[0]["read_status"] is False

    # Note: validates the Python-side JSON parse. Real end-to-end correctness
    # (AppleScript actually emitting valid JSON when the data contains '|')
    # is proven by integration tests.
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_handles_pipe_in_subject(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Subject containing '|' must not break parsing (the bug this refactor fixes)."""
        mock_run.return_value = (
            '[{"id":"abc","subject":"Q3 Report | Draft",'
            '"sender":"boss@example.com","date_received":"Wed Feb 5 2025",'
            '"read_status":true}]'
        )
        result = connector.search_messages("Gmail", "INBOX")
        assert len(result) == 1
        assert result[0]["subject"] == "Q3 Report | Draft"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """If _run_applescript raises MailAccountNotFoundError, search_messages must not swallow it.

        Regression guard: a previous version wrapped the tell-block in try/on error,
        which downgraded MailAccountNotFoundError to MailAppleScriptError.
        """
        mock_run.side_effect = MailAccountNotFoundError("Can't get account \"NoSuch\".")
        with pytest.raises(MailAccountNotFoundError):
            connector.search_messages("NoSuch", "INBOX")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Similar regression guard for MailMailboxNotFoundError."""
        mock_run.side_effect = MailMailboxNotFoundError("Can't get mailbox \"NoSuch\".")
        with pytest.raises(MailMailboxNotFoundError):
            connector.search_messages("Gmail", "NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_with_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test message search with filters."""
        mock_run.return_value = "[]"

        connector.search_messages(
            "Gmail",
            "INBOX",
            sender_contains="john@example.com",
            subject_contains="meeting",
            read_status=False,
            limit=10
        )

        # Verify the script includes filter conditions
        call_args = mock_run.call_args[0][0]
        assert 'sender contains "john@example.com"' in call_args
        assert 'subject contains "meeting"' in call_args
        assert "read status is false" in call_args
        # Limit is enforced by accumulating matches and exiting the repeat
        # when count of resultData reaches the bound. `items 1 thru N of`
        # is avoided — Mail rejects it on live message collection references.
        assert "if (count of resultData) >= 10 then exit repeat" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_without_filters_omits_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """AppleScript rejects `whose true` — no-filter searches must drop `whose`.

        Regression guard for a bug where `search_messages("X", "INBOX")` with no
        filters emitted `messages of mailboxRef whose true`, which Mail.app
        rejects with `Illegal comparison or logical (-1726)`.
        """
        mock_run.return_value = "[]"
        connector.search_messages("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "whose true" not in script
        # With NO filters, the generated source must reference `mailboxRef`
        # without a `whose` clause.
        assert "messages of mailboxRef\n" in script or "messages of mailboxRef " in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_does_not_slice_message_reference(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail rejects `items 1 thru N of (messages ...)` with error -1728.

        The limit must be enforced via `count of` + indexed `item i of`, not
        by slicing the live message collection reference.
        """
        mock_run.return_value = "[]"
        connector.search_messages("Gmail", "INBOX", limit=5)
        script = mock_run.call_args[0][0]
        assert "items 1 thru" not in script
        assert "if (count of resultData) >= 5 then exit repeat" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_is_flagged_in_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector.search_messages("Gmail", "INBOX", is_flagged=True)
        script = mock_run.call_args[0][0]
        assert "flagged status is true" in script

        connector.search_messages("Gmail", "INBOX", is_flagged=False)
        script = mock_run.call_args[0][0]
        assert "flagged status is false" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_date_range_in_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector.search_messages(
            "Gmail", "INBOX", date_from="2026-04-01", date_to="2026-04-15"
        )
        script = mock_run.call_args[0][0]
        assert 'date received >= (date "2026-04-01")' in script
        # date_to gets +1 day so the full day is inclusive
        assert 'date received < (date "2026-04-16")' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_rejects_malformed_date_from(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Malformed dates must raise ValueError, not be sent to AppleScript.

        Prevents AppleScript injection via unescaped date strings.
        """
        with pytest.raises(ValueError, match="date_from"):
            connector.search_messages(
                "Gmail", "INBOX",
                date_from='2024-01-01", delete mailbox',
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_rejects_malformed_date_to(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="date_to"):
            connector.search_messages("Gmail", "INBOX", date_to="not-a-date")
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_has_attachment_true_post_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """has_attachment=True can't go in whose — applied inside the loop."""
        mock_run.return_value = "[]"
        # Combine with a read_status filter so the whose clause exists and the
        # "count attachments not in whose" assertion is meaningful.
        connector.search_messages(
            "Gmail", "INBOX", read_status=True, has_attachment=True
        )
        script = mock_run.call_args[0][0]
        # The attachment check MUST NOT appear in the whose clause line.
        whose_line = [
            ln for ln in script.splitlines() if "whose" in ln and "messages of" in ln
        ][0]
        assert "mail attachments" not in whose_line
        # But it MUST appear as a post-filter inside the loop.
        assert (
            "if (count of mail attachments of msg) = 0 then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_has_attachment_false_post_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector.search_messages("Gmail", "INBOX", has_attachment=False)
        script = mock_run.call_args[0][0]
        assert (
            "if (count of mail attachments of msg) > 0 then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_no_attachment_filter_has_no_check(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """When has_attachment is None, no attachment post-filter code appears."""
        mock_run.return_value = "[]"
        connector.search_messages("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "mail attachments of msg" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_result_includes_flagged(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """New in #28: result rows include the flagged status."""
        mock_run.return_value = (
            '[{"id":"1","subject":"s","sender":"a@b.c",'
            '"date_received":"Mon","read_status":false,"flagged":true}]'
        )
        result = connector.search_messages("Gmail", "INBOX")
        assert result[0]["flagged"] is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_script_quotes_id_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Guard against NSJSONSerialization silently dropping the 'id' key.

        AppleScript record key `id:` collides with NSObject's id selector and
        gets stripped during NSDictionary conversion. Must be quoted as `|id|:`.
        """
        mock_run.return_value = "[]"
        connector.search_messages("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "|id|:(id of msg as text)" in script
        # The bare form must not appear in the msgRecord literal — it would collide.
        assert ", id:(id of msg" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting a message."""
        mock_run.return_value = (
            '{"id":"12345","subject":"Subject","sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false,'
            '"content":"Message body"}'
        )

        result = connector.get_message("12345", include_content=True)

        assert result["id"] == "12345"
        assert result["subject"] == "Subject"
        assert result["content"] == "Message body"
        assert result["read_status"] is True
        assert result["flagged"] is False

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_handles_pipe_in_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Body containing '|' must not break parsing."""
        mock_run.return_value = (
            '{"id":"99","subject":"x","sender":"a@b.com",'
            '"date_received":"Mon Jan 1 2024","read_status":false,"flagged":false,'
            '"content":"col1|col2|col3"}'
        )
        result = connector.get_message("99", include_content=True)
        assert result["content"] == "col1|col2|col3"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_script_quotes_id_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Same guard as test_search_messages_script_quotes_id_key, for get_message."""
        mock_run.return_value = '{"id":"x","subject":"","sender":"","date_received":"","read_status":false,"flagged":false,"content":""}'
        connector.get_message("x")
        script = mock_run.call_args[0][0]
        assert "|id|:(id of msg as text)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_email_basic(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test sending a basic email."""
        mock_run.return_value = "sent"

        result = connector.send_email(
            subject="Test",
            body="Test body",
            to=["recipient@example.com"]
        )

        assert result is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_email_with_cc_bcc(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test sending email with CC and BCC."""
        mock_run.return_value = "sent"

        result = connector.send_email(
            subject="Test",
            body="Test body",
            to=["recipient@example.com"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"]
        )

        assert result is True

        # Verify script includes recipients
        call_args = mock_run.call_args[0][0]
        assert "recipient@example.com" in call_args
        assert "cc@example.com" in call_args
        assert "bcc@example.com" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_read(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test marking messages as read."""
        mock_run.return_value = "2"

        result = connector.mark_as_read(["12345", "12346"], read=True)

        assert result == 2

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_unread(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test marking messages as unread."""
        mock_run.return_value = "1"

        result = connector.mark_as_read(["12345"], read=False)

        assert result == 1

        # Verify script sets read status to false
        call_args = mock_run.call_args[0][0]
        assert "set read status of msg to false" in call_args

    def test_mark_as_read_empty_list(self, connector: AppleMailConnector) -> None:
        """Test marking with empty list."""
        result = connector.mark_as_read([])
        assert result == 0

    # ---- get_thread ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_resolution_script_shape(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor-resolution AppleScript must query by internal id and quote keys."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            "[]",
        ]
        connector.get_thread("12345")
        anchor_script = mock_run.call_args_list[0][0][0]
        # All record keys must be |quoted| per the v0.4.1 selector-collision rule.
        assert "|rfc_message_id|:(message id of msg)" in anchor_script
        assert "|subject|:(subject of msg)" in anchor_script
        # Anchor lookup iterates by internal id.
        assert "whose id is 12345" in anchor_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor lookup failure propagates MailMessageNotFoundError."""
        mock_run.side_effect = MailMessageNotFoundError("Can't get message")
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("99999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_returns_anchor_plus_replies_sorted(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor + 2 replies in candidates → all 3 sorted by date_received."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Re: Q3","in_reply_to":"","references_raw":""}',
            '['
            '{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false},'
            '{"id":"101","rfc_message_id":"<r1@x>","in_reply_to":"<anchor@x>",'
            '"references_raw":"<anchor@x>","subject":"Re: Q3","sender":"b@x",'
            '"date_received":"Tue Jan 2 2024","read_status":true,"flagged":false},'
            '{"id":"102","rfc_message_id":"<r2@x>","in_reply_to":"<r1@x>",'
            '"references_raw":"<anchor@x> <r1@x>","subject":"Re: Q3","sender":"a@x",'
            '"date_received":"Wed Jan 3 2024","read_status":false,"flagged":false}'
            ']'
        ]
        result = connector.get_thread("100")
        assert len(result) == 3
        assert [m["id"] for m in result] == ["100", "101", "102"]
        # Response rows match search_messages shape (6 fields).
        for m in result:
            assert set(m.keys()) == {
                "id", "subject", "sender", "date_received", "read_status", "flagged",
            }

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_drops_threading_internals_from_output(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Response rows must NOT leak rfc_message_id / in_reply_to / references_raw."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Q3","in_reply_to":"","references_raw":""}',
            '[{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector.get_thread("100")
        for m in result:
            assert "rfc_message_id" not in m
            assert "in_reply_to" not in m
            assert "references_raw" not in m
            assert "references_parsed" not in m

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_orphan_anchor_returns_single_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor with no threading headers → thread = [anchor] only."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<orphan@x>","subject":"Standalone",'
            '"in_reply_to":"","references_raw":""}',
            '[{"id":"500","rfc_message_id":"<orphan@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Standalone","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector.get_thread("500")
        assert len(result) == 1
        assert result[0]["id"] == "500"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_candidate_script_uses_base_subject_and_account(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Candidate script must use normalized subject and scope to anchor's account."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<a@x>",'
            '"subject":"Re: Re: Q3 Report","in_reply_to":"","references_raw":""}',
            '[]',
        ]
        connector.get_thread("1")
        candidate_script = mock_run.call_args_list[1][0][0]
        assert 'account "Gmail"' in candidate_script
        # Base subject strips all Re: prefixes.
        assert 'subject contains "Q3 Report"' in candidate_script
        assert 'subject contains "Re:' not in candidate_script


class TestWrapAsJsonScript:
    def test_wrapper_contains_framework_directive(self) -> None:
        script = _wrap_as_json_script('tell application "Mail"\n    set resultData to {}\nend tell')
        assert 'use framework "Foundation"' in script
        assert "use scripting additions" in script

    def test_wrapper_appends_json_serialization(self) -> None:
        script = _wrap_as_json_script('tell application "Mail"\n    set resultData to {}\nend tell')
        assert "NSJSONSerialization" in script
        assert "dataWithJSONObject:resultData" in script

    def test_wrapper_preserves_body(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body)
        assert body in script

    def test_wrapper_orders_framework_before_body_before_epilogue(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body)
        framework_idx = script.index('use framework "Foundation"')
        body_idx = script.index(body)
        epilogue_idx = script.index("NSJSONSerialization")
        assert framework_idx < body_idx < epilogue_idx
