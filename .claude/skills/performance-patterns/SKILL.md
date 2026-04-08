---
name: performance-patterns
description: Use when optimizing Apple Mail MCP operations, diagnosing slow queries, adding new filtering logic, or modifying how data is fetched from Mail.app. Covers osascript overhead, whose clause optimization, batch operation patterns, and known operation timings.
---

# Apple Mail MCP Performance Patterns

## The Core Insight

**The bottleneck is per-subprocess overhead.** Each `osascript` call costs 100-300ms regardless of what it does. All performance work reduces the number of subprocess calls.

## Known Operation Timings

| Operation | Time | Notes |
|-----------|------|-------|
| Single `osascript` call overhead | 100-300ms | Minimum cost per subprocess |
| `search_messages` (typical INBOX) | ~1-5s | Depends on mailbox size and filter count |
| `get_message` (single) | <1s | Direct ID lookup |
| `send_email` | ~1-2s | Includes Mail.app compose + send |
| `mark_as_read` (bulk) | ~1-2s | Single script for N messages |
| `move_messages` | ~1-3s | Varies by account type (Gmail slower) |
| `save_attachments` | ~2-5s | Depends on attachment count/size |

## Pattern 1: Use `whose` Clauses for Server-Side Filtering

```applescript
-- GOOD: Server-side filter (fast, Mail.app evaluates internally)
set msgs to (messages of mbox whose sender contains "user@example.com")

-- BAD: Fetch all then filter in Python (slow, transfers all data)
set msgs to every message of mbox
-- then filter in Python loop
```

`whose` clauses let Mail.app filter internally without transferring unmatched messages over IPC. This is 10-50x faster for large mailboxes.

## Pattern 2: Single Script Per Batch Operation

```python
# GOOD: One osascript call for N messages (near-constant time)
script = """
tell application "Mail"
    repeat with msgId in {id1, id2, id3}
        set read status of (first message whose id is msgId) to true
    end repeat
end tell
"""
self._run_applescript(script)

# BAD: N osascript calls for N messages (linear time)
for msg_id in message_ids:
    self._run_applescript(f'tell application "Mail" ...')
```

Batch operations should always build a single AppleScript that handles all items.

## Pattern 3: Use `limit` for Pagination

The `search_messages` tool accepts a `limit` parameter (default: 50). AppleScript uses `items 1 thru N of` for server-side limiting. Always pass a reasonable limit — fetching 10,000 messages when the user wants the latest 10 wastes time.

## Pattern 4: Accept Optional Account/Mailbox Parameters

Message ID lookup is O(accounts x mailboxes) when searching globally. Always accept optional `account` and `mailbox` parameters to narrow the search scope. If the caller knows which account, the search is dramatically faster.

## Anti-Patterns

- **Repeated subprocess calls in loops** — Build one script, execute once
- **Fetching all properties when only some are needed** — The pipe-delimited format fetches a fixed set; future JSON migration should fetch selectively
- **Not using `whose` clauses** — Manual filtering in Python transfers all data first
- **Default timeout too low for large operations** — Increase from 60s for bulk operations on large mailboxes

## Gmail Performance Notes

Gmail operations are inherently slower than IMAP because:
- Move requires copy + delete (two operations instead of one)
- Label operations don't map cleanly to folder operations
- Search across Gmail labels may scan differently than IMAP folders

## Profiling

No formal benchmarking infrastructure yet (see issue #31). When added:
- Use 5 iterations per operation
- Calculate mean, stdev, CV%
- Set thresholds at 5x documented baseline
- Detect cold starts (first run > 2x median of remaining)
