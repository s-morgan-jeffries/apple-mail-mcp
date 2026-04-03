---
name: applescript-mail
description: Use when writing, modifying, or debugging ANY AppleScript that interacts with Apple Mail. Also use when encountering AppleScript errors, unexpected Mail.app behavior, when adding new connector methods, or when debugging pipe-delimited output parsing. Covers string escaping, attachment handling, Gmail compatibility, message ID lookup patterns, and known Mail.app automation limitations.
---

# AppleScript + Apple Mail Patterns

## Critical: Pipe-Delimited Output Parsing

Apple Mail MCP returns AppleScript results as pipe-delimited strings with newline-separated records:

```applescript
-- AppleScript returns:
-- "12345|Subject Here|sender@example.com|Mon Jan 1 2024|false"

-- Python parses:
parts = line.split("|")
# parts[0] = message_id, parts[1] = subject, etc.
```

**Known fragility:** If ANY field contains a `|` character (common in email subjects), parsing breaks silently. There is no escaping mechanism. This is the project's biggest technical debt.

**Workaround:** Fields are positional. Always check `len(parts) >= expected_count` before accessing. Log warnings on unexpected field counts.

**Future fix:** Migrate to JSON output with inline helpers (duplicated per script — AppleScript has no modules, same pattern as OmniFocus MCP).

## Gmail Label-Based System

Gmail doesn't support standard IMAP move operations. The `move_messages` tool has a `gmail_mode` parameter:

```python
# Standard IMAP (Exchange, iCloud, etc.)
move message to destination_mailbox

# Gmail mode (copy + delete)
duplicate message to destination_mailbox
delete message  # Removes from source label
```

**Bug story:** Early versions silently failed when moving Gmail messages. The move appeared to succeed but the message stayed in the source mailbox. `gmail_mode` was added to handle this.

**When to use:** Always expose `gmail_mode` as an optional parameter on any tool that moves or archives messages.

## Message ID Lookup

Finding a specific message by ID requires searching across accounts and mailboxes:

```applescript
tell application "Mail"
    set allAccounts to every account
    repeat with acct in allAccounts
        set allMailboxes to every mailbox of acct
        repeat with mbox in allMailboxes
            set msgs to (messages of mbox whose id is targetId)
            if (count of msgs) > 0 then
                return first item of msgs
            end if
        end repeat
    end repeat
end tell
```

**Performance:** This is O(accounts × mailboxes). For users with many accounts, this can be slow. The `whose` clause makes it tolerable but not fast.

**Optimization:** If the caller knows the account and mailbox, always accept them as optional parameters to narrow the search.

## String Escaping

**Always use `escape_applescript_string()` for user-provided text:**

```python
# In utils.py — escapes backslashes first, then double quotes
def escape_applescript_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
```

**Bug story:** Unescaped quotes in email subjects caused AppleScript blocks to fail silently. The error appears as a generic "Can't make" error in stderr with no indication of the actual cause.

**Rule:** Every string interpolated into AppleScript MUST go through `escape_applescript_string()`. No exceptions. Check via `check_applescript_safety.sh`.

## Attachment Handling

Attachments use POSIX file references:

```applescript
-- Sending attachments
set theAttachment to POSIX file "/Users/user/file.pdf"
make new attachment with properties {file name: theAttachment} at after the last paragraph

-- Saving attachments
save attachment theAttach in POSIX file "/Users/user/Downloads/"
```

**Path conversion:** Python `Path` objects → `.as_posix()` → AppleScript `POSIX file "..."`.

**Security:** Always validate:
- File exists before sending
- Directory exists before saving
- No path traversal (`..` in path)
- Extension not in blocklist (.exe, .bat, .sh, .app, etc.)
- Size under 25MB limit

## `whose` Clause Filtering

Use AppleScript `whose` clauses for server-side filtering instead of fetching all messages:

```applescript
-- GOOD: Server-side filter (fast)
set msgs to (messages of mbox whose sender contains "user@example.com")

-- BAD: Fetch all then filter in Python (slow)
set msgs to every message of mbox
-- then filter in Python
```

**Combine clauses** for multi-field search:
```applescript
messages whose sender contains "user" and subject contains "report"
```

**Limitation:** `whose` clauses don't support OR logic well. For OR conditions, use multiple `whose` queries and merge results in Python.

## Known Mail.app Automation Limitations

1. **No scheduled sending** — Mail.app has no AppleScript support for delayed/scheduled sends
2. **No thread/conversation access** — Messages are individual objects; no thread grouping in AppleScript API
3. **No rule management** — Mail rules cannot be read or modified via AppleScript
4. **No smart mailbox access** — Smart mailboxes are not exposed to AppleScript
5. **Rich text body** — `content of message` returns plain text; HTML body requires alternate approach
6. **Read receipt** — Cannot request or detect read receipts
7. **Draft management** — Creating drafts is possible but managing them is limited

## Error Handling Pattern

```applescript
try
    -- operation
    return "result_data"
on error errMsg
    return "ERROR: " & errMsg
end try
```

**Python-side parsing:**
```python
if result.startswith("ERROR:"):
    raise MailAppleScriptError(result[7:])
```

**stderr-based errors** are caught in `_run_applescript()` and routed to typed exceptions:
- `"Can't get account"` → `MailAccountNotFoundError`
- `"Can't get mailbox"` → `MailMailboxNotFoundError`
- `"Can't get message"` → `MailMessageNotFoundError`
- Everything else → `MailAppleScriptError`

## Checklist: New AppleScript Operation

1. [ ] All user strings escaped with `escape_applescript_string()`
2. [ ] All inputs sanitized with `sanitize_input()`
3. [ ] Error handling with `try/on error` in AppleScript
4. [ ] Timeout considered (complex operations may need > 60s)
5. [ ] Integration test written against real Mail.app
6. [ ] `check_applescript_safety.sh` passes
7. [ ] Gmail compatibility considered (does this operation work with labels?)
