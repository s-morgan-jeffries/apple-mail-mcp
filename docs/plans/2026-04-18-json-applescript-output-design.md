# Replace pipe-delimited AppleScript output with JSON

**Issue:** #23
**Date:** 2026-04-18
**Status:** Approved

## Context

Apple Mail MCP's connector communicates with AppleScript by returning pipe-delimited strings and parsing them in Python with `line.split("|")`. Every field is positional. If any field value contains `|` — common in email subjects and bodies — parsing breaks silently with no error. The project documents this as its biggest technical debt (see `.claude/skills/applescript-mail/SKILL.md`).

Five methods in `src/apple_mail_mcp/mail_connector.py` are affected:

| Method | Current state |
|---|---|
| `search_messages` | 5 fields, pipe-delimited, working but fragile |
| `get_message` | 7 fields, pipe-delimited (maxsplit=6), working but fragile |
| `get_attachments` | 4 fields, pipe-delimited, working but fragile |
| `list_accounts` | Half-baked pseudo-JSON, parsed as `[{"raw": str}]` — effectively broken |
| `list_mailboxes` | Returns raw AppleScript record string as `[{"raw": str}]` with a TODO — effectively broken |

## Non-goals

- Changing the MCP tool return shapes exposed via `server.py`. The public API keeps working; only the internal connector → tool handoff changes.
- Changing error propagation. `ERROR:` prefix strings from AppleScript keep their meaning.
- Fixing other known AppleScript quirks (Gmail move, date format normalization). Tracked separately.

## Design

### AppleScript pattern

Each refactored script has this shape:

```applescript
use framework "Foundation"
use scripting additions

tell application "Mail"
    try
        -- build a native AppleScript list or record with descriptive keys
        set resultData to ...
    on error errMsg
        return "ERROR: " & errMsg
    end try
end tell

-- NSJSONSerialization lives outside the tell block for stability
set jsonData to (current application's NSJSONSerialization's ¬
    dataWithJSONObject:resultData options:0 |error|:(missing value))
return (current application's NSString's alloc()'s ¬
    initWithData:jsonData encoding:4) as text
```

Verified via osascript smoke test: AppleScript records auto-bridge to NSDictionary with property names as JSON keys; NSJSONSerialization handles quote, apostrophe, pipe, backslash, Unicode, integer, and boolean escaping correctly. Test input `{myName:"Alice\"O'Brien", myAge:30, isAdmin:true}` produced `{"myAge":30,"myName":"Alice\"O'Brien","isAdmin":true}`.

### Python-side wrapper

To keep scripts DRY, add a single helper in `mail_connector.py`:

```python
def _wrap_as_json_script(body: str) -> str:
    """Wrap a tell-block body with ASObjC imports + NSJSONSerialization epilogue.

    The `body` must assign the result to a variable named `resultData` inside
    a `tell application "Mail"` block, and return `"ERROR: ..."` on failure
    via `try/on error`.
    """
    return f'''
    use framework "Foundation"
    use scripting additions

    {body}

    set jsonData to (current application's NSJSONSerialization's dataWithJSONObject:resultData options:0 |error|:(missing value))
    return (current application's NSString's alloc()'s initWithData:jsonData encoding:4) as text
    '''
```

Each of the 5 methods uses this helper, writing only the Mail-specific middle.

### Python-side parse

New utility in `src/apple_mail_mcp/utils.py`:

```python
def parse_applescript_json(result: str) -> Any:
    """Parse JSON emitted by an AppleScript helper, or raise on ERROR: prefix."""
    result = result.strip()
    if result.startswith("ERROR:"):
        raise MailAppleScriptError(result[len("ERROR:"):].strip())
    return json.loads(result)
```

Each method's body becomes:

```python
script = _wrap_as_json_script(tell_body)
result = self._run_applescript(script)
return parse_applescript_json(result)  # list or dict
```

### Field naming

AppleScript property names become JSON keys become Python dict keys. Use `snake_case` throughout: `read_status`, `mime_type`, `unread_count`, `email_addresses`, `date_received`. AppleScript accepts these as record property names.

### Per-method output shapes

| Method | Return type | Keys |
|---|---|---|
| `list_accounts` | `list[dict]` | `name`, `email_addresses` (list of str) |
| `list_mailboxes` | `list[dict]` | `name`, `unread_count` |
| `search_messages` | `list[dict]` | `id`, `subject`, `sender`, `date_received`, `read_status` |
| `get_message` | `dict` | `id`, `subject`, `sender`, `date_received`, `read_status`, `flagged`, `content` |
| `get_attachments` | `list[dict]` | `name`, `mime_type`, `size`, `downloaded` |

### Error handling

Unchanged at the boundary. `tell` blocks have `try/on error errMsg … return "ERROR: " & errMsg`. `parse_applescript_json` detects the prefix. The existing typed-exception mapping in `_run_applescript()` (account-not-found, mailbox-not-found, message-not-found) continues to run on stderr before parsing ever sees the result.

### Testing

- **Unit tests:** Update 3 existing mocks (2 in `tests/unit/test_mail_connector.py`, 1 in `tests/unit/test_attachments.py`) from pipe-delimited strings to JSON. Add tests for `list_accounts` and `list_mailboxes` now that they return structured data.
- **New unit tests:** `tests/unit/test_utils.py` gains cases for `parse_applescript_json`: valid JSON, `ERROR:` prefix raises `MailAppleScriptError`, malformed JSON raises `json.JSONDecodeError`, whitespace handling.
- **Integration tests:** Existing `list_mailboxes` and `search_messages` tests already exist. Add 3 more: `list_accounts`, `get_message`, `get_attachments`. These prove the ASObjC pattern produces valid JSON against real Mail.app.

### Migration strategy

Big-bang in one PR. 5 scripts, 5 parse sites, 3 updated mocks, 2 new unit methods, 3 new integration tests. Per-script migration would double the test-fixture churn for no benefit.

### Backwards compatibility

The server layer (`server.py`) wraps connector returns into `{"success": True, "messages": [...]}` etc. The connector's internal return shape changes (from `{"raw": str}` for `list_accounts`/`list_mailboxes`, and from pipe-split tuples for the other three, to proper dicts), which means:

- `server.py` callers that previously did `[{"raw": ...}]` workarounds get deleted.
- MCP tool public shapes gain real fields for `list_accounts` and `list_mailboxes` (they were unusable before). This is an improvement, not a break — no caller was relying on the `"raw"` field.

## Verification

1. `make test` — all unit tests pass; coverage ≥ 90%.
2. `make test-e2e` — existing 20 e2e tests still pass (they mock the connector; no AppleScript change affects them).
3. `make test-integration` — run locally against a real Mail.app account. The 5 refactored methods return well-typed data; no `{"raw": ...}` shapes remain.
4. `make check-all` — lint, typecheck, complexity, parity all green.
5. Manual smoke: hit each of the 5 methods via the MCP client and confirm structured output.

## Follow-ups (out of scope)

- Normalize `date_received` to ISO 8601. Currently each script emits the AppleScript default string format ("Friday, January 1, 2024 at 12:00:00 PM"). File a new issue.
- Review other connector methods' return types for opportunities to tighten now that JSON is available — e.g., `send_email` returning the new message ID as a typed dict instead of a string.
