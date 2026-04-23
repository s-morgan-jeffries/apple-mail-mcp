# `imap_connector.py` + `keychain.py` — Design

**Issue:** #41
**Date:** 2026-04-23
**Status:** Approved (via conversational brainstorming)

## Context

#41 is the keystone of the IMAP arc. Prior work has settled everything *around* it — spike #39 falsified the Mail.app-credential-retrieval hypothesis; decision #68 chose user-supplied Keychain items as the auth path; the graceful-degradation invariants in [`imap-auth-options-decision.md`](../research/imap-auth-options-decision.md#graceful-degradation-invariants) specified the runtime behavior; the roadmap comment on #41 itself ranked the delegation candidates by honest value.

What remains: actually designing and building the two new modules — `keychain.py` (reads app passwords) and `imap_connector.py` (talks IMAP). This doc captures the design locked in through brainstorming so the implementation plan has a fixed target.

**Scope:** `keychain.py` + `imap_connector.py` standalone. `mail_connector.py` is **not** modified — delegation wiring is deferred to #40. Only `search_messages` is implemented on the IMAP side; the other delegation candidates (#66, #72, #73) are future work on a validated architecture.

## Locked design decisions

From conversational brainstorming (in order they were answered):

1. **Scope:** new modules only, no `mail_connector` changes, zero blast radius on existing tests. `#40` wires delegation later.
2. **Methods implemented:** `search_messages` only. `get_thread` / `get_message` / `get_attachments` are per-issue later; `save_attachments` is deferred (#74).
3. **Host/port derivation:** out of scope. `ImapConnector` takes fully-resolved `(host, port, email, password)`. The integration test hardcodes `imap.mail.me.com:993` the same way the spike does. `#40`'s factory handles the Mail.app-account-name → host correlation.
4. **Connection lifecycle:** per-call. Each `search_messages` call opens an IMAPClient, LOGINs, operates, LOGOUTs. Stateless connector. Pooling is tracked separately in #75.
5. **Return shape:** `ImapConnector.search_messages` returns `list[dict]` byte-for-byte matching the return of `mail_connector.search_messages` (not the MCP-tool `{"success": True, ...}` wrapper — that's the outer method's job). Specifically each dict has keys `id`, `subject`, `sender`, `date_received`, `read_status`, `flagged`.
6. **Error hierarchy:** `MailKeychainError` (base) with `MailKeychainEntryNotFoundError` and `MailKeychainAccessDeniedError` subclasses. All inherit from the existing `MailError` in [`exceptions.py`](../../src/apple_mail_mcp/exceptions.py:6). No custom IMAP exception types — let `OSError`, `socket.timeout`, `imapclient.exceptions.LoginError`, `imapclient.exceptions.IMAPClientError` propagate as-is per the invariants-doc catch set.

## Module layout

```
src/apple_mail_mcp/
├── keychain.py         # NEW  — Keychain password lookup
├── imap_connector.py   # NEW  — IMAPClient wrapper; search_messages only
├── exceptions.py       # EDIT — add MailKeychainError + 2 subclasses
├── mail_connector.py   # UNTOUCHED
├── server.py           # UNTOUCHED
└── ...                 # everything else unchanged
```

## `keychain.py` — API and behavior

```python
"""macOS Keychain password retrieval for IMAP credentials.

Users populate Keychain entries via `security add-generic-password`
with service name `apple-mail-mcp.imap.<mail_app_account_name>`.
This module retrieves them. See docs/research/imap-auth-options-decision.md
for the chosen auth path.
"""

SERVICE_NAME_PREFIX = "apple-mail-mcp.imap."

def get_imap_password(mail_app_account: str, email: str) -> str:
    """Return the app-specific password stored in Keychain.

    Args:
        mail_app_account: Mail.app account name (e.g. "iCloud", "Gmail").
        email: Email address the password is keyed to.

    Returns:
        The password, as stored.

    Raises:
        MailKeychainEntryNotFoundError: No matching item exists. Caller
            should treat this as "IMAP not opted in for this account"
            and silently fall back to AppleScript.
        MailKeychainAccessDeniedError: The item exists but access was
            denied (ACL refused the caller, or user denied the
            interactive prompt). Caller should surface this on the
            first failure per the invariants-doc logging rules.
        MailKeychainError: Any other security(1) failure (binary missing,
            malformed output, etc.).
    """
```

**Implementation shape.** Invokes `subprocess.run(["security", "find-generic-password", "-w", "-s", SERVICE_NAME_PREFIX + mail_app_account, "-a", email])`. Maps exit codes:

- exit `0` → return `stdout.rstrip("\n")`.
- exit `44` → `MailKeychainEntryNotFoundError` (`security`'s "item could not be found" error).
- exit `128` or `25308` stderr code → `MailKeychainAccessDeniedError` (interactive denial or ACL refusal).
- any other exit → `MailKeychainError` with stderr attached.

`check=False` is essential — we want to inspect the exit code rather than have subprocess raise `CalledProcessError`.

## `imap_connector.py` — API and behavior

```python
"""IMAPClient wrapper. Stateless, per-call connection lifecycle.

This module is deliberately unaware of Mail.app, Keychain, and the MCP
server. It takes fully-resolved credentials and talks IMAP. The caller
is responsible for correlating Mail.app account name → (host, port,
email) and fetching the password via keychain.get_imap_password.
"""

CONNECT_TIMEOUT_S: float = 3.0  # Invariant 4 in the decision doc.

class ImapConnector:
    def __init__(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        connect_timeout: float = CONNECT_TIMEOUT_S,
    ) -> None: ...

    def search_messages(
        self,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,   # ISO "YYYY-MM-DD"
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...
```

### Parameter semantics

Parameters are identical to `mail_connector.search_messages` *minus* `account` (already resolved at construction). Default behavior is identical: `None` means "no filter"; `limit=None` means "no limit." Date strings are ISO 8601 `YYYY-MM-DD`, validated via the same regex used in [`mail_connector.py:23`](../../src/apple_mail_mcp/mail_connector.py#L23) — reuse the constant rather than duplicate the pattern.

### IMAP SEARCH mapping

| Parameter | IMAP criterion |
|-----------|----------------|
| `sender_contains` | `FROM "<text>"` |
| `subject_contains` | `SUBJECT "<text>"` |
| `read_status=True` | `SEEN` |
| `read_status=False` | `UNSEEN` |
| `is_flagged=True` | `FLAGGED` |
| `is_flagged=False` | `UNFLAGGED` |
| `date_from` | `SINCE <dd-Mmm-yyyy>` |
| `date_to` | `BEFORE <dd-Mmm-yyyy + 1 day>` (inclusive upper bound) |
| `has_attachment` | post-filter via `BODYSTRUCTURE` — see below |
| `limit` | `uids[-limit:]` after SEARCH, before FETCH |
| no filters | `ALL` (IMAP requires at least one criterion) |

IMAP date format is `dd-Mmm-yyyy` (e.g. `22-Apr-2026`), not ISO. Conversion happens inside the connector.

`date_to` semantics match `mail_connector`: the upper bound is inclusive of the full day. IMAP's `BEFORE` is exclusive, so we pass `date_to + 1 day`.

### `has_attachment` post-filter

IMAP SEARCH has no native criterion for "has an attachment." Some providers expose Gmail-specific `X-GM-RAW "has:attachment"`, but relying on provider extensions defeats the universality we want. Implementation: after SEARCH, FETCH `BODYSTRUCTURE` for the candidate UIDs and filter client-side. Since `limit` caps the candidate set at a finite number (default unbounded is not expected for real callers; `mail_connector`'s test suite uses `limit=50`), this stays tractable. Document the cost in the docstring: `has_attachment` adds one extra FETCH round-trip per search and is more expensive than the other filters.

### Envelope → dict translation

IMAPClient returns `imapclient.response_types.Envelope` for `FETCH ENVELOPE`. Translation produces dicts with exactly these keys to match `mail_connector`:

| dict key | Source |
|----------|--------|
| `id` | `envelope.message_id` (bytes → str) |
| `subject` | `envelope.subject` (bytes → str, utf-8 with `errors="replace"`) |
| `sender` | First entry of `envelope.from_`, formatted as `"Name <mailbox@host>"` (matches `sender` of msg from AppleScript) |
| `date_received` | `envelope.date.isoformat()` if `datetime`, else best-effort parse from bytes |
| `read_status` | `b"\\Seen" in flags` from the FETCH FLAGS |
| `flagged` | `b"\\Flagged" in flags` from the FETCH FLAGS |

Notes:
- `envelope.message_id` may come back with angle-brackets (`<...@...>`); strip them for parity with AppleScript's `id of msg`.
- Subject, sender name, and mailbox encoded per RFC 2047 come back as raw bytes; decode with `errors="replace"` so one malformed header can't crash the whole search.
- If `envelope.from_` is empty (unusual but legal), return `""` for `sender`.

### Connection lifecycle

Each `search_messages` call:

1. `IMAPClient(self._host, port=self._port, ssl=True, timeout=self._connect_timeout)` — TLS-on, connect timeout from invariant 4.
2. `.login(self._email, self._password)`. On `LoginError`, propagate — the caller (delegation layer in #40, tests here) decides whether to fall back.
3. `.select_folder(mailbox, readonly=True)` — readonly so no state change on the server.
4. `.search(criteria)` — build criteria from parameters.
5. `.fetch(uids, [b"ENVELOPE", b"FLAGS"])`. If `has_attachment is not None`, include `b"BODYSTRUCTURE"` and post-filter.
6. `.logout()` in a `finally` block.

No IDLE, no pooling, no retry. All of those are out of scope.

## Exceptions

Add to [`src/apple_mail_mcp/exceptions.py`](../../src/apple_mail_mcp/exceptions.py):

```python
class MailKeychainError(MailError):
    """Keychain operation failed."""


class MailKeychainEntryNotFoundError(MailKeychainError):
    """Requested Keychain entry does not exist.

    Expected and benign: signals that the user has not opted in to
    IMAP for this account. Delegation layer treats this as a silent
    fall-back-to-AppleScript signal.
    """


class MailKeychainAccessDeniedError(MailKeychainError):
    """Keychain refused access (ACL denied or user denied prompt).

    Worth surfacing to the user on first failure — see invariant 5
    in the decision doc.
    """
```

No new IMAP-side exception types. See the locked-decisions list above.

## Testing

### Unit — `tests/unit/test_keychain.py`

Mock `subprocess.run`. Coverage:

- **Happy path.** exit 0, password returned.
- **Entry missing.** exit 44 → `MailKeychainEntryNotFoundError`.
- **Access denied (ACL).** exit 128 → `MailKeychainAccessDeniedError`.
- **Access denied (user pressed Deny).** exit 1 with stderr containing `25308` (or whatever `security`'s deny code is — verify empirically and pin) → `MailKeychainAccessDeniedError`.
- **Other failure.** exit 2 with arbitrary stderr → `MailKeychainError` with stderr in message.
- **Password with trailing newline.** Ensure `rstrip("\n")` behavior — `security -w` always adds one.
- **Password with embedded whitespace.** Keychain allows any bytes; we pass through.

Approximately 60 LOC.

### Unit — `tests/unit/test_imap_connector.py`

Mock `IMAPClient` at the class level. Coverage:

- **Connection + LOGIN happy path.** Verify `IMAPClient(host, port, ssl=True, timeout=3.0)` and `.login(email, password)` are called.
- **Connection failure.** `IMAPClient(...)` raises `OSError` → propagates.
- **LOGIN failure.** `.login()` raises `LoginError` → propagates.
- **SELECT + SEARCH happy path.** Verify `.select_folder("INBOX", readonly=True)`, then `.search(["ALL"])`.
- **Each filter mapping.** One test per IMAP-SEARCH-mapping row; verify the criteria list is built correctly.
- **No filters → `ALL`.** Confirm the empty-criteria-means-ALL fallback.
- **`date_from` / `date_to` ISO parsing.** Invalid ISO raises `ValueError`. Valid ISO produces correct `dd-Mmm-yyyy` IMAP form.
- **`date_to` inclusivity.** `date_to="2026-04-22"` produces `BEFORE 23-Apr-2026`.
- **`has_attachment=True`.** SEARCH runs normally; then FETCH includes `BODYSTRUCTURE`; result filtered to only messages with at least one non-text part.
- **`has_attachment=False`.** Same but inverted filter.
- **`limit` enforcement.** SEARCH returns 100 UIDs, `limit=10` → only last 10 are FETCHed.
- **Empty SEARCH result.** No UIDs → returns `[]`, no FETCH attempted, LOGOUT still called.
- **Envelope translation.** Feed a synthetic `Envelope` with encoded subject, multi-recipient from, bracketed message-id; verify the output dict matches the expected shape exactly.
- **LOGOUT on exception.** If any mid-flight operation raises, LOGOUT is still called (test via `finally` behavior).

Approximately 150 LOC.

### Integration — `tests/integration/test_imap_connector.py`

New file. `@pytest.mark.integration`. Guarded by `MAIL_TEST_MODE` and `MAIL_TEST_ACCOUNT` per existing convention in [`tests/integration/test_mail_integration.py:8-12`](../../tests/integration/test_mail_integration.py).

Minimum coverage:

- **End-to-end.** Call `keychain.get_imap_password("iCloud", email)` → pass creds to `ImapConnector(host="imap.mail.me.com", port=993, ...)` → call `search_messages()` with no filters → assert it returns a list (empty or not).
- **Keychain entry missing.** Use a fake account name → expect `MailKeychainEntryNotFoundError`.

The Yahoo-style "app-password UI unavailable" cases are documented in the decision doc; integration tests don't depend on them.

No attempt to exercise FETCH against a populated mailbox in this PR — the spike confirmed the stack works end-to-end through SEARCH, and the empty-mailbox caveat is already on record. A future PR that lands a FETCH exerciser on a populated mailbox (likely landing with #72 get_message) will cover that gap.

## Dependencies

`imapclient>=3.0.0` already landed in the `research` optional-dependencies group via #70. For this PR, it **moves to the primary** dependency set in [`pyproject.toml`](../../pyproject.toml) — the `imap_connector.py` module is now shipping code, not a spike artifact.

## Out of scope (explicit, for reference during implementation)

- No factory to correlate Mail.app account name → host/port (deferred to #40).
- No changes to `mail_connector.py` (deferred to #40).
- No connection pooling (#75).
- No setup-helper CLI (#76).
- No `get_thread` / `get_message` / `get_attachments` / `save_attachments` (#66, #72, #73, #74).
- No feature flag — Keychain entry presence is the opt-in signal.

## References

- [`docs/research/imap-auth-options-decision.md`](../research/imap-auth-options-decision.md) — auth path and graceful-degradation invariants.
- [`docs/research/imap-hybrid-approach.md`](../research/imap-hybrid-approach.md) — background architecture research (delegation-auth section superseded).
- [`scripts/spike_imap_icloud.py`](../../scripts/spike_imap_icloud.py) — the spike this module productionizes.
- [`src/apple_mail_mcp/mail_connector.py:221`](../../src/apple_mail_mcp/mail_connector.py#L221) — the `search_messages` that this IMAP implementation mirrors on return shape.
