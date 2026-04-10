# IMAP Hybrid Approach — Research Findings

**Status:** Research complete, not yet implemented
**Related issues:** #15 (closed), #39, #40, #41
**Date:** 2026-04

## Question

AppleScript has fundamental limitations (O(accounts×mailboxes) message lookup, fragile pipe-delimited parsing, no threading). Is there a native macOS framework — like Calendar's EventKit — we could use instead? If not, what are the alternatives?

## Finding: No Swift framework for Mail on macOS

Unlike Calendar (which has EventKit), Apple never built a public Swift framework for Mail.app data access on macOS.

- **MailKit** is iOS-only (`MessageUI` / `MFMailComposeViewController`)
- **Apple Mail plugin API** is deprecated and sandboxed
- There is no EventKit equivalent for Mail

The realistic alternatives are:

1. **AppleScript** (current) — most complete Mail.app automation, but fragile
2. **Direct IMAP** — bypasses Mail.app, talks to mail server directly
3. **Hybrid** — AppleScript for write/UI operations, IMAP for read/search

## Recommendation: Hybrid approach is viable

IMAP solves the biggest architectural problems without breaking the security model. The AppleScript path remains for operations that need Mail.app (compose, reply, forward, UI state).

## Method-by-method audit

### Could use IMAP (read/data retrieval)

| Method | Current Problem | IMAP Benefit |
|--------|----------------|--------------|
| `search_messages` | Pipe-delimited parsing, single mailbox, limited filters | Server-side SEARCH, cross-folder, rich query syntax |
| `get_message` | O(accounts×mailboxes) nested loop | O(1) UID fetch |
| `get_attachments` | N+1 lookup, silent failures | BODYSTRUCTURE provides metadata natively |
| `save_attachments` | N+1 lookup for download | Partial fetch with byte ranges |

### Needs Mail.app (write/compose)

| Method | Why |
|--------|-----|
| `send_email` | Compose UI, account selection, signature |
| `send_email_with_attachments` | Attachment UI integration |
| `reply_to_message` | Quote handling, signature, compose UI |
| `forward_message` | Attachment forwarding, compose UI |

IMAP is receive-only. Sending needs SMTP, but we'd rather delegate to Mail.app than reimplement outbound message composition.

### AppleScript-only (Mail.app state)

| Method | Why |
|--------|-----|
| `list_accounts` | Mail.app object model |
| `list_mailboxes` | Mail.app hierarchy |
| `mark_as_read` | Could use IMAP flags, but color flags are Mail.app specific |
| `move_messages` | Gmail workaround uses Mail.app; true move in IMAP |
| `flag_message` | Color flags are Mail.app specific |
| `create_mailbox` | Mail.app specific |
| `delete_messages` | Trash state is Mail.app specific |

## Problems IMAP fixes

### 1. The N+1 message lookup

**9 of 15 methods** currently do this:

```applescript
repeat with acc in accounts
  repeat with mb in mailboxes of acc
    try
      set msg to first message of mb whose id is msgId
    end try
  end repeat
end repeat
```

With 3 accounts × 20 mailboxes, finding one message = 60 iterations. Marking 100 messages = 6,000 iterations. Each iteration is an AppleScript→Mail.app IPC roundtrip.

**IMAP equivalent:** `imap.uid('FETCH', message_id, 'ALL')` — O(1) or O(log n).

### 2. Pipe-delimited parsing

Current code in `search_messages`, `get_message`, `get_attachments`:

```python
parts = line.split("|")
```

Breaks silently when email subjects, senders, or attachment names contain `|`. No escape mechanism exists because AppleScript has no JSON serialization. IMAP returns structured binary data with length-prefixed fields — no parsing ambiguity.

### 3. No threading / conversations

AppleScript Mail API doesn't expose thread IDs. Conversations are a Mail.app UI construct, not data. IMAP has RFC 5256 THREAD extension (supported by Fastmail, Dovecot, Cyrus; Gmail uses its own X-GM-THRID).

### 4. Full-body fetches through stdout

`get_message` sends entire message body (potentially megabytes) through AppleScript→stdout→Python. No streaming, no partial fetch, no headers-only option.

IMAP supports `BODY[HEADER]` (headers only, ~1KB) and `BODY[TEXT]<0.10000>` (first 10KB of body).

