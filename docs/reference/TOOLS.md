# Tools Documentation

Complete reference for all MCP tools provided by the Apple Mail MCP server.

## Overview

**Current Version:** v0.6.0
**Total Tools:** 27

## Phase 1 Tools (v0.1.0) - Core Foundation

### search_messages

Search for messages matching specified criteria.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Conditional | None | Account name (e.g., "Gmail", "iCloud"). Required when `source` is None; ignored when `source` is a list. |
| `mailbox` | string | No | "INBOX" | Mailbox/folder name. Ignored when `source` is a list. |
| `sender_contains` | string | No | None | Filter by sender email or domain |
| `subject_contains` | string | No | None | Filter by subject keywords |
| `read_status` | boolean | No | None | Filter by read status (true=read, false=unread) |
| `is_flagged` | boolean | No | None | Filter by flagged status (true=flagged, false=not flagged) |
| `date_from` | string | No | None | Inclusive lower bound on `date_received`. ISO 8601 YYYY-MM-DD. |
| `date_to` | string | No | None | Inclusive upper bound on `date_received` (full day included). ISO 8601 YYYY-MM-DD. |
| `has_attachment` | boolean | No | None | Filter messages with (true) or without (false) attachments |
| `limit` | integer | No | 50 | Maximum number of results to return |
| `source` | list[string] \| null | No | null | Optional list of message ids (with optional `"SELECTED"` sentinel) to scope the search to. `null` (default) searches the account/mailbox normally. |
| `include_attachments` | boolean | No | false | When true, each row includes an `attachments` field with per-attachment metadata. Default off — opt-in because the AppleScript fallback path can be slow on cold caches (#142). Free on the IMAP fast path. |
| `body_contains` | string | No | None | Substring match against message body content. IMAP: server-side `BODY` predicate (sub-second). AppleScript: per-message body read (very slow — see performance note). Case-insensitive. |
| `text_contains` | string | No | None | Substring match against headers + body (RFC 3501 `TEXT`). IMAP: server-side `TEXT` predicate. AppleScript: matches `content + subject + sender` (recipients omitted). Same perf characteristics as `body_contains`. |

**Notes:**
- Returns metadata-only rows (id, subject, sender, date_received, read_status, flagged). For full bodies, pipe the result ids into `get_messages([ids])`.
- Malformed `date_from` / `date_to` raise `error_type: validation_error`. Only ISO 8601 YYYY-MM-DD is accepted; relative dates like "7 days ago" are not supported.
- `has_attachment` is filtered after the initial server-side match because Mail.app rejects attachment predicates inside its `whose` clause.
- `source=[ids]` (folded-in `get_selected_messages` and the `thread_of` use case) scopes the search to a specific id list. Filter parameters (`sender_contains`, `read_status`, etc.) compose with `source` — the resolved messages are post-filtered. The literal token `"SELECTED"` may appear in the list and is server-resolved to Mail.app's current UI selection (zero-or-more ids); mixed lists like `["SELECTED", "12345"]` are valid. Returns `account: null` and `mailbox: null` in the response. Missing ids drop out silently (partial-results convention).
- For thread retrieval, call `get_thread(message_id)` to expand an anchor into thread member ids; pipe those ids into `source=[ids]` for filtered metadata.
- Omitting both `account` and `source` returns `error_type: validation_error`.
- `include_attachments` defaults to **false** for `search_messages` (unlike `get_messages` which defaults to true). Reason: search results can span 50+ rows, and the AppleScript fallback path enumerates attachments per row — measured 1s for 50 messages but 97s for 100 cold-cache messages on a 47k-message Gmail INBOX (#142). To get attachment metadata for a small known set, prefer the two-step: `search_messages(...)` to get ids → `get_messages([those_ids])` (default-on attachments, bounded cardinality).

**Performance note for `body_contains` / `text_contains`:**

On the IMAP path, body search is server-side and sub-second. On the AppleScript fallback, body search is **dramatically slower** — measured 148s for 100 cold-cache messages on a 47k-message INBOX, vs 1s for `subject_contains` on the same slice. This is because Mail.app must read each candidate message's body from disk. To get sub-second body search, run `apple-mail-mcp setup-imap --account <name>` to enable IMAP delegation for that account.

When the call commits to the AppleScript path **and** a body/text filter is set, the response includes a `warnings` field describing the cost — see "Warnings" below.

**Warnings field:**

`search_messages` responses may include an optional `warnings: list[str]` field that surfaces proactive cost concerns before slow paths run. The field is **omitted** when there are no warnings (don't pollute the cheap-call default case). Currently only fires for AppleScript-path body/text search; future detection conditions may extend the mechanism. Example:

```json
{
  "success": true,
  "messages": [...],
  "count": 17,
  "warnings": [
    "AppleScript body search can take minutes on large mailboxes (measured 148s for 100 cold-cache messages on a 47k-message Gmail INBOX). Run `apple-mail-mcp setup-imap --account 'Gmail'` for sub-second IMAP body search."
  ]
}
```

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "mailbox": "INBOX",
  "messages": [
    {
      "id": "12345",
      "subject": "Meeting Tomorrow",
      "sender": "john@example.com",
      "date_received": "Mon Jan 15 2024 10:30:00",
      "read_status": false,
      "flagged": false
    }
  ],
  "count": 1
}
```

**Examples:**

```python
# Find all unread messages
search_messages(account="Gmail", read_status=False)

# Find messages from specific sender
search_messages(account="Gmail", sender_contains="john@example.com")

# Return metadata for Mail.app's current UI selection
search_messages(source=["SELECTED"])

# Scope to specific ids (e.g., from a prior get_thread call)
search_messages(source=["12345", "67890"])

# Mixed list: selection plus an explicit id
search_messages(source=["SELECTED", "12345"])

# Filter the selection to unread messages only
search_messages(source=["SELECTED"], read_status=False)

# Find messages with keyword in subject
search_messages(account="Gmail", subject_contains="invoice", limit=10)

# Complex search
search_messages(
    account="Gmail",
    mailbox="Work",
    sender_contains="@company.com",
    subject_contains="urgent",
    read_status=False,
    limit=20
)
```

**Error Codes:**

- `account_not_found`: Specified account doesn't exist
- `not_found`: Mailbox not found
- `unknown`: Unexpected error occurred

---

### get_messages

Retrieve full details of one or more messages, with bodies. Returns a list (always — possibly of length 0 or 1).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | List of message ids to fetch. May include the literal token `"SELECTED"` (server-resolved to Mail.app's current UI selection at call time). Mixed lists like `["SELECTED", "12345"]` are valid. Empty list is a no-op. |
| `include_content` | boolean | No | true | Include message bodies |
| `headers_only` | boolean | No | false | IMAP fast-path optimization for explicit ids; ignored on AppleScript fallback |
| `account` | string | No | None | Mail.app account name. With `mailbox`, activates the IMAP fast path for explicit ids (issue #72) |
| `mailbox` | string | No | None | Folder for the IMAP fast path (e.g. "INBOX") |
| `include_attachments` | boolean | No | true | When true, each message gains an `attachments: [{name, mime_type, size, downloaded}]` field. Default on for `get_messages` because id-list cardinality is bounded (typically 1-10) — cost is acceptable on both paths. |

**Notes:**
- Missing ids drop out silently — the response contains whatever was found (partial-results convention).
- The `"SELECTED"` sentinel is resolved server-side via `mail.get_selected_messages()` at call time. Empty selection expands to nothing.
- Pair with `search_messages` (metadata-only, criteria-based) and `get_thread` (thread member ids) to fetch bodies for specific messages.

**Performance note (path-dependent cost):**

For accounts configured with IMAP (via `apple-mail-mcp setup-imap --account <name>`), `include_attachments` is essentially free — `BODYSTRUCTURE` bundles into the existing FETCH. For accounts without IMAP, the AppleScript fallback enumerates attachments per message — fine for small id lists (1-10) but can be slow on cold caches for larger lists. If you have a mix of IMAP-configured and non-IMAP accounts, expect variance. To opt out: pass `include_attachments=False`.

**Returns:**

```json
{
  "success": true,
  "messages": [
    {
      "id": "12345",
      "subject": "Meeting Tomorrow",
      "sender": "john@example.com",
      "date_received": "Mon Jan 15 2024 10:30:00",
      "read_status": false,
      "flagged": true,
      "content": "Let's meet tomorrow at 2pm to discuss the project..."
    }
  ],
  "count": 1
}
```

**Examples:**

```python
# Get a single message with body
get_messages(["12345"])

# Get the user's current selection (full bodies)
get_messages(["SELECTED"])

# Mixed: selection plus an explicit id
get_messages(["SELECTED", "12345"])

# Skip body fetch on the IMAP fast path
get_messages(["abc@x"], account="iCloud", mailbox="INBOX", headers_only=True)
```

**Error Codes:**

- `unknown`: Unexpected error occurred

---

### get_thread

Return all messages in the thread containing the given anchor message, sorted by `date_received` ascending. Result rows are metadata-only — pipe ids into `get_messages([ids])` for full bodies, or into `search_messages(source=[ids], ...)` for filtered metadata.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_id` | string | Yes | - | Internal id of any message in the thread (from `search_messages` or `get_messages` results). |

**Returns:**

```json
{
  "success": true,
  "thread": [
    {"id": "100", "subject": "Q3 Report", "sender": "alice@x.com",
     "date_received": "Mon Jan 1 2024 10:00:00", "read_status": true, "flagged": false},
    {"id": "101", "subject": "Re: Q3 Report", "sender": "bob@x.com",
     "date_received": "Mon Jan 1 2024 14:30:00", "read_status": true, "flagged": false}
  ],
  "count": 2
}
```

Uses the connector's tiered IMAP threading dispatch (Tier 1 X-GM-THRID for Gmail per #122, Tier 3 header-search BFS fallback) when IMAP is configured; falls back to AppleScript otherwise.

**Examples:**

```python
# Get the conversation around a message found via search
matches = search_messages(account="Gmail", subject_contains="Q3")
thread = get_thread(matches["messages"][0]["id"])

# Pipe thread ids into get_messages for bodies
ids = [m["id"] for m in thread["thread"]]
full = get_messages(ids)
```

**Error Codes:**

- `message_not_found`: Anchor message doesn't exist or was deleted
- `unknown`: Unexpected error occurred

---

### send_email

Send an email via Apple Mail.

**⚠️ Security Note:** This operation requires user confirmation before sending.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `subject` | string | Yes | - | Email subject line |
| `body` | string | Yes | - | Email body (plain text) |
| `to` | array[string] | Yes | - | List of recipient email addresses |
| `from_account` | string | No | None | Mail.app account name or UUID to send from. None = Mail.app default sender. Useful when the user has multiple configured accounts. Returns `error_type: "account_not_found"` if no match. |
| `cc` | array[string] | No | [] | List of CC recipients |
| `bcc` | array[string] | No | [] | List of BCC recipients |

**Returns:**

```json
{
  "success": true,
  "message": "Email sent successfully",
  "details": {
    "subject": "Meeting Tomorrow",
    "recipients": 3
  }
}
```

**Examples:**

```python
# Simple email
send_email(
    subject="Hello",
    body="Just wanted to say hi!",
    to=["friend@example.com"]
)

# Email with CC and BCC
send_email(
    subject="Project Update",
    body="Here's the latest status...",
    to=["team@company.com"],
    cc=["manager@company.com"],
    bcc=["archive@company.com"]
)

# Email to multiple recipients
send_email(
    subject="Team Meeting",
    body="Meeting at 2pm today.",
    to=["alice@company.com", "bob@company.com", "charlie@company.com"]
)
```

**Validation Rules:**

- At least one `to` recipient required
- Maximum 100 total recipients (to + cc + bcc)
- All email addresses must be valid format
- User confirmation required before sending

**Error Codes:**

- `validation_error`: Invalid recipients or parameters
- `cancelled`: User cancelled the send operation
- `send_error`: Mail.app failed to send the email
- `unknown`: Unexpected error occurred

---

### list_mailboxes

List all mailboxes (folders) for a specific account.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Account name (e.g., "Gmail", "iCloud") |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "mailboxes": [
    {
      "name": "INBOX",
      "unread_count": 5
    },
    {
      "name": "Sent",
      "unread_count": 0
    },
    {
      "name": "Archive",
      "unread_count": 2
    }
  ]
}
```

**Examples:**

```python
# List mailboxes
list_mailboxes(account="Gmail")

# List mailboxes for different account
list_mailboxes(account="iCloud")
```

**Error Codes:**

- `account_not_found`: Account doesn't exist
- `unknown`: Unexpected error occurred

---

### update_message

Patch one or more messages: change read state, flag color, and/or move to another mailbox in a single call. Replaces `mark_as_read`, `move_messages`, and `flag_message`.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | Up to 100 message IDs to update |
| `read_status` | boolean \| null | No | null | `True` marks read, `False` marks unread, `null` leaves unchanged |
| `flagged` | boolean \| null | No | null | `True` flags (orange if no `flag_color` given), `False` clears, `null` leaves unchanged |
| `flag_color` | string \| null | No | null | One of `orange`, `red`, `yellow`, `blue`, `green`, `purple`, `gray`, `none`. `"none"` clears the flag. Implies `flagged=True` for non-`none` values. |
| `destination_mailbox` | string \| null | No | null | Target mailbox name to move to. Requires `account`. |
| `account` | string \| null | No | null | Account name (required when `destination_mailbox` is set; also unlocks the IMAP narrow-path optimization) |
| `source_mailbox` | string \| null | No | null | Optional narrow-path hint — if all messages live in this mailbox, the AppleScript scan is bypassed (IMAP fast path) |
| `gmail_mode` | boolean | No | false | Use Gmail-specific copy+delete instead of move (label-based accounts) |

**Patch semantics:** caller specifies only the fields they want changed. At least one field parameter must be set; otherwise returns `validation_error`.

**Order of operations:** read-state and flag changes apply first (in the source mailbox), then the move. IMAP requires the message to exist in the source folder for STORE before MOVE.

**Returns:**

```json
{
  "success": true,
  "count": 3
}
```

**Examples:**

```python
# Mark messages as read
update_message(message_ids=["12345", "12346"], read_status=True)

# Flag a message red
update_message(message_ids=["12345"], flag_color="red")

# Clear a flag
update_message(message_ids=["12345"], flagged=False)

# Move to Archive on a Gmail account
update_message(
    message_ids=["12345"],
    destination_mailbox="Archive",
    account="Gmail",
    gmail_mode=True,
)

# Restore from Trash — no special verb required
update_message(
    message_ids=["12345"],
    destination_mailbox="INBOX",
    source_mailbox="Deleted Messages",
    account="iCloud",
)

# Combined: mark read, flag green, and move — all in one AppleScript pass
update_message(
    message_ids=["12345"],
    read_status=True,
    flag_color="green",
    destination_mailbox="Done",
    account="Work",
)
```

**Validation Rules:**

- Maximum 100 message IDs per request
- At least one of `read_status`, `flagged`, `flag_color`, `destination_mailbox` must be set
- `destination_mailbox` requires `account`

**Error Codes:**

- `validation_error`: Too many IDs, no fields set, or missing `account` for move
- `account_not_found`: `account` does not match a configured Mail.app account
- `not_found`: `destination_mailbox` not found on the account
- `unknown`: Unexpected error occurred

---

## Coming Soon (Phase 2 - v0.2.0)

### send_email_with_attachments

Send email with file attachments.

**Parameters:**
- `subject`, `body`, `to`, `cc`, `bcc` (same as send_email)
- `attachments`: array[string] - File paths to attach

### create_mailbox

Create a new mailbox/folder.

**Parameters:**
- `account`: string - Account name
- `name`: string - Mailbox name
- `parent_mailbox`: string (optional) - Parent for nested mailboxes

### delete_messages

Delete messages (move to trash).

**Parameters:**
- `message_ids`: array[string] - Messages to delete
- `confirm`: boolean - Require confirmation

---

## Error Handling

All tools return a consistent error format:

```json
{
  "success": false,
  "error": "Detailed error message",
  "error_type": "error_category"
}
```

**Common Error Types:**

- `account_not_found`: Account doesn't exist
- `mailbox_not_found`: Mailbox doesn't exist
- `message_not_found`: Message doesn't exist or was deleted
- `validation_error`: Invalid parameters
- `permission_error`: Insufficient permissions
- `cancelled`: User cancelled the operation
- `unknown`: Unexpected error

---

## Best Practices

### Search Performance

```python
# Good: Use specific filters
search_messages(
    account="Gmail",
    sender_contains="@company.com",
    read_status=False,
    limit=20
)

# Bad: Retrieve everything then filter
all_messages = search_messages(account="Gmail", limit=10000)
# ... filter in Python
```

### Error Handling

```python
# Always check success field
result = search_messages(account="Gmail")

if result["success"]:
    messages = result["messages"]
    print(f"Found {result['count']} messages")
else:
    print(f"Error: {result['error']}")
    print(f"Type: {result['error_type']}")
```

### Batch Operations

```python
# Good: Process in batches
message_ids = [...]  # Large list
for i in range(0, len(message_ids), 100):
    batch = message_ids[i:i+100]
    update_message(message_ids=batch, read_status=True)

# Bad: Single request with too many IDs
update_message(message_ids=message_ids, read_status=True)  # May fail if > 100
```

### Account Names

```python
# Use exact account name from Mail.app
# Check in Mail → Settings → Accounts

# Good
list_mailboxes(account="Gmail")

# Bad (won't work)
list_mailboxes(account="gmail")
list_mailboxes(account="my gmail account")
```

---

## Security Considerations

### Sending Emails

- All send operations require user confirmation
- Validate recipients before sending
- Limit recipient count to prevent spam
- Operations are logged for audit trail

### Input Validation

- All inputs are sanitized and validated
- Email addresses must match valid format
- Message IDs are sanitized
- File paths are validated (Phase 2+)

### Rate Limiting

- Bulk operations limited to 100 items
- Consider implementing additional rate limits for production use

---

## Phase 2 Tools (v0.2.0)

### send_email_with_attachments

Send an email with file attachments via Apple Mail.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `subject` | string | Yes | - | Email subject line |
| `body` | string | Yes | - | Email body content |
| `to` | list[string] | Yes | - | List of recipient email addresses |
| `attachments` | list[string] | Yes | - | List of file paths to attach |
| `from_account` | string | No | None | Mail.app account name or UUID to send from. None = Mail.app default sender. Returns `error_type: "account_not_found"` if no match. |
| `cc` | list[string] | No | None | CC recipients |
| `bcc` | list[string] | No | None | BCC recipients |

**Returns:**

```json
{
  "success": true,
  "message_id": "67890",
  "recipients": ["recipient@example.com"],
  "attachment_count": 2
}
```

**Examples:**

```python
# Send email with single attachment
send_email_with_attachments(
    subject="Monthly Report",
    body="Please find the report attached.",
    to=["manager@company.com"],
    attachments=["/Users/me/Documents/report.pdf"]
)

# Send with multiple attachments
send_email_with_attachments(
    subject="Project Files",
    body="Here are all the project files.",
    to=["team@company.com"],
    cc=["manager@company.com"],
    attachments=[
        "/Users/me/Projects/design.pdf",
        "/Users/me/Projects/specs.docx"
    ]
)
```

**Security Notes:**
- File size limit: 25MB per attachment (default)
- Dangerous file types blocked by default (.exe, .bat, .sh, etc.)
- Path traversal attacks prevented
- All file paths validated before sending

---

### save_attachments

Save attachments from a message to a directory.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_id` | string | Yes | - | Message ID to save attachments from |
| `save_directory` | string | Yes | - | Directory path to save attachments |
| `attachment_indices` | list[int] | No | None | Specific attachment indices (None = all) |

**Returns:**

```json
{
  "success": true,
  "count": 2,
  "directory": "/Users/me/Downloads",
  "saved_files": [
    "report.pdf",
    "data.xlsx"
  ]
}
```

**Examples:**

```python
# Save all attachments
save_attachments(
    message_id="12345",
    save_directory="/Users/me/Downloads"
)

# Save specific attachments only
save_attachments(
    message_id="12345",
    save_directory="/Users/me/Downloads",
    attachment_indices=[1, 3]  # Save 1st and 3rd only
)
```

**Security Notes:**
- Directory must exist and be writable
- Path traversal attacks prevented
- Filenames sanitized for safety
- Existing files will be overwritten

---

### create_mailbox

Create a new mailbox/folder.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Account name to create mailbox in |
| `name` | string | Yes | - | Name of the new mailbox |
| `parent_mailbox` | string | No | None | Parent mailbox for nesting (None = top-level) |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "mailbox": "Client Work",
  "parent": "Projects"
}
```

**Examples:**

```python
# Create top-level mailbox
create_mailbox(
    account="Gmail",
    name="Archive"
)

