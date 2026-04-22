# `get_thread` — conversation reconstruction from Mail.app

**Issue:** #29
**Date:** 2026-04-21
**Status:** Approved

## Context

Issue #29 asks for a tool that returns all messages in a conversation given a message reference. Mail clients commonly display this as a "conversation view."

Mail.app does not expose a native `thread` or `conversation` object via AppleScript (confirmed empirically — the existing claim in the applescript-mail skill was correct on this point, unlike the same skill's earlier incorrect claim about rules). Threading therefore has to be reconstructed by the connector from RFC 5322 header fields: each message's `Message-ID`, `In-Reply-To`, and `References` headers collectively define a reply graph.

Mail.app does expose:
- `message id of msg` — the RFC 822 Message-ID, direct property access.
- `headers of msg` — a collection of header objects with `name` and `content`, including `in-reply-to` and `references` when present.

The key performance constraint, verified empirically against a production Gmail INBOX:

- `whose subject contains "X"` — sub-second (subject is indexed for search).
- `whose message id is "X"` — ~21 seconds for a single exact-match lookup in the same INBOX (not indexed; linear scan).

The 21 s-per-id figure rules out any algorithm that issues one `whose message id` lookup per reference. A single small thread could require minutes.

## Non-goals

- **Cross-account threading.** v1 searches only within the anchor message's account. If a thread spans multiple accounts (forwarding, aliases), cross-account reconstruction is a future enhancement.
- **Perfect recall when subjects are rewritten mid-thread.** The subject-prefilter optimization below misses thread members whose subject was deliberately changed. Rare in practice; documented as a known limitation.
- **IMAP-based threading.** See "Future considerations" — the AppleScript path must stand on its own. IMAP support is tracked in #41.

## Design

### Tool contract

```python
@mcp.tool()
def get_thread(message_id: str) -> dict[str, Any]:
    """Return all messages in the thread containing the given message, chronological."""
```

Return shape:

```json
{
  "success": true,
  "thread": [
    {"id": "...", "subject": "...", "sender": "...",
     "date_received": "...", "read_status": false, "flagged": false},
    ...
  ],
  "count": N
}
```

Per-message fields match `search_messages` output (6 fields). No message content; callers chain `get_message` for bodies.

Error paths:
- Anchor not found → `MailMessageNotFoundError` (server maps to `error_type: "not_found"`).
- Anchor has malformed / missing threading headers → thread = `[anchor]`, `count: 1`. Not an error.

### Algorithm (two AppleScript calls total)

**Call 1 — resolve anchor.** Iterate accounts/mailboxes looking for a message whose internal `id` matches (same pattern as `get_message`). Return: account name, RFC 822 `message-id`, subject, `in-reply-to` header value, `references` header value.

**Python step — compute base subject.** Iteratively strip leading `Re:`, `Fwd:`, `Fw:`, `R:` prefixes (case-insensitive) so `"Re: Re: Q3 Report"` and `"Q3 Report"` reduce to the same key.

**Python step — seed known-ids set.**
```
known_ids = {anchor.rfc_message_id}
if anchor.in_reply_to:  known_ids.add(anchor.in_reply_to)
if anchor.references:   known_ids.update(parse_message_ids(anchor.references))
```
`parse_message_ids` extracts `<id>` tokens from a `References:` header's whitespace-separated list.

**Call 2 — collect candidates.** For each mailbox in the anchor's account:
- `whose subject contains "<base_subject>"` (Mail's fast indexed search).
- For each hit, read headers collection, extract `message-id`, `in-reply-to`, `references`.
- Return `[{rfc_message_id, in_reply_to, references_raw, id, subject, sender, date_received, read_status, flagged}, ...]`.

Single AppleScript loops all mailboxes internally so this is one osascript call, not one per mailbox.

**Python step — graph walk.**
```
thread_ids: set[str] = set()
candidates: list[dict] = <result of Call 2>
known_ids: set[str] = <from anchor>
changed = True
while changed:
    changed = False
    for cand in candidates:
        if cand.internal_id in thread_ids: continue
        cand_refs = {cand.rfc_message_id} | {cand.in_reply_to} | parse(cand.references_raw)
        if cand_refs & known_ids:
            thread_ids.add(cand.internal_id)
            known_ids |= cand_refs
            changed = True
# Guard: at most 100 iterations (real threads terminate on pass 1 or 2).
```

Anchor itself is always included.

**Python step — sort and shape.** Sort accepted candidates by `date_received` ascending. Drop the threading-header fields; return the 6 search-shape fields.

### Files to modify

| Path | Change |
|---|---|
| `src/apple_mail_mcp/mail_connector.py` | New `get_thread(message_id)` method; helper `_normalize_subject`; helper `_parse_rfc822_ids`; two new AppleScript bodies (resolve-anchor, collect-candidates) |
| `src/apple_mail_mcp/server.py` | New `@mcp.tool() get_thread`; error mapping (`MailMessageNotFoundError` → `not_found`) |
| `src/apple_mail_mcp/security.py` | Add `get_thread` to `OPERATION_TIERS["cheap_reads"]` |
| `tests/unit/test_mail_connector.py` | Unit tests for `_normalize_subject`, `_parse_rfc822_ids`, graph walk, empty-headers case, script-shape guards |
| `tests/unit/test_server.py` | `TestGetThread` — success, not-found, error-type mapping |
| `tests/unit/test_security.py` | Extend tier-assignment test |
| `tests/e2e/test_mcp_tools.py` | `EXPECTED_TOOLS` 16 → 17; invocation case with mocked connector |
| `tests/e2e/test_stdio_transport.py` | `EXPECTED_TOOLS` 16 → 17 |
| `tests/integration/test_mail_integration.py` | Real-Mail test: find a known-threaded message, assert thread length ≥ 2 OR skip if no threaded messages in test INBOX |
| `docs/reference/TOOLS.md` | Phase 4 entry with parameter table, return shape, limitations |
| `.claude/CLAUDE.md` | API surface 16 → 17; Phase 4 bullet expanded |
| `.claude/skills/applescript-mail/SKILL.md` | Correct the "No thread/conversation access" entry to describe the header-based reconstruction pattern |
| `.claude/skills/api-design/SKILL.md` | Tool count bump |
| `docs/guides/TESTING.md` | Tool count bump |
| `evals/agent_tool_usability/run_eval.py` | Add `get_thread` to `TOOL_NAMES` |
| `evals/agent_tool_usability/tool_descriptions.md` | New tool entry |
| `evals/agent_tool_usability/scenarios.py` | 1–2 scenarios ("show me the full thread for message X") |

### Known limitations (documented in docstring)

1. **Subject rewrites miss thread members.** A message whose subject was rewritten mid-thread is not included. Mitigation: accept the tradeoff; documented.
2. **Orphan anchors** (no threading headers) return `thread: [anchor]` rather than an error.
3. **Single-account scope.** The tool searches only the anchor's account. Cross-account threading is a separate feature.
4. **Cycle / malformed reference loops** are handled by the change-tracking while-loop plus a hard cap of 100 passes.

### Security / safety

- No mutation — read-only.
- No account parameter — does not trigger `check_test_mode_safety`.
- Rate limited in `cheap_reads` (same as `list_accounts`, `list_mailboxes`, `get_message`, `get_attachments`, `save_attachments`, `list_rules`).
- `message_id` is sanitized + escaped before being inserted into AppleScript (same pattern as `get_message`).
- Subject pre-filter value (`base_subject`) is escaped before insertion — this is where an attacker-controlled subject could land in a `whose` clause, so escaping is mandatory.

## Future considerations

Tracked as follow-up **#66** (blocks on #41's `imap_connector`).

When IMAP is available, `get_thread` should have a clean IMAP code path:

- **IMAP THREAD extension (RFC 5256):** server-side threading. Client sends `THREAD REFERENCES UTF-8 ALL` and gets back a tree. Removes the subject-prefilter dependency entirely; no client-side graph walk; handles subject rewrites correctly.
- **Gmail `X-GM-THRID`:** Gmail tags every message with a 64-bit thread id accessible via IMAP. Single exact-match fetch against the thread id of the anchor returns every member in sub-second. Canonical for Gmail accounts.
- **Cheap header reads:** `BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)]` fetches just the threading headers without the body. Mail.app AppleScript has no equivalent.

The AppleScript implementation landing in #29 is the baseline; the IMAP path (once available) adds capability without removing the AppleScript fallback.

## Verification

1. `uv run pytest tests/unit/test_mail_connector.py -k get_thread` — connector unit tests (normalize_subject, parse_rfc822_ids, graph walk, empty-headers, script-shape guards).
2. `uv run pytest tests/unit/test_server.py -k get_thread` — server unit tests pass.
3. `make check-all` — green.
4. `make test-e2e` — 23 e2e tests (22 existing + 1 new invocation case) pass.
5. `MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=Gmail uv run pytest tests/integration/.../test_get_thread --run-integration -v` — given a known-threaded message in Gmail, returns the full thread; given an orphan message, returns `[anchor]`.
6. Manual osascript probe: stacked `subject contains "..."` across multiple mailboxes works under Mail.app; headers of candidates read cleanly.
