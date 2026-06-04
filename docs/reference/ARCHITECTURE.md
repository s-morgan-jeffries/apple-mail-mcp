# Architecture

## Component diagram

```
Claude Desktop / MCP client
        |  (MCP JSON-RPC over stdio)
        v
server.py (FastMCP)
  |-- 23 tools (@_tool / @mcp.tool)        9 read-only, 14 mutating
  |-- input validation + sanitization
  |-- elicitation (confirmation) gates on destructive ops
  |-- structured responses ({"success": bool, ...})
        |
        v
mail_connector.py (AppleMailConnector) — dispatch + domain logic
        |                                   \
        | AppleScript path (baseline)        | IMAP fast path (when hinted + creds)
        v                                     v
  subprocess.run(["osascript", "-"])    imap_connector.py (ImapConnector / pool)
        |                                     |
        v                                     v
  Apple Mail.app (macOS Automation)     the account's IMAP server
```

## Dispatch model (the central v0.8.0 abstraction)

**AppleScript is the universal baseline** — every operation works through `osascript` against Mail.app,
with no extra setup. On top of that, several read and bulk-mutation operations take an **IMAP fast
path** when two conditions hold:

1. the caller hints the location — an `account` (and, where relevant, a `source_mailbox` / `mailbox`), and
2. the account has Keychain IMAP credentials (opt-in via `apple-mail-fast-mcp setup-imap`).

When both hold, the connector issues server-side IMAP (e.g. `SEARCH`, `UID MOVE`, `STORE`) instead of
driving Mail.app's per-message AppleScript loop. **On any IMAP failure** — no credentials, bad
password, offline, capability gap — it falls back to AppleScript, so functionality is never lost; you
only gain speed when IMAP is configured and reachable. Failures are absorbed by the
`_IMAP_FALLBACK_EXCS` set and a **per-account circuit breaker** (`_imap_breaker_*`, ~30 s cooldown,
#118) so a flaky account doesn't pay the connect/login cost on every call.

IMAP connections are created per call by default; an opt-in **connection pool** (`APPLE_MAIL_MCP_IMAP_POOL=1`,
#75) amortizes the ~400 ms TCP+TLS+LOGIN across calls.

Fast paths shipped in v0.8.0: search (#32-era), `get_messages` / `get_attachments` / `get_thread`
reads, and the bulk mutations — move (#149), delete (#150), read-status (#151), flag (#152) — each
with an AppleScript fallback. Compose/send (`create_draft` and the `send_now=true` send) is
**always** AppleScript — it needs Mail.app's compose machinery.

## Dual-emit message-ID model (#148)

A message row's `id` is **path-native**: Mail.app's internal numeric id on the AppleScript path, the
RFC 5322 `Message-ID` (bracketless) on the IMAP path. To let callers cross paths without caring which
produced a row, read tools also emit `rfc_message_id` (always the RFC id, or null). The mutation
fast-paths and `create_draft(reply_to=/forward_of=)` accept **either** form — pass back the `id` a
read tool gave you verbatim.

## Drafts lifecycle (#134)

Mail.app's real primitive is the draft — every outgoing message is a draft until sent. Three tools
model the lifecycle:

- `create_draft` — new / reply (`reply_to`) / forward (`forward_of`); `send_now=true` sends instead of saving.
- `update_draft` — **delete-and-recreate** (Mail.app forbids mutating a saved draft in place); reply/forward threading headers are preserved by re-seeding from the original.
- `delete_draft` — move a draft to Trash.

## IMAP thread tiers (`get_thread`)

`find_thread_members` picks the cheapest correct strategy per provider (see
[../research/imap-thread-strategies.md](../research/imap-thread-strategies.md)):

| Tier | Strategy | When | Cost |
|------|----------|------|------|
| 1 | Gmail `X-GM-THRID` via `[Gmail]/All Mail` | Gmail, All Mail exposed over IMAP | ~4 round-trips, mailbox-count-independent |
| 1.5 | per-mailbox `X-GM-THRID` iteration | Gmail, All Mail hidden | ~6× faster than BFS, but ∝ label count |
| 2 | RFC 5256 `THREAD REFERENCES` per mailbox | server advertises THREAD (e.g. Fastmail) | per-mailbox THREAD × M |
| 3 | per-mailbox header-search BFS | universal (e.g. iCloud — no THREAD/X-GM) | M × N × 3 `SEARCH HEADER` round-trips |

If IMAP isn't configured/reachable, `get_thread` reconstructs the thread via AppleScript (subject
prefilter + `In-Reply-To`/`References` header walk).

## Module responsibilities

| Module | Role |
|--------|------|
| `server.py` | MCP tool registration, validation, elicitation gates, response formatting |
| `mail_connector.py` | AppleScript generation/execution + IMAP-fast-path dispatch |
| `imap_connector.py` | IMAP client, connection pool, search/fetch/bulk-mutation fast paths |
| `security.py` | Input sanitization, rate limiting, audit logging, confirmation flows |
| `utils.py` | Pure functions: escaping, parsing, validation |
| `drafts.py` / `templates.py` | Draft-seed state and email-template storage under `~/.apple_mail_mcp/` |
| `exceptions.py` | Typed exception hierarchy |

## Design decisions

- **Thin server, thick connector.** Business logic never lives in `server.py`; it stays in the connector.
- **Single AppleScript execution point.** All AppleScript runs through `_run_applescript()` — the unit-test mock boundary and the one place timeout/error-routing lives (stderr → typed exceptions).
- **JSON output via ASObjC.** AppleScript emits JSON through `NSJSONSerialization` (`_wrap_as_json_script` / `parse_applescript_json`), not fragile pipe-delimited text. See [APPLESCRIPT_GOTCHAS.md](APPLESCRIPT_GOTCHAS.md).
- **Structured responses.** Every tool returns `{"success": bool, ...}`; errors carry `error` + `error_type`. No exception reaches the LLM.
- **Confirmation by elicitation.** Destructive tools (`delete_*`, `create_rule` with move/forward/delete actions, `create_draft` with `send_now`) gate behind MCP elicitation, fail-closed.
- **Gmail label mode.** `gmail_mode` uses copy+delete instead of move for Gmail's label-based system.