# Create nested mailbox
create_mailbox(
    account="Gmail",
    name="Client Work",
    parent_mailbox="Projects"
)

# Create organizational structure
create_mailbox(account="Gmail", name="2024")
create_mailbox(account="Gmail", name="Q1", parent_mailbox="2024")
create_mailbox(account="Gmail", name="Q2", parent_mailbox="2024")
```

**Security Notes:**
- Mailbox names sanitized for safety
- Path traversal attacks prevented
- Special characters removed

---

### delete_messages

Delete messages — always moves them to the account's Trash mailbox.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | List of message IDs to delete |
| `permanent` | boolean | No | False | Reserved; currently a no-op. Passing `True` emits a `DeprecationWarning`. See [issue #111](https://github.com/s-morgan-jeffries/apple-mail-mcp/issues/111). |

**Returns:**

```json
{
  "success": true,
  "count": 2,
  "permanent": false
}
```

**Examples:**

```python
# Move messages to trash
delete_messages(
    message_ids=["12345", "12346"],
)
```

**Note on `permanent`:**

Mail.app's AppleScript dictionary exposes no path to permanent-delete that bypasses Trash. Calling `delete msg` always moves to the account's Trash; calling `delete` again on a message already in Trash is a no-op, and there is no `empty trash` command. The `permanent` parameter is preserved for API compatibility but currently has no effect; passing `True` raises a `DeprecationWarning` so the gap is visible. Track #111 for status.

**Safety Notes:**
- Bulk deletions limited to 100 messages for safety
- All deletes are recoverable from the account's Trash mailbox until that mailbox is emptied (typically by Mail.app's per-account "empty trash" schedule, configurable in Mail's preferences)

---

## Phase 3 Tools (v0.3.0)

### reply_to_message

Reply to a message.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_id` | string | Yes | - | ID of the message to reply to |
| `body` | string | Yes | - | Reply body text |
| `reply_all` | boolean | No | False | If True, reply to all recipients; if False, reply only to sender |
| `from_account` | string | No | None | Mail.app account name or UUID to send the reply from. None = Mail.app default sender. Returns `error_type: "account_not_found"` if no match. |

