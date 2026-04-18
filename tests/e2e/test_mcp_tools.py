"""End-to-end tests for MCP tool registration and invocation.

These tests exercise the full FastMCP dispatch layer in-process: they
enumerate tools via mcp.list_tools() and invoke them via mcp.call_tool().
The mail connector is mocked; no AppleScript runs.

MAIL_TEST_MODE is disabled per-test so the safety gate does not interfere
with mocked dispatch. These tests verify MCP wiring, not safety behavior
(safety is covered by tests/unit/test_security.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apple_mail_mcp import server

pytestmark = pytest.mark.e2e

EXPECTED_TOOLS = {
    "list_mailboxes",
    "search_messages",
    "get_message",
    "send_email",
    "mark_as_read",
    "send_email_with_attachments",
    "get_attachments",
    "save_attachments",
    "move_messages",
    "flag_message",
    "create_mailbox",
    "delete_messages",
    "reply_to_message",
    "forward_message",
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

    async def test_list_accounts_not_exposed_as_tool(self) -> None:
        """list_accounts exists on the connector but is intentionally not an MCP tool.

        This mirrors the known warning from scripts/check_client_server_parity.sh.
        If list_accounts is ever exposed, update check_client_server_parity.sh
        AND delete this test.
        """
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert "list_accounts" not in names

    async def test_every_tool_has_description(self) -> None:
        tools = await server.mcp.list_tools()
        missing = [t.name for t in tools if not (t.description and t.description.strip())]
        assert not missing, f"tools missing description: {missing}"

    @pytest.mark.parametrize(
        "tool_name,expected_required",
        [
            ("send_email", {"to", "subject", "body"}),
            ("search_messages", {"account"}),
            ("move_messages", {"message_ids", "account", "destination_mailbox"}),
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
