# Apple Mail MCP Server

[![Tests](https://github.com/s-morgan-jeffries/apple-mail-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/s-morgan-jeffries/apple-mail-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server that provides programmatic access to Apple Mail, enabling AI assistants like Claude to read, send, search, and manage emails on macOS.

> ⚠️ **Pre-1.0 — expect breaking changes.** The MCP tool surface (tool names, parameters, return shapes) is still evolving as the project matures. Pin to a specific version (for example, `apple-mail-mcp==0.5.0`) and review the [CHANGELOG](CHANGELOG.md) before upgrading. The aim is to reach 1.0 once the v0.6.0 polish work lands.

## Tools (27)

**Core:** list_mailboxes, search_messages, get_message, get_selected_messages, send_email, mark_as_read
**Attachments & Management:** send_email_with_attachments, get_attachments, save_attachments, move_messages, flag_message, create_mailbox, delete_messages
**Reply/Forward:** reply_to_message, forward_message
**Discovery & Rules:** list_accounts, list_rules, get_thread, set_rule_enabled, create_rule, update_rule, delete_rule
**Templates:** list_templates, get_template, save_template, delete_template, render_template

See [docs/reference/TOOLS.md](docs/reference/TOOLS.md) for full parameter and return-shape documentation.

## Prerequisites

- macOS 10.15 (Catalina) or later
- Python 3.10 or later
- Apple Mail configured with at least one account
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
# From source (recommended for development)
git clone https://github.com/s-morgan-jeffries/apple-mail-mcp.git
cd apple-mail-mcp
uv sync --dev
```

## Configuration

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "uv",
      "args": ["--directory", "/path/to/apple-mail-mcp", "run", "python", "-m", "apple_mail_mcp.server"]
    }
  }
}
```

## Permissions

On first run, macOS will prompt for Automation access. Grant permission in:
**System Settings > Privacy & Security > Automation > Terminal (or your IDE)**

## Optional: faster search via IMAP

`search_messages` works out of the box via AppleScript. For large mailboxes (thousands of messages), AppleScript's `whose` clause can take 1–5 seconds per query. If you want faster server-side search, you can enable IMAP delegation per account by adding a Keychain entry.

**How it works.** If a Keychain entry exists for an account, the server uses IMAP (fast, server-side SEARCH). Otherwise — or on any IMAP failure (offline, wrong password, timeout) — it silently falls back to AppleScript. You never lose functionality; you only gain speed when IMAP is configured and reachable. No config flags, no environment variables; the Keychain entry's presence is the opt-in.

**One-time setup per account.**

1. Generate an app-specific password at your provider. The procedure varies:
   - **iCloud:** [appleid.apple.com/account/manage](https://appleid.apple.com/account/manage) → App-Specific Passwords. Requires 2FA on your Apple ID (default).
   - **Gmail:** [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Requires 2-Step Verification on your Google account.
   - **Yahoo / Fastmail / AOL:** generate an app password in the provider's account-security settings.

2. Run the `setup-imap` subcommand. It prompts for the password (no echo), writes the Keychain entry, and verifies by connecting:
   ```bash
   apple-mail-mcp setup-imap --account iCloud
   ```
   Substitute the Mail.app account name exactly — whatever it's labeled in Mail.app (e.g. `iCloud`, `Gmail`, `"Yahoo!"`). The CLI:
   - looks up the account's primary email from Mail.app (override with `--email`),
   - prompts via `getpass` so the password never lands in shell history,
   - writes to Keychain at `apple-mail-mcp.imap.<account>` (idempotent — re-running with a new password updates the existing entry),
   - opens an IMAP connection and runs a real LOGIN to confirm the password works. On rejection it rolls back the Keychain entry so you can retry without leaving a broken item behind.

3. If you see a one-time "security wants to use the 'login' keychain" prompt on the next IMAP-backed call, click **Always Allow**.

To remove the entry later: `apple-mail-mcp setup-imap --account iCloud --uninstall`.

**Verifying the setup.** The `setup-imap` command does this for you. If you want to spot-check post-hoc:
```bash
uv run python -c "from apple_mail_mcp.mail_connector import AppleMailConnector; \
    print(AppleMailConnector().search_messages(account='<ACCOUNT_NAME>', limit=1))"
```
If IMAP is working, the call returns in ~1 second. If it logs a WARNING about falling back (visible with `--log-level=DEBUG`), check that the account name matches Mail.app's account name exactly and that the email in your Keychain entry matches what `email addresses of account` returns.

**Known provider quirks.**

- **iCloud:** the IMAP server accepts `@icloud.com` / `@me.com` aliases as LOGIN username, not the Apple ID email. The server (and `setup-imap`) reads `email addresses of account` from Mail.app for that reason.
- **Yahoo:** app passwords have been progressively deprecated; the option may not be available for all accounts. If Yahoo's account-security page doesn't show the option, IMAP setup isn't possible for that account and AppleScript is the only path.
- **Gmail:** requires 2-Step Verification enabled. If your Google Workspace admin has disabled app passwords at the tenant level, IMAP setup isn't possible for that account.

**Write operations** (`send_email`, `reply_to_message`, `forward_message`) always use AppleScript regardless of IMAP configuration — these need Mail.app's compose UI.

## Development

```bash
# Setup
uv sync --dev

# Common commands
make test              # Run unit tests
make lint              # Lint with ruff
make typecheck         # Type check with mypy
make check-all         # All checks (lint, typecheck, test, complexity, version-sync, parity)
make coverage          # Coverage report
make test-integration  # Integration tests (requires Mail.app)

# Validation scripts
./scripts/check_version_sync.sh          # Version consistency
./scripts/check_client_server_parity.sh  # Connector-server alignment
./scripts/check_complexity.sh            # Cyclomatic complexity
./scripts/check_applescript_safety.sh    # AppleScript safety audit
```

### Branch Convention

`{type}/issue-{num}-{description}` — e.g., `feature/issue-42-thread-support`

## Architecture

```
server.py (FastMCP tools — thin orchestration)
  -> mail_connector.py (AppleScript bridge — domain logic)
     -> subprocess.run(["osascript", ...])
        -> Apple Mail.app
```

- **server.py** — MCP tool registration, input validation, response formatting
- **mail_connector.py** — All AppleScript generation and execution
- **security.py** — Input sanitization, audit logging, confirmation flows
- **utils.py** — Pure functions: escaping, parsing, validation
- **exceptions.py** — Typed exception hierarchy

## Security

- Local execution only (no cloud processing)
- Uses existing Mail.app authentication (no credential storage)
- All inputs sanitized and AppleScript-escaped
- Destructive operations require confirmation
- Operation audit logging
- See [SECURITY.md](SECURITY.md) for policy and [docs/SECURITY.md](docs/SECURITY.md) for detailed analysis

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, coding standards, and PR process.

## License

[MIT](LICENSE)