**Returns:**

```json
{
  "success": true,
  "reply_id": "67890",
  "original_message_id": "12345",
  "reply_all": false
}
```

**Examples:**

```python
# Reply to sender only
reply_to_message(
    message_id="12345",
    body="Thanks for your email! I'll get back to you soon."
)

# Reply to all recipients
reply_to_message(
    message_id="12345",
    body="Thanks everyone for the discussion.",
    reply_all=True
)

# Quick acknowledgment
reply_to_message(
    message_id="12345",
    body="Received, thank you!"
)
```

**Notes:**
- Reply automatically maintains proper email threading
- Original subject is preserved with "Re:" prefix
- Reply-To headers are respected
- Message is sent immediately after creation

---

### forward_message

Forward a message to recipients.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_id` | string | Yes | - | ID of the message to forward |
| `to` | list[string] | Yes | - | List of recipient email addresses |
| `body` | string | No | "" | Optional body text to add before forwarded content |
| `from_account` | string | No | None | Mail.app account name or UUID to send the forward from. None = Mail.app default sender. Returns `error_type: "account_not_found"` if no match. |
| `cc` | list[string] | No | None | Optional CC recipients |
| `bcc` | list[string] | No | None | Optional BCC recipients |

**Returns:**

```json
{
  "success": true,
  "forward_id": "67890",
  "original_message_id": "12345",
  "recipients": ["colleague@example.com"],
  "cc": null,
  "bcc": null
}
```

**Examples:**

```python
# Simple forward
forward_message(
    message_id="12345",
    to=["colleague@example.com"]
)

