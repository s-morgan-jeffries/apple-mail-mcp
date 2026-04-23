# IMAP Auth Options — Decision

**Status:** Decided 2026-04-22. Chosen path: **(a) user-supplied Keychain item per Mail.app account**.
**Related issues:** #68 (this decision), #39 (prior spike — falsified), #40, #41, #66 (downstream)
**Supersedes:** the "Authentication: macOS Keychain" section of [`imap-hybrid-approach.md`](./imap-hybrid-approach.md).

## Recommendation

Production IMAP authentication will read per-account app-specific passwords from a user-populated Keychain entry, keyed by a project-scoped service name and the Mail.app account's email. A setup helper will hide the raw `security` invocation. No part of the production path will rely on reading Mail.app's own stored credentials, which are not reachable from an unsigned user-scoped process on modern macOS.

## Graceful degradation invariants

IMAP is an optional enhancement, never a prerequisite. The following invariants are binding on the implementation (#41) and on any future work touching the delegation layer. They exist so that a user with no Keychain entry, a user with a revoked app password, and a user working offline all get identical, working tool behavior — just via AppleScript.

1. **AppleScript baseline.** Every MCP tool this server exposes must function when IMAP is not available for any reason. AppleScript + Mail.app is the universal baseline. IMAP is, and will remain, a strictly additive enhancement for read / search operations.

2. **Per-account opt-in.** IMAP is enabled per Mail.app account by the presence of a Keychain entry at `apple-mail-mcp.imap.<mail_app_account_name>`. Accounts without an entry never attempt IMAP; they go straight to AppleScript with no per-call check.

3. **Runtime failure → fallback.** For accounts with IMAP configured, any runtime failure of an IMAP operation falls back to AppleScript for *that operation*. The failure classes that must be caught:
   - `OSError` / `socket.timeout` — offline, DNS failure, host unreachable, wifi dropped mid-operation.
   - `imapclient.exceptions.LoginError` — creds rejected (password revoked, app password deleted at the provider, account locked).
   - `imapclient.exceptions.IMAPClientError` — protocol-level error or server-side error mid-session.
   - Explicit per-operation timeout.
   Application-level errors (mailbox does not exist, invalid search criteria, permissions error) are **not** caught; they propagate to the caller because AppleScript would fail identically.

4. **Fail-fast connect timeout.** The IMAP connect timeout is ≤3 seconds. When offline, fallback happens within that budget; the user does not sit through the TCP default of tens of seconds on every operation.

5. **Quiet fallback, audible first failure.** Fallback emits at `DEBUG` log level in the steady state. The *first* IMAP failure per account per server-process lifetime emits a single `WARNING` log entry identifying the account, the failure class, and the fact that subsequent operations on that account will also use AppleScript until the process restarts or the operator intervenes. Subsequent failures for the same account drop back to DEBUG.

6. **Write operations always AppleScript.** `send_email`, `send_email_with_attachments`, `reply_to_message`, `forward_message` always use AppleScript regardless of IMAP state. They are never candidates for delegation. (Repeating the existing hybrid design to forestall confusion.)

7. **No whole-session disable.** We do not introduce a global kill switch that disables IMAP for the whole process after N consecutive failures. Fallback is per-operation; an operation that succeeds later (e.g. user reconnects wifi) re-enables IMAP for that call with no state change. Rationale: a laptop oscillating between offline and online is a common case; a session-wide disable would be user-hostile.

## Context

Spike #39 tested the architectural assumption from [`imap-hybrid-approach.md`](./imap-hybrid-approach.md) that Mail.app's IMAP credentials could be pulled from the login Keychain via `security find-internet-password`. The assumption was falsified in under a minute: zero items with `ptcl=imap`/`imps` exist in the login or system keychain, and the Internet Accounts framework DB (`~/Library/Accounts/Accounts4.sqlite`) is TCC-protected. See [Spike Findings (#39)](./imap-hybrid-approach.md#spike-findings-39-2026-04-22).

This decision evaluates the five alternatives catalogued during that spike.

## Options evaluated

### (a) User-supplied Keychain item per Mail.app account — CHOSEN

**Mechanism.** User creates a `find-generic-password` entry with service name `apple-mail-mcp.imap.<mail_app_account_name>` and account set to their email. The server reads it via `subprocess.run(["security", "find-generic-password", "-w", ...])`.

**Reproduction.** [`scripts/spike_imap_icloud.py`](../../scripts/spike_imap_icloud.py) against iCloud on 2026-04-22:

```
Service:  apple-mail-mcp.imap.iCloud
Account:  s.morgan.jeffries@icloud.com
Server:   imap.mail.me.com:993

[4426.1 ms] Keychain lookup OK (password length 19)
[ 404.8 ms] TLS connect OK
[ 306.1 ms] LOGIN OK
[ 131.8 ms] SELECT INBOX OK (0 messages total)
[ 536.0 ms] SEARCH UNSEEN OK (0 unread)
[ 217.4 ms] fallback SEARCH ALL OK (0 total)
```

The 4.4-second Keychain lookup is a one-time prompt the first time the `security` binary reads a new item; "Always Allow" on that prompt makes subsequent reads ~10ms. TLS handshake, LOGIN, SELECT, and SEARCH all completed normally. FETCH was not exercised in this run because the test Apple ID's IMAP inbox is empty (user merged two Apple IDs; the login email's residual mailbox has no messages) — FETCH is not a novel operation for IMAPClient and is considered de-risked by the successful SEARCH.

