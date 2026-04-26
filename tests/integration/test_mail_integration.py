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

from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

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

    def test_list_mailboxes_by_uuid(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """#61: account-gated tools also accept the account UUID.

        Discovers the test account's UUID at runtime via list_accounts,
        then calls list_mailboxes with the UUID. Results must match
        calling with the name.
        """
        accounts = connector.list_accounts()
        match = next((a for a in accounts if a["name"] == test_account), None)
        assert match is not None, f"Test account {test_account!r} not found"
        uuid = match["id"]

        # Sanity check: it really is a UUID-shaped string.
        from apple_mail_mcp.utils import is_account_uuid
        assert is_account_uuid(uuid), f"Expected UUID, got {uuid!r}"

        by_uuid = connector.list_mailboxes(uuid)
        by_name = connector.list_mailboxes(test_account)

        assert isinstance(by_uuid, list)
        # Results may not match in order, but both lists should have the same
        # set of mailbox names.
        assert {m["name"] for m in by_uuid} == {m["name"] for m in by_name}

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

    def test_get_thread_orphan_anchor(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """For any message, get_thread must at minimum return the anchor itself.

        This exercises the anchor-resolution + candidate-collection path
        end-to-end without needing a known-threaded message. Skips if the
        inbox is empty.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        thread = connector.get_thread(matches[0]["id"])
        assert isinstance(thread, list)
        assert len(thread) >= 1
        for m in thread:
            assert set(m.keys()) >= {
                "id", "subject", "sender", "date_received",
                "read_status", "flagged",
            }
        # Anchor must be in the result.
        assert any(m["id"] == matches[0]["id"] for m in thread)

    def test_get_thread_rejects_nonexistent_anchor(
        self, connector: AppleMailConnector
    ) -> None:
        """Nonexistent anchor raises MailMessageNotFoundError."""
        from apple_mail_mcp.exceptions import MailMessageNotFoundError
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("99999999999")

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


class TestRuleCRUDIntegration:
    """End-to-end CRUD on a test-prefixed Mail.app rule.

    Self-cleaning: always deletes the test rule at the end via try/finally,
    even if intermediate assertions fail. Idempotent: a leftover from a
    previous failed run is detected and removed at the start.

    Refers to a rule whose name starts with '[apple-mail-mcp-test]' —
    this is the test prefix the safety gate uses, but the connector
    itself doesn't enforce it. We use a recognizable name so manual
    cleanup is easy if all else fails.
    """

    TEST_RULE_NAME = "[apple-mail-mcp-test] integration test rule"

    def _delete_test_rule_if_present(
        self, connector: AppleMailConnector
    ) -> None:
        """Find and delete any rule with TEST_RULE_NAME, regardless of state."""
        for r in connector.list_rules():
            if r["name"] == self.TEST_RULE_NAME:
                connector.delete_rule(r["index"])

    def test_full_crud_cycle(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """Create → list → enable-toggle → update → delete a test rule."""
        # Pre-clean: in case a previous run left a leftover.
        self._delete_test_rule_if_present(connector)

        try:
            # 1. CREATE
            new_index = connector.create_rule(
                name=self.TEST_RULE_NAME,
                conditions=[
                    {
                        "field": "subject",
                        "operator": "contains",
                        "value": "this-string-will-not-match-anything-zzz",
                    }
                ],
                actions={"mark_read": True},
                match_logic="all",
                enabled=True,
            )
            assert new_index >= 1

            # 2. LIST: verify it's there with expected index, name, enabled.
            rules = connector.list_rules()
            test_rule = next(
                (r for r in rules if r["name"] == self.TEST_RULE_NAME),
                None,
            )
            assert test_rule is not None, (
                f"Created rule not found in list_rules output. "
                f"Saw: {[r['name'] for r in rules]}"
            )
            assert test_rule["index"] == new_index
            assert test_rule["enabled"] is True

            # 3. SET_RULE_ENABLED: toggle off.
            connector.set_rule_enabled(new_index, enabled=False)
            rules = connector.list_rules()
            test_rule = next(
                r for r in rules if r["name"] == self.TEST_RULE_NAME
            )
            assert test_rule["enabled"] is False

            # 4. UPDATE: rename + re-enable + change actions + match_logic.
            # NOTE: `conditions=` deliberately not exercised here — Mail.app
            # on macOS Tahoe has a recursion bug in
            # removeFromCriteriaAtIndex: that crashes Mail on any path that
            # removes a rule condition. The connector refuses `conditions=`
            # with MailUnsupportedRuleActionError; see test_mail_connector
            # for unit coverage of the refusal.
            renamed = self.TEST_RULE_NAME + " v2"
            connector.update_rule(
                rule_index=new_index,
                name=renamed,
                enabled=True,
                match_logic="any",
                actions={"mark_flagged": True, "flag_color": "red"},
            )
            rules = connector.list_rules()
            updated_rule = next(
                (r for r in rules if r["name"] == renamed), None
            )
            assert updated_rule is not None, (
                f"Updated rule with new name not found. "
                f"Saw: {[r['name'] for r in rules]}"
            )
            assert updated_rule["enabled"] is True

            # Restore the original name so cleanup finds it.
            connector.update_rule(rule_index=updated_rule["index"], name=self.TEST_RULE_NAME)

            # 5. DELETE: remove it. delete_rule returns the rule's name.
            test_rule = next(
                r for r in connector.list_rules()
                if r["name"] == self.TEST_RULE_NAME
            )
            deleted_name = connector.delete_rule(test_rule["index"])
            assert deleted_name == self.TEST_RULE_NAME

            # 6. VERIFY GONE
            rules_after = connector.list_rules()
            names_after = [r["name"] for r in rules_after]
            assert self.TEST_RULE_NAME not in names_after, (
                f"Test rule still in list after delete: {names_after}"
            )
        finally:
            # Defensive cleanup if anything above raised.
            self._delete_test_rule_if_present(connector)


class TestTemplateIntegration:
    """End-to-end: save a template referencing reply-context placeholders,
    render it against a real message from the test inbox, verify the
    auto-fills came through.

    Storage isolation: redirects APPLE_MAIL_MCP_HOME at tmp_path to avoid
    touching the real templates directory.
    """

    def test_round_trip_with_real_message_data(
        self,
        connector: AppleMailConnector,
        test_account: str,
        tmp_path: Path,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Save → reload from disk → render against real message data.

        Pulls subject and sender from search_messages (which already
        returns those fields, so we don't depend on get_message — that
        path has a pre-existing AppleScript-quoting bug on UUID-style
        IDs that's unrelated to this feature). Auto-fill behavior is
        unit-tested with mocked get_message in test_mail_connector.
        """
        from email.utils import parseaddr

        from apple_mail_mcp.templates import Template, TemplateStore

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        store = TemplateStore()

        # Try a few likely mailboxes — the test account may have an
        # empty INBOX but messages elsewhere.
        msg: dict | None = None
        for mb in ("INBOX", "Archive", "Sent Messages"):
            try:
                matches = connector.search_messages(
                    account=test_account, mailbox=mb, limit=1
                )
            except Exception:
                continue
            if matches:
                msg = matches[0]
                break
        if msg is None:
            pytest.skip("no messages found in test account")

        # Save a template that exercises every reply-context placeholder.
        store.save(
            Template(
                name="integration-reply",
                subject="Re: {original_subject}",
                body=(
                    "Hi {recipient_name},\n\n"
                    "Thanks for reaching out (writing on {today}).\n"
                ),
            )
        )

        # Build the var dict the same way auto_template_vars would,
        # but from search_messages data so we sidestep the get_message
        # quoting bug.
        from datetime import date

        sender_field = str(msg.get("sender") or "")
        display_name, email_addr = parseaddr(sender_field)
        recipient_email = email_addr or sender_field
        recipient_name = display_name or recipient_email
        original_subject = str(msg.get("subject") or "")
        today = date.today().isoformat()

        loaded = store.get("integration-reply")
        rendered = loaded.render(
            {
                "recipient_name": recipient_name,
                "recipient_email": recipient_email,
                "original_subject": original_subject,
                "today": today,
            }
        )
        assert rendered["subject"] == f"Re: {original_subject}"
        assert recipient_name in rendered["body"]
        assert today in rendered["body"]
