# Architecture

## Component Diagram

```
Claude Desktop / MCP Client
        |
        | (MCP JSON-RPC over stdio)
        v
server.py (FastMCP)
  |-- 14 @mcp.tool() functions
  |-- Input validation
  |-- Response formatting
  |-- Error wrapping (exceptions -> dicts)
        |
        v
mail_connector.py (AppleMailConnector)
  |-- AppleScript generation
  |-- subprocess.run(["osascript", "-"])
  |-- Output parsing (JSON via ASObjC NSJSONSerialization)
  |-- Error routing (stderr -> typed exceptions)
        |
        v
Apple Mail.app (via macOS Automation)
```

## Module Responsibilities

| Module | Role | Dependencies |
|--------|------|-------------|
| `server.py` | MCP tool registration, orchestration | connector, security, exceptions |
| `mail_connector.py` | All AppleScript I/O | utils, exceptions |
| `security.py` | Validation, audit logging | utils, exceptions |
| `utils.py` | Pure functions (escaping, parsing) | stdlib only |
| `exceptions.py` | Exception class definitions | none |

## Design Decisions

**Two-file separation:** Server is thin (MCP plumbing), connector is thick (domain logic). Business logic never goes in server.py.

**Single execution point:** All AppleScript runs through `_run_applescript()`. This is the mock boundary for unit tests and the single place where timeout/error handling lives.

**Structured responses:** Every tool returns `{"success": bool, ...}`. Errors include `error` (message) and `error_type` (category). No exceptions reach the LLM.

**Pipe-delimited output:** AppleScript returns `field1|field2|field3`. This is fragile but functional. Planned migration to JSON output.

**Gmail compatibility:** `gmail_mode` parameter on move/archive operations handles Gmail's label-based system (copy+delete instead of move).
