"""
Unit tests for the FastMCP server layer in apple_mail_mcp.server.

These tests exercise each @mcp.tool() function directly as a regular Python
callable with a mocked AppleMailConnector. They cover server-layer concerns
that the connector tests cannot: input validation, confirmation flows,
exception-to-error_type mapping, structured response shape, and
operation_logger calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    DeclinedElicitation,
)

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from apple_mail_mcp.server import (
    create_mailbox,
    create_rule,
    delete_messages,
    delete_rule,
    delete_template,
    get_messages,
    get_template,
    get_thread,
    list_accounts,
    list_mailboxes,
    list_rules,
    list_templates,
    render_template,
    save_attachments,
    save_template,
    search_messages,
    update_message,
    update_rule,
)


@pytest.fixture
def mock_mail() -> Any:
    with patch("apple_mail_mcp.server.mail") as m:
        yield m


@pytest.fixture
def mock_logger() -> Any:
    with patch("apple_mail_mcp.server.operation_logger") as m:
        yield m


@pytest.fixture
def mock_ctx_accept() -> MagicMock:
    """Mock MCP Context that accepts elicitation."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data={}))
    return ctx


@pytest.fixture
def mock_ctx_decline() -> MagicMock:
    """Mock MCP Context that declines elicitation."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=DeclinedElicitation())
    return ctx


# ---------------------------------------------------------------------------
# 0. list_accounts
# ---------------------------------------------------------------------------


class TestListAccounts:
    def test_success_returns_accounts_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_accounts.return_value = [
            {"id": "UUID-1", "name": "Gmail",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            {"id": "UUID-2", "name": "iCloud",
             "email_addresses": ["me@icloud.com"],
             "account_type": "iCloud", "enabled": True},
        ]

        result = list_accounts()

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["accounts"]) == 2
        assert result["accounts"][0]["id"] == "UUID-1"
        mock_mail.list_accounts.assert_called_once_with()
        mock_logger.log_operation.assert_called_once_with(
            "list_accounts", {}, "success"
        )

    def test_empty_returns_empty_list(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_accounts.return_value = []

        result = list_accounts()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["accounts"] == []

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_accounts.side_effect = RuntimeError("boom")

        result = list_accounts()

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 0b. list_rules
# ---------------------------------------------------------------------------


class TestListRules:
    def test_success_returns_rules_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"name": "Junk filter", "enabled": True},
            {"name": "News", "enabled": False},
        ]

        result = list_rules()

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["rules"]) == 2
        mock_mail.list_rules.assert_called_once_with()
        mock_logger.log_operation.assert_called_once_with(
            "list_rules", {}, "success"
        )

    def test_empty_returns_empty_list(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = []

        result = list_rules()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["rules"] == []

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_rules.side_effect = RuntimeError("boom")

        result = list_rules()

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 0c. Rule mutations: delete_rule, create_rule, update_rule
# ---------------------------------------------------------------------------


class TestDeleteRule:
    async def test_success_with_accepted_ctx(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        mock_mail.delete_rule.return_value = "Junk filter"
        result = await delete_rule(rule_index=1, ctx=mock_ctx_accept)
        assert result["success"] is True
        assert result["deleted_name"] == "Junk filter"
        mock_ctx_accept.elicit.assert_awaited_once()
        mock_mail.delete_rule.assert_called_once_with(1)

    async def test_declined_ctx_blocks_delete(
        self, mock_mail: MagicMock, mock_ctx_decline: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await delete_rule(rule_index=1, ctx=mock_ctx_decline)
        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.delete_rule.assert_not_called()

    async def test_returns_rule_not_found_when_index_missing(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = []
        result = await delete_rule(rule_index=99, ctx=None)
        assert result["success"] is False
        assert result["error_type"] == "rule_not_found"


class TestCreateRule:
    def test_success_returns_new_index(self, mock_mail: MagicMock) -> None:
        mock_mail.create_rule.return_value = 6
        result = create_rule(
            name="My New Rule",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "X"}
            ],
            actions={"mark_read": True},
        )
        assert result["success"] is True
        assert result["rule_index"] == 6
        assert result["name"] == "My New Rule"

    def test_no_elicitation_for_create(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        """create_rule is additive — no confirmation prompt."""
        # create_rule is sync, takes no ctx, so this just confirms it
        # works without one.
        mock_mail.create_rule.return_value = 1
        result = create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"delete": True},
        )
        assert result["success"] is True
        # No elicit call possible — sync function doesn't accept ctx.

    def test_validation_error_returns_validation_type(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.create_rule.side_effect = ValueError("invalid field")
        result = create_rule(
            name="X",
            conditions=[
                {"field": "bogus", "operator": "contains", "value": "Y"}
            ],
            actions={"delete": True},
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"


class TestUpdateRule:
    # ---- Irreversible patches: prompt required ---------------------------

    async def test_conditions_patch_prompts_and_succeeds(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1,
            conditions=[
                {"field": "subject", "operator": "contains", "value": "X"}
            ],
            ctx=mock_ctx_accept,
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()
        mock_mail.update_rule.assert_called_once()

    async def test_actions_patch_prompts_and_succeeds(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1,
            actions={"mark_read": True},
            ctx=mock_ctx_accept,
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()
        mock_mail.update_rule.assert_called_once()

    async def test_match_logic_patch_prompts_and_succeeds(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1, match_logic="any", ctx=mock_ctx_accept
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()
        mock_mail.update_rule.assert_called_once()

    async def test_declined_ctx_blocks_irreversible_update(
        self, mock_mail: MagicMock, mock_ctx_decline: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "X", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1,
            actions={"delete": True},
            ctx=mock_ctx_decline,
        )
        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.update_rule.assert_not_called()

    # ---- Reversible-only patches: no prompt ------------------------------

    async def test_enabled_only_patch_does_not_prompt(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1, enabled=False, ctx=mock_ctx_accept
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_not_awaited()
        mock_mail.update_rule.assert_called_once()

    async def test_name_only_patch_does_not_prompt(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Junk filter", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1, name="renamed", ctx=mock_ctx_accept
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_not_awaited()
        mock_mail.update_rule.assert_called_once()

    async def test_enabled_plus_name_does_not_prompt(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "X", "enabled": True},
        ]
        result = await update_rule(
            rule_index=1,
            enabled=False,
            name="renamed",
            ctx=mock_ctx_accept,
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_not_awaited()
        mock_mail.update_rule.assert_called_once()

    async def test_enabled_only_works_without_ctx(
        self, mock_mail: MagicMock
    ) -> None:
        """Migration path for callers porting from set_rule_enabled."""
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "X", "enabled": True},
        ]
        result = await update_rule(rule_index=1, enabled=True)
        assert result["success"] is True
        mock_mail.update_rule.assert_called_once()

    # ---- Error mapping ----------------------------------------------------

    async def test_returns_unsupported_action_error_type(
        self, mock_mail: MagicMock, mock_ctx_accept: MagicMock
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "X", "enabled": True},
        ]
        mock_mail.update_rule.side_effect = MailUnsupportedRuleActionError(
            "uses run-script"
        )
        result = await update_rule(
            rule_index=1,
            actions={"delete": True},
            ctx=mock_ctx_accept,
        )
        assert result["success"] is False
        assert result["error_type"] == "unsupported_rule_action"


# ---------------------------------------------------------------------------
# 1. list_mailboxes
# ---------------------------------------------------------------------------


class TestListMailboxes:
    def test_success_returns_mailboxes_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_mailboxes.return_value = [
            {"name": "INBOX", "unread_count": 3},
            {"name": "Sent", "unread_count": 0},
        ]

        result = list_mailboxes("Gmail")

        assert result["success"] is True
        assert result["account"] == "Gmail"
        assert len(result["mailboxes"]) == 2
        mock_mail.list_mailboxes.assert_called_once_with("Gmail")
        mock_logger.log_operation.assert_called_once_with(
            "list_mailboxes", {"account": "Gmail"}, "success"
        )

    def test_account_not_found_maps_to_error_type(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_mailboxes.side_effect = MailAccountNotFoundError("nope")

        result = list_mailboxes("Bogus")

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"
        assert "Bogus" in result["error"]
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.list_mailboxes.side_effect = RuntimeError("boom")

        result = list_mailboxes("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 2. search_messages
# ---------------------------------------------------------------------------


class TestSearchMessages:
    def test_success_returns_messages_with_count(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = [
            {"id": "1"},
            {"id": "2"},
        ]

        result = search_messages(
            "Gmail",
            mailbox="INBOX",
            sender_contains="alice@example.com",
            read_status=False,
            limit=10,
        )

        assert result["success"] is True
        assert result["account"] == "Gmail"
        assert result["mailbox"] == "INBOX"
        assert result["count"] == 2
        assert len(result["messages"]) == 2
        mock_mail.search_messages.assert_called_once_with(
            account="Gmail",
            mailbox="INBOX",
            sender_contains="alice@example.com",
            subject_contains=None,
            read_status=False,
            is_flagged=None,
            date_from=None,
            date_to=None,
            has_attachment=None,
            limit=10,
            include_attachments=False,
            body_contains=None,
            text_contains=None,
            on_warning=ANY,
        )
        mock_logger.log_operation.assert_called_once()
        logged_op, logged_params, logged_status = mock_logger.log_operation.call_args.args
        assert logged_op == "search_messages"
        assert logged_status == "success"
        assert logged_params["filters"]["sender"] == "alice@example.com"

    def test_account_not_found_maps_to_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.side_effect = MailAccountNotFoundError("x")

        result = search_messages("Bogus")

        assert result["success"] is False
        assert result["error_type"] == "not_found"
        mock_logger.log_operation.assert_not_called()

    def test_mailbox_not_found_maps_to_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.side_effect = MailMailboxNotFoundError("x")

        result = search_messages("Gmail", mailbox="Missing")

        assert result["success"] is False
        assert result["error_type"] == "not_found"

    def test_advanced_filters_propagate_to_connector(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """New in #28: is_flagged, date_from, date_to, has_attachment must
        pass through to the connector and appear in the audit log."""
        mock_mail.search_messages.return_value = []

        result = search_messages(
            "Gmail",
            mailbox="INBOX",
            is_flagged=True,
            date_from="2026-04-01",
            date_to="2026-04-15",
            has_attachment=True,
            limit=25,
        )

        assert result["success"] is True
        mock_mail.search_messages.assert_called_once_with(
            account="Gmail",
            mailbox="INBOX",
            sender_contains=None,
            subject_contains=None,
            read_status=None,
            is_flagged=True,
            date_from="2026-04-01",
            date_to="2026-04-15",
            has_attachment=True,
            limit=25,
            include_attachments=False,
            body_contains=None,
            text_contains=None,
            on_warning=ANY,
        )
        logged_params = mock_logger.log_operation.call_args.args[1]
        assert logged_params["filters"] == {
            "sender": None,
            "subject": None,
            "read_status": None,
            "is_flagged": True,
            "date_from": "2026-04-01",
            "date_to": "2026-04-15",
            "has_attachment": True,
            "body_contains": None,
            "text_contains": None,
        }

    def test_malformed_date_maps_to_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Connector raises ValueError on bad date; server surfaces
        error_type: validation_error (not generic unknown)."""
        mock_mail.search_messages.side_effect = ValueError(
            "date_from must be ISO 8601 YYYY-MM-DD, got: 'nope'"
        )

        result = search_messages("Gmail", date_from="nope")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "date_from" in result["error"]
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.side_effect = RuntimeError("boom")

        result = search_messages("Gmail")

        assert result["success"] is False
        assert result["error_type"] == "unknown"

    # ---- source="selected" (folded-in get_selected_messages, #131) -------

    # ---- source=None default (search the mailbox) -----------------------

    def test_no_source_no_account_returns_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        result = search_messages()  # source=None default, no account

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "account" in result["error"]
        mock_mail.search_messages.assert_not_called()
        mock_mail.get_selected_messages.assert_not_called()
        mock_mail.get_message.assert_not_called()

    def test_no_source_with_account_unchanged(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Regression: existing positional callers still work."""
        mock_mail.search_messages.return_value = [{"id": "1"}]

        result = search_messages("Gmail")

        assert result["success"] is True
        assert result["account"] == "Gmail"
        mock_mail.search_messages.assert_called_once()
        mock_mail.get_selected_messages.assert_not_called()
        mock_mail.get_message.assert_not_called()

    # ---- source=["SELECTED"] sentinel -----------------------------------

    def test_source_selected_sentinel_returns_selection(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "12345",
                "subject": "Hello",
                "sender": "alice@example.com",
                "date_received": "Mon Jan 1 2024",
                "read_status": True,
                "flagged": False,
            }
        ]

        result = search_messages(source=["SELECTED"])

        assert result["success"] is True
        assert result["count"] == 1
        assert result["account"] is None
        assert result["mailbox"] is None
        assert result["messages"][0]["id"] == "12345"
        mock_mail.get_selected_messages.assert_called_once_with(
            include_content=False,
            include_attachments=False,
        )
        mock_mail.search_messages.assert_not_called()

    def test_source_selected_empty_selection(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_selected_messages.return_value = []

        result = search_messages(source=["SELECTED"])

        assert result["success"] is True
        assert result["count"] == 0
        assert result["messages"] == []

    def test_source_selected_does_not_require_account(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_selected_messages.return_value = []

        result = search_messages(source=["SELECTED"])

        assert result["success"] is True
        # No validation_error even though account is None.

    def test_source_selected_post_filters_by_other_params(
        self, mock_mail: MagicMock
    ) -> None:
        """Filters compose with source=[ids] (unlike pre-#144 source='selected')."""
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "1",
                "subject": "alpha",
                "sender": "alice@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
            },
            {
                "id": "2",
                "subject": "beta",
                "sender": "bob@example.com",
                "date_received": "2026-04-02",
                "read_status": False,
                "flagged": False,
            },
        ]

        result = search_messages(
            source=["SELECTED"], read_status=False
        )

        assert [m["id"] for m in result["messages"]] == ["2"]

    # ---- source=[explicit ids] -----------------------------------------

    def test_source_explicit_ids_returns_those_messages(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_message.side_effect = [
            {
                "id": "12345",
                "subject": "first",
                "sender": "a@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
            },
            {
                "id": "67890",
                "subject": "second",
                "sender": "b@example.com",
                "date_received": "2026-04-02",
                "read_status": False,
                "flagged": False,
            },
        ]

        result = search_messages(source=["12345", "67890"])

        assert result["success"] is True
        assert result["count"] == 2
        assert result["account"] is None
        assert result["mailbox"] is None
        assert [m["id"] for m in result["messages"]] == ["12345", "67890"]
        # Per-id metadata fetch with no body, no attachments (search default).
        assert mock_mail.get_message.call_count == 2
        first_call = mock_mail.get_message.call_args_list[0]
        assert first_call.args[0] == "12345"
        assert first_call.kwargs.get("include_content") is False
        assert first_call.kwargs.get("include_attachments") is False
        mock_mail.search_messages.assert_not_called()
        mock_mail.get_selected_messages.assert_not_called()

    def test_source_explicit_ids_post_filters(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_message.side_effect = [
            {
                "id": "1",
                "subject": "alpha",
                "sender": "alice@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
            },
            {
                "id": "2",
                "subject": "beta",
                "sender": "bob@example.com",
                "date_received": "2026-04-02",
                "read_status": False,
                "flagged": False,
            },
        ]

        result = search_messages(
            source=["1", "2"], read_status=False
        )

        assert [m["id"] for m in result["messages"]] == ["2"]

    def test_source_mixed_selected_and_explicit_ids(
        self, mock_mail: MagicMock
    ) -> None:
        """SELECTED token expands inline; mixed with real ids."""
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "sel-1",
                "subject": "from selection",
                "sender": "x@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
            },
        ]
        mock_mail.get_message.return_value = {
            "id": "explicit-1",
            "subject": "explicit",
            "sender": "y@example.com",
            "date_received": "2026-04-02",
            "read_status": True,
            "flagged": False,
        }

        result = search_messages(source=["SELECTED", "explicit-1"])

        assert result["success"] is True
        assert [m["id"] for m in result["messages"]] == ["sel-1", "explicit-1"]
        # search_messages defaults include_attachments=False on both paths.
        mock_mail.get_selected_messages.assert_called_once_with(
            include_content=False,
            include_attachments=False,
        )
        mock_mail.get_message.assert_called_once()

    def test_source_empty_list_returns_empty(
        self, mock_mail: MagicMock
    ) -> None:
        result = search_messages(source=[])

        assert result["success"] is True
        assert result["count"] == 0
        assert result["messages"] == []
        mock_mail.search_messages.assert_not_called()
        mock_mail.get_selected_messages.assert_not_called()
        mock_mail.get_message.assert_not_called()

    def test_source_nonexistent_id_skipped(
        self, mock_mail: MagicMock
    ) -> None:
        """Partial-results: missing ids drop out, found ids return."""
        from apple_mail_mcp.exceptions import MailMessageNotFoundError

        mock_mail.get_message.side_effect = [
            MailMessageNotFoundError("nope"),
            {
                "id": "good-id",
                "subject": "found",
                "sender": "a@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
            },
        ]

        result = search_messages(source=["bogus", "good-id"])

        assert result["success"] is True
        assert [m["id"] for m in result["messages"]] == ["good-id"]

    # ---- include_attachments (#133 + #142) -------------------------------

    def test_include_attachments_default_is_false(
        self, mock_mail: MagicMock
    ) -> None:
        """search_messages defaults include_attachments=False (unbounded
        cardinality on AppleScript fallback). Default protects the
        cheap-search semantic."""
        mock_mail.search_messages.return_value = []

        search_messages("Gmail")

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert kwargs["include_attachments"] is False

    def test_include_attachments_true_passes_through_to_connector(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = []

        search_messages("Gmail", include_attachments=True)

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert kwargs["include_attachments"] is True

    def test_include_attachments_with_source_list(
        self, mock_mail: MagicMock
    ) -> None:
        """source=[ids] path also threads include_attachments through to
        per-id mail.get_message calls."""
        mock_mail.get_message.return_value = {
            "id": "1",
            "subject": "x",
            "sender": "a@example.com",
            "date_received": "2026-04-01",
            "read_status": True,
            "flagged": False,
        }

        search_messages(source=["1"], include_attachments=True)

        first_call = mock_mail.get_message.call_args_list[0]
        assert first_call.kwargs.get("include_attachments") is True

    # ---- body_contains / text_contains (#145) ---------------------------

    def test_body_contains_passes_through_to_connector(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = []

        search_messages("Gmail", body_contains="urgent")

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert kwargs["body_contains"] == "urgent"

    def test_text_contains_passes_through_to_connector(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = []

        search_messages("Gmail", text_contains="alice")

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert kwargs["text_contains"] == "alice"

    def test_body_and_text_contains_both_supplied(
        self, mock_mail: MagicMock
    ) -> None:
        """Both filters compose (AND)."""
        mock_mail.search_messages.return_value = []

        search_messages(
            "Gmail", body_contains="report", text_contains="alice"
        )

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert kwargs["body_contains"] == "report"
        assert kwargs["text_contains"] == "alice"

    def test_default_no_body_or_text_contains(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = []

        search_messages("Gmail")

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert kwargs["body_contains"] is None
        assert kwargs["text_contains"] is None

    def test_source_list_with_body_contains_forces_content_fetch(
        self, mock_mail: MagicMock
    ) -> None:
        """source=[ids] + body_contains: per-id fetch must include content
        so the post-filter can match against bodies."""
        mock_mail.get_message.return_value = {
            "id": "1",
            "subject": "x",
            "sender": "a@example.com",
            "date_received": "2026-04-01",
            "read_status": True,
            "flagged": False,
            "content": "this body contains urgent text",
        }

        search_messages(source=["1"], body_contains="urgent")

        first_call = mock_mail.get_message.call_args_list[0]
        # Body needed for the post-filter — include_content forced True.
        assert first_call.kwargs.get("include_content") is True

    def test_source_list_body_contains_post_filters(
        self, mock_mail: MagicMock
    ) -> None:
        """source=[ids] post-filter drops rows whose body doesn't match."""
        mock_mail.get_message.side_effect = [
            {
                "id": "match",
                "subject": "x",
                "sender": "a@example.com",
                "date_received": "2026-04-01",
                "read_status": True,
                "flagged": False,
                "content": "the body has urgent text",
            },
            {
                "id": "no-match",
                "subject": "x",
                "sender": "b@example.com",
                "date_received": "2026-04-02",
                "read_status": True,
                "flagged": False,
                "content": "nothing relevant here",
            },
        ]

        result = search_messages(
            source=["match", "no-match"], body_contains="urgent"
        )

        assert [m["id"] for m in result["messages"]] == ["match"]

    # ---- warnings field (#146) ------------------------------------------

    def test_warnings_field_present_when_callback_fires(
        self, mock_mail: MagicMock
    ) -> None:
        """When the connector emits a warning via the on_warning callback,
        the response includes a warnings list."""
        def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
            on_warning = kwargs.get("on_warning")
            if on_warning is not None:
                on_warning("AppleScript body search may be slow")
            return []

        mock_mail.search_messages.side_effect = fake_search

        result = search_messages("Gmail", body_contains="urgent")

        assert "warnings" in result
        assert any(
            "AppleScript body search" in w for w in result["warnings"]
        )

    def test_warnings_field_omitted_when_no_callback_fires(
        self, mock_mail: MagicMock
    ) -> None:
        """No warnings emitted by connector → response has no warnings field
        (don't pollute the cheap-call default case)."""
        mock_mail.search_messages.return_value = []

        result = search_messages("Gmail")

        assert "warnings" not in result

    def test_on_warning_callback_passed_to_connector(
        self, mock_mail: MagicMock
    ) -> None:
        """Server creates a callback and passes it through to the connector."""
        mock_mail.search_messages.return_value = []

        search_messages("Gmail", body_contains="x")

        kwargs = mock_mail.search_messages.call_args.kwargs
        assert callable(kwargs.get("on_warning"))


# ---------------------------------------------------------------------------
# 3. get_messages
# ---------------------------------------------------------------------------


class TestGetMessages:
    def test_single_id_returns_one_in_list(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_message.return_value = {"id": "1", "subject": "Hi"}

        result = get_messages(["1"], include_content=False)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["messages"][0]["id"] == "1"
        # All six params flow through per id; include_attachments defaults
        # True for get_messages (bounded id-list cardinality, see #133+#142).
        mock_mail.get_message.assert_called_once_with(
            "1",
            include_content=False,
            headers_only=False,
            account=None,
            mailbox=None,
            include_attachments=True,
        )
        mock_logger.log_operation.assert_called_once()

    def test_list_of_ids_returns_many(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_message.side_effect = [
            {"id": "1", "subject": "first"},
            {"id": "2", "subject": "second"},
        ]

        result = get_messages(["1", "2"])

        assert result["success"] is True
        assert result["count"] == 2
        assert [m["id"] for m in result["messages"]] == ["1", "2"]
        assert mock_mail.get_message.call_count == 2

    def test_empty_list_returns_empty_no_error(
        self, mock_mail: MagicMock
    ) -> None:
        result = get_messages([])

        assert result["success"] is True
        assert result["count"] == 0
        assert result["messages"] == []
        mock_mail.get_message.assert_not_called()
        mock_mail.get_selected_messages.assert_not_called()

    def test_selected_sentinel_expands_to_selection(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "sel-1",
                "subject": "selected one",
                "sender": "x@example.com",
                "date_received": "Mon",
                "read_status": True,
                "flagged": False,
                "content": "body",
            },
            {
                "id": "sel-2",
                "subject": "selected two",
                "sender": "y@example.com",
                "date_received": "Tue",
                "read_status": True,
                "flagged": False,
                "content": "body",
            },
        ]

        result = get_messages(["SELECTED"])

        assert result["success"] is True
        assert [m["id"] for m in result["messages"]] == ["sel-1", "sel-2"]
        # SELECTED expands via get_selected_messages — full bodies + attachments default-on for get_messages.
        mock_mail.get_selected_messages.assert_called_once_with(
            include_content=True,
            include_attachments=True,
        )
        # No per-id get_message lookup needed for SELECTED-resolved rows.
        mock_mail.get_message.assert_not_called()

    def test_mixed_selected_and_real_ids(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_selected_messages.return_value = [
            {
                "id": "sel-1",
                "subject": "from selection",
                "sender": "x@example.com",
                "date_received": "Mon",
                "read_status": True,
                "flagged": False,
                "content": "body",
            },
        ]
        mock_mail.get_message.return_value = {
            "id": "real-1",
            "subject": "explicit",
            "content": "explicit body",
        }

        result = get_messages(["SELECTED", "real-1"])

        assert result["success"] is True
        assert [m["id"] for m in result["messages"]] == ["sel-1", "real-1"]
        mock_mail.get_selected_messages.assert_called_once_with(
            include_content=True,
            include_attachments=True,
        )
        mock_mail.get_message.assert_called_once()

    def test_nonexistent_id_skipped_partial_results(
        self, mock_mail: MagicMock
    ) -> None:
        """Per-id MailMessageNotFoundError is dropped silently (partial-results)."""
        mock_mail.get_message.side_effect = [
            MailMessageNotFoundError("missing"),
            {"id": "good", "subject": "found"},
        ]

        result = get_messages(["bogus", "good"])

        assert result["success"] is True
        assert [m["id"] for m in result["messages"]] == ["good"]

    def test_imap_hint_params_pass_through_per_id(
        self, mock_mail: MagicMock
    ) -> None:
        """Issue #72: account+mailbox activate the IMAP fast path."""
        mock_mail.get_message.return_value = {"id": "abc@x", "subject": "Hi"}

        result = get_messages(
            ["abc@x"], account="iCloud", mailbox="INBOX", headers_only=True
        )

        assert result["success"] is True
        mock_mail.get_message.assert_called_once_with(
            "abc@x",
            include_content=True,
            headers_only=True,
            account="iCloud",
            mailbox="INBOX",
            include_attachments=True,
        )

    # ---- include_attachments (#133 + #142) -------------------------------

    def test_include_attachments_default_is_true(
        self, mock_mail: MagicMock
    ) -> None:
        """get_messages defaults include_attachments=True (bounded cardinality)."""
        mock_mail.get_message.return_value = {"id": "1", "subject": "Hi"}

        get_messages(["1"])

        kwargs = mock_mail.get_message.call_args.kwargs
        assert kwargs["include_attachments"] is True

    def test_include_attachments_false_opts_out(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_message.return_value = {"id": "1", "subject": "Hi"}

        get_messages(["1"], include_attachments=False)

        kwargs = mock_mail.get_message.call_args.kwargs
        assert kwargs["include_attachments"] is False

    def test_include_attachments_threads_to_get_selected(
        self, mock_mail: MagicMock
    ) -> None:
        """SELECTED sentinel path also receives include_attachments."""
        mock_mail.get_selected_messages.return_value = []

        get_messages(["SELECTED"], include_attachments=False)

        mock_mail.get_selected_messages.assert_called_once_with(
            include_content=True,
            include_attachments=False,
        )

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.get_message.side_effect = RuntimeError("boom")

        result = get_messages(["1"])

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 5. update_message — patch tool replacing mark_as_read + move_messages + flag_message (#135)
# ---------------------------------------------------------------------------


class TestUpdateMessage:
    # ---- Validation -----------------------------------------------------

    def test_no_fields_returns_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        """At least one mutation field is required."""
        result = update_message(["1"])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.update_message.assert_not_called()

    def test_empty_message_ids_returns_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        result = update_message([], read_status=True)

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.update_message.assert_not_called()

    def test_over_limit_fails_validation(
        self, mock_mail: MagicMock
    ) -> None:
        result = update_message([str(i) for i in range(101)], read_status=True)

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.update_message.assert_not_called()

    # ---- Individual fields ----------------------------------------------

    def test_read_status_only(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        result = update_message(["1"], read_status=True)

        assert result["success"] is True
        assert result["updated"] == 1
        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["read_status"] is True
        assert kwargs["flagged"] is None
        assert kwargs["flag_color"] is None
        assert kwargs["destination_mailbox"] is None

    def test_flagged_only(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(["1"], flagged=True)

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["flagged"] is True
        assert kwargs["flag_color"] is None

    def test_flag_color_only(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(["1"], flag_color="red")

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["flag_color"] == "red"

    def test_destination_mailbox_only(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(
            ["1"], destination_mailbox="Archive", account="Gmail"
        )

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["destination_mailbox"] == "Archive"
        assert kwargs["account"] == "Gmail"

    # ---- Combinations (single-pass, AC #2) ------------------------------

    def test_combined_read_and_move(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(
            ["1"],
            read_status=True,
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["read_status"] is True
        assert kwargs["destination_mailbox"] == "Archive"
        assert kwargs["account"] == "iCloud"
        assert kwargs["source_mailbox"] == "INBOX"
        # All passed in a single connector call — implies single AppleScript pass.
        assert mock_mail.update_message.call_count == 1

    def test_combined_flag_and_move(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(
            ["1"],
            flag_color="red",
            destination_mailbox="Archive",
            account="iCloud",
        )

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["flag_color"] == "red"
        assert kwargs["destination_mailbox"] == "Archive"

    def test_all_fields_combined_single_pass(
        self, mock_mail: MagicMock
    ) -> None:
        """All mutation fields combine into one connector call (one
        AppleScript pass / one IMAP STORE+MOVE)."""
        mock_mail.update_message.return_value = 2

        update_message(
            ["1", "2"],
            read_status=True,
            flag_color="orange",
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )

        assert mock_mail.update_message.call_count == 1

    # ---- Narrow-path passthrough (AC #7) --------------------------------

    def test_narrow_path_account_and_source_mailbox(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(
            ["1"],
            read_status=True,
            account="Gmail",
            source_mailbox="INBOX",
        )

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["account"] == "Gmail"
        assert kwargs["source_mailbox"] == "INBOX"

    # ---- gmail_mode passthrough -----------------------------------------

    def test_gmail_mode_passes_through(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.return_value = 1

        update_message(
            ["1"],
            destination_mailbox="Archive",
            account="Gmail",
            gmail_mode=True,
        )

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["gmail_mode"] is True

    # ---- Trash-restore semantics (AC #6) --------------------------------

    def test_trash_restore_works(
        self, mock_mail: MagicMock
    ) -> None:
        """update_message can move messages out of Trash — no special verb."""
        mock_mail.update_message.return_value = 1

        update_message(
            ["1"],
            destination_mailbox="INBOX",
            account="iCloud",
            source_mailbox="Deleted Messages",
        )

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs["destination_mailbox"] == "INBOX"
        assert kwargs["source_mailbox"] == "Deleted Messages"

    # ---- Error mapping --------------------------------------------------

    def test_account_not_found_maps_to_account_not_found(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.side_effect = MailAccountNotFoundError("x")

        result = update_message(
            ["1"], destination_mailbox="Archive", account="Bogus"
        )

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"

    def test_mailbox_not_found_maps_to_not_found(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.side_effect = MailMailboxNotFoundError("x")

        result = update_message(
            ["1"], destination_mailbox="Bogus", account="Gmail"
        )

        assert result["success"] is False
        assert result["error_type"] == "not_found"

    def test_value_error_maps_to_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.side_effect = ValueError("invalid flag color")

        result = update_message(["1"], flag_color="rainbow")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.update_message.side_effect = RuntimeError("boom")

        result = update_message(["1"], read_status=True)

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 7b. get_thread
# ---------------------------------------------------------------------------


class TestGetThread:
    def test_success_returns_thread_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.return_value = [
            {"id": "1", "subject": "Q3", "sender": "a@b", "date_received": "Mon", "read_status": True, "flagged": False},
            {"id": "2", "subject": "Re: Q3", "sender": "c@d", "date_received": "Tue", "read_status": False, "flagged": False},
        ]

        result = get_thread("1")

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["thread"]) == 2
        mock_mail.get_thread.assert_called_once_with("1")
        mock_logger.log_operation.assert_called_once_with(
            "get_thread", {"message_id": "1"}, "success"
        )

    def test_message_not_found_maps_to_message_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.side_effect = MailMessageNotFoundError("nope")

        result = get_thread("nope")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        assert "nope" in result["error"]
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.side_effect = RuntimeError("boom")

        result = get_thread("1")

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 8. save_attachments
# ---------------------------------------------------------------------------


class TestSaveAttachments:
    def test_success_returns_saved_count(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.return_value = 2

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is True
        assert result["saved"] == 2
        assert result["directory"] == str(tmp_path)
        mock_logger.log_operation.assert_called_once()

    def test_directory_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        missing = tmp_path / "does_not_exist"

        result = save_attachments("1", str(missing))

        assert result["success"] is False
        assert result["error_type"] == "directory_not_found"
        mock_mail.save_attachments.assert_not_called()

    def test_path_is_file_not_directory(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        file_path = tmp_path / "a.txt"
        file_path.write_text("x")

        result = save_attachments("1", str(file_path))

        assert result["success"] is False
        assert result["error_type"] == "invalid_directory"
        mock_mail.save_attachments.assert_not_called()

    def test_connector_value_error_maps_to_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.side_effect = ValueError("bad index")

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_message_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.side_effect = MailMessageNotFoundError("x")

        result = save_attachments("999", str(tmp_path))

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        mock_mail.save_attachments.side_effect = RuntimeError("boom")

        result = save_attachments("1", str(tmp_path))

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 11. create_mailbox
# ---------------------------------------------------------------------------


class TestCreateMailbox:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.create_mailbox.return_value = True

        result = create_mailbox("Gmail", "Projects", parent_mailbox="Work")

        assert result["success"] is True
        assert result["account"] == "Gmail"
        assert result["mailbox"] == "Projects"
        assert result["parent"] == "Work"
        mock_mail.create_mailbox.assert_called_once_with(
            account="Gmail", name="Projects", parent_mailbox="Work"
        )

    def test_empty_name_validation_error(self, mock_mail: MagicMock) -> None:
        result = create_mailbox("Gmail", "")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_mailbox.assert_not_called()

    def test_whitespace_only_name_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        result = create_mailbox("Gmail", "   ")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_mailbox.assert_not_called()

    def test_account_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.create_mailbox.side_effect = MailAccountNotFoundError("x")

        result = create_mailbox("Bogus", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"

    def test_connector_value_error_maps_to_validation_error(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.create_mailbox.side_effect = ValueError("bad name")

        result = create_mailbox("Gmail", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_applescript_error(self, mock_mail: MagicMock) -> None:
        mock_mail.create_mailbox.side_effect = MailAppleScriptError("fail")

        result = create_mailbox("Gmail", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "applescript_error"

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock
    ) -> None:
        mock_mail.create_mailbox.side_effect = RuntimeError("boom")

        result = create_mailbox("Gmail", "Proj")

        assert result["success"] is False
        assert result["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 12. delete_messages
# ---------------------------------------------------------------------------


class TestDeleteMessages:
    def test_success(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.return_value = 2

        result = delete_messages(["1", "2"], permanent=False)

        assert result["success"] is True
        assert result["count"] == 2
        assert result["permanent"] is False
        mock_mail.delete_messages.assert_called_once_with(
            message_ids=["1", "2"],
            permanent=False,
            skip_bulk_check=False,
            account=None,
            source_mailbox=None,
        )

    def test_passes_source_mailbox_through(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.return_value = 1
        delete_messages(["1"], account="Gmail", source_mailbox="INBOX")
        mock_mail.delete_messages.assert_called_once_with(
            message_ids=["1"],
            permanent=False,
            skip_bulk_check=False,
            account="Gmail",
            source_mailbox="INBOX",
        )

    def test_empty_list_early_exit(self, mock_mail: MagicMock) -> None:
        result = delete_messages([])

        assert result["success"] is True
        assert result["count"] == 0
        mock_mail.delete_messages.assert_not_called()

    def test_over_limit_validation_error(self, mock_mail: MagicMock) -> None:
        result = delete_messages([str(i) for i in range(101)])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.delete_messages.assert_not_called()

    def test_value_error_from_connector(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.side_effect = ValueError("bad")

        result = delete_messages(["1"])

        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_message_not_found(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.side_effect = MailMessageNotFoundError("x")

        result = delete_messages(["999"])

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"

    def test_unexpected_exception_maps_to_unknown(self, mock_mail: MagicMock) -> None:
        mock_mail.delete_messages.side_effect = RuntimeError("boom")

        result = delete_messages(["1"])

        assert result["success"] is False
        assert result["error_type"] == "unknown"

    def test_permanent_true_threads_through_to_connector(
        self, mock_mail: MagicMock
    ) -> None:
        """Issue #111: the connector emits a DeprecationWarning when
        permanent=True; the server's job is just to forward the flag
        unchanged so the warning fires from the user's call frame."""
        mock_mail.delete_messages.return_value = 1
        result = delete_messages(["1"], permanent=True)
        assert result["success"] is True
        # Server still echoes the (now-meaningless) flag in its response
        # for backwards compatibility with existing callers.
        assert result["permanent"] is True
        mock_mail.delete_messages.assert_called_once_with(
            message_ids=["1"],
            permanent=True,
            skip_bulk_check=False,
            account=None,
            source_mailbox=None,
        )


# ---------------------------------------------------------------------------
# Rate limiting integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tight_limits() -> Any:
    """Monkeypatch TIER_LIMITS down to 2 calls/60s so we can trip them easily."""
    import apple_mail_mcp.security as sec
    original = sec.TIER_LIMITS.copy()
    sec.TIER_LIMITS.update({
        "cheap_reads": (2, 60.0),
        "expensive_ops": (2, 60.0),
        "sends": (2, 60.0),
    })
    yield
    sec.TIER_LIMITS.update(original)


# ---------------------------------------------------------------------------
# Email templates (#30)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_templates(tmp_path: Any, monkeypatch: Any) -> Any:
    """Redirect template storage to a tmp dir for the duration of the test."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    return tmp_path / "templates"


class TestListTemplates:
    def test_empty_when_no_templates(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        result = list_templates()
        assert result == {"success": True, "templates": [], "count": 0}

    def test_returns_saved_templates_sorted(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        save_template(name="zebra", body="z\n", subject="Z")
        save_template(name="alpha", body="a\n")
        result = list_templates()
        assert result["count"] == 2
        assert [t["name"] for t in result["templates"]] == ["alpha", "zebra"]
        assert result["templates"][1]["subject"] == "Z"
        assert result["templates"][0]["subject"] is None


class TestGetTemplate:
    def test_returns_template_and_placeholders(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        save_template(
            name="t1",
            body="Hi {recipient_name}, today is {today}.\n",
            subject="Re: {original_subject}",
        )
        result = get_template("t1")
        assert result["success"] is True
        assert result["name"] == "t1"
        assert result["subject"] == "Re: {original_subject}"
        assert result["body"] == "Hi {recipient_name}, today is {today}.\n"
        assert result["placeholders"] == [
            "original_subject",
            "recipient_name",
            "today",
        ]

    def test_missing_returns_typed_error(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        result = get_template("missing")
        assert result["success"] is False
        assert result["error_type"] == "template_not_found"

    def test_invalid_name_returns_typed_error(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        result = get_template("../etc/passwd")
        assert result["success"] is False
        assert result["error_type"] == "invalid_template_name"


class TestSaveTemplate:
    def test_create_returns_created_true(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        result = save_template(name="new", body="hi\n")
        assert result == {"success": True, "name": "new", "created": True}

    def test_overwrite_returns_created_false(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        save_template(name="x", body="v1\n")
        result = save_template(name="x", body="v2\n")
        assert result == {"success": True, "name": "x", "created": False}

    def test_empty_body_rejected(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        result = save_template(name="x", body="   ")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_invalid_name_returns_typed_error(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        result = save_template(name="bad name with spaces", body="ok\n")
        assert result["success"] is False
        assert result["error_type"] == "invalid_template_name"

    def test_normalizes_missing_trailing_newline(
        self, isolated_templates: Any, mock_logger: MagicMock
    ) -> None:
        save_template(name="x", body="no trailing newline")
        loaded = get_template("x")
        assert loaded["body"].endswith("\n")


class TestDeleteTemplate:
    async def test_success_with_accepted_ctx(
        self,
        isolated_templates: Any,
        mock_ctx_accept: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        save_template(name="goner", body="bye\n")
        result = await delete_template("goner", ctx=mock_ctx_accept)
        assert result == {"success": True, "name": "goner"}
        mock_ctx_accept.elicit.assert_awaited_once()
        # Confirm it was actually deleted from disk:
        assert get_template("goner")["error_type"] == "template_not_found"

    async def test_decline_returns_cancelled(
        self,
        isolated_templates: Any,
        mock_ctx_decline: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        save_template(name="keep", body="x\n")
        result = await delete_template("keep", ctx=mock_ctx_decline)
        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        # Still on disk:
        assert get_template("keep")["success"] is True

    async def test_nonexistent_skips_elicit(
        self,
        isolated_templates: Any,
        mock_ctx_accept: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        # Confirm we don't bother the user when the template doesn't exist.
        result = await delete_template("never-existed", ctx=mock_ctx_accept)
        assert result["success"] is False
        assert result["error_type"] == "template_not_found"
        mock_ctx_accept.elicit.assert_not_awaited()


class TestRenderTemplate:
    def test_renders_with_user_supplied_vars_only(
        self,
        isolated_templates: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        # No message_id — auto_template_vars returns just {today: ...}.
        mock_mail.auto_template_vars.return_value = {"today": "2026-04-25"}
        save_template(
            name="r",
            body="Hi {name}, today is {today}.\n",
        )
        result = render_template(name="r", vars={"name": "Alice"})
        assert result["success"] is True
        assert result["subject"] is None
        assert result["body"] == "Hi Alice, today is 2026-04-25.\n"
        assert result["used_vars"] == {"today": "2026-04-25", "name": "Alice"}

    def test_uses_message_id_for_auto_fills(
        self,
        isolated_templates: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        mock_mail.auto_template_vars.return_value = {
            "today": "2026-04-25",
            "recipient_name": "Bob Builder",
            "recipient_email": "bob@example.com",
            "original_subject": "Project X",
        }
        save_template(
            name="reply",
            subject="Re: {original_subject}",
            body="Hi {recipient_name},\nThanks for your note.\n",
        )
        result = render_template(name="reply", message_id="abc-123")
        mock_mail.auto_template_vars.assert_called_once_with("abc-123")
        assert result["subject"] == "Re: Project X"
        assert result["body"].startswith("Hi Bob Builder")

    def test_user_vars_override_auto_fills(
        self,
        isolated_templates: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        mock_mail.auto_template_vars.return_value = {
            "today": "2026-04-25",
            "recipient_name": "Auto Name",
        }
        save_template(name="t", body="Hello {recipient_name}.\n")
        result = render_template(
            name="t", message_id="x", vars={"recipient_name": "Override"}
        )
        assert "Override" in result["body"]
        assert "Auto Name" not in result["body"]

    def test_missing_var_returns_typed_error(
        self,
        isolated_templates: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        mock_mail.auto_template_vars.return_value = {"today": "x"}
        save_template(name="t", body="Need {something_else}.\n")
        result = render_template(name="t")
        assert result["success"] is False
        assert result["error_type"] == "missing_template_variable"
        assert "something_else" in result["error"]

    def test_template_not_found_returns_typed_error(
        self,
        isolated_templates: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        result = render_template(name="never-existed")
        assert result["success"] is False
        assert result["error_type"] == "template_not_found"


# ---------------------------------------------------------------------------
# create_draft / update_draft / delete_draft (drafts lifecycle, #134)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_drafts(monkeypatch: Any, tmp_path: Any) -> Any:
    """Point ~/.apple_mail_mcp/drafts/ at a tmp dir for the test."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    return tmp_path


class TestCreateDraftTool:
    @pytest.fixture(autouse=True)
    def stub_security(self, monkeypatch: Any) -> None:
        # Default: safety + rate-limit pass; recipient-validation passes.
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_test_mode_safety",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_rate_limit",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_mcp.server.validate_send_operation",
            lambda *a, **kw: (True, None),
        )

    @pytest.mark.asyncio
    async def test_save_fresh_draft_returns_id(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.return_value = {
            "draft_id": "161055", "sent_message_id": ""
        }
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x"
        )
        assert result["success"] is True
        assert result["draft_id"] == "161055"
        mock_mail.create_draft.assert_called_once()
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "new"
        assert kwargs["seed_id"] is None
        assert kwargs["send_now"] is False

    @pytest.mark.asyncio
    async def test_reply_to_routes_to_reply_seed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.return_value = {
            "draft_id": "161056", "sent_message_id": ""
        }
        result = await create_draft(reply_to="160989", body="thanks")
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "reply"
        assert kwargs["seed_id"] == "160989"

    @pytest.mark.asyncio
    async def test_forward_of_routes_to_forward_seed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.return_value = {
            "draft_id": "161057", "sent_message_id": ""
        }
        result = await create_draft(
            forward_of="160989", to=["x@example.com"], body="fyi"
        )
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "forward"
        assert kwargs["seed_id"] == "160989"

    @pytest.mark.asyncio
    async def test_mutually_exclusive_seeds_rejected(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        result = await create_draft(reply_to="1", forward_of="2")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_template_vars_without_template_name_rejected(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        result = await create_draft(template_vars={"x": "y"})
        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    @pytest.mark.asyncio
    async def test_fresh_seed_requires_to(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        result = await create_draft(subject="hi", body="x")
        assert result["success"] is False
        assert "'to'" in result["error"]

    @pytest.mark.asyncio
    async def test_fresh_seed_requires_subject(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        result = await create_draft(to=["a@example.com"], body="x")
        assert result["success"] is False
        assert "'subject'" in result["error"]

    @pytest.mark.asyncio
    async def test_send_now_elicits_and_sends(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.return_value = {
            "draft_id": "", "sent_message_id": ""
        }
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            send_now=True, ctx=mock_ctx_accept,
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["send_now"] is True

    @pytest.mark.asyncio
    async def test_send_now_declined_blocks_send(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_decline: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            send_now=True, ctx=mock_ctx_decline,
        )
        assert result["success"] is False
        assert result["error_type"] == "cancelled"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_draft_state_persisted_for_reply_seed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.drafts import DraftStateStore
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.return_value = {
            "draft_id": "161056", "sent_message_id": ""
        }
        await create_draft(reply_to="160989", body="thanks", reply_all=True)
        store = DraftStateStore()
        seed = store.get_seed("161056")
        assert seed is not None
        assert seed.seed_kind == "reply"
        assert seed.seed_id == "160989"
        assert seed.reply_all is True

    @pytest.mark.asyncio
    async def test_draft_state_not_persisted_for_send_now(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.drafts import DraftStateStore
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.return_value = {
            "draft_id": "", "sent_message_id": ""
        }
        await create_draft(
            reply_to="160989", body="thanks", send_now=True,
            ctx=mock_ctx_accept,
        )
        store = DraftStateStore()
        # send_now=True returns empty draft_id, nothing to persist.
        # Sanity: nothing in state dir.
        assert list(store.root.iterdir()) == [] if store.root.is_dir() else True


class TestUpdateDraftTool:
    @pytest.fixture(autouse=True)
    def stub_security(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_test_mode_safety",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_rate_limit",
            lambda *a, **kw: None,
        )

    @pytest.mark.asyncio
    async def test_update_uses_disk_seed_state(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord
        from apple_mail_mcp.server import update_draft

        store = DraftStateStore()
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="reply", seed_id="160000", reply_all=True),
        )

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "Re: hello", "body": "old body",
            "in_reply_to": "<orig@x>", "references": "<orig@x>",
            "attachment_names": [],
        }
        mock_mail.delete_draft.return_value = True
        mock_mail.create_draft.return_value = {
            "draft_id": "161000", "sent_message_id": ""
        }

        result = await update_draft(draft_id="160991", body="new body")
        assert result["success"] is True
        assert result["draft_id"] == "161000"
        # Disk state used directly, no fallback lookup.
        mock_mail.find_message_by_message_id.assert_not_called()
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "reply"
        assert kwargs["seed_id"] == "160000"
        assert kwargs["reply_all"] is True
        assert kwargs["body"] == "new body"
        # Recipients preserved from existing state.
        assert kwargs["to"] == ["alice@example.com"]

    @pytest.mark.asyncio
    async def test_update_falls_back_to_in_reply_to(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        # No disk state. Must fall back to In-Reply-To header lookup.
        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "Re: hi", "body": "",
            "in_reply_to": "<orig@x>", "references": "<orig@x>",
            "attachment_names": [],
        }
        mock_mail.find_message_by_message_id.return_value = "999000"
        mock_mail.create_draft.return_value = {
            "draft_id": "161000", "sent_message_id": ""
        }

        result = await update_draft(draft_id="160991", body="patched")
        assert result["success"] is True
        mock_mail.find_message_by_message_id.assert_called_once_with(
            "<orig@x>"
        )
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "reply"
        assert kwargs["seed_id"] == "999000"

    @pytest.mark.asyncio
    async def test_update_treats_as_fresh_when_no_seed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["alice@example.com"], "cc": [], "bcc": [],
            "subject": "fresh", "body": "old",
            "in_reply_to": "", "references": "",
            "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "161000", "sent_message_id": ""
        }

        result = await update_draft(draft_id="160991", body="new")
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["seed"] == "new"
        assert kwargs["seed_id"] is None
        # Fallback NOT called when there's no In-Reply-To.
        mock_mail.find_message_by_message_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_preserves_attachments_when_paths_none(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["a@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "",
            "attachment_names": ["report.pdf"],
        }
        # Mock the extraction to return a fake path; verify update_draft
        # passes that path through to create_draft.
        fake_path = tmp_path / "extracted.pdf"
        fake_path.write_bytes(b"%PDF")
        mock_mail.extract_draft_attachments.return_value = [fake_path]
        mock_mail.create_draft.return_value = {
            "draft_id": "161000", "sent_message_id": ""
        }

        result = await update_draft(draft_id="160991", body="patched")
        assert result["success"] is True
        mock_mail.extract_draft_attachments.assert_called_once()
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["attachment_paths"] == [fake_path]

    @pytest.mark.asyncio
    async def test_update_clears_attachments_when_empty_list(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["a@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "",
            "attachment_names": ["report.pdf"],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "161000", "sent_message_id": ""
        }

        result = await update_draft(draft_id="160991", attachment_paths=[])
        assert result["success"] is True
        # Existing attachments NOT extracted (caller cleared explicitly).
        mock_mail.extract_draft_attachments.assert_not_called()
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["attachment_paths"] == []

    @pytest.mark.asyncio
    async def test_update_returns_new_draft_id(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["a@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "",
            "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "999999", "sent_message_id": ""
        }

        result = await update_draft(draft_id="160991", body="new")
        # Critical contract: the returned draft_id is the NEW one.
        assert result["draft_id"] == "999999"
        assert result["draft_id"] != "160991"

    @pytest.mark.asyncio
    async def test_update_send_now_elicits(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["a@example.com"], "cc": [], "bcc": [],
            "subject": "hi", "body": "x",
            "in_reply_to": "", "references": "",
            "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "", "sent_message_id": ""
        }

        result = await update_draft(
            draft_id="160991", send_now=True, ctx=mock_ctx_accept
        )
        assert result["success"] is True
        mock_ctx_accept.elicit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_persists_new_seed_state(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord
        from apple_mail_mcp.server import update_draft

        store = DraftStateStore()
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="reply", seed_id="160000", reply_all=False),
        )

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991",
            "to": ["a@example.com"], "cc": [], "bcc": [],
            "subject": "Re: x", "body": "",
            "in_reply_to": "<orig@x>", "references": "<orig@x>",
            "attachment_names": [],
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "999999", "sent_message_id": ""
        }

        await update_draft(draft_id="160991", body="patched")
        # Old draft's state cleared, new draft's state persisted under
        # the new id.
        assert store.get_seed("160991") is None
        new_seed = store.get_seed("999999")
        assert new_seed is not None
        assert new_seed.seed_id == "160000"

    @pytest.mark.asyncio
    async def test_update_template_vars_without_template_name_rejected(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        result = await update_draft(
            draft_id="160991", template_vars={"x": "y"}
        )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_mail.get_draft_state.assert_not_called()


class TestDeleteDraftTool:
    def test_success(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import delete_draft

        mock_mail.delete_draft.return_value = True
        result = delete_draft(draft_id="160991")
        assert result["success"] is True
        assert result["draft_id"] == "160991"
        mock_mail.delete_draft.assert_called_once_with("160991")

    def test_clears_disk_state(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord
        from apple_mail_mcp.server import delete_draft

        store = DraftStateStore()
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="forward", seed_id="999"),
        )
        assert store.get_seed("160991") is not None

        mock_mail.delete_draft.return_value = True
        delete_draft(draft_id="160991")
        assert store.get_seed("160991") is None

    def test_not_found_returns_typed_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftNotFoundError
        from apple_mail_mcp.server import delete_draft

        mock_mail.delete_draft.side_effect = MailDraftNotFoundError(
            "no draft with id '999'"
        )
        result = delete_draft(draft_id="999")
        assert result["success"] is False
        assert result["error_type"] == "draft_not_found"

    def test_invalid_id_returns_typed_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftInvalidIdError
        from apple_mail_mcp.server import delete_draft

        mock_mail.delete_draft.side_effect = MailDraftInvalidIdError(
            "draft_id '../escape' must match ..."
        )
        result = delete_draft(draft_id="../escape")
        assert result["success"] is False
        assert result["error_type"] == "invalid_draft_id"


class TestDraftToolErrorPaths:
    """Coverage for the error-handling branches of all three draft tools.

    These are tedious but each one corresponds to a real
    response-shape contract that callers depend on for branching
    (account_not_found vs file_not_found vs unknown, etc.)."""

    @pytest.fixture(autouse=True)
    def stub_security(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_test_mode_safety",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_rate_limit",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "apple_mail_mcp.server.validate_send_operation",
            lambda *a, **kw: (True, None),
        )

    # ------------------------------------------------------------------
    # create_draft
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_draft_seed_not_found(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.side_effect = MailMessageNotFoundError(
            "no message"
        )
        result = await create_draft(reply_to="999", body="x")
        assert result["error_type"] == "message_not_found"

    @pytest.mark.asyncio
    async def test_create_draft_account_not_found(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.side_effect = MailAccountNotFoundError(
            "no account 'Bogus'"
        )
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            from_account="Bogus",
        )
        assert result["error_type"] == "account_not_found"

    @pytest.mark.asyncio
    async def test_create_draft_file_not_found(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.side_effect = FileNotFoundError(
            "attachment missing"
        )
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            attachment_paths=["/nonexistent/x.pdf"],
        )
        assert result["error_type"] == "file_not_found"

    @pytest.mark.asyncio
    async def test_create_draft_applescript_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.side_effect = MailAppleScriptError(
            "osascript failed"
        )
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x"
        )
        assert result["error_type"] == "applescript_error"

    @pytest.mark.asyncio
    async def test_create_draft_unknown_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        mock_mail.create_draft.side_effect = RuntimeError("boom")
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x"
        )
        assert result["error_type"] == "unknown"

    @pytest.mark.asyncio
    async def test_create_draft_template_error_returns_typed(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        # No template stored at this name → template_not_found.
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            template_name="never-existed",
        )
        assert result["error_type"] == "template_not_found"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_draft_send_now_safety_gate_blocks(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        monkeypatch.setattr(
            "apple_mail_mcp.server.check_test_mode_safety",
            lambda *a, **kw: {
                "success": False, "error": "blocked",
                "error_type": "safety_violation",
            },
        )
        result = await create_draft(
            to=["real@example.com"], subject="hi", body="x",
            send_now=True, ctx=mock_ctx_accept,
        )
        assert result["error_type"] == "safety_violation"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_draft_send_now_rate_limit_blocks(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        monkeypatch.setattr(
            "apple_mail_mcp.server.check_rate_limit",
            lambda *a, **kw: {
                "success": False, "error": "rate-limited",
                "error_type": "rate_limit",
            },
        )
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            send_now=True, ctx=mock_ctx_accept,
        )
        assert result["error_type"] == "rate_limit"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_draft_send_now_validation_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import create_draft

        monkeypatch.setattr(
            "apple_mail_mcp.server.validate_send_operation",
            lambda *a, **kw: (False, "too many recipients"),
        )
        result = await create_draft(
            to=["a@example.com"], subject="hi", body="x",
            send_now=True, ctx=mock_ctx_accept,
        )
        assert result["error_type"] == "validation_error"
        mock_mail.create_draft.assert_not_called()

    # ------------------------------------------------------------------
    # update_draft
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_update_draft_not_found(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.exceptions import MailDraftNotFoundError
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.side_effect = MailDraftNotFoundError(
            "no draft"
        )
        result = await update_draft(draft_id="160991", body="x")
        assert result["error_type"] == "draft_not_found"

    @pytest.mark.asyncio
    async def test_update_draft_template_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": [], "cc": [], "bcc": [],
            "subject": "", "body": "", "in_reply_to": "",
            "references": "", "attachment_names": [],
        }
        result = await update_draft(
            draft_id="160991", template_name="never-existed"
        )
        assert result["error_type"] == "template_not_found"

    @pytest.mark.asyncio
    async def test_update_draft_send_now_safety_gate_blocks(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": ["real@gmail.com"],
            "cc": [], "bcc": [], "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_test_mode_safety",
            lambda *a, **kw: {
                "success": False, "error": "blocked",
                "error_type": "safety_violation",
            },
        )
        result = await update_draft(
            draft_id="160991", send_now=True, ctx=mock_ctx_accept
        )
        assert result["error_type"] == "safety_violation"
        mock_mail.create_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_draft_send_now_rate_limit_blocks(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        monkeypatch: Any,
        mock_ctx_accept: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": ["a@example.com"],
            "cc": [], "bcc": [], "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        monkeypatch.setattr(
            "apple_mail_mcp.server.check_rate_limit",
            lambda *a, **kw: {
                "success": False, "error": "limit",
                "error_type": "rate_limit",
            },
        )
        result = await update_draft(
            draft_id="160991", send_now=True, ctx=mock_ctx_accept
        )
        assert result["error_type"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_update_draft_send_now_declined(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        mock_ctx_decline: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": ["a@example.com"],
            "cc": [], "bcc": [], "subject": "x", "body": "y",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        result = await update_draft(
            draft_id="160991", send_now=True, ctx=mock_ctx_decline
        )
        assert result["error_type"] == "cancelled"
        mock_mail.delete_draft.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_draft_applescript_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": [], "cc": [], "bcc": [],
            "subject": "", "body": "", "in_reply_to": "",
            "references": "", "attachment_names": [],
        }
        mock_mail.create_draft.side_effect = MailAppleScriptError("boom")
        result = await update_draft(draft_id="160991", body="x")
        assert result["error_type"] == "applescript_error"

    @pytest.mark.asyncio
    async def test_update_draft_unknown_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.side_effect = RuntimeError("boom")
        result = await update_draft(draft_id="160991", body="x")
        assert result["error_type"] == "unknown"

    @pytest.mark.asyncio
    async def test_update_draft_tempdir_cleaned_up(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
        tmp_path: Any,
        monkeypatch: Any,
    ) -> None:
        """When extraction populates tempdir, it must be cleaned up
        even on a downstream failure."""
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": ["a@example.com"],
            "cc": [], "bcc": [], "subject": "x", "body": "y",
            "in_reply_to": "", "references": "",
            "attachment_names": ["report.pdf"],
        }
        # Track which tempdirs get created so we can verify cleanup.
        created_dirs: list[str] = []
        original = __import__("tempfile").TemporaryDirectory

        def tracking_tempdir(*args: Any, **kwargs: Any) -> Any:
            td = original(*args, **kwargs)
            created_dirs.append(td.name)
            return td

        monkeypatch.setattr(
            "apple_mail_mcp.server.tempfile.TemporaryDirectory",
            tracking_tempdir,
        )

        # Have extract create a real file so the path is non-empty.
        from pathlib import Path

        def fake_extract(
            draft_id: str, names: list[str], dest: Path
        ) -> list[Path]:
            (dest / "0").mkdir(parents=True, exist_ok=True)
            p = dest / "0" / "report.pdf"
            p.write_bytes(b"x")
            return [p]

        mock_mail.extract_draft_attachments.side_effect = fake_extract
        # Force a failure AFTER extraction so the finally block runs.
        mock_mail.create_draft.side_effect = RuntimeError("boom")

        await update_draft(draft_id="160991", body="x")

        assert created_dirs, "tempdir was never created"
        # All tempdirs must be cleaned up.
        for d in created_dirs:
            from pathlib import Path as _Path
            assert not _Path(d).exists(), f"tempdir {d} not cleaned up"

    # ------------------------------------------------------------------
    # delete_draft
    # ------------------------------------------------------------------

    def test_delete_draft_applescript_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import delete_draft

        mock_mail.delete_draft.side_effect = MailAppleScriptError("boom")
        result = delete_draft(draft_id="160991")
        assert result["error_type"] == "applescript_error"

    def test_delete_draft_unknown_error(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        from apple_mail_mcp.server import delete_draft

        mock_mail.delete_draft.side_effect = RuntimeError("boom")
        result = delete_draft(draft_id="160991")
        assert result["error_type"] == "unknown"

    @pytest.mark.asyncio
    async def test_update_draft_delete_step_not_found(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """If the draft is somehow deleted between get_draft_state and
        the orchestrator's own delete_draft call, return a typed error
        rather than crash."""
        from apple_mail_mcp.exceptions import MailDraftNotFoundError
        from apple_mail_mcp.server import update_draft

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": [], "cc": [], "bcc": [],
            "subject": "", "body": "", "in_reply_to": "",
            "references": "", "attachment_names": [],
        }
        mock_mail.delete_draft.side_effect = MailDraftNotFoundError("gone")
        result = await update_draft(draft_id="160991", body="x")
        assert result["error_type"] == "draft_not_found"

    @pytest.mark.asyncio
    async def test_update_draft_template_success_renders(
        self,
        isolated_drafts: Any,
        mock_mail: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """Template renders subject + body when caller didn't supply them."""
        from apple_mail_mcp.server import update_draft
        from apple_mail_mcp.templates import Template, TemplateStore

        # Write a template the renderer can pick up.
        store = TemplateStore()
        store.save(Template(
            name="patch", subject="[patched] {today}",
            body="Hi {recipient_name},\n\nUpdated.\n",
        ))

        mock_mail.get_draft_state.return_value = {
            "draft_id": "160991", "to": ["a@example.com"],
            "cc": [], "bcc": [], "subject": "old", "body": "old",
            "in_reply_to": "", "references": "", "attachment_names": [],
        }
        mock_mail.auto_template_vars.return_value = {
            "today": "2026-05-08",
            "recipient_name": "alice",
        }
        mock_mail.create_draft.return_value = {
            "draft_id": "999", "sent_message_id": "",
        }

        result = await update_draft(
            draft_id="160991", template_name="patch"
        )
        assert result["success"] is True
        kwargs = mock_mail.create_draft.call_args.kwargs
        assert kwargs["subject"].startswith("[patched]")
        assert "Hi alice" in kwargs["body"]


class TestConnectorCreateDraftEdgeCase:
    """Connector-layer error path that the server tests don't reach."""

    @pytest.fixture
    def connector(self) -> Any:
        from apple_mail_mcp.mail_connector import AppleMailConnector
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_mcp.mail_connector.AppleMailConnector._run_applescript")
    def test_applescript_error_not_seed_not_found_propagates(
        self, mock_run: MagicMock, connector: Any
    ) -> None:
        """A non-SEED_NOT_FOUND AppleScript error in create_draft must
        propagate as MailAppleScriptError (the typed exception path
        below the SEED_NOT_FOUND branch)."""
        mock_run.side_effect = MailAppleScriptError("Mail.app crashed")
        with pytest.raises(MailAppleScriptError, match="Mail.app crashed"):
            connector.create_draft(
                seed="reply", seed_id="160000", body="x"
            )
