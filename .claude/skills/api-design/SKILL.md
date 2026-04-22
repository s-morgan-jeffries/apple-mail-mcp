---
name: api-design
description: Use BEFORE adding any new tool, parameter, or endpoint to the Apple Mail MCP server. Also use when considering API changes, evaluating feature requests, or when tempted to create a specialized operation. Contains the decision tree that prevents tool sprawl and the anti-pattern catalog.
---

# Apple Mail MCP API Design

The current API has 17 tools organized into 4 phases. Tool count should grow slowly and deliberately. Every new tool request must pass through the decision tree.

## Decision Tree: Use BEFORE Adding Any Tool

```
Can an existing tool handle this with current parameters?  (70% of cases: YES)
  |-- Searching with a new filter? -> Add parameter to search_messages()
  |-- Reading with different format? -> Add parameter to get_message()
  |-- Sending with new options? -> Extend send_email() or send_email_with_attachments()
  |
Can an existing tool handle this with a NEW parameter?  (20% of cases: YES)
  |-- Example: search by date range -> add date_from, date_to to search_messages()
  |-- Example: search flagged only -> add is_flagged to search_messages()
  |
Is this truly a distinct operation?  (10% of cases: MAYBE)
  |-- Different Mail.app object? (accounts, rules, smart mailboxes)
  |-- Different action type? (not CRUD on messages/mailboxes)
  |
If NO to all three: You might need a new tool. This is rare.
```

## Anti-Patterns (Never Do These)

### Field-Specific Tools
```python
# WRONG - creates tool sprawl
mark_as_flagged(message_id)
mark_as_unflagged(message_id)
mark_as_junk(message_id)

# RIGHT - one tool with parameters
flag_message(message_ids, flag_color)
# or extend update capabilities into a general update_message() tool
```

### Specialized Search Tools
```python
# WRONG - each filter becomes a tool
get_unread_messages()
get_flagged_messages()
get_messages_from_sender()

# RIGHT - parameters on existing search
search_messages(read_status="unread")
search_messages(is_flagged=True)
search_messages(sender_contains="user@example.com")
```

### Formatted Text Returns
```python
# WRONG - returns human-readable string
def list_mailboxes():
    return "Inbox (42 messages)\nSent (15 messages)"

# RIGHT - returns structured data
def list_mailboxes():
    return {"success": True, "mailboxes": [{"name": "Inbox", "count": 42}, ...]}
```

## Response Format Rules

1. **Always return `dict[str, Any]`** with `"success": bool`
2. **Errors include `"error"` (message) and `"error_type"` (category)**
3. **Never return formatted text strings** — return structured data
4. **Include context fields** — e.g., `"messages_found": 5` alongside the results

## Tool Naming Convention

- Verb + noun: `search_messages`, `get_message`, `send_email`
- Plural when accepting lists: `delete_messages`, `mark_as_read`
- Specific verb over generic: `flag_message` not `update_message_flag`

## Adding a New Tool Checklist

1. Verify it doesn't fit in an existing tool (run through decision tree above)
2. Write unit tests (mock AppleScript)
3. Write integration tests (real Mail.app)
4. Implement in `mail_connector.py`
5. Expose in `server.py`
6. Run `./scripts/check_client_server_parity.sh`
7. Update `docs/reference/TOOLS.md`
8. Add blind eval scenarios if tool has non-obvious parameters
