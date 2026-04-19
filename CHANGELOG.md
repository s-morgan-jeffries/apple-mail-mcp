# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] - 2026-04-19

Patch release: dep hygiene and v0.4.0 follow-ups. Four connector bugs that unit tests couldn't catch were surfaced by running the three new integration tests against real Mail.app.

### Added
- Integration tests for `list_accounts`, `get_message`, and `get_attachments` against real Mail.app, fulfilling the #23 design doc commitment (#57)

### Changed
- Bumped transitive deps to clear `pip-audit` findings from the v0.4.0 release: `authlib` 1.6.9 → 1.7.0, `cryptography` 46.0.6 → 46.0.7, `pytest` 9.0.2 → 9.0.3, `python-multipart` 0.0.22 → 0.0.26. `fastmcp`/`mcp`/`pydantic`/`starlette`/`uvicorn` unchanged (#57)

### Fixed
- `search_messages` with no filter conditions emitted `messages of mailboxRef whose true` — Mail rejected with error -1726. The `whose` clause is now dropped entirely when no filters are supplied (#57)
- `search_messages` with a `limit` emitted `items 1 thru N of (messages of mailboxRef …)` — Mail rejected with error -1728. Replaced with a `count of` + indexed `item i of` repeat loop (#57)
- `_run_applescript` error-substring matcher checked for straight-apostrophe `Can't`, but macOS stderr uses curly `Can’t`. `MailAccountNotFoundError` and `MailMailboxNotFoundError` were silently degraded to generic errors. Curly apostrophes are now normalized before dispatch (#57)
- Several AppleScript record keys (`subject`, `sender`, `content`, `date_received`, `read_status`, `flagged`, `mime_type`, `downloaded`, `email_addresses`, `unread_count`) were silently dropped by NSJSONSerialization when values came from live Mail objects. Extended the prior `|name|` / `|id|` / `|size|` quoting to **every** record key across all 5 JSON-emitting methods (#57)

## [0.4.0] - 2026-04-19

Quality and infrastructure milestone. No new MCP tools; focus on test coverage, safety, and parsing robustness.

### Added
- Test-mode safety system (`MAIL_TEST_MODE`, `MAIL_TEST_ACCOUNT`) — account-gated destructive operations are constrained to a designated test account and sends are constrained to RFC 2606 reserved domains (#19)
- Three-tier sliding-window rate limiting (general / send / expensive) replacing the previous stub (#17)
- Proper MCP elicitation for destructive operation confirmation, replacing the previous stub (#18)
- Unit tests for all 14 `server.py` MCP tool handlers, lifting coverage from 0 % to 99 % (#16)
- E2E tests exercising FastMCP tool registration, schema, and invocation — 20 in-process tests covering all 14 tools (#21)
- stdio subprocess smoke test verifying the real MCP transport layer (#50)
- Blind-agent eval framework under `evals/agent_tool_usability/` — 36 scenarios across 9 categories, runnable against any OpenRouter-accessible model (#22)
- `docs/guides/COMPLEXITY.md` — rationale and exception table for the CC ≤ 20 ceiling (#24)
- IMAP hybrid-approach research document (#15)

### Changed
- AppleScript output now emits JSON via ASObjC + `NSJSONSerialization` instead of the fragile pipe-delimited format that broke silently when any field contained `|` (#23). Finishes previously-placeholder `list_accounts` and `list_mailboxes` return shapes.
- Coverage threshold raised from 60 % to 90 % in both `pyproject.toml` and CI, matching the documented target (#20)
- Pre-commit hook now enforces version sync across `pyproject.toml`, `__init__.py`, and `.claude/CLAUDE.md` — failures block the commit locally instead of surfacing later in CI (#25)

### Fixed
- Three `NSJSONSerialization` selector-collision bugs discovered during the JSON-output migration's integration smoke: `name`, `id`, and `size` AppleScript record keys were silently dropped and are now quoted as `|name|`, `|id|`, `|size|` (#23)

## [0.3.0] - 2025-10-11

Phase 3: Smart reply and forward.

### Added
- `reply_to_message` tool with reply-all support
- `forward_message` tool with CC/BCC support
- Reply/forward security tests (body sanitization, special character escaping)

## [0.2.0] - 2025-10-11

Phase 2: Message management and attachments.

### Added
- `send_email_with_attachments` tool
- `get_attachments` tool
- `save_attachments` tool with directory validation
- `move_messages` tool with Gmail label-based workaround (`gmail_mode`)
- `flag_message` tool with color support
- `create_mailbox` tool with parent mailbox support
- `delete_messages` tool with permanent delete option
- Attachment security validation (type blocklist, size limits, filename sanitization)
- Bulk operation validation (max 100 items)

## [0.1.0] - 2025-10-11

Initial release. Phase 1: Core mail operations.

### Added
- `list_mailboxes` tool
- `search_messages` tool with sender/subject/read-status filters
- `get_message` tool with optional content inclusion
- `send_email` tool with CC/BCC support
- `mark_as_read` tool with bulk support
- AppleScript-based Mail.app integration via subprocess
- Custom exception hierarchy for Mail errors
- Input sanitization and AppleScript string escaping
- Security module with operation logging and validation
- Unit test suite with mocked AppleScript
- Integration test framework (opt-in via `--run-integration`)