# Forward with context
forward_message(
    message_id="12345",
    to=["team@company.com"],
    body="FYI - thought this would be relevant to our project."
)

# Forward to multiple recipients with CC
forward_message(
    message_id="12345",
    to=["colleague1@example.com", "colleague2@example.com"],
    cc=["manager@example.com"],
    body="Please review this email thread."
)
```

**Notes:**
- Original message content is automatically included
- Attachments are preserved by default
- Subject is prefixed with "Fwd:"
- Email validation is enforced for all recipients
- Message is sent immediately after creation

---

## Tool Combinations

### Example Workflows

**Inbox Zero Workflow:**

```python
# 1. Find all unread messages
unread = search_messages(account="Gmail", read_status=False)

# 2. For each message, get full details
for msg in unread["messages"]:
    full_msg = get_message(message_id=msg["id"])
    # Process message...

# 3. Mark processed messages as read
processed_ids = [msg["id"] for msg in unread["messages"]]
update_message(message_ids=processed_ids, read_status=True)
```

**Email Response Workflow:**

```python
# 1. Search for specific email
results = search_messages(
    account="Gmail",
    sender_contains="client@company.com",
    subject_contains="proposal",
    limit=1
)

# 2. Get full message
original = get_message(message_id=results["messages"][0]["id"])

