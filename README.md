# Apple Mail MCP Server

[![Tests](https://github.com/s-morgan-jeffries/apple-mail-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/s-morgan-jeffries/apple-mail-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server that provides programmatic access to Apple Mail, enabling AI assistants like Claude to read, send, search, and manage emails on macOS.

## Tools (14)

**Core:** list_mailboxes, search_messages, get_message, send_email, mark_as_read
**Attachments & Management:** send_email_with_attachments, get_attachments, save_attachments, move_messages, flag_message, create_mailbox, delete_messages
**Reply/Forward:** reply_to_message, forward_message

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
