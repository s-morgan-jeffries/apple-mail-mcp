# E2E tests for MCP tool registration and invocation

**Issue:** #21
**Date:** 2026-04-17
**Status:** Approved

## Context

`tests/e2e/test_mcp_tools.py` currently contains a 30-line stub that only asserts `mcp is not None` and that the server module imports. The project exposes 14 MCP tools via FastMCP, and there is no test that verifies those tools are actually registered, have valid schemas, or can be dispatched through the MCP layer.

Unit tests in `tests/unit/test_server.py` already invoke each `@mcp.tool()` function directly with a mocked connector. The gap they leave is FastMCP's own dispatch layer — schema generation, parameter binding, response wrapping, and tool discoverability by an MCP client.

## Non-goals

- **Subprocess / stdio coverage.** A stdio smoke test (spawn server, connect via MCP client SDK, round-trip `list_tools`) is out of scope and will be filed as a separate v0.4.0 issue. Different infrastructure (async pipe plumbing, process lifecycle) and different failure modes justify a distinct PR.
- **Error-path invocation tests per tool.** Unit tests already cover the error mapping (account-not-found, rate-limit, safety-gate). Duplicating at the E2E layer has low marginal value.
- **Integration coverage against real Mail.app.** That is `make test-integration` territory.

## Design

### Structure

Extend `tests/e2e/test_mcp_tools.py` with two test classes:

1. `TestToolRegistration` — synchronous assertions over `mcp.list_tools()` / `mcp.get_tool()`.
2. `TestToolInvocation` — async, parametrized happy-path invocation for all 14 tools via `await mcp.call_tool(...)`.

All tests run in-process. No subprocesses, no real AppleScript. `make test-e2e` already sets `MAIL_TEST_MODE=true`, which the safety gate respects.

### TestToolRegistration

Assertions:

- **Name parity.** `set(await mcp.list_tools())` equals a hard-coded `EXPECTED_TOOLS` constant of 14 names. Mismatches force an update, preventing silent drift.
- **`list_accounts` intentionally absent.** A dedicated test documents the known parity warning (the connector method exists but is not exposed as an MCP tool).
- **Description hygiene.** For each tool, `tool.description` is a non-empty string (catches tools that lose their docstring during refactors).
- **Input-schema shape spot-check.** For 3 high-signal tools (`send_email`, `search_messages`, `move_messages`), assert the JSON schema has `type: object` and the expected required properties. Full per-tool schema assertions are over-scoped.

### TestToolInvocation

One parametrized test covering all 14 tools. Each parameter tuple is:

```
(tool_name, args_dict, connector_method, connector_return_value)
```

Test body:

1. `patch.object(server, "mail", MagicMock())` at function scope.
2. Configure `mock_mail.<connector_method>.return_value = connector_return_value`.
3. `result = await mcp.call_tool(tool_name, args_dict)`.
4. Assert the unwrapped result is a dict with `success=True` and no `error` key.
5. Assert `mock_mail.<connector_method>` was called once.

FastMCP `call_tool` returns a structured `ToolResult`; the test extracts the `structured_content` (or equivalent) and asserts against it. Exact attribute access will be confirmed during implementation against the installed `fastmcp` version.

### Mocking strategy

Match existing `tests/unit/test_server.py` — `patch.object(server, "mail", ...)`. No new conftest fixtures. The mail connector is a module-level singleton, which makes this the natural seam.

### Data constants

`EXPECTED_TOOLS` and the parametrize list live at the top of the test module. When tools are added or renamed, both constants update in the same PR as the tool change — a desirable coupling.

### Coverage expectations

E2E tests are excluded from coverage via the pytest markers already in place. This PR does not affect the 90% `fail_under` threshold.

## Verification

- `make test-e2e` passes locally (~<2s, no Mail.app interaction).
- `make check-all` still green (lint, typecheck, unit tests, complexity, version sync, parity).
- New tests fail as expected when:
  - A tool is removed from `server.py` without updating `EXPECTED_TOOLS`.
  - A tool's `@mcp.tool()` decorator is removed.
  - A tool's docstring/description is deleted.

## Follow-up issues

- **New v0.4.0 issue:** "Add stdio subprocess smoke test for MCP server" — spawn `python -m apple_mail_mcp.server`, connect via MCP client SDK, assert `list_tools` handshake returns 14 tools. Separate PR, separate fixture plumbing.
