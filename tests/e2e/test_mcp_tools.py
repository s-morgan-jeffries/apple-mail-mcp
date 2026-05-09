"""End-to-end tests for MCP tool registration and invocation.

These tests exercise the full FastMCP dispatch layer in-process: they
enumerate tools via mcp.list_tools() and invoke them via mcp.call_tool().
The mail connector is mocked; no AppleScript runs.

MAIL_TEST_MODE is disabled per-test so the safety gate does not interfere
with mocked dispatch. These tests verify MCP wiring, not safety behavior
(safety is covered by tests/unit/test_security.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apple_mail_mcp import server

pytestmark = pytest.mark.e2e

EXPECTED_TOOLS = {
    # Discovery
    "list_accounts",
    "list_mailboxes",
    "list_rules",
    "search_messages",
    "get_messages",
    "get_thread",
    # Drafts lifecycle (#134)
    "create_draft",
    "update_draft",
    "delete_draft",
    # Mutations
    "update_message",
    "save_attachments",
    "create_mailbox",
    "delete_messages",
    # Rule CRUD (#63)
    "create_rule",
    "update_rule",
    "delete_rule",
    # Templates (#30)
    "list_templates",
    "get_template",
    "save_template",
    "delete_template",
    "render_template",
}


@pytest.fixture(autouse=True)
def _disable_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable MAIL_TEST_MODE so the safety gate does not interfere.

    The connector is mocked, so destructive operations cannot reach Mail.app.
    """
    monkeypatch.setenv("MAIL_TEST_MODE", "false")


@pytest.fixture
def mock_mail(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the module-level mail connector with a MagicMock."""
    mock = MagicMock()
    monkeypatch.setattr(server, "mail", mock)
    return mock


class TestToolRegistration:
    """Verify tools are registered with correct names and schemas."""

    async def test_expected_tool_names_registered(self) -> None:
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS

    async def test_every_tool_has_description(self) -> None:
        tools = await server.mcp.list_tools()
        missing = [t.name for t in tools if not (t.description and t.description.strip())]
        assert not missing, f"tools missing description: {missing}"

    @pytest.mark.parametrize(
        "tool_name,expected_required",
        [
            ("update_message", {"message_ids"}),
            ("delete_draft", {"draft_id"}),
        ],
    )
    async def test_tool_schema_required_fields(
        self, tool_name: str, expected_required: set[str]
    ) -> None:
        tool = await server.mcp.get_tool(tool_name)
        schema = tool.parameters
        assert schema["type"] == "object"
        required = set(schema.get("required", []))
        # Tool may have additional required fields beyond what we check; we
        # only assert the subset that must always be required.
        missing = expected_required - required
        assert not missing, (
            f"{tool_name} missing required fields {missing}; "
            f"actual required: {required}"
        )


# Sentinels replaced at test-time with values derived from tmp_path. Needed
# because parametrize is evaluated at collection time and cannot reference
# per-test fixtures directly.
_TMP_DIR = "__TMP_DIR__"
_TMP_FILE = "__TMP_FILE__"


# (tool_name, call_args, connector_method, connector_return_value)
INVOCATION_CASES: list[tuple[str, dict[str, Any], str, Any]] = [
    (
        "list_accounts",
        {},
        "list_accounts",
        [{"id": "UUID-1", "name": "Gmail",
          "email_addresses": ["me@gmail.com"],
          "account_type": "imap", "enabled": True}],
    ),
    (
        "list_rules",
        {},
        "list_rules",
        [{"name": "Junk filter", "enabled": True}],
    ),
    (
        "list_mailboxes",
        {"account": "TestAccount"},
        "list_mailboxes",
        ["INBOX", "Sent"],
    ),
    (
        "search_messages",
        {"account": "TestAccount"},
        "search_messages",
        [],
    ),
    (
        "get_messages",
        {"message_ids": ["msg-1"]},
        "get_message",
        {"id": "msg-1", "subject": "s", "from": "a@example.com"},
    ),
    (
        "get_thread",
        {"message_id": "msg-1"},
        "get_thread",
        [{"id": "msg-1", "subject": "Q3", "sender": "a@b",
          "date_received": "Mon", "read_status": True, "flagged": False}],
    ),
    (
        "create_draft",
        {"to": ["a@example.com"], "subject": "s", "body": "b"},
        "create_draft",
        {"draft_id": "draft-1", "sent_message_id": ""},
    ),
    (
        "delete_draft",
        {"draft_id": "draft-1"},
        "delete_draft",
        True,
    ),
    (
        "update_message",
        {"message_ids": ["msg-1"], "read_status": True},
        "update_message",
        1,
    ),
    (
        "save_attachments",
        {"message_id": "msg-1", "save_directory": _TMP_DIR},
        "save_attachments",
        0,
    ),
    (
        "create_mailbox",
        {"account": "TestAccount", "name": "NewBox"},
        "create_mailbox",
        True,
    ),
    (
        "delete_messages",
        {"message_ids": ["msg-1"]},
        "delete_messages",
        1,
    ),
]


class TestToolInvocation:
    """Invoke each tool via mcp.call_tool and verify structured response shape."""

    @pytest.mark.parametrize(
        "tool_name,call_args,connector_method,connector_return",
        INVOCATION_CASES,
        ids=lambda p: p if isinstance(p, str) else None,
    )
    async def test_tool_invocation_happy_path(
        self,
        mock_mail: MagicMock,
        tmp_path: Path,
        tool_name: str,
        call_args: dict[str, Any],
        connector_method: str,
        connector_return: Any,
    ) -> None:
        # Materialize tmp_path-dependent sentinels. save_attachments requires
        # the directory to exist; send_email_with_attachments requires each
        # attachment path to exist on disk.
        tmp_file = tmp_path / "attachment.txt"
        resolved_args: dict[str, Any] = {}
        for key, value in call_args.items():
            if value == _TMP_DIR:
                resolved_args[key] = str(tmp_path)
            elif isinstance(value, list) and _TMP_FILE in value:
                tmp_file.write_text("dummy")
                resolved_args[key] = [
                    str(tmp_file) if item == _TMP_FILE else item for item in value
                ]
            else:
                resolved_args[key] = value

        getattr(mock_mail, connector_method).return_value = connector_return

        result = await server.mcp.call_tool(tool_name, resolved_args)

        assert result.structured_content is not None
        assert result.structured_content["success"] is True
        assert "error" not in result.structured_content
        getattr(mock_mail, connector_method).assert_called_once()
