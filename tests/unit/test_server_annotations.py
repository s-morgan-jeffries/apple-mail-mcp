"""Tests for MCP tool annotations (#217).

Covers:

- The `_tool` decorator helper behavior (skip mutating tools in read-only mode,
  register otherwise, pass annotations through).
- Every tool registered on the production `mcp` instance has the expected
  `readOnlyHint` / `destructiveHint` / `idempotentHint` values per the
  classification table in the issue.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest

from apple_mail_mcp import server

# ---------------------------------------------------------------------------
# Classification — the source of truth that the tests below assert against.
# Keep aligned with the table in the #217 implementation plan.
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS: set[str] = {
    "list_accounts",
    "list_mailboxes",
    "list_rules",
    "list_templates",
    "search_messages",
    "get_messages",
    "get_thread",
    "get_attachment_content",
    "get_template",
    "render_template",
}

# Mutating tools, partitioned by destructiveHint.
DESTRUCTIVE_TOOLS: set[str] = {
    "update_message",
    "update_mailbox",
    "update_rule",
    "update_draft",
    "delete_draft",
    "delete_mailbox",
    "delete_messages",
    "delete_rule",
    "delete_template",
}

ADDITIVE_TOOLS: set[str] = {
    "create_mailbox",
    "create_draft",
    "create_rule",
    "save_template",
    "save_attachments",
}

MUTATING_TOOLS = DESTRUCTIVE_TOOLS | ADDITIVE_TOOLS

# Idempotent: same args → same end state.
NON_IDEMPOTENT_TOOLS: set[str] = {
    "create_draft",   # each call may create a new draft
    "create_rule",    # rules may be appended on each call
}


# ---------------------------------------------------------------------------
# _tool helper behavior
# ---------------------------------------------------------------------------


class TestToolHelper:
    """The `_tool` decorator's read-only gating behavior."""

    def test_mutating_tool_skipped_when_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mcp = Mock()
        monkeypatch.setattr(server, "mcp", fake_mcp)
        monkeypatch.setattr(server, "_READ_ONLY", True)

        def my_mutator() -> int:
            return 42

        result = server._tool(
            {"destructiveHint": True}, mutating=True
        )(my_mutator)

        assert result is my_mutator
        fake_mcp.tool.assert_not_called()

    def test_mutating_tool_registered_when_not_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[dict[str, Any], Any]] = []

        def fake_tool(*, annotations: dict[str, Any]) -> Any:
            def inner(fn: Any) -> Any:
                captured.append((annotations, fn))
                return fn

            return inner

        fake_mcp = Mock()
        fake_mcp.tool.side_effect = fake_tool
        monkeypatch.setattr(server, "mcp", fake_mcp)
        monkeypatch.setattr(server, "_READ_ONLY", False)

        def my_mutator() -> None:
            return None

        server._tool({"destructiveHint": True}, mutating=True)(my_mutator)

        assert len(captured) == 1
        ann, fn = captured[0]
        assert ann == {"destructiveHint": True}
        assert fn is my_mutator

    def test_read_only_tool_always_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mutating=False (default) registers even in read-only mode."""
        fake_mcp = Mock()
        fake_mcp.tool.return_value = lambda fn: fn
        monkeypatch.setattr(server, "mcp", fake_mcp)
        monkeypatch.setattr(server, "_READ_ONLY", True)

        @server._tool({"readOnlyHint": True})
        def my_reader() -> None:
            return None

        fake_mcp.tool.assert_called_once_with(annotations={"readOnlyHint": True})


# ---------------------------------------------------------------------------
# Per-tool annotations on the production `mcp` instance
# ---------------------------------------------------------------------------


def _registered_tools() -> dict[str, Any]:
    """Return the production mcp's tools keyed by name."""
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name: t for t in tools}


def _hint(tool: Any, key: str) -> Any:
    """Pull a hint from a Tool object's annotations, supporting either an
    object-with-attribute or dict-shaped representation."""
    ann = tool.annotations
    if ann is None:
        return None
    # FastMCP exposes annotations as a ToolAnnotations dataclass-ish object,
    # but allows dict at construction. Support both via getattr fallback.
    if hasattr(ann, key):
        return getattr(ann, key)
    if isinstance(ann, dict):
        return ann.get(key)
    return None


class TestProductionAnnotations:
    """Default-mode registration must match the classification table."""

    def test_tool_count_unchanged_in_default_mode(self) -> None:
        names = set(_registered_tools().keys())
        assert names == READ_ONLY_TOOLS | MUTATING_TOOLS

    def test_classifications_partition_correctly(self) -> None:
        """No overlap between read-only and mutating sets; counts are 10 + 14."""
        assert READ_ONLY_TOOLS.isdisjoint(MUTATING_TOOLS)
        assert len(READ_ONLY_TOOLS) == 10
        assert len(MUTATING_TOOLS) == 14
        assert DESTRUCTIVE_TOOLS.isdisjoint(ADDITIVE_TOOLS)

    @pytest.mark.parametrize("name", sorted(READ_ONLY_TOOLS))
    def test_read_only_tool_annotations(self, name: str) -> None:
        tool = _registered_tools()[name]
        assert _hint(tool, "readOnlyHint") is True, name
        assert _hint(tool, "destructiveHint") is False, name
        assert _hint(tool, "idempotentHint") is True, name

    @pytest.mark.parametrize("name", sorted(DESTRUCTIVE_TOOLS))
    def test_destructive_tool_annotations(self, name: str) -> None:
        tool = _registered_tools()[name]
        assert _hint(tool, "readOnlyHint") is False, name
        assert _hint(tool, "destructiveHint") is True, name
        expected_idempotent = name not in NON_IDEMPOTENT_TOOLS
        assert _hint(tool, "idempotentHint") is expected_idempotent, name

    @pytest.mark.parametrize("name", sorted(ADDITIVE_TOOLS))
    def test_additive_tool_annotations(self, name: str) -> None:
        tool = _registered_tools()[name]
        assert _hint(tool, "readOnlyHint") is False, name
        assert _hint(tool, "destructiveHint") is False, name
        expected_idempotent = name not in NON_IDEMPOTENT_TOOLS
        assert _hint(tool, "idempotentHint") is expected_idempotent, name


# ---------------------------------------------------------------------------
# Argparse — --read-only is visible and parses
# ---------------------------------------------------------------------------


class TestReadOnlyFlag:
    def test_pre_parse_returns_false_with_no_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["apple-mail-fast-mcp"])
        assert server._pre_parse_read_only() is False

    def test_pre_parse_returns_true_with_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["apple-mail-fast-mcp", "--read-only"])
        assert server._pre_parse_read_only() is True

    def test_pre_parse_ignores_unknown_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-parse uses parse_known_args; downstream args don't crash it."""
        monkeypatch.setattr(
            "sys.argv", ["apple-mail-fast-mcp", "setup-imap", "--account", "Gmail"]
        )
        assert server._pre_parse_read_only() is False

    def test_root_parser_advertises_flag(self) -> None:
        """--help should mention --read-only so users can discover it."""
        parser = server._build_arg_parser()
        help_text = parser.format_help()
        assert "--read-only" in help_text
