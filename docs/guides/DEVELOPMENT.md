# Setup Guide

This guide will help you set up the Apple Mail MCP server for use with Claude Desktop.

## Prerequisites

- macOS 10.15 (Catalina) or later
- Python 3.10 or later
- Apple Mail configured with at least one email account
- Claude Desktop installed

## Installation

### Option 1: Install from PyPI (Coming Soon)

```bash
pip install apple-mail-mcp
```

### Option 2: Install from Source

```bash
# Clone the repository
git clone https://github.com/morgancoopercom/apple-mail-mcp.git
cd apple-mail-mcp

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install package
pip install -e .
```

## Configuration

### Claude Desktop Setup

1. Locate your Claude Desktop configuration file:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

2. Add the Apple Mail MCP server configuration:

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

If you installed from source, use the full path to the venv:

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/path/to/apple-mail-mcp/venv/bin/python",
      "args": ["-m", "apple_mail_mcp.server"]
    }
  }
}
```

3. Restart Claude Desktop

## Permissions

When you first use the MCP server, macOS will prompt you for permissions:

### Automation Permission

1. A dialog will appear: **"python" would like to control "Mail"**
2. Click **OK** to grant permission

If you accidentally denied permission:

1. Open **System Settings** (or System Preferences)
2. Go to **Privacy & Security** → **Automation**
3. Find **python** or **Terminal** in the list
4. Check the box next to **Mail**

### Full Disk Access (Optional)

Only needed for Phase 4 analytics features using SQLite database access.

1. Open **System Settings** → **Privacy & Security** → **Full Disk Access**
2. Click the **+** button
3. Navigate to your Python executable
4. Enable Full Disk Access

## Verification

### Test the Installation

1. Open Claude Desktop
2. Start a new conversation
3. Try a simple command:

```
List my mailboxes
```

You should see a list of your Mail.app mailboxes.

### Test Basic Operations

Try these commands to verify everything works:

```
# Search for unread emails
Show me my unread emails

# Get mailbox information
List all my mail folders

# Search with filters
Find emails from john@example.com in the last week
```

## Troubleshooting

### "Command not found" Error

**Problem:** Claude Desktop can't find the Python command.

**Solutions:**
1. Use the full path to Python in the config:
   ```json
   "command": "/usr/local/bin/python3"
   ```
2. Or use the venv path:
   ```json
   "command": "/path/to/venv/bin/python"
   ```

### "Permission Denied" Error

**Problem:** Automation permission not granted.

**Solution:**
1. Check System Settings → Privacy & Security → Automation
2. Grant permission for Python/Terminal to control Mail

### "Account Not Found" Error

**Problem:** The MCP server can't access your mail account.

**Solutions:**
1. Verify Mail.app is open and configured
2. Check the account name (use exact name from Mail.app preferences)
3. Try using a different account name

### No Response from MCP Server

**Problem:** The server isn't starting or responding.

**Solutions:**
1. Check Claude Desktop logs:
   - macOS: `~/Library/Logs/Claude/mcp.log`
2. Verify Python installation:
   ```bash
   python3 --version
   ```
3. Test the server directly:
   ```bash
   python -m apple_mail_mcp.server
   ```

### "Module not found" Error

**Problem:** Dependencies not installed.

**Solution:**
```bash
pip install fastmcp
```

## Configuration Options

### Custom Timeout

Set a custom timeout for AppleScript operations:

```python
from apple_mail_mcp.mail_connector import AppleMailConnector

connector = AppleMailConnector(timeout=120)  # 2 minutes
```

### Logging

Enable debug logging:

```bash
export LOGLEVEL=DEBUG
python -m apple_mail_mcp.server
```

## Next Steps

- Read the [Tools Documentation](TOOLS.md) to learn about available operations
- Review [Security Considerations](SECURITY.md)
- Check out [Examples](EXAMPLES.md) for common use cases

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/morgancoopercom/apple-mail-mcp/issues)
- **Discussions**: [GitHub Discussions](https://github.com/morgancoopercom/apple-mail-mcp/discussions)
- **Documentation**: [Full Documentation](../README.md)
