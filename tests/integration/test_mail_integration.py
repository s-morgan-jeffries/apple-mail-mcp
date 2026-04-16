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
