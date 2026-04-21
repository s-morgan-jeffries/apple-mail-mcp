"""
Integration tests for Apple Mail MCP.

These tests require:
1. Apple Mail.app installed and running
2. At least one configured mail account
3. Permission granted for automation
4. Environment variables for safety gate (when running tools via server.py):
   - MAIL_TEST_MODE=true
   - MAIL_TEST_ACCOUNT=<test account name>

Run with: MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=TestAccount pytest --run-integration
"""

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

# Skip all integration tests by default
# Run with: pytest --run-integration
pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default. Use --run-integration to run."
)


@pytest.fixture
def connector() -> AppleMailConnector:
    """Create a real connector instance."""
    return AppleMailConnector()


@pytest.fixture
def test_account() -> str:
    """
    Return the test account name from MAIL_TEST_ACCOUNT env var.

    This matches the account name the server.py safety gate verifies.
    """
    import os
    return os.getenv("MAIL_TEST_ACCOUNT", "Gmail")


class TestMailIntegration:
    """Integration tests with real Apple Mail."""

    def test_list_mailboxes(self, connector: AppleMailConnector, test_account: str) -> None:
        """Test listing mailboxes from real account."""
        result = connector.list_mailboxes(test_account)
        assert isinstance(result, list)
        # Should have at least INBOX
        assert len(result) > 0

    def test_search_messages(self, connector: AppleMailConnector, test_account: str) -> None:
        """Test searching messages in real mailbox."""
        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            limit=5
        )
        assert isinstance(result, list)
        # Mailbox might be empty, so just check type

    def test_search_unread_messages(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Test searching for unread messages."""
        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            read_status=False,
            limit=10
        )
        assert isinstance(result, list)

        # Verify all returned messages are unread
        for msg in result:
            assert msg["read_status"] is False

    def test_search_flagged_messages(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """New in #28: is_flagged pushes a flagged-status whose clause."""
        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            is_flagged=True,
            limit=5,
        )
        assert isinstance(result, list)
        for msg in result:
            assert msg["flagged"] is True

    def test_search_with_date_range(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """New in #28: date_from + date_to stack in the whose clause.

        Uses a wide range so the query is guaranteed to return something on
        any realistic test mailbox with recent activity.
        """
        from datetime import date, timedelta

        today = date.today()
        range_start = (today - timedelta(days=365)).isoformat()
        range_end = today.isoformat()

        result = connector.search_messages(
            account=test_account,
            mailbox="INBOX",
            date_from=range_start,
            date_to=range_end,
            limit=5,
        )
        assert isinstance(result, list)
        # Non-empty only validates that a stacked date whose clause survives
        # round-trip to Mail. Empty inbox or no recent messages is a valid pass.

    def test_search_rejects_malformed_date(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Malformed date raises ValueError before any AppleScript runs."""
        with pytest.raises(ValueError):
            connector.search_messages(
                account=test_account,
                mailbox="INBOX",
                date_from="not-a-date",
            )

    def test_list_accounts(self, connector: AppleMailConnector) -> None:
        """Real list_accounts returns structured account records.

        Guards against the pre-0.4.0 `[{"raw": str}]` placeholder shape and
        the NSJSONSerialization `|name|` selector-collision bug fixed in #23.
        Exercises the v0.5.0 fields added in #26: id, account_type, enabled.
        """
        result = connector.list_accounts()
        assert isinstance(result, list)
        assert len(result) >= 1
        for acct in result:
            assert set(acct.keys()) >= {
                "id", "name", "email_addresses", "account_type", "enabled",
            }
            assert isinstance(acct["id"], str) and acct["id"]
            assert isinstance(acct["name"], str) and acct["name"]
            assert isinstance(acct["email_addresses"], list)
            assert isinstance(acct["account_type"], str) and acct["account_type"]
            assert isinstance(acct["enabled"], bool)
            # No "raw" key left over from the old placeholder
            assert "raw" not in acct

    def test_list_rules(self, connector: AppleMailConnector) -> None:
        """Real list_rules returns structured rule records.

        Rules list may be empty for a user who has never configured any. Empty
        is a valid pass. Non-empty entries must have name + enabled with the
        right types.
        """
        result = connector.list_rules()
        assert isinstance(result, list)
        for rule in result:
            assert set(rule.keys()) >= {"name", "enabled"}
            assert isinstance(rule["name"], str) and rule["name"]
            assert isinstance(rule["enabled"], bool)

    def test_get_message(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Real get_message returns a full structured message.

        Chains off search_messages for a real ID. Guards against the
        NSJSONSerialization `|id|` selector-collision bug fixed in #23.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        target_id = matches[0]["id"]
        result = connector.get_message(target_id)

        assert set(result.keys()) >= {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged", "content",
        }
        assert result["id"] == target_id
        assert isinstance(result["subject"], str)
        assert isinstance(result["sender"], str)
        assert isinstance(result["date_received"], str)
        assert isinstance(result["read_status"], bool)
        assert isinstance(result["flagged"], bool)
        assert isinstance(result["content"], str)

    def test_get_attachments(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Real get_attachments returns a structured list (possibly empty).

        Chains off search_messages for a real ID. Guards against the
        NSJSONSerialization `|size|` selector-collision bug fixed in #23.
        Empty list is a valid pass — most messages have no attachments.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        result = connector.get_attachments(matches[0]["id"])
        assert isinstance(result, list)
        for att in result:
            assert set(att.keys()) >= {"name", "mime_type", "size", "downloaded"}
            assert isinstance(att["name"], str)
            assert isinstance(att["mime_type"], str)
            assert isinstance(att["size"], int)
            assert isinstance(att["downloaded"], bool)


class TestMailSendIntegration:
    """
    Integration tests for sending emails.

    WARNING: These tests will send real emails!
    Only run if you have a test account configured.
    """

    @pytest.mark.skip(reason="Sends real email - enable manually")
    def test_send_email(self, connector: AppleMailConnector) -> None:
        """
        Test sending a real email.

        MANUALLY ENABLE THIS TEST and update recipient!
        """
        result = connector.send_email(
            subject="Test Email from Apple Mail MCP",
            body="This is a test email sent via the MCP integration test suite.",
            to=["YOUR_TEST_EMAIL@example.com"]  # UPDATE THIS!
        )
        assert result is True


class TestErrorHandling:
    """Test error handling with real Mail.app."""

    def test_nonexistent_account(self, connector: AppleMailConnector) -> None:
        """Test error when account doesn't exist."""
        from apple_mail_mcp.exceptions import MailAccountNotFoundError

        with pytest.raises(MailAccountNotFoundError):
            connector.list_mailboxes("NonExistentAccount12345")

    def test_nonexistent_mailbox(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Test error when mailbox doesn't exist."""
        from apple_mail_mcp.exceptions import MailMailboxNotFoundError

        with pytest.raises(MailMailboxNotFoundError):
            connector.search_messages(
                account=test_account,
                mailbox="NonExistentMailbox12345"
            )
