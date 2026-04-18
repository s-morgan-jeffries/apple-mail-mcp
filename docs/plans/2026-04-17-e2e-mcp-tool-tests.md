# E2E MCP Tool Tests Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the stub `tests/e2e/test_mcp_tools.py` with real coverage of the FastMCP dispatch layer: tool registration, schemas, and happy-path invocation for all 14 tools.

**Architecture:** In-process tests against the exported `mcp` FastMCP instance. Two classes: `TestToolRegistration` (sync, schema assertions) and `TestToolInvocation` (async, parametrized invocation). Connector mocked via `patch.object(server, "mail", ...)`. `MAIL_TEST_MODE` disabled in an autouse fixture so the safety gate doesn't interfere — mocked connector makes the gate redundant.

**Tech Stack:** pytest, pytest-asyncio (already in `asyncio_mode = auto`), FastMCP's `list_tools()` / `call_tool()` / `get_tool()`.

**Design doc:** [`docs/plans/2026-04-17-e2e-mcp-tool-tests-design.md`](./2026-04-17-e2e-mcp-tool-tests-design.md)

---

## Pre-flight

**Verified against the installed `fastmcp`:**

- `await mcp.list_tools()` → `list[FunctionTool]`, length 14.
- Each `FunctionTool` has `.name`, `.description` (str), `.parameters` (JSON-schema dict with keys `type`, `properties`, `required`, `additionalProperties`).
- `await mcp.call_tool(name, args)` → `ToolResult` with `.structured_content` (our tool's raw `dict[str, Any]` return) and `.content` (a list of `TextContent` with the JSON-serialized form).
- Tools that use elicitation (`send_email`, `forward_message`, `delete_messages`, `move_messages`, etc.) log "Elicitation not supported by client, proceeding without confirmation" when called with no real client context — they proceed to invoke the connector. Happy-path invocation works.

**Existing expected tool names** (from `@mcp.tool()` decorators in `src/apple_mail_mcp/server.py`):

```
list_mailboxes, search_messages, get_message, send_email, mark_as_read,
send_email_with_attachments, get_attachments, save_attachments,
move_messages, flag_message, create_mailbox, delete_messages,
reply_to_message, forward_message
```

That's 14. `list_accounts` is a connector method NOT exposed (known parity-check warning).

---

## Task 1: Replace stub — add expected-tools constant and basic registration test

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py` (replace entire contents)

**Step 1: Write the failing test**

Overwrite `tests/e2e/test_mcp_tools.py` with:

```python
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
```

**Step 2: Run test to verify it fails or passes as expected**

Run: `uv run pytest tests/e2e/test_mcp_tools.py::TestToolRegistration::test_expected_tool_names_registered -v`

Expected: PASS (the server already registers these 14 tools). If this fails, either a tool was renamed or `EXPECTED_TOOLS` is wrong — fix before continuing.

**Step 3: Commit**

```bash
git add tests/e2e/test_mcp_tools.py
git commit -m "Replace e2e stub with tool-name registration test"
```

---

## Task 2: list_accounts is intentionally absent

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py`

**Step 1: Add the test**

Inside `TestToolRegistration`:

```python
    async def test_list_accounts_not_exposed_as_tool(self) -> None:
        """list_accounts exists on the connector but is intentionally not an MCP tool.

        This mirrors the known warning from scripts/check_client_server_parity.sh.
        If list_accounts is ever exposed, update check_client_server_parity.sh
        AND delete this test.
        """
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert "list_accounts" not in names
```

**Step 2: Run**

`uv run pytest tests/e2e/test_mcp_tools.py::TestToolRegistration::test_list_accounts_not_exposed_as_tool -v`

Expected: PASS.

**Step 3: Commit**

```bash
git add tests/e2e/test_mcp_tools.py
git commit -m "Assert list_accounts is intentionally not an MCP tool"
```

---

## Task 3: Description hygiene — every tool has a non-empty description

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py`

**Step 1: Add the test**

Inside `TestToolRegistration`:

```python
    async def test_every_tool_has_description(self) -> None:
        tools = await server.mcp.list_tools()
        missing = [t.name for t in tools if not (t.description and t.description.strip())]
        assert not missing, f"tools missing description: {missing}"
```

**Step 2: Run**

`uv run pytest tests/e2e/test_mcp_tools.py::TestToolRegistration::test_every_tool_has_description -v`

Expected: PASS. If any tool has an empty description, add a docstring to that tool in `server.py` before continuing.

**Step 3: Commit**

```bash
git add tests/e2e/test_mcp_tools.py
git commit -m "Require every MCP tool to have a non-empty description"
```

---

## Task 4: Input-schema spot-checks for three high-signal tools

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py`

**Step 1: Add the test**

Inside `TestToolRegistration`:

```python
    @pytest.mark.parametrize(
        "tool_name,expected_required",
        [
            ("send_email", {"to", "subject", "body"}),
            ("search_messages", {"account", "mailbox"}),
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
```

**Step 2: Run**

`uv run pytest tests/e2e/test_mcp_tools.py::TestToolRegistration::test_tool_schema_required_fields -v`

Expected: PASS (3 parametrized cases). If a case fails, inspect the tool's signature in `server.py` — either the test expectation is wrong or a required parameter lost its `Annotated[...]` / type annotation.

**Step 3: Commit**

```bash
git add tests/e2e/test_mcp_tools.py
git commit -m "Spot-check input schemas for send_email, search_messages, move_messages"
```

---

## Task 5: Scaffold invocation test class + first happy-path case

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py`

**Step 1: Add the class with one invocation case**

Append to the file (below `TestToolRegistration`):

```python
# (tool_name, call_args, connector_method, connector_return_value)
INVOCATION_CASES: list[tuple[str, dict[str, Any], str, Any]] = [
    (
        "list_mailboxes",
        {"account": "TestAccount"},
        "list_mailboxes",
        ["INBOX", "Sent"],
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
        tool_name: str,
        call_args: dict[str, Any],
        connector_method: str,
        connector_return: Any,
    ) -> None:
        getattr(mock_mail, connector_method).return_value = connector_return

        result = await server.mcp.call_tool(tool_name, call_args)

        assert result.structured_content is not None
        assert result.structured_content.get("success") is True
        assert "error" not in result.structured_content
        getattr(mock_mail, connector_method).assert_called_once()
```

**Step 2: Run**

`uv run pytest tests/e2e/test_mcp_tools.py::TestToolInvocation -v`

Expected: PASS (1 case). If it fails, the test scaffolding is wrong — fix before expanding coverage.

**Step 3: Commit**

```bash
git add tests/e2e/test_mcp_tools.py
git commit -m "Add invocation test scaffolding with list_mailboxes case"
```

---

## Task 6: Expand invocation cases to all 14 tools

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py`

**Step 1: Replace `INVOCATION_CASES` with the full list**

Consult `src/apple_mail_mcp/server.py` for the exact parameter name of each tool. Replace `INVOCATION_CASES` with:

```python
INVOCATION_CASES: list[tuple[str, dict[str, Any], str, Any]] = [
    (
        "list_mailboxes",
        {"account": "TestAccount"},
        "list_mailboxes",
        ["INBOX", "Sent"],
    ),
    (
        "search_messages",
        {"account": "TestAccount", "mailbox": "INBOX"},
        "search_messages",
        [],
    ),
    (
        "get_message",
        {"message_id": "msg-1"},
        "get_message",
        {"id": "msg-1", "subject": "hi", "from": "a@example.com"},
    ),
    (
        "send_email",
        {"to": ["a@example.com"], "subject": "s", "body": "b"},
        "send_email",
        {"success": True, "message_id": "abc"},
    ),
    (
        "mark_as_read",
        {"message_ids": ["msg-1"]},
        "mark_as_read",
        {"marked": 1},
    ),
    (
        "send_email_with_attachments",
        {
            "to": ["a@example.com"],
            "subject": "s",
            "body": "b",
            "attachment_paths": ["/tmp/fake.txt"],
        },
        "send_email_with_attachments",
        {"success": True, "message_id": "abc"},
    ),
    (
        "get_attachments",
        {"message_id": "msg-1"},
        "get_attachments",
        [],
    ),
    (
        "save_attachments",
        {"message_id": "msg-1", "output_directory": "/tmp"},
        "save_attachments",
        {"saved": []},
    ),
    (
        "move_messages",
        {
            "message_ids": ["msg-1"],
            "account": "TestAccount",
            "destination_mailbox": "Archive",
        },
        "move_messages",
        {"moved": 1},
    ),
    (
        "flag_message",
        {"message_id": "msg-1", "flag_color": "red"},
        "flag_message",
        {"flagged": True},
    ),
    (
        "create_mailbox",
        {"account": "TestAccount", "mailbox_name": "NewBox"},
        "create_mailbox",
        {"created": True},
    ),
    (
        "delete_messages",
        {"message_ids": ["msg-1"]},
        "delete_messages",
        {"deleted": 1},
    ),
    (
        "reply_to_message",
        {"message_id": "msg-1", "body": "b"},
        "reply_to_message",
        {"success": True},
    ),
    (
        "forward_message",
        {"message_id": "msg-1", "to": ["a@example.com"]},
        "forward_message",
        {"success": True},
    ),
]
```

**Important:** Some tool parameter names may differ (e.g., `mailbox_name` vs. `name`, `attachment_paths` vs. `attachments`, `output_directory` vs. `save_directory`). Before running, re-read the `@mcp.tool()` decorators in `src/apple_mail_mcp/server.py` and adjust `call_args` keys to match each tool's signature exactly. FastMCP will raise `ValidationError` on mismatched parameter names.

Also double-check the connector method name (the `mail.XXX(...)` call inside each tool). In most cases it matches the tool name, but verify before running.

**Step 2: Run**

`uv run pytest tests/e2e/test_mcp_tools.py::TestToolInvocation -v`

Expected: 14 PASS. If a case fails:
- `ValidationError` → parameter name in `call_args` is wrong; fix to match tool signature.
- `AssertionError` on `success is True` → tool applied extra validation (e.g., rejected the mock return shape); adjust `connector_return` so the tool wraps it as success.
- `AssertionError` on `assert_called_once` → the tool short-circuited before calling the connector (e.g., validation failure); inspect tool body and fix args.

Do NOT disable cases. If a tool is genuinely hard to happy-path with mocks (e.g., does extra validation on the connector return), include a short note in the plan and ask — do not merge a skipped tool.

**Step 3: Commit**

```bash
git add tests/e2e/test_mcp_tools.py
git commit -m "Cover all 14 MCP tools with parametrized happy-path invocation"
```

---

## Task 7: Run the full suite and confirm everything is green

**Step 1: Run the e2e tests**

`make test-e2e`

Expected: all tests in `tests/e2e/test_mcp_tools.py` pass. Runtime under 2 seconds.

**Step 2: Run the complete check suite**

`make check-all`

Expected: lint, typecheck, unit tests (221 passing), complexity, version sync, parity — all green. Coverage still ≥ 90%.

**Step 3: If anything fails, fix root cause and commit separately**

Common issues:
- Ruff complains about unused imports or line length — auto-fix with `make format`.
- Mypy error on `Any` in the parametrize tuple — already handled by `list[tuple[str, dict[str, Any], str, Any]]`; if mypy still complains, cast via `pytest.param(..., id=...)`.

---

## Task 8: Update documentation

**Files:**
- Modify: `docs/guides/TESTING.md`
- Modify: `.claude/CLAUDE.md`

**Step 1: Document the new E2E layer**

In `docs/guides/TESTING.md`, find the section that describes test tiers. Add a short paragraph (or line in an existing table) explaining:

- `make test-e2e` exercises the FastMCP dispatch layer in-process.
- It mocks the connector, so it catches schema / registration / invocation bugs but not AppleScript bugs.
- Subprocess / stdio smoke coverage is tracked in issue #50 (link if convention allows).

In `.claude/CLAUDE.md`, update the Tests line if the unit count changed (it should still be 221; the new e2e tests don't count as unit). The "**Tests:** 221 unit" line stays, but consider appending `| **E2E:** 14+ tools` if that matches the project's header style. Check recent commits to `.claude/CLAUDE.md` for style — keep it consistent.

**Step 2: Verify docs render / lint**

`make lint` (will ignore markdown but catches Python issues) and visually inspect the changes.

**Step 3: Commit**

```bash
git add docs/guides/TESTING.md .claude/CLAUDE.md
git commit -m "Document new E2E MCP tool test layer"
```

---

## Task 9: Open PR closing #21

**Step 1: Push and open PR**

```bash
git push -u origin feature/issue-21-e2e-mcp-tests
gh pr create --title "E2E tests for MCP tool registration and invocation (#21)" --body "..."
```

PR body should:

- Summarize the new test classes and tool coverage.
- Reference the design doc and the follow-up subprocess issue (#50).
- Include the test-plan checklist: `make test-e2e`, `make check-all`, CI green.
- Use `Closes #21`.

**Step 2: Wait for CI**

`gh pr checks <pr-number> --watch`

**Step 3: On green, merge**

Use `/merge-and-status` — same pattern as the previous issue.

---

## Verification (end-to-end)

- `make test-e2e` — new file passes, runtime < 2s.
- `make check-all` — green, coverage ≥ 90%.
- Parametrize IDs show all 14 tool names in pytest output (readable failure messages).
- Removing a `@mcp.tool()` decorator from `server.py` causes `test_expected_tool_names_registered` to fail — confirms the guard works.
- Removing a docstring from any tool causes `test_every_tool_has_description` to fail.
- PR #<n> merges cleanly, #21 auto-closes.
