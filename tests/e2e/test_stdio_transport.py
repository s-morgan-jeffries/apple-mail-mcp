"""Smoke tests for the MCP server over the real stdio transport.

These tests spawn the server as a subprocess and connect via the MCP client
SDK. They catch a different class of bug than tests/e2e/test_mcp_tools.py:

- Startup errors (import failures, missing env, FastMCP banner interfering
  with stdout framing).
- JSON-RPC framing issues over pipes.
- Stream lifecycle bugs (handshake timeout, stream closure, premature EOF).

In-process FastMCP tests mock the transport — these tests don't.

Keep scope narrow: a handshake + list_tools round-trip is enough to cover
the transport layer. Per-tool behavior is the job of test_mcp_tools.py.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.e2e

EXPECTED_TOOLS = {
    "list_accounts",
    "list_rules",
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

# Per #50 acceptance: test must complete within 15 seconds.
HANDSHAKE_TIMEOUT_SECONDS = 15.0


async def _list_tools_over_stdio() -> set[str]:
    """Spawn the server, complete the MCP handshake, and return the tool names."""
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "apple_mail_mcp.server"],
        env=None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {t.name for t in result.tools}


async def test_stdio_subprocess_lists_all_tools() -> None:
    """The real stdio handshake surfaces all 14 tools a client would see.

    If this fails where test_mcp_tools.py passes, the bug is in the transport
    layer: banner output contaminating stdout, stream closure, JSON-RPC framing,
    or protocol-version negotiation.
    """
    names = await asyncio.wait_for(
        _list_tools_over_stdio(), timeout=HANDSHAKE_TIMEOUT_SECONDS
    )
    assert names == EXPECTED_TOOLS