**Setup UX.** Per account, one-time:

1. Generate an app-specific password at the provider (appleid.apple.com for iCloud; 2FA-enabled Google Accounts; Yahoo account security).
2. Run `security add-generic-password -s "apple-mail-mcp.imap.<Name>" -a <email> -w <password> -T "" -U` — the setup helper will wrap this.

**Provider coverage.** Works for any provider that still exposes IMAP + app passwords. **Verified on iCloud (2026-04-22).** Yahoo's app-password UI was not available for the test account on 2026-04-23 — Yahoo has been sunsetting app passwords for several years and the option has quietly disappeared or become non-self-serve for at least some accounts. Gmail (with 2FA enabled), Fastmail, and AOL are expected to work based on public docs but were not exercised in this spike. For providers that are OAuth-only via Mail.app's UI (some enterprise Outlook configurations, some Google Workspace tenants), the user would need a separate app-password path at the provider or fall back to AppleScript. Provider-by-provider coverage is documented, not blocked on — the mechanism is validated, and per-provider feasibility is a deployment-time question for the user.

**Security posture.** User-scoped Keychain item with the ACL the user chooses (`-T ""` = prompt-on-first-access is recommended). No credentials ever touch the MCP server's filesystem, env, or logs. No credential in the MCP server config. The server shells out to `security` exactly like Mail.app itself does.