# 3. Send reply
send_email(
    subject=f"Re: {original['message']['subject']}",
    body="Thank you for your proposal...",
    to=[original["message"]["sender"]]
)
```

---

## Phase 4 Tools (v0.5.0)

### list_rules

List all Mail.app rules. Returns each rule's 1-based positional index, name, and enabled state. The `index` is the handle the mutation tools (`update_rule`, `delete_rule`) use to address a specific rule.

**Parameters:** None.

**Returns:**

```json
{
  "success": true,
  "rules": [
    {"index": 1, "name": "Junk filter", "enabled": true},
    {"index": 2, "name": "News From Apple", "enabled": false}
  ],
  "count": 2
}
```

**Field notes:**

- `index`: 1-based positional index, matching Mail.app's AppleScript reference (`rule N`). Indexes can shift if rules are reordered or deleted in Mail's UI between calls — re-fetch the list before mutating.
- `name`: Rule display name. **Not guaranteed unique** — Mail.app allows multiple rules with the same name. Use `index`, not `name`, for unambiguous addressing.
- `enabled`: Reflects the rule's toggle in Mail.app's Rules preferences.

---

### create_rule

Create a new rule. Appended at the end of the rules list.

**Parameters:**

- `name` (str, required): Display name. Need not be unique.
- `conditions` (list, required, ≥1): List of `{field, operator, value}` records. `field` ∈ `from`, `to`, `subject`, `body`, `any_recipient`, `header_name`. `operator` ∈ `contains`, `does_not_contain`, `begins_with`, `ends_with`, `equals`. When `field=="header_name"`, an additional `header_name` key is required to specify which header to test.
- `actions` (dict, required, ≥1 action): Any subset of `move_to`, `copy_to`, `mark_read`, `mark_flagged`, `flag_color`, `delete`, `forward_to`. `move_to`/`copy_to` take `{account, mailbox}`. `flag_color` ∈ `none`, `red`, `orange`, `yellow`, `green`, `blue`, `purple`, `gray` and is only meaningful with `mark_flagged: true`. `forward_to` is a list of email addresses.
- `match_logic` (str, default `"all"`): `"all"` requires every condition; `"any"` requires at least one.
- `enabled` (bool, default `true`): Whether the rule is active immediately.

**Returns:**

```json
{"success": true, "rule_index": 7, "name": "From OmniFocus support"}
```

No confirmation prompt — creation is additive and the rule can be deleted afterward.

**Example:**

```python
create_rule(
    name="File OmniFocus replies",
    conditions=[{"field": "from", "operator": "contains", "value": "@omnifocus.com"}],
    actions={"move_to": {"account": "Personal", "mailbox": "Support"}, "mark_read": True},
)
```

---

### update_rule

Patch a rule's properties. Only the fields you pass are changed. Also serves as the enable/disable mechanism — pass `enabled=True|False` (the standalone `set_rule_enabled` tool was folded into this one in #130).

**Parameters:**

- `rule_index` (int, required): 1-based index from `list_rules`.
- `name` (str, optional): New display name.
- `enabled` (bool, optional): New enabled state.
- `match_logic` (str, optional): `"all"` or `"any"`.
- `actions` (dict, optional): When provided, **replaces** the rule's actions wholesale (per the same schema as `create_rule`'s `actions`).

**Conditional confirmation:** prompts the user via MCP elicitation only when the patch touches `conditions`, `actions`, or `match_logic` (irreversible replacements). Patches limited to `enabled` and/or `name` skip the prompt — both are trivially reversible.

**Returns:**

```json
{"success": true, "rule_index": 7}
```

**Limitations:**

- **`conditions` cannot be replaced.** Mail.app on macOS Tahoe (16.0 / macOS 26) has a recursion bug in `-[MFMessageRule(Applescript) removeFromCriteriaAtIndex:]`: any AppleScript path that removes a rule condition (delete by index, delete every, or assignment of a new list) crashes Mail. `update_rule` raises `MailUnsupportedRuleActionError` if `conditions=` is passed. To change a rule's conditions, delete it with `delete_rule` and recreate with `create_rule`.
- Rules whose existing actions include unsupported types (`run AppleScript`, `redirect message`, `play sound`, `notify`, `reply text`, color-message highlights) raise `MailUnsupportedRuleActionError` to avoid clobbering settings outside our schema.

---

### delete_rule

Delete a rule by index.

**Parameters:**

- `rule_index` (int, required): 1-based index from `list_rules`.

**Confirmation:** elicits user confirmation before deletion.

**Returns:**

```json
{"success": true, "rule_index": 7, "name": "File OmniFocus replies"}
```

---

### list_accounts

List all configured email accounts in Apple Mail, with identity, type, and enabled state. Account ids are stable across name changes — prefer them over names when chaining into other tools.

**Parameters:** None.

**Returns:**

```json
{
  "success": true,
  "accounts": [
    {
      "id": "B21B254B-CC54-4DA4-B3D9-793E57A8E908",
      "name": "Gmail",
      "email_addresses": ["me@gmail.com"],
      "account_type": "imap",
      "enabled": true
    }
  ],
  "count": 1
}
```

**Field notes:**

- `id`: Account UUID. Stable across display-name changes; future tools may accept this in place of `name`.
- `account_type`: One of `imap`, `pop`, `iCloud`, `hotmail`, `iCal`, `smtp`. Derived from Mail's internal type constant.
- `enabled`: `false` for accounts the user has disabled in Mail.app preferences.

**Examples:**

```python
# Discover accounts before chaining into a mailbox listing
accounts = list_accounts()
first_enabled = next(a for a in accounts["accounts"] if a["enabled"])
list_mailboxes(first_enabled["name"])
```

---

## Email Templates (v0.5.0)

Store and reuse common reply / forward / send bodies. Templates live as
plain-text files on disk that you can edit in any editor; the tools
provide a programmatic CRUD layer plus a render step that does
placeholder substitution and pulls reply-context fields out of a
referenced message.

### Storage

Templates are files at `~/.apple_mail_mcp/templates/<name>.md`. Override
the location with the `APPLE_MAIL_MCP_HOME` environment variable
(`templates/` is appended automatically). The directory is created on
first save.

### File format

```
subject: Re: {original_subject}