### 5. Gmail label workaround race condition

`move_messages` with `gmail_mode=True`:

```applescript
duplicate msg to destMailbox
delete msg
```

Brief window where message exists in both places. If delete fails, message duplicates. No atomicity.

IMAP Gmail extensions provide atomic label operations:

```python
imap.copy(msg_id, '[Gmail]/Archive')
imap.store(msg_id, '+FLAGS', '\\Deleted')
```

## Authentication: macOS Keychain

The key insight is that **Mail.app already stores IMAP credentials in the Keychain** when the user sets up an account. Python can retrieve them without prompting:

```bash
security find-internet-password -s imap.gmail.com -a user@gmail.com -w
```

This approach:
- **No credential storage** in MCP server (maintains current security model)
- **No OAuth re-implementation** (Mail.app handled the OAuth flow)
- **No user prompt** (Keychain access from Python is unrestricted)

### Auth caveats

- **OAuth2 accounts** (Gmail, Outlook) store app-specific passwords or OAuth tokens. The `security` command retrieves whatever Mail.app stored. Some OAuth setups may need the `email-oauth2-proxy` shim.
- **2FA** is handled at Mail.app setup time; by the time we retrieve from Keychain, it's a valid credential.
- **Keychain trust model**: Python scripts have unrestricted Keychain access. This is acceptable for a local MCP server the user controls.

## Protocol support

Common question: what about POP3-only accounts?

POP3-only is extremely rare in 2026. All major providers support IMAP:
- Gmail, Outlook, Yahoo, iCloud, Fastmail, AOL, ProtonMail (via Bridge)

**Fallback design:** The hybrid is per-account, not all-or-nothing. If Keychain has IMAP credentials for an account, use IMAP. Otherwise fall back to AppleScript. The AppleScript path never goes away.

## Library choice: IMAPClient

| Library | Pros | Cons |
|---------|------|------|
| `imaplib` (stdlib) | No dependency | Low-level, synchronous, verbose |
| **`imapclient`** | **Mature, Pythonic, UID-aware, tested against all major providers** | **One new dependency** |
| `aioimaplib` | Async, IDLE support | Less mature, async doesn't fit current architecture |

**Recommendation:** `imapclient>=3.0.0`. Sync matches our current FastMCP architecture. IDLE/async can come later if needed.

## Proposed architecture

Not to be implemented until a future milestone, but the shape:

```
src/apple_mail_mcp/
├── server.py              # FastMCP tools (unchanged)
├── mail_connector.py      # Delegates read ops to IMAP when available
├── applescript_connector.py  # Renamed from current mail_connector logic
├── imap_connector.py      # New: IMAPClient wrapper for read operations
├── keychain.py            # New: macOS Keychain credential retrieval
├── security.py            # Unchanged
├── utils.py               # Unchanged
└── exceptions.py          # Add MailKeychainError, MailImapError
```

**Feature flag:** `MAIL_USE_IMAP=true` for opt-in during development. Default off until stable.

**Delegation logic:**

```python
class AppleMailConnector:
    def search_messages(self, account, mailbox, ...):
        if self._imap_available(account):
            return self._imap.search(...)
        return self._applescript.search(...)

    def send_email(self, ...):
        # Always AppleScript — sending needs compose UI
        return self._applescript.send(...)
```

## When to implement

This research justifies the approach but does not justify immediate implementation. The right triggers are:

- **Issue #23** (replace pipe-delimited with JSON) — IMAP makes this moot for read ops; better to skip straight to IMAP
- **Issue #28** (advanced search filters) — IMAP SEARCH is far more capable than AppleScript `whose`
- **Issue #29** (get_thread tool) — impossible without IMAP THREAD extension

Follow-up issues #39, #40, #41 are on v0.5.0 as spikes.

## References

- [IMAPClient docs](https://imapclient.readthedocs.io/)
- [RFC 5256: IMAP SORT and THREAD](https://www.rfc-editor.org/rfc/rfc5256.html)
- [macOS security(1) man page](x-man-page://1/security) — for `find-internet-password`
- [email-oauth2-proxy](https://github.com/simonrob/email-oauth2-proxy) — OAuth2 shim if needed