**Why chosen.** Works today. Survives all observed macOS versions. Produces no new attack surface. UX cost is one setup step per account; given IMAP offers substantial perf and correctness wins (#40, #66), the one-time setup is proportional. Can be scripted into a `--setup-account` subcommand on the server binary to reduce the step to a one-liner.

### (b) Extract Mail.app's Google OAuth token + XOAUTH2 — REJECTED at stage 1

**Mechanism (hypothesized).** Read a `gena="Google OAuth"` generic-password item that Mail.app is presumed to create when the user authenticates Gmail via Internet Accounts, then use XOAUTH2 against `imap.gmail.com:993`.

**Reproduction.** [`scripts/spike_imap_gmail_oauth.py`](../../scripts/spike_imap_gmail_oauth.py) on 2026-04-22:

```
STAGE 1: Locate 'gena=Google OAuth' Keychain items
  Found 2 item(s):
    service='Fantastical CalDAV: apidata.googleusercontent.com'
      account='s.morgan.jeffries@gmail.com'
    service='Fantastical Exchange: outlook.office365.com'
      account='sjeffries@geisinger.edu'
```

**Finding.** Both `gena="Google OAuth"` items in the login Keychain are owned by **Fantastical**, not Mail.app. Mail.app does not create any login-keychain items with this attribute. Its Google OAuth material lives outside the user-readable login keychain — in the Data Protection Keychain or the Accounts framework DB, both TCC-protected.

Option (b) has nothing to work with at stage 1; stages 2–4 were never reached in a meaningful way.

**Secondary finding.** The Fantastical items *are* silently readable via `security find-generic-password -w` on this machine — no Keychain prompt appears. So the general claim "Keychain ACLs will prompt third parties" does not hold universally; it depends entirely on how the creating app configured the ACL. Nothing to act on here beyond noting that any future attempt to pull OAuth material from Keychain would need empirical per-provider verification.

**Rejected because.** The stored token doesn't exist in a place we can reach. Even if Mail.app stored a refresh token somewhere we *could* read, refreshing it to an access token requires Apple's private OAuth client credentials — not recoverable. Rebuilding option (b) around a user-registered Google OAuth client would give us back the worst part of (a) (user setup per account) plus OAuth token lifecycle complexity, with no UX benefit over (a) itself.

### (c) TCC-entitled helper reading Accounts framework DB — REJECTED

Would require shipping a signed helper binary with Full Disk Access and bundling it into an MCP server distributed as a Python package. Signing/notarization per release, TCC prompt during install, fragile across macOS upgrades. Wildly disproportionate to the problem. No prototype.

### (d) Env vars / config file — REJECTED

Functionally equivalent to (a) but with credentials in plaintext on disk or in process env, readable by any process running as the user, leaking into shell history / logs / crash dumps. No benefit whatsoever over (a) — the Keychain integration is the entire point. Rejected on security grounds. No prototype.

### (e) Abandon IMAP — NOT TRIGGERED

Only relevant if both (a) and (b) failed. (a) succeeded; (e) is moot.

## Implementation architecture

The chosen path produces this module layout when #41 is executed:

```
src/apple_mail_mcp/
├── keychain.py         # NEW — thin wrapper around `security find-generic-password`
├── imap_connector.py   # NEW — IMAPClient wrapper for read ops, takes creds from keychain
├── mail_connector.py   # unchanged shape — delegates read ops to IMAP when creds exist
└── exceptions.py       # add MailKeychainError, MailImapError
```

**`keychain.py` contract:**

```python
SERVICE_NAME_PREFIX = "apple-mail-mcp.imap."

def get_imap_password(mail_app_account: str, email: str) -> str:
    """Retrieve an IMAP app password from Keychain. Raises MailKeychainError
    with actionable setup instructions if the entry is missing."""
```

**Server-side setup helper (future work, scope of #41 follow-on):**

```bash
apple-mail-mcp setup-imap --account iCloud --email user@icloud.com
# → prompts for app password, writes to Keychain with the right service/account
```

**Discovery / fallback.** The presence or absence of a Keychain entry for a given Mail.app account is a per-account opt-in signal. Accounts without an entry fall back to AppleScript. No feature flag needed — the data model expresses the state directly.

## Consequences

- **#41** (architecture: `imap_connector.py` alongside `mail_connector.py`) — **unblock**. This decision sets the auth contract; #41 can proceed to detailed design of the module layout and delegation logic.
- **#40** (spike: IMAPClient integration for `search_messages`) — remains **blocked on #41**. Once `keychain.py` + `imap_connector.py` exist, the spike becomes a normal-sized integration task: take the delegation layer, run `search_messages` through it, benchmark. Updated the label and comment accordingly.
- **#66** (IMAP-based thread reconstruction) — remains **blocked on #41**. Same reasoning.
- **`imap-hybrid-approach.md`** — the "Authentication: macOS Keychain" section is now formally superseded by this document. The banner will link here.
- **`pyproject.toml`** — gains an optional `research` extra with `imapclient>=3.0.0`. When the production IMAP path lands, `imapclient` will move into the primary dependency set; the `research` extra stays as-is for continued spike work.

## Known caveats to carry into implementation

1. **First-access Keychain prompt.** The user will see one "security wants to use Keychain" prompt the first time the server reads each new entry. Clicking "Always Allow" persists the grant. The setup helper's UX should mention this up front.
2. **Multiple accounts per provider.** Keying the Keychain item by Mail.app account name (not by provider or email) means a user with two Gmail accounts in Mail.app creates two distinct entries. This is the correct semantic — Mail.app's own account model is the source of truth.
3. **Empty-mailbox test caveat.** The 2026-04-22 iCloud spike authenticated against an Apple ID whose IMAP inbox was empty, as was the user's other iCloud-typed Mail.app account — iCloud Mail simply isn't this user's primary inbox. FETCH envelope was therefore not exercised in either spike run. Integration tests on #41's delivery should include FETCH against a populated mailbox (any provider).
4. **iCloud alias semantics.** iCloud accepts IMAP LOGIN by any email alias on the Apple ID, which makes the `email` arg of `keychain.get_imap_password` semi-flexible. The production contract should standardize on the primary Apple ID email to avoid confusion; the setup helper should document this.
5. **No "auto-discovery" of Mail.app accounts into Keychain.** We do not attempt to enumerate Mail.app accounts and pre-populate Keychain entries from their configuration. Users explicitly run the setup helper per account. This is deliberate — it keeps the data flow one-directional (user → Keychain → server).
6. **Per-provider app-password availability is not guaranteed.** The mechanism works; whether any given provider still offers an app password the user can create is a provider-policy question that varies by account, tenant, and year. Yahoo was unavailable for the test account on 2026-04-23; Gmail app passwords require 2FA and can be disabled by Google Workspace admins; iCloud remains the most reliable path. The setup helper should fail loudly and surface the provider's documentation URL when a user attempts to set up an account whose provider no longer exposes app passwords.

## References

- [`scripts/spike_imap_icloud.py`](../../scripts/spike_imap_icloud.py) — (a) prototype
- [`scripts/spike_imap_gmail_oauth.py`](../../scripts/spike_imap_gmail_oauth.py) — (b) spike
- [`scripts/probe_keychain_for_imap.sh`](../../scripts/probe_keychain_for_imap.sh) — #39 probe
- [`imap-hybrid-approach.md`](./imap-hybrid-approach.md) — background architecture research
- [IMAPClient documentation](https://imapclient.readthedocs.io/)
