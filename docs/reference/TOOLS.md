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
| `source_mailbox` | string \| null | No | null | Optional narrow-path hint — narrows the AppleScript scan to one mailbox. Required to unlock the IMAP fast path on move-only patches (#149) — without it, the move runs via AppleScript even when IMAP is configured. |
| `gmail_mode` | boolean | No | false | Use Gmail-specific copy+delete instead of move (label-based accounts) |

**Patch semantics:** caller specifies only the fields they want changed. At least one field parameter must be set; otherwise returns `validation_error`.

**Order of operations:** read-state and flag changes apply first (in the source mailbox), then the move. IMAP requires the message to exist in the source folder for STORE before MOVE.

**Performance — IMAP fast paths:**

- **Move-only patches (#149):** When `destination_mailbox` is the only field set and `source_mailbox` is provided, the move runs server-side via IMAP `UID MOVE`. On a 47k-message Gmail INBOX this drops the move from ~57s to <1s. Falls back to AppleScript when the server lacks `MOVE` / `UIDPLUS`.
- **Read-status-only patches (#151):** When `read_status` is the only field set and `account` + `source_mailbox` are provided, the read/unread mutation runs server-side via IMAP `UID STORE +/-FLAGS (\Seen)`. `\Seen` is base IMAP (RFC 3501), universal across all servers — no capability check needed.
- **Flag-only patches (#152):** When `flagged` is the only field set (no `flag_color`) and `account` + `source_mailbox` are provided, the flag/unflag runs server-side via IMAP `UID STORE +/-FLAGS (\Flagged)`. Same base-IMAP universality as `\Seen`. Bare `\Flagged` renders identically in Mail.app to the existing AppleScript default flag (verified empirically, no UI divergence).

Combined patches (move + read, read + flag, etc.) and any patch with `flag_color` set currently run via AppleScript regardless — Mail.app's color attributes (`$MailFlagBit*` user keywords) are out of IMAP scope. All fast paths require Keychain credentials per the IMAP setup flow (`apple-mail-mcp setup-imap --account <name>`); they fall back to AppleScript transparently when IMAP isn't configured.

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

### update_mailbox

Rename and/or re-parent (move) an existing mailbox.

**Two delivery paths:**

- **Rename only** (`new_name` set, `new_parent` is `None`): AppleScript's `set name of mailbox X to "Y"`. Fast, no IMAP credentials needed.
- **Move** (`new_parent` set, optionally combined with rename): IMAP `RENAME`. Requires IMAP credentials in Keychain (#73 opt-in flow) — returns `error_type: "imap_required"` when missing.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Mail.app account name or UUID (from `list_accounts`) |
| `name` | string | Yes | - | Current mailbox name. Slash-separated for nested mailboxes (e.g. `"Archive/2024"`) |
| `new_name` | string | No | None | Replacement leaf name. `None` keeps the current leaf when moving. Path-traversal characters stripped via `sanitize_mailbox_name`. At least one of `new_name` / `new_parent` is required. |
| `new_parent` | string | No | None | Destination parent path. `None` keeps current parent (rename-only). `""` (empty string) moves to top-level. Non-empty string moves under that path. |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "name": "Archive/2024",
  "new_name": null,
  "new_parent": "OldStuff"
}
```

**Examples:**

```python
# Simple rename (no IMAP needed)
update_mailbox(account="Gmail", name="ToDo", new_name="Tasks")

# Rename a nested mailbox (slash-separated path)
update_mailbox(
    account="Gmail", name="Projects/Q1", new_name="Q1-Archive",
)

# Move a nested mailbox to a different parent (IMAP)
update_mailbox(
    account="Gmail", name="Inbox/Projects/Q1",
    new_parent="Archive/2024",
)
# -> "Inbox/Projects/Q1" becomes "Archive/2024/Q1"

# Promote a nested mailbox to top-level
update_mailbox(account="Gmail", name="Inbox/Old", new_parent="")
# -> "Inbox/Old" becomes "Old"

# Move + rename in one IMAP RENAME
update_mailbox(
    account="Gmail", name="A/B", new_name="Renamed", new_parent="C",
)
# -> "A/B" becomes "C/Renamed"
```

**Caveat — Gmail system labels:** Renaming or moving a Gmail folder under
`[Gmail]/` (Drafts, Sent Mail, Trash, etc.) may not stick — Gmail's
IMAP server may auto-restore the canonical name. User-created Gmail
labels behave normally. Tracked as #164.

**Error Codes:**

- `validation_error`: Empty / whitespace-only `name`, missing both `new_name` and `new_parent`, or `new_name` sanitizes to empty.
- `imap_required`: Move requested but no IMAP credentials in Keychain for `account`.
- `mailbox_not_found`: No mailbox at `name`.
- `account_not_found`: `account` doesn't match any configured account.
- `applescript_error`: Mail.app rejected a rename for an underlying reason.
- `unknown`: Unexpected error.

---

### delete_mailbox

Delete a mailbox via IMAP. Mail.app's AppleScript dictionary doesn't
expose a working delete primitive for mailboxes (verified by probe), so
this operation requires IMAP credentials in Keychain (#73 opt-in flow).

**Always elicits user confirmation** (destructive). Refuses non-empty
mailboxes by default to prevent accidental data loss; pass
`delete_messages=True` to cascade.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `account` | string | Yes | - | Mail.app account name or UUID |
| `name` | string | Yes | - | Mailbox name. Slash-separated for nested. |
| `delete_messages` | boolean | No | False | When False, refuses if the mailbox contains messages. When True, cascade-deletes the mailbox and its contents. |

**Returns:**

```json
{
  "success": true,
  "account": "Gmail",
  "name": "Old/Archive",
  "deleted_message_count": 0
}
```

`deleted_message_count` is 0 when the mailbox was empty; positive when
`delete_messages=True` cascaded.

**Examples:**

```python
# Safe delete (refuses if any messages)
delete_mailbox(account="Gmail", name="Old/Empty")

# Cascade-delete a non-empty mailbox
delete_mailbox(
    account="Gmail", name="Old/Archive", delete_messages=True,
)
```

**Error Codes:**

- `cancelled`: User declined the elicitation prompt.
- `validation_error`: Empty `name`.
- `imap_required`: No IMAP credentials in Keychain for `account`.
- `mailbox_not_empty`: Mailbox contains messages and `delete_messages=False`.
- `mailbox_not_found`: No mailbox at `name`.
- `account_not_found`: `account` doesn't match any configured account.
- `unknown`: Unexpected error.

---

### delete_messages

Delete messages — always moves them to the account's Trash mailbox.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message_ids` | list[string] | Yes | - | List of message IDs to delete |
| `permanent` | boolean | No | False | Reserved; currently a no-op. Passing `True` emits a `DeprecationWarning`. See [issue #111](https://github.com/s-morgan-jeffries/apple-mail-mcp/issues/111). |
| `account` | string \| null | No | null | Account name (or UUID). Pair with `source_mailbox` to narrow the scan and unlock the IMAP fast path (#150). |
| `source_mailbox` | string \| null | No | null | Mailbox the messages live in. Required to unlock the IMAP fast path (#150) — without it, the delete runs via AppleScript even when IMAP is configured. Either alone (without `account`) raises `validation_error`. |

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
# Move messages to trash (cross-scan; finds them across all mailboxes)
delete_messages(
    message_ids=["12345", "12346"],
)

# Faster: narrow-scan or IMAP fast path when source is known
delete_messages(
    message_ids=["12345", "12346"],
    account="iCloud",
    source_mailbox="INBOX",
)
```

**Performance — IMAP fast path (#150):** When invoked with `account` and `source_mailbox`, the delete runs server-side via IMAP `UID MOVE` to the account's Trash folder. On a 47k-message Gmail INBOX this drops the operation from ~57s to <1s — the AppleScript path uses `whose message id is`, which is a linear scan against RFC 5322 Message-IDs. Trash folder is resolved via RFC 6154 SPECIAL-USE `\Trash`; falls back to conventional names (`Trash`, `[Gmail]/Trash`, `Deleted Messages`, `Deleted Items`). Capability fallback chain: `MOVE` → `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` (UIDPLUS only) → AppleScript. Requires Keychain credentials per the IMAP setup flow (`apple-mail-mcp setup-imap --account <name>`); falls back to AppleScript transparently when IMAP isn't configured or the server lacks both `MOVE` and `UIDPLUS`.

**Note on `permanent`:**

Mail.app's AppleScript dictionary exposes no path to permanent-delete that bypasses Trash. Calling `delete msg` always moves to the account's Trash; calling `delete` again on a message already in Trash is a no-op, and there is no `empty trash` command. The `permanent` parameter is preserved for API compatibility but currently has no effect; passing `True` raises a `DeprecationWarning` so the gap is visible. Track #111 for status.

**Safety Notes:**
- Bulk deletions limited to 100 messages for safety
- All deletes are recoverable from the account's Trash mailbox until that mailbox is emptied (typically by Mail.app's per-account "empty trash" schedule, configurable in Mail's preferences)

---

## Drafts Lifecycle (v0.7.0)

The drafts lifecycle replaces the v0.6 send group (`send_email`,
`send_email_with_attachments`, `reply_to_message`, `forward_message`)
with three tools that match Mail.app's actual primitive: every outgoing
message is a draft until you `send` it. Net surface: 4 → 3, with
`update_draft` and `delete_draft` being net-new capabilities (deferred
sends, edit-before-send, discard).

### create_draft

Create a draft (fresh, reply, or forward). Optionally send immediately.

**⚠️ Security Note:** When `send_now=True`, requires user confirmation.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `reply_to` | string | No | None | Mail.app id of a message to reply to. Mutually exclusive with `forward_of`. When set, `to`/`cc` recipients and `subject` are auto-derived (override by passing them explicitly). |
| `forward_of` | string | No | None | Mail.app id of a message to forward. Mutually exclusive with `reply_to`. `to` is required (recipient of the forward). |
| `to` | array[string] | When fresh | None | Recipient list. For reply/forward: `None` keeps auto-derived; `[]` clears; populated list replaces. |
| `cc` | array[string] | No | None | CC recipients (same semantics as `to` for reply/forward). |
| `bcc` | array[string] | No | None | BCC recipients. |
| `subject` | string | When fresh | None | Subject. For reply/forward, `None` keeps Mail's `Re:`/`Fwd:` prefix. |
| `body` | string | No | "" | Body text. For reply/forward, a non-empty body **replaces** Mail's auto-quoted content (the auto-quote isn't readable from AppleScript before save). Empty body leaves Mail's auto-quote intact. |
| `attachment_paths` | array[string] | No | None | List of file paths to attach. |
| `reply_all` | boolean | No | False | For `reply_to` only — use `reply to all`. |
| `template_name` | string | No | None | Optional template to render for `subject` + `body`. Caller-supplied `subject`/`body` override the rendered output. |
| `template_vars` | object | No | None | Variables for the template renderer. Requires `template_name`. |
| `from_account` | string | No | None | Mail.app account name or UUID. None = Mail's default. |
| `send_now` | boolean | No | False | `False` saves as draft. `True` sends immediately and elicits confirmation. |

**Returns:**

```json
{
  "success": true,
  "draft_id": "161055",
  "sent_message_id": "",
  "details": {"seed_kind": "new", "send_now": false}
}
```

`draft_id` is empty when sent (`send_now=True`); `sent_message_id` is
reserved for future use.

**Examples:**

```python
# Save a fresh draft for later
create_draft(
    to=["alice@example.com"],
    subject="Project Update",
    body="Here's the latest..."
)

# Reply, save as draft (preserves Mail's auto-quote)
create_draft(reply_to="160989")

# Reply with custom body, then send
create_draft(reply_to="160989", body="Sounds good, thanks!", send_now=True)

# Forward with attachment
create_draft(
    forward_of="160989",
    to=["recipient@example.com"],
    body="FYI",
    attachment_paths=["/tmp/report.pdf"]
)

# Template-driven send
create_draft(
    reply_to="160989",
    template_name="thanks-for-meeting",
    send_now=True
)
```

**Error Codes:**

- `validation_error`: Mutually exclusive seeds, missing required fields, or `template_vars` without `template_name`.
- `message_not_found`: `reply_to` / `forward_of` doesn't match any Mail.app message.
- `account_not_found`: `from_account` doesn't match.
- `file_not_found`: An attachment path doesn't exist.
- `cancelled`: User declined the elicitation prompt (when `send_now=True`).
- `applescript_error`, `unknown`: Lower-level failures.

---

### update_draft

Update an existing draft. Implemented as **delete-and-recreate** —
Mail.app forbids mutating saved drafts, so this tool reads the
current state, deletes the draft, and creates a new one with the
merged fields. Threading headers (for replies) and forward anchors
are preserved via persisted seed metadata.

**⚠️ Returns a NEW `draft_id`** — the input id is no longer valid
after this call. Callers caching the id must re-read the response.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `draft_id` | string | Yes | - | Mail.app id of the existing draft. |
| `to` / `cc` / `bcc` | array[string] | No | None | Override recipient groups: `None` keeps existing, `[]` clears, populated list replaces. |
| `subject` | string | No | None | Override subject. `None` keeps existing. |
| `body` | string | No | None | Override body. `None` keeps existing; non-None replaces (including `""`). |
| `attachment_paths` | array[string] | No | None | Override attachments: `None` **preserves existing** (extracted to a temp dir and re-attached); `[]` clears; populated list replaces. |
| `template_name` / `template_vars` | string / object | No | None | Optional template render. User-supplied `subject`/`body` override the rendered output. |
| `from_account` | string | No | None | Override sender. |
| `send_now` | boolean | No | False | `False` saves new draft. `True` sends after eliciting confirmation. |

**Returns:**

```json
{
  "success": true,
  "draft_id": "161200",
  "sent_message_id": "",
  "details": {"seed_kind": "reply", "send_now": false}
}
```

**Externally-created drafts:** for drafts not created via `create_draft`,
seed recovery falls back to scanning Mail.app for the draft's
`In-Reply-To` header — this can take 30s+ on large mailboxes. Forward
seeds without persisted state are misclassified as fresh.

**Examples:**

```python
# Fix a typo in the body, keep recipients/attachments/threading
update_draft(draft_id="161055", body="Corrected body text")

# Add a recipient (replaces the to list)
update_draft(draft_id="161055", to=["alice@example.com", "bob@example.com"])

# Clear all attachments
update_draft(draft_id="161055", attachment_paths=[])

# Send the draft after editing
update_draft(draft_id="161055", body="Final version", send_now=True)
```

**Error Codes:** Same as `create_draft`, plus:

- `draft_not_found`: `draft_id` doesn't match any existing draft.
- `invalid_draft_id`: `draft_id` failed validation (path traversal, etc.).

---

### delete_draft

Move a draft to Trash. One-way discard for the lifecycle; Mail.app no
longer treats trashed drafts as editable.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `draft_id` | string | Yes | - | Mail.app id of the draft. |

**Returns:**

```json
{"success": true, "draft_id": "161055"}
```

**Error Codes:**

- `draft_not_found`: `draft_id` doesn't match any existing draft.
- `invalid_draft_id`: `draft_id` failed validation.

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

# 3. Send reply (use create_draft with send_now=True to skip the
#    save-then-send dance)
create_draft(
    reply_to=results["messages"][0]["id"],
    body="Thank you for your proposal...",
    send_now=True,
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
caller passes the rendered subject + body to `create_draft` to send.
For most workflows, use `create_draft(template_name=...)` directly,
which folds rendering into the send call.

```python
# Inline render-then-send (one tool call):
create_draft(
    reply_to="<abc@example.com>",
    template_name="polite-decline",
    send_now=True,
)

# Standalone render for a "preview" workflow (no draft created):
rendered = render_template(
    name="status-update",
    vars={"project": "Q3 plan", "status": "on track"},
)
# rendered = {"success": True, "subject": "...", "body": "...", "used_vars": {...}}
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
