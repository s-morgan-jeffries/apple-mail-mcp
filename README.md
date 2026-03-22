# Apple Mail MCP Server

A Model Context Protocol (MCP) server that provides programmatic access to Apple Mail, enabling AI assistants like Claude to read, send, search, and manage emails on macOS.

## Features

### Phase 1 (v0.1.0) - Current
- 🔍 **Search messages** with filters (sender, subject, read status, date range)
- 📧 **Send emails** with confirmation prompts
- 📬 **Read messages** with full content and metadata
- 📂 **List mailboxes** and folder structure
- ✅ **Mark as read/unread**
- 🖱️ **Get selected messages** — retrieve whichever message(s) are currently highlighted in Mail

### Coming Soon
- **Phase 2 (v0.2.0)**: Attachments, moving messages, flags, threads
- **Phase 3 (v0.3.0)**: Reply/forward, bulk operations, templates
- **Phase 4 (v0.4.0)**: Analytics, multi-account, advanced workflows

## Installation

### Prerequisites
- macOS 10.15 (Catalina) or later
- Python 3.10 or later
- Apple Mail configured with at least one account

### Install from PyPI

```bash
pip install apple-mail-mcp
```

### Install from source

```bash
git clone https://github.com/morgancoopercom/apple-mail-mcp.git
cd apple-mail-mcp
pip install -e ".[dev]"
```

## Configuration

### Claude Desktop

Add to your Claude Desktop configuration file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "python",
      "args": ["-m", "apple_mail_mcp.server"]
    }
  }
}
```

### Permissions

On first use, macOS will prompt you to grant permissions:

1. **Automation**: Allow the MCP server to control Apple Mail
2. **Full Disk Access** (optional): Only needed for analytics features in Phase 4

## Usage

Once configured, you can interact with Apple Mail through Claude Desktop using natural language:

### Examples

**Search emails:**
```
Find all unread emails from john@example.com in the last week
```

**Read messages:**
```
Show me the content of the most recent email from my manager
```

**Send email:**
```
Draft an email to alice@example.com thanking her for the meeting
```

**Manage folders:**
```
List all my mailboxes and show unread counts
```

**Mark as read:**
```
Mark all newsletters from this week as read
```

**Work with selected mail:**
```
Summarize the selected mail
```

## Available Tools

### `search_messages`
Search for messages with various filters.

**Parameters:**
- `account` (required): Account name (e.g., "Gmail", "iCloud")
- `mailbox` (optional): Mailbox name (default: "INBOX")
- `sender_contains` (optional): Filter by sender email/domain
- `subject_contains` (optional): Filter by subject keywords
- `read_status` (optional): Filter by read/unread status
- `date_from` (optional): Start date for date range
- `date_to` (optional): End date for date range
- `limit` (optional): Maximum results to return

### `get_message`
Get full details of a specific message.

**Parameters:**
- `message_id` (required): Message ID from search results
- `include_content` (optional): Include message body (default: true)

### `send_email`
Send an email via Apple Mail.

**Parameters:**
- `subject` (required): Email subject
- `body` (required): Email body (plain text)
- `to` (required): List of recipient email addresses
- `cc` (optional): List of CC recipients
- `bcc` (optional): List of BCC recipients

**Security:** All send operations require user confirmation.

### `list_mailboxes`
List all mailboxes and folders for an account.

**Parameters:**
- `account` (required): Account name

### `mark_as_read`
Mark messages as read or unread.

**Parameters:**
- `message_ids` (required): List of message IDs
- `read` (optional): true for read, false for unread (default: true)

### `get_selected_messages`
Get the message(s) currently selected/highlighted in Apple Mail.

**Parameters:**
- `include_content` (optional): Include message body text (default: true)

**Returns:** List of message objects with full metadata and content.

## Security

This MCP server prioritizes security and user control:

- ✅ **Local execution**: All operations run locally on your machine
- ✅ **User confirmation**: Sending emails requires explicit approval
- ✅ **Input sanitization**: All inputs are validated and escaped
- ✅ **Audit logging**: Operations are logged for transparency
- ✅ **No credential storage**: Uses existing Mail.app authentication
- ✅ **Read-only by default**: Destructive operations require confirmation

### Attack Surface Considerations

When using an MCP connector with your local Mail account:

- **Prompt injection**: Malicious email content could influence AI behavior
  - *Mitigation*: User confirmation for sensitive operations
- **Data exfiltration**: Email content is accessible to Claude
  - *Mitigation*: Local processing, no cloud sync beyond Claude API
- **Unintended actions**: Bugs could cause unwanted operations
  - *Mitigation*: Confirmation prompts, rate limiting, comprehensive tests

## Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/morgancoopercom/apple-mail-mcp.git
cd apple-mail-mcp

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install with dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov

# Run specific test file
pytest tests/unit/test_mail_connector.py

# Run integration tests (requires Mail.app setup)
pytest tests/integration/
```

### Code Quality

```bash
# Format code
black src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

### Test-Driven Development

This project uses TDD throughout:

1. Write test first (red)
2. Implement minimal code to pass (green)
3. Refactor for quality (refactor)
4. Repeat

See [tests/](tests/) for examples.

## Architecture

### Components

```
apple-mail-mcp/
├── server.py          # FastMCP server implementation
├── mail_connector.py  # AppleScript interface
├── tools.py           # MCP tool implementations
├── security.py        # Input validation, confirmations
└── utils.py           # Helper functions
```

### How It Works

1. **MCP Protocol**: Claude Desktop communicates with the server via JSON-RPC
2. **AppleScript Bridge**: Python executes AppleScript commands via `osascript`
3. **Mail.app**: AppleScript controls Mail.app to perform operations
4. **Response**: Results are returned to Claude as structured data

### Performance

- **Search**: ~1-5 seconds for typical mailboxes (1000s of messages)
- **Send**: ~1-2 seconds
- **Read**: <1 second per message
- **Optimization**: Uses AppleScript `whose` clauses for efficient filtering

## Roadmap

- [x] **v0.1.0**: Core search, read, send functionality
- [ ] **v0.2.0**: Attachments, moving messages, flags, threads
- [ ] **v0.3.0**: Reply/forward, bulk operations, templates
- [ ] **v0.4.0**: Analytics, multi-account, SQLite integration

See [GitHub Issues](https://github.com/morgancoopercom/apple-mail-mcp/issues) for detailed planning.

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Write tests for new functionality
4. Ensure all tests pass (`pytest`)
5. Commit changes (`git commit -m 'Add amazing feature'`)
6. Push to branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

- Built with [FastMCP](https://github.com/jlowin/fastmcp) by Jeremiah Lowin
- Uses Apple Mail's AppleScript interface
- Inspired by the [MCP Protocol](https://modelcontextprotocol.io/) by Anthropic

## Support

- **Issues**: [GitHub Issues](https://github.com/morgancoopercom/apple-mail-mcp/issues)
- **Discussions**: [GitHub Discussions](https://github.com/morgancoopercom/apple-mail-mcp/discussions)
- **Documentation**: [docs/](docs/)

## Related Projects

- [supermemoryai/apple-mcp](https://github.com/supermemoryai/apple-mcp) - TypeScript Apple ecosystem MCP
- [54yyyu/pyapple-mcp](https://github.com/54yyyu/pyapple-mcp) - Python Apple system MCP
- [loopwork/iMCP](https://github.com/loopwork/iMCP) - Swift native MCP implementation
