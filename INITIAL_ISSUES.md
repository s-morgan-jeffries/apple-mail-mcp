# Initial Issues

Issues to create after infrastructure bootstrap. Organized by milestone.

---

## Milestone: v0.4.0 — Infrastructure Hardening

### Infrastructure

1. **[infra] Rename remote `master` branch to `main` and update GitHub default**
   Labels: `infrastructure`
   Set `main` as default branch on GitHub, delete remote `master`.

2. **[infra] Set up GitHub milestones for v0.4.0 and v0.5.0**
   Labels: `infrastructure`
   Create milestones, assign these issues.

3. **[infra] Research native macOS Mail frameworks (MailKit, MessageUI, Swift bridges)**
   Labels: `research`, `enhancement`
   AppleScript has limitations (no scheduling, fragile parsing). Investigate Swift/EventKit-style approach like the Calendar project uses. Determine which operations benefit from native frameworks vs AppleScript.

4. **[infra] Add server.py tool-level tests (currently 0% coverage)**
   Labels: `testing`, `priority:high`
   Server layer (validation, logging, error wrapping) has zero test coverage. Add tests for each @mcp.tool() function using mocked connector.

5. **[infra] Implement proper rate limiting (currently a stub)**
   Labels: `enhancement`, `security`
   `rate_limit_check()` in security.py always returns True. Implement actual timing-based rate limiting.

6. **[infra] Implement proper user confirmation mechanism**
   Labels: `enhancement`, `security`
   `require_confirmation()` in security.py always returns True. Design and implement real confirmation flow for destructive operations.

7. **[infra] Add test database safety system (MAIL_TEST_MODE)**
   Labels: `testing`, `security`
   Implement environment-variable-gated safety for destructive operations (send, delete, move). Verify target account before proceeding. Pattern from OmniFocus/Calendar siblings.

8. **[infra] Reach 80% overall test coverage**
   Labels: `testing`
   Current: ~52%. Target: 80%. Primary gap: server.py (0%), mail_connector.py (89%), security.py (86%).

9. **[infra] Add E2E tests for MCP tool registration and invocation**
   Labels: `testing`
   Test that tools are properly registered, accept parameters, and return correct response format through the full MCP stack.

10. **[infra] Add blind agent eval scenarios**
    Labels: `testing`, `enhancement`
    Create eval scenarios testing whether LLMs can correctly use mail tools from descriptions alone. Pattern from OmniFocus/Calendar siblings.

### Quality

11. **[quality] Replace pipe-delimited AppleScript output with JSON**
    Labels: `enhancement`, `refactor`
    Current pipe-delimited parsing breaks if fields contain `|`. Migrate to JSON output from AppleScript (with inline helpers, duplicated per script — AppleScript has no modules).

12. **[quality] Add cyclomatic complexity exceptions documentation**
    Labels: `documentation`
    Document which functions are allowed to exceed CC threshold and why (similar to OmniFocus's `get_tasks`/`update_task` exceptions).

13. **[quality] Version sync automation — add pre-commit hook verification**
    Labels: `infrastructure`
    The version drifted (pyproject.toml=0.3.0, __init__.py=0.1.0). Ensure `check_version_sync.sh` runs as part of pre-commit workflow.

---

## Milestone: v0.5.0 — New Tools & Capabilities

### New Tools

14. **[feature] Add `get_accounts` tool**
    Labels: `enhancement`
    List all configured email accounts with account type and status.

15. **[feature] Add `get_rules` tool for mail rules management**
    Labels: `enhancement`
    Read and optionally manage Apple Mail rules.

16. **[feature] Add `search_messages` advanced filters (date range, has-attachment, flagged)**
    Labels: `enhancement`
    Extend search with date_from, date_to, has_attachment, is_flagged filters.

17. **[feature] Add `get_thread` tool for conversation view**
    Labels: `enhancement`
    Retrieve all messages in a thread/conversation by thread ID or message reference.

18. **[feature] Add email template support**
    Labels: `enhancement`
    Store and apply email templates for common reply/forward patterns.

---

## Milestone: v0.6.0 — Performance & Polish

### Performance

19. **[perf] Add benchmark test suite with documented baselines**
    Labels: `testing`, `performance`
    Establish timing baselines for all operations. Detect regressions with 5x threshold.

20. **[perf] Profile and optimize search_messages for large mailboxes**
    Labels: `performance`
    Current search uses `whose` clauses but may be slow for mailboxes with 10k+ messages. Profile and optimize.

---

## Label Definitions

| Label | Color | Description |
|-------|-------|-------------|
| `infrastructure` | `#0e8a16` | Build, CI, tooling, project structure |
| `testing` | `#1d76db` | Test coverage, test infrastructure |
| `enhancement` | `#a2eeef` | New feature or improvement |
| `security` | `#b60205` | Security-related |
| `refactor` | `#d4c5f9` | Code improvement without behavior change |
| `documentation` | `#0075ca` | Documentation only |
| `research` | `#f9d0c4` | Investigation, no code changes expected |
| `performance` | `#fbca04` | Performance improvement |
| `priority:high` | `#b60205` | Should be addressed first |