Hi {recipient_name},

Thanks for reaching out.
```

The optional header block (`key: value` lines) is terminated by a blank
line; everything after is the body. The only recognized header in v1 is
`subject:`. Placeholders use Python `str.format` syntax: `{name}`. To
include a literal brace, double it: `{{` / `}}`.

### Placeholder substitution

`render_template` returns the rendered subject (or null) and body. The
following variables are auto-populated:

| Variable | When | Source |
|----------|------|--------|
| `today` | always | Current date, ISO format `YYYY-MM-DD` |
| `recipient_name` | when `message_id` provided | Display name parsed from the original sender |
| `recipient_email` | when `message_id` provided | Email parsed from the original sender |
| `original_subject` | when `message_id` provided | The original message's subject |

User-supplied `vars` always override auto-fills on conflict. Any
placeholder that's neither auto-populated nor user-supplied raises
`MailTemplateMissingVariableError` listing every unfilled name.

### list_templates

List all stored templates. Returns each template's name and subject
(may be null).

```json
{
  "success": true,
  "templates": [
    {"name": "polite-decline", "subject": "Re: {original_subject}"},
    {"name": "status-update", "subject": null}
  ],
  "count": 2
}
```

### get_template

Read a single template by name. Returns name, subject (may be null),
body, and the sorted list of placeholders found across subject + body.

```json
{
  "success": true,
  "name": "polite-decline",
  "subject": "Re: {original_subject}",
  "body": "Hi {recipient_name},\n\nUnfortunately I won't be able to take this on.\n",
  "placeholders": ["original_subject", "recipient_name"]
}
```

### save_template

Create or overwrite a template. Returns `created: true` for new
templates, `created: false` when an existing template was overwritten.

```python
save_template(
    name="polite-decline",
    body="Hi {recipient_name},\n\nUnfortunately I won't be able to take this on.\n",
    subject="Re: {original_subject}",
)
```

No confirmation prompt — additive (or self-overwrite, which is the
explicit intent of an idempotent save). Names must match
`^[a-zA-Z0-9_-]{1,64}$`; anything outside that range (spaces, slashes,
dots, oversized) raises `invalid_template_name`.

### delete_template

Remove a template by name. **Elicits user confirmation** before deleting.

```json
{"success": true, "name": "polite-decline"}
```

### render_template

Render a template into ready-to-send text. **No side effects** — the
caller passes the rendered subject + body to `reply_to_message`,
`forward_message`, or `send_email` to actually send.

```python
# Render a reply template against a real message:
rendered = render_template(
    name="polite-decline",
    message_id="<abc@example.com>",
)
# rendered = {
#   "success": True,
#   "subject": "Re: Project X update",
#   "body": "Hi Alice,\n\nUnfortunately I won't be able to...",
#   "used_vars": {"today": "2026-04-25", "recipient_name": "Alice", ...}
# }
reply_to_message(
    message_id="<abc@example.com>",
    body=rendered["body"],
)

# Or for a fresh send with custom vars:
rendered = render_template(
    name="status-update",
    vars={"project": "Q3 plan", "status": "on track"},
)
send_email(
    to=["alice@example.com"],
    subject=rendered["subject"] or "Status update",
    body=rendered["body"],
)
```

User-supplied `vars` override auto-fills. Missing placeholders return
`missing_template_variable` error. Bad message IDs surface as
`message_not_found`.

---

## API Stability

- **Phase 1 (v0.1.x)**: Core tools stable
- **Phase 2 (v0.2.x)**: Attachments + management
- **Phase 3 (v0.3.x)**: Reply/forward
- **Phase 4 (v0.5.x)**: Discovery (list_accounts), email templates
- **Phase 5+**: Further enhancements, backward compatible

Breaking changes will only occur in major versions (1.0.0, 2.0.0, etc.).
