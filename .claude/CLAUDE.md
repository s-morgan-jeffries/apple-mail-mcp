# Apple Mail MCP Server

An MCP server bridging Claude and Apple Mail via AppleScript on macOS.

**Stack:** Python 3.10+, FastMCP, AppleScript (via `osascript`)
**Version:** v0.4.1 | **Tests:** 254 unit / 21 e2e | **Coverage:** 95%

## Commands

```bash
make test                  # Unit tests (~1s, mocked AppleScript)
make test-integration      # Real Mail.app tests (requires test account)
make test-e2e              # End-to-end MCP tool tests
make lint                  # Ruff linting
make format                # Ruff formatting
make typecheck             # Mypy strict mode
make check-all             # All checks (lint, typecheck, test, complexity, version-sync, parity)
make coverage              # Coverage report
./scripts/check_complexity.sh          # Cyclomatic complexity check
./scripts/check_client_server_parity.sh  # Verify all connector methods are exposed
./scripts/check_version_sync.sh        # Version consistency across files
```

**Running the server:** `uv run python -m apple_mail_mcp.server` or via Claude Desktop config.

## API Surface (14 MCP tools)

**Core (Phase 1):** list_mailboxes, search_messages, get_message, send_email, mark_as_read
**Attachments & Management (Phase 2):** send_email_with_attachments, get_attachments, save_attachments, move_messages, flag_message, create_mailbox, delete_messages
**Reply/Forward (Phase 3):** reply_to_message, forward_message

## Core Principles

- **TDD always** — RED/GREEN/REFACTOR. Tests before implementation.
- **Backend + frontend together** — Every feature touches `mail_connector.py` AND `server.py`. Verify with `check_client_server_parity.sh`.
- **Sanitize everything twice** — All user input: `sanitize_input()` then `escape_applescript_string()` before AppleScript.
- **Structured responses** — Every tool returns `{"success": bool, ...}`. Errors include `error` and `error_type`.
- **Security checklist per feature** — Input validation, escaping, path traversal, rate limiting, audit logging.
- **If you touched AppleScript, write integration tests** — Unit tests mock `_run_applescript()` and CANNOT catch AppleScript bugs.

## AppleScript Gotchas

**JSON output from AppleScript:** Scripts emit JSON via ASObjC + `NSJSONSerialization` (wrap with `_wrap_as_json_script`, parse with `parse_applescript_json`). Always quote the `name` record key as `|name|:` — the bare form is silently dropped during NSDictionary conversion. Coerce `missing value` to safe defaults (`{}` / `0`) before serializing. See applescript-mail skill for details.

**Gmail mode:** Gmail's label-based system doesn't support standard IMAP move. The `move_messages` tool has a `gmail_mode` parameter that uses copy+delete instead of move.

**Message ID lookup:** Finding a message by ID requires searching across all accounts and mailboxes. AppleScript `whose` clauses are used for efficiency.

**String escaping:** Always use `escape_applescript_string()` for user text. Unescaped quotes/backslashes break AppleScript silently.

**Attachment paths:** Use POSIX file references (`POSIX file "/path/to/file"`) in AppleScript. Path objects converted via `.as_posix()`.

**Timeout:** Default 60s, configurable via `AppleMailConnector(timeout=N)`. Some operations on large mailboxes may need more.

## Performance Constraints

- Each `osascript` subprocess call: 100-300ms overhead minimum
- Search: ~1-5s for typical mailboxes (uses `whose` clauses)
- Send: ~1-2s
- Read: <1s per message
- Bulk operations capped at 100 items

## Testing Requirements

| Type | When Required | How |
|------|--------------|-----|
| Unit tests | Every code change | `make test` |
| Integration tests | New/modified AppleScript | `make test-integration` |
| E2E tests | New/modified tools | `make test-e2e` |

**Hard rule:** If you wrote or modified AppleScript in the connector, integration tests must cover it before merge.

**Integration test safety:** When running tests via `server.py` tools, set `MAIL_TEST_MODE=true` and `MAIL_TEST_ACCOUNT=<test account name>`. The safety gate blocks destructive operations on non-test accounts and blocks sends to non-reserved recipient domains (must be @example.com, .test, .invalid, .localhost, etc.). See `check_test_mode_safety` in [src/apple_mail_mcp/security.py](src/apple_mail_mcp/security.py).

## Branch Convention

`{type}/issue-{num}-{description}` — e.g., `feature/issue-42-thread-support`, `fix/issue-99-timeout`

CHANGELOG.md is only updated on release branches, never on feature branches.

## Skills

Load these skills when working in their domains:

- **release** — Full release workflow: milestone check, version bump, changelog, validation, tagging, PR
- **applescript-mail** — Apple Mail AppleScript patterns, quirks, workarounds, JSON emission via ASObjC
- **api-design** — Tool design philosophy, decision tree for new tools
- **integration-testing** — Real Mail.app testing, why mocks miss AppleScript bugs
- **performance-patterns** — Operation timings, `whose` clause optimization, batch patterns, Gmail notes

## Key Files

- `src/apple_mail_mcp/mail_connector.py` — Core AppleScript client (~1120 lines)
- `src/apple_mail_mcp/server.py` — FastMCP server wrapping the connector (~1120 lines)
- `src/apple_mail_mcp/security.py` — Input validation, audit logging, confirmation flows
- `src/apple_mail_mcp/utils.py` — Pure functions: escaping, parsing, validation
- `src/apple_mail_mcp/exceptions.py` — Custom exception hierarchy
- `docs/reference/TOOLS.md` — Complete API reference
