# Apple Mail AppleScript Gotchas

Known issues, limitations, and workarounds for Apple Mail automation via AppleScript.

## Pipe-Delimited Output Parsing

AppleScript results are returned as `field1|field2|field3` with newline-separated records. This breaks if any field contains a `|` character (common in email subjects).

**Current workaround:** Check `len(parts) >= expected_count` before accessing fields.

**Planned fix:** Migrate to JSON output with inline helpers.

## Gmail Label-Based System

Gmail doesn't support standard IMAP move. Use `gmail_mode=True` for copy+delete:

```python
connector.move_messages(ids, "Archive", gmail_mode=True)
```

## Message ID Lookup is O(accounts x mailboxes)

Finding a message by ID searches all accounts and mailboxes. Accept optional `account` and `mailbox` parameters to narrow the search.

## String Escaping is Critical

Always use `escape_applescript_string()` for user text. Unescaped quotes/backslashes cause silent failures with generic "Can't make" errors.

## Known Mail.app Limitations

| Feature | Status |
|---------|--------|
| Scheduled sending | Not available via AppleScript |
| Thread/conversation grouping | Not exposed to AppleScript |
| Mail rules management | Not accessible |
| Smart mailbox access | Not exposed |
| HTML body reading | Plain text only via `content of message` |
| Read receipts | Not accessible |
| Draft management | Limited support |

## Attachment Paths

Use POSIX file references: `POSIX file "/path/to/file"`. Convert Python `Path` objects via `.as_posix()`.

## Error Parsing

AppleScript errors appear in stderr. The connector parses them into typed exceptions:
- `"Can't get account"` -> `MailAccountNotFoundError`
- `"Can't get mailbox"` -> `MailMailboxNotFoundError`
- `"Can't get message"` -> `MailMessageNotFoundError`

## Timeout Considerations

Default: 60 seconds. Large mailboxes (10k+ messages) may need `timeout=120` or higher. Configurable via `AppleMailConnector(timeout=N)`.
