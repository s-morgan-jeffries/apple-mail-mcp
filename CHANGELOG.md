# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

**Flag color labels in `update_message(flag_color=...)` (#185):** The map from color name to AppleScript flag index in [`utils.py:get_flag_index`](src/apple_mail_mcp/utils.py) had two pairs of swapped labels. Empirical testing (Gmail/Mail.app, 2026-05-12) confirmed that callers passing certain colors got a different color in Mail.app's UI than they asked for:

- `flag_color="orange"` previously rendered as **red**; now renders as **orange**.
- `flag_color="red"` previously rendered as **orange**; now renders as **red**.
- `flag_color="blue"` previously rendered as **green**; now renders as **blue**.
- `flag_color="green"` previously rendered as **blue**; now renders as **green**.
- `yellow`, `purple`, `gray`, `none` were correctly labeled and unchanged.

The AppleScript-path default for `update_message(flagged=True)` (no `flag_color`) was also adjusted from `get_flag_index('orange')` to `get_flag_index('red')` so the no-color rendering remains red (matching #152's IMAP fast path which sets bare `\Flagged`). Net behavior: `update_message(flagged=True)` still produces a red flag, regardless of which path runs.

Callers who were relying on the buggy mapping should update their calls to use the color name they actually want.

### Performance

**IMAP fast path for flag-only `update_message` (#152):** When `update_message` is called with only `flagged` set (no `flag_color`, `read_status`, or `destination_mailbox`) plus `account` + `source_mailbox`, the flag/unflag mutation now runs server-side via IMAP `UID STORE +/-FLAGS (\Flagged)` — single round-trip after Message-ID resolution. `\Flagged` is base IMAP (RFC 3501), universal across all servers; no capability check, no fallback variants. Verified empirically (Gmail/Mail.app, 2026-05-12) that bare `\Flagged` produces identical visual state to today's AppleScript path's `set flag index = 0` — no UI difference. Color-specifying calls (`flag_color="red"` etc.) still route through AppleScript since Mail.app's color encoding (`$MailFlagBit*` user keywords) is out of IMAP scope. **This closes the IMAP-fast-paths-for-mutations arc** (#149 move, #150 delete, #151 read, #152 flag) — every single-field mutation on `update_message` and `delete_messages` now has an IMAP path on the common path.

**IMAP fast path for read-status-only `update_message` (#151):** When `update_message` is called with only `read_status` set (no `flagged`, `flag_color`, or `destination_mailbox`) plus `account` + `source_mailbox`, the read/unread mutation now runs server-side via IMAP `UID STORE +/-FLAGS (\Seen)` — single round-trip after Message-ID resolution. `\Seen` is base IMAP (RFC 3501), universal across all servers; no capability check needed, no fallback variants. Resolves the dual-ID problem from #147 for the read-status path. Combined patches (read + move, read + flag) still run via AppleScript pending #152. With #149/#150 already landed, the IMAP-fast-paths-for-mutations arc now covers move-only, delete, and read-status-only on the common path; `flag_message` (#152) is the last domino.

**IMAP fast path for `delete_messages` (#150):** When invoked with `account` and `source_mailbox`, `delete_messages` now runs server-side via IMAP `UID MOVE` to the account's Trash folder (RFC 6851), atomic and single round-trip after Message-ID resolution. Resolves the same dual-ID problem from #147 that #149 fixed for moves: callers feeding RFC 5322 Message-IDs from `search_messages`'s IMAP path no longer pay the AppleScript `whose message id is` linear scan (~57s on a 47k-message Gmail INBOX). Trash folder is resolved via RFC 6154 SPECIAL-USE `\Trash` flag; falls back to conventional names (`Trash`, `[Gmail]/Trash`, `Deleted Messages`, `Deleted Items`) when SPECIAL-USE isn't advertised. Capability fallback chain: `MOVE` → `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` (UIDPLUS only) → AppleScript. Servers without either capability, or without a discoverable Trash folder, fall through transparently. Combined with #149, the IMAP-fast-paths-for-mutations arc now covers move and delete on the common path; `mark_as_read` (#151) and `flag_message` (#152) still pending.

**IMAP fast path for move-only `update_message` (#149):** When a caller invokes `update_message` with only `destination_mailbox` set (no `read_status`, `flagged`, or `flag_color`) and provides `source_mailbox`, the move now runs server-side via IMAP `UID MOVE` (RFC 6851) — atomic, single round-trip after Message-ID resolution. Resolves the dual-ID problem from #147: callers feeding RFC 5322 Message-IDs from `search_messages`'s IMAP path no longer pay the AppleScript `whose message id is` linear scan (~57s on a 47k-message Gmail INBOX). Capability detection prefers `MOVE`, falls back to `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` when only `UIDPLUS` (RFC 4315) is advertised — the scoped `UID EXPUNGE` is safe (removes only the just-moved UIDs, not other `\Deleted`-flagged messages in the mailbox). Servers advertising neither MOVE nor UIDPLUS fall through to AppleScript via the existing graceful-degradation path. Combined patches (move + read/flag in one call) still run via AppleScript pending #150 / #151 / #152. Requires `source_mailbox` — without it, IMAP would have to SEARCH every mailbox per Message-ID, defeating the speed win.

## [0.7.0] - 2026-05-10

API-surface release. Two parallel arcs landed: (1) the #129 audit-driven consolidation that collapsed seven near-duplicate tools into shared verbs (`update_message`, `update_rule`, `get_messages`, expanded `search_messages` filters), and (2) the mailbox + drafts CRUD additions that complete the write-side surface (`update_mailbox`, `delete_mailbox`, `create_draft` / `update_draft` / `delete_draft`). The IMAP thread-discovery work from v0.6.0 is now fully tiered with Tier 1.5 (Gmail per-mailbox X-GM-THRID) and Tier 2 (RFC 5256 THREAD) shipped. Net tool count: 27 → 23 — fewer surfaces, broader coverage.

### ⚠️ Breaking changes

The audit-driven consolidations remove or reshape several public tools. Per-change migration notes live in the relevant Changed/Added entries below; the headline list:

- **Removed tools** (functionality folded into existing tools — see entries for migration paths): `set_rule_enabled` (#130), `get_selected_messages` (#131), `get_attachments` (#133), `get_message` (#144), `mark_as_read` / `move_messages` / `flag_message` (#135), `send_email` / `send_email_with_attachments` / `reply_to_message` / `forward_message` (#134 — replaced by `create_draft` / `update_draft` with `send_now=True`).
- **`search_messages.source` changed shape**: `str` (`"all"` / `"selected"`) → `list[str] | None` with the literal `"SELECTED"` token. Callers passing `source="selected"` must switch to `source=["SELECTED"]`. (#144)
- **`search_messages` `include_content` parameter dropped.** Bodies are always included on the `content` row field; post-process if you need to suppress them. (#131)
- **`search_messages` result order is newest-first** (was oldest-first in v0.5.0; landed in v0.6.0 via #114 but worth re-flagging for upgraders skipping versions).
- **`sender` field format changed**: now `"Display Name <email>"` (was bare email). Callers parsing the field should split on `<`. (#158)
- **`delete_messages(permanent=True)` now emits `DeprecationWarning`** — Mail.app has no permanent-delete path that bypasses Trash; the parameter has always been a silent no-op. (Landed in v0.6.0 via #111; reiterated here for upgraders.)

### Added

**`update_mailbox` tool (#102):** Rename and/or re-parent (move) an existing mailbox. Two delivery paths: rename-only (no `new_parent`) goes via AppleScript with no IMAP needed; move (any `new_parent`, optionally combined with rename) goes via IMAP RENAME and requires Keychain credentials per the #73 opt-in flow. Combined "move and rename" works in one IMAP RENAME. `new_parent=""` moves to top-level. Path-traversal-safe: `new_name` is sanitized via `sanitize_mailbox_name`. (#165, #166)

**`delete_mailbox` tool (#162):** IMAP-only — Mail.app's AppleScript dictionary's `delete` command rejects mailbox specifiers, so this operation lives outside AppleScript. Pre-flight SELECT-readonly to read the EXISTS count; refuses non-empty mailboxes (`error_type: "mailbox_not_empty"`) unless `delete_messages=True` is passed to cascade. Always elicits user confirmation (destructive). Requires Keychain credentials per #73. (#166)

**Drafts lifecycle (#134):** Three new tools — `create_draft`, `update_draft`, `delete_draft` — give a complete compose/reply/forward authoring loop. `seed_kind="compose" | "reply" | "reply_all" | "forward"` selects the source-message-derived defaults (subject prefix, recipients, quoted body); `template_name` renders an existing template into the draft body. Save-as-draft semantics by default (no send); `send_now=true` is an explicit opt-in that runs through the same safety + rate-limit gates as the prior send tools. Draft state persisted under `~/.apple_mail_mcp/drafts/` so subsequent calls can resolve a draft by id. **Subsumes and removes** `send_email`, `send_email_with_attachments`, `reply_to_message`, `forward_message`. Migration: `send_email(to=..., subject=..., body=...)` → `create_draft(seed_kind="compose", to=[...], subject=..., body=..., send_now=True)`; `reply_to_message(message_id=X, body=Y)` → `create_draft(seed_kind="reply", reply_to_id=X, body=Y, send_now=True)`; `forward_message(message_id=X, to=[...])` → `create_draft(seed_kind="forward", forward_id=X, to=[...], send_now=True)`. (#160)

**IMAP thread-discovery Tier 1.5 + Tier 2 (#123, #125):** Completes the tiered dispatch in `find_thread_members` (started in v0.6.0 as Tier 1 + Tier 3). **Tier 1.5 (Gmail per-mailbox X-GM-THRID):** when `X-GM-EXT-1` is advertised but `[Gmail]/All Mail` is hidden over IMAP (per-folder size opt-out — common), find the anchor's UID in INBOX or `\Sent`, FETCH its X-GM-THRID, then SEARCH X-GM-THRID across every selectable folder. ~2+M*2 round-trips (~186 on a 92-folder account vs ~1100 for BFS). **Tier 2 (RFC 5256 THREAD):** when `THREAD=REFERENCES` or `THREAD=REFS` is advertised (Fastmail, some Dovecot deployments), per mailbox issue a narrow SEARCH for anchor UID + sibling refs, then THREAD on the full mailbox to collect intersecting clusters, then FETCH ENVELOPE+FLAGS. Both tiers fall through cleanly to the existing Tier 3 (header-search BFS) when capabilities aren't advertised or the server rejects mid-flight. Cleared the v0.6.0 deferrals — Gmail / Fastmail / iCloud / generic IMAP all now land on the most efficient strategy their server actually supports. (#169)

**`atexit` hook for `ImapConnectionPool.close()` (#127):** Cached IMAP sessions close cleanly on interpreter shutdown rather than dropping connections silently. Matters for short-lived tools, CI runs, and any environment where the parent shell doesn't otherwise exercise the cleanup path. Idempotent — safe to call `close()` manually too. (#167)

**Gmail-mode benchmark separation (#101):** Bulk-ops benchmarks now run twice — once via standard IMAP MOVE, once via Gmail's copy+delete fallback (`gmail_mode=True`) — so the cost difference between the two paths is measurable separately. Surfaces the steady-state penalty Gmail imposes on label-based moves. (#168)

**`from_account` parameter on the send path (#155):** Optional `from_account: str | None = None` parameter — pass an account name or UUID (matching the `account` convention elsewhere) to choose which configured Mail.app account sends the message. Resolves to the account's primary email address and sets it on the AppleScript `sender` property. Validation: raises `error_type: "account_not_found"` if no account matches. `from_account=None` (default) preserves Mail.app's default-sender behavior. Originally landed on the v0.6 send tools (`send_email` / `send_email_with_attachments` / `reply_to_message` / `forward_message`); after the drafts lifecycle (#134) absorbed those, the parameter now lives on `create_draft` and `update_draft`. External contribution from @robertvitali.

**`body_contains` and `text_contains` filters on `search_messages` (#145):** Substring match against message body content (`body_contains`) or headers + body (`text_contains`, RFC 3501 `TEXT` semantics). Sub-second on the IMAP path (server-side `BODY` / `TEXT` predicates). Slow on the AppleScript fallback — measured 148s for 100 cold-cache messages on a 47k-message Gmail INBOX. AppleScript `text_contains` approximates the IMAP semantic by matching `content + subject + sender` (recipients omitted). Combinable with all other filter parameters and with `source=[ids]` scoping.

**Slow-operation warnings (#146):** `search_messages` responses may include a new `warnings: list[str]` field that surfaces proactive cost concerns before slow paths run. v0.7.0 detection: when the call commits to AppleScript with `body_contains` or `text_contains` set, the response includes a warning advising IMAP setup for sub-second body search. Schema is additive — existing callers ignoring the field are unaffected, the field is omitted entirely when no warnings fire. Mechanism is general enough that future tools can opt in.

### Changed

**`update_mailbox` / `delete_mailbox` refuse Gmail system labels (#164):** Operations targeting the bare `[Gmail]` parent or any `[Gmail]/...` child path now return `error_type: "unsupported_gmail_system_label"` instead of failing with a confusing `IMAPClientError` (or worse, a no-op "success"). For `update_mailbox`, both the source `name` and the resulting destination (when `new_parent` is provided) are checked. Pre-flight: no AppleScript or IMAP traffic. Localized Gmail prefixes (e.g. `[Google Mail]/Tutta la posta` on Italian Gmail) intentionally not detected — proper detection needs an IMAP session for SPECIAL-USE flag enumeration; tracked as a follow-up. The Gmail-label CRUD tools (sub-feature 2 of #164) remain deferred. (#170)

**"Display Name &lt;email&gt;" sender format (#158):** The `sender` field on message objects now consistently emits the human-readable form `"Alice <alice@example.com>"` instead of the bare email address. Provides display-friendly output without losing parseability — callers that need just the email can split on `<`. **Observable behavior change:** callers expecting the bare-email format from the AppleScript path will need to adjust. (#161)


**`update_message` consolidates `mark_as_read` + `move_messages` + `flag_message` (#135):** Single CRUD-style update tool replaces the three previous mutation tools. Patch semantics — caller specifies only the fields to change; all mutations apply in one AppleScript pass via the existing bulk-update helper. Order of operations: read-state and flag changes apply first (in source mailbox), then the move (IMAP requires the message to exist in the source folder for STORE before MOVE). Trash-restore is just `update_message(ids, destination_mailbox="INBOX", source_mailbox="Deleted Messages", account="iCloud")` — no new verb required. Migration: `mark_as_read([id], read=True)` → `update_message([id], read_status=True)`; `move_messages([id], "Archive", "Gmail")` → `update_message([id], destination_mailbox="Archive", account="Gmail")`; `flag_message([id], "red")` → `update_message([id], flag_color="red")`. Sixth consolidation from the #129 audit. Tool count: 24 → 22.

**`update_rule` absorbs `set_rule_enabled` (#130):** The standalone `set_rule_enabled` MCP tool is removed; toggle a rule's enabled state via `update_rule(rule_index, enabled=True|False)` instead. `update_rule` now prompts for confirmation only when the patch touches `conditions`, `actions`, or `match_logic` (irreversible fields); patches limited to `enabled` and/or `name` skip the prompt. Migration: callers that did `set_rule_enabled(idx, True)` should call `update_rule(idx, enabled=True)`. First of the consolidations from the #129 audit (27 → 20 tools).

**`include_attachments` on `get_messages` and `search_messages`; remove `get_attachments` MCP tool (#133 + #142):** Folds the standalone `get_attachments` MCP tool into the read tools as an optional flag. `get_messages` adds `include_attachments: bool = True` (default-on — id-list cardinality is bounded, so attachment metadata is cheap-enough on the AppleScript fallback path). `search_messages` adds `include_attachments: bool = False` (default-off — AppleScript per-row attachment enumeration scales non-linearly with cold-cache state; bench measured 1s for 50 messages vs 97s for 100 cold-cache messages on a 47k-message Gmail INBOX, ruling out default-on). On the IMAP fast path, `BODYSTRUCTURE` bundles into the existing FETCH for both tools — essentially free. The connector primitive `mail.get_attachments()` stays as an internal helper. Migration: callers of `get_attachments(message_id)` should call `get_messages([message_id], include_attachments=True)` (the default) and read `response["messages"][0]["attachments"]`. Per-account variance: users with mixed IMAP-configured / non-IMAP accounts should pass `include_attachments=False` to `search_messages` for AppleScript-bound calls; documented in TOOLS.md. Tool count: 25 → 24.

**Restore `get_thread`; introduce `get_messages`; reshape `search_messages.source` (#144 + #140):** Reverts the placement decisions in #131 and #132 after dialogue iteration converged on a cleaner two-axis design (output: metadata vs bodies; input: shared `list[str]` shape with `"SELECTED"` sentinel). `search_messages.source` becomes `list[str] | None`: `None` (default) searches the account/mailbox normally, a list scopes the search to those specific ids, and the literal token `"SELECTED"` may appear in the list and is server-resolved to Mail.app's current UI selection. Filter parameters compose with `source=[ids]`. The `thread_of` parameter is removed; thread retrieval moves to a restored `get_thread(message_id)` MCP tool — making the lookup cost honest and producing a reusable id list. New `get_messages(message_ids: list[str])` MCP tool returns full messages (bodies) for an id list, with the same `"SELECTED"` sentinel convention; replaces singular `get_message` (which is removed; connector primitive `mail.get_message()` stays). Migration: `search_messages(source="selected")` → `search_messages(source=["SELECTED"])`; `search_messages(thread_of=X)` → `get_thread(X)` for metadata or `get_thread(X)` then `get_messages([those_ids])` for bodies; `get_message(id)` → `get_messages([id])`.

**`search_messages` absorbs `get_thread` (#132):** The standalone `get_thread` MCP tool is removed; pass `thread_of=<message_id>` to `search_messages` to retrieve all messages in the same thread as the anchor. Composes with the other filter parameters: `thread_of=X + read_status=False` returns unread thread members, `thread_of=X + sender_contains="alice"` returns alice's contributions to the thread, etc. Anchor-not-found returns `error_type: "message_not_found"` (preserving prior `get_thread` semantics). The Tier 1 / Tier 3 IMAP threading dispatch from #122 is preserved — the server tier delegates to the existing `mail.get_thread()` connector primitive. Migration: callers of `get_thread(message_id)` should call `search_messages(thread_of=message_id)`. Third consolidation from the #129 audit.

**`search_messages` absorbs `get_selected_messages` (#131):** The standalone `get_selected_messages` MCP tool is removed; pass `source="selected"` to `search_messages` to retrieve Mail.app's current UI selection. When `source="selected"`, all filter parameters (`account`, `mailbox`, `sender_contains`, `subject_contains`, `read_status`, `is_flagged`, `date_from`, `date_to`, `has_attachment`, `limit`) are silently ignored — selection is global to Mail.app. Message bodies are always included on the `content` row field; the prior `include_content` knob is dropped (callers that need to suppress bodies can post-process). The `account` parameter is now optional in the `search_messages` signature; with `source="all"` (default) it remains required, returning a `validation_error` if omitted. Migration: callers of `get_selected_messages()` should call `search_messages(source="selected")`. Second consolidation from the #129 audit.

### Documentation

**API surface audit (#129):** Recommended consolidating 27 → 20 tools by collapsing near-duplicate verbs into shared CRUD-style tools and folding standalone retrievers into filter parameters on the read tools. Drove every "X absorbs Y" change in this release. Final landed count: 23 (consolidations + the new mailbox/drafts CRUD additions). (#136)

### Fixed

**Security-gate registry stale entries (release-review):** `OPERATION_TIERS` and `SEND_OPERATIONS` in `security.py` referenced the four removed v0.6 send tools (`send_email`, `send_email_with_attachments`, `reply_to_message`, `forward_message`) and were missing the new draft tools. Effect: any production call to `create_draft(send_now=True)` or `update_draft(send_now=True)` would hit `KeyError: 'create_draft'` from the rate-limit gate (test fixtures stubbed `check_rate_limit`, masking the bug from CI). Additionally, `create_draft` / `update_draft` weren't in `SEND_OPERATIONS`, so the test-mode reserved-domain check didn't apply to send_now flows with explicit recipients. Fix: registered `create_draft` / `update_draft` under `"sends"` (and `delete_draft` under `"expensive_ops"`) in `OPERATION_TIERS`, added `create_draft` / `update_draft` to `SEND_OPERATIONS`, and removed the dead v0.6 entries. The implicit-reply test-mode bypass (where reply-context recipients aren't surfaced to the safety gate) is tracked separately as a follow-up (#175).

**Dependency bumps for vulnerability remediation:** `python-multipart` 0.0.26 → 0.0.28 (transitive via `mcp` / `fastmcp`; CVE-2026-42561), `pip` 26.0.1 → 26.1.1 (transitive via `pip-audit` dev dep; CVE-2026-3219, CVE-2026-6357). Lockfile regenerated; no API impact.

### Tooling

- `pyproject.toml` `version = "0.7.0"`
- `__init__.py` `__version__ = "0.7.0"`
- `MailUnsupportedGmailSystemLabelError` added to the typed exception hierarchy (#164)
- `is_gmail_system_label()` pure helper added to `utils.py` (#164)

## [0.6.0] - 2026-05-03

Performance and ergonomics release. The IMAP delegation arc started in v0.5.0 is now complete: `search_messages`, `get_message`, `get_attachments`, and `get_thread` all delegate to IMAP transparently when configured, with a clean cross-provider fallback story. Headline numbers: search drops 60s → 2.7s on a populous mailbox, the IMAP fast path is wired through every read tool, and a small per-account circuit breaker keeps offline / stale-credential bursts bounded. Setup is no longer a four-line raw-shell incantation — there's a real CLI with verification.

### Added

**Setup-IMAP CLI (#76):** New `apple-mail-mcp setup-imap --account <name>` subcommand replaces the raw `security add-generic-password` recipe. Prompts for the password via `getpass` (no shell history), writes the Keychain entry idempotently, opens an IMAP connection to verify the password actually works, rolls back on auth failure. `--uninstall` removes the entry. `--email` overrides the Mail.app-derived default for the rare alias case. Default invocation (no subcommand) still starts the MCP server, so Claude Desktop is unaffected. (#116)

**IMAP delegation for read tools:**
- `get_message` (#72): one-round-trip lookup via `SEARCH HEADER Message-ID` + `FETCH` when `account` and `mailbox` are supplied. Replaces the AppleScript account×mailbox scan (~6-18s worst case). New `headers_only` knob skips body fetch when only metadata is needed. Surfaced + fixed a latent identifier mismatch: AppleScript path uses Mail.app's internal numeric id; IMAP returns RFC 5322 Message-IDs. Callers who forward `account`+`mailbox` from `search_messages` now stay on the IMAP path consistently. (#117)
- `get_attachments` (#73): one BODYSTRUCTURE FETCH replaces the per-attachment property loop. Also surfaces three classes of attachment Mail.app's AppleScript silently drops: forwarded `message/rfc822` parts, multipart/related inline images with filenames, Unicode filenames. (#119)
- `get_thread` (#122): tiered, capability-detected dispatch. Tier 1 is Gmail's `X-GM-THRID` against `[Gmail]/All Mail` — ~5 round-trips, mailbox-count-independent (replaces ~1100 round-trips on a 91-label account when All Mail is exposed over IMAP). Falls through cleanly to the existing per-mailbox header-search BFS when the capability or `[Gmail]/All Mail` isn't available. (#126)

**IMAP connection pooling (#75):** New `ImapConnectionPool` class — opt-in via `APPLE_MAIL_MCP_IMAP_POOL=1` env var, default off. Caches IMAP sessions keyed by `(host, email)`, with idle-timeout reconnect (270s default), per-connection locking (thread-safety designed in even though FastMCP is single-threaded today), and invalidation on protocol/network errors. **Live measurement on a 5-call interactive workflow against iCloud: 10.6s → 6.3s, ~40% faster.** (#120)

**IMAP failure circuit breaker (#118):** Per-account 30s cooldown on `AppleMailConnector` after non-benign IMAP failures. Bursts of calls during offline / stale-credential conditions stop wasting round-trips on the same broken account. Specialized `LoginError` warning text now names the exact `setup-imap` command — surfaces silent IMAP degradation that the AppleScript fallback otherwise hides indefinitely. (#121)

**Benchmark suite (#31):** Captured baselines for search, attachment, bulk-ops, and pool scenarios. `make benchmark` / `make benchmark-baseline` Makefile targets. Skipped by default; opt-in via `--run-benchmark`. (#100)

**`get_selected_messages` tool (#11):** Returns the messages currently selected in Mail.app's UI. External contribution.

### Changed

**`search_messages` AppleScript fallback path is 22× faster on large mailboxes (#32).** Replaced `whose <filter>` server-side predicate (which forces full-mailbox materialization on Mail.app's side) with manual reverse-order iteration plus per-message IF filters. Live: 60s → 2.7s on a populous mailbox; new `search_messages_with_zero_matches` benchmark confirms full-scan worst case stays bounded. INFO-level log fires when AppleScript search exceeds 5s, pointing users at IMAP setup. **Observable behavior change**: results now return newest-first; previously oldest-first. Callers that relied on the old order should reverse the result list. (#114)

**Bulk operations cubic loop fix (#103).** `move_messages`, `flag_message`, `mark_as_read`, `delete_messages` accepted a new optional `source_mailbox` parameter that narrows the AppleScript scan from O(N × accounts × mailboxes) to O(N) when the caller knows where the messages are. (#112)

**`/merge-and-status` slash command** now surfaces untriaged contributor issues alongside contributor PRs. (#104, #105)

### Fixed

**`delete_messages(permanent=True)` was a silent no-op (#111).** Empirically probed Mail.app's AppleScript surface — confirmed there's no path to permanent-delete that bypasses Trash. Parameter now emits `DeprecationWarning` so callers see the gap; docs corrected to describe actual behavior; AppleScript path unchanged (still moves to Trash, recoverable). (#115)

**Dependency bumps:** fastmcp ≥3.2.4, pytest ≥9.0.3, ruff ≥0.15.12, mypy ≥1.20.2, pytest-cov ≥7.1.0. uv.lock regenerated. (#106-110, #113)

**`check_dependencies.sh`** now invokes pip-audit via `uv run` — fixes a CI-only failure mode. (#99)

### Documentation

**Research: IMAP thread-discovery strategies** (`docs/research/imap-thread-strategies.md`, #80, #124). Empirical capability survey of iCloud, Gmail, and documented-from-public-docs Fastmail/Dovecot. Discovered iCloud doesn't advertise THREAD (contradicting public claims), Gmail doesn't advertise THREAD on the live account, and Gmail's `[Gmail]/All Mail` is opt-in over IMAP. Recommended a tiered, capability-detected dispatch — Tier 1 (X-GM-THRID) shipped in #122; Tier 2 (RFC 5256 THREAD) tracked as #123, Tier 1.5 (per-mailbox X-GM-THRID for hidden All Mail) tracked as #125. Both deferred to v0.7.0.

**README** rewritten to reflect the new IMAP setup flow (single CLI command instead of raw `security` recipe).

### Tooling

- `pyproject.toml` `version = "0.6.0"`
- `__init__.py` `__version__ = "0.6.0"`

## [0.5.0] - 2026-04-26

Major minor release. Fifteen new MCP tools across four feature areas (account discovery, rule management, email templates, IMAP-backed performance), several long-standing AppleScript-injection bugs closed, and contributor-experience tightening prompted by an honest look at how earlier external PRs got handled. The README, CONTRIBUTING.md, and `.github/PULL_REQUEST_TEMPLATE.md` were all reworked to make the project safer and more welcoming to contribute to.

### Added

**Rule CRUD (#63):** `set_rule_enabled`, `create_rule`, `update_rule`, `delete_rule`. Addresses rules by 1-based positional index (rules have no stable id in Mail.app's AppleScript interface). Medium-tier schema: 6 condition fields × 5 operators, AND/OR match logic, 7 actions. Full-replacement semantics for `actions`; condition-replacement is refused with a typed error due to a recursion bug in Mail.app on macOS Tahoe (`-[MFMessageRule(Applescript) removeFromCriteriaAtIndex:]`) that crashes Mail on any condition-deletion path. (#84)

**Email templates (#30):** `list_templates`, `get_template`, `save_template`, `delete_template`, `render_template`. File-per-template storage at `~/.apple_mail_mcp/templates/<name>.md` (overridable via `APPLE_MAIL_MCP_HOME`). Simple `{placeholder}` substitution with reply-context auto-fills (`recipient_name`, `recipient_email`, `original_subject`, `today`). Render-only API — caller passes the result to existing `reply_to_message`/`forward_message`/`send_email`. First persistent-state feature in the project; the `~/.apple_mail_mcp/` convention is documented in CLAUDE.md. (#85)

**Discovery & threads:**
- `list_accounts` returns each account's id (UUID), display name, email addresses, type, and enabled state (#62, closes #26)
- `list_rules` lists Mail.app rules with index, name, and enabled state (#64, closes #27)
- `get_thread` reconstructs conversations using IMAP THREAD when available, falling back to AppleScript header-based reconstruction (#67, #81; closes #29 and #66)
- `search_messages` gains 4 new filters: `is_flagged`, `date_from`, `date_to`, `has_attachment` (#65, closes #28)

**IMAP-backed performance:**
- New `imap_connector.py` and `keychain.py` modules. When a Keychain entry exists for an account, search and thread tools transparently use IMAP for server-side execution (~1s vs 1-5s); on any IMAP failure they silently fall back to AppleScript with no functional loss. (#78, #79; closes #40 and #41)
- IMAP graceful-degradation invariants documented (#71)
- IMAP auth path decision documented after Keychain-spike findings (#69, #70; closes #39 and #68)

**Account-id (UUID) acceptance:** Account-gated tools now accept either the display name or the stable account UUID (returned by `list_accounts`). Names remain valid for convenience; UUIDs survive renames. (#82, closes #61)

**Documentation & contributor experience:**
- `docs/guides/SECURITY_CHECKLIST.md` unifies security guidance previously scattered across CLAUDE.md (#93, closes #87)
- CONTRIBUTING.md adds an acknowledgment to early contributors whose PRs were closed without comment, plus issue-first workflow guidance and granular test requirements (#93, closes #87)
- PR template surfaces linked-issue and tests-added checks as explicit fields (#95, closes #88)
- README adds a pre-1.0 warning recommending version pinning (#96, closes #89)
- Tools count in README and CLAUDE.md brought current (14 → 26)

**Tooling:**
- `/merge-and-status` slash command now surfaces open PRs from external contributors so they don't sit unreviewed (#94, closes #90)

### Fixed

- **AppleScript injection in 6 connector methods.** `mark_as_read`, `move_messages`, `flag_message`, `delete_messages`, `reply_to_message`, and `forward_message` interpolated raw message IDs into AppleScript without escaping. Each ID is now individually sanitized + escaped + quoted. Original report by [@martparve](https://github.com/martparve) in #34, with regression test guards added in this release.
- **Crashes on UUID-style message IDs.** `get_message`, `get_attachments`, `_resolve_thread_anchor_applescript`, and `save_attachments` interpolated escaped IDs without surrounding quotes; AppleScript then parsed dashes/dots/`@` in iCloud-format IDs as syntax tokens and errored. Wrapped the escaped value in literal quotes everywhere. (#34, closes #86)
- Pyright false positives for `imapclient` calls (#83)

### Changed

- GitHub Actions: `actions/checkout` 4 → 6, `astral-sh/setup-uv` 6 → 7 (#13, #14)
- Coverage now 92% (was 95% in v0.4.1); new connector and template code accounts for the small drop. Floor remains 90%.

## [0.4.1] - 2026-04-19

Patch release: dep hygiene and v0.4.0 follow-ups. Four connector bugs that unit tests couldn't catch were surfaced by running the three new integration tests against real Mail.app.

### Added
- Integration tests for `list_accounts`, `get_message`, and `get_attachments` against real Mail.app, fulfilling the #23 design doc commitment (#57)

### Changed
- Bumped transitive deps to clear `pip-audit` findings from the v0.4.0 release: `authlib` 1.6.9 → 1.7.0, `cryptography` 46.0.6 → 46.0.7, `pytest` 9.0.2 → 9.0.3, `python-multipart` 0.0.22 → 0.0.26. `fastmcp`/`mcp`/`pydantic`/`starlette`/`uvicorn` unchanged (#57)

### Fixed
- `search_messages` with no filter conditions emitted `messages of mailboxRef whose true` — Mail rejected with error -1726. The `whose` clause is now dropped entirely when no filters are supplied (#57)
- `search_messages` with a `limit` emitted `items 1 thru N of (messages of mailboxRef …)` — Mail rejected with error -1728. Replaced with a `count of` + indexed `item i of` repeat loop (#57)
- `_run_applescript` error-substring matcher checked for straight-apostrophe `Can't`, but macOS stderr uses curly `Can’t`. `MailAccountNotFoundError` and `MailMailboxNotFoundError` were silently degraded to generic errors. Curly apostrophes are now normalized before dispatch (#57)
- Several AppleScript record keys (`subject`, `sender`, `content`, `date_received`, `read_status`, `flagged`, `mime_type`, `downloaded`, `email_addresses`, `unread_count`) were silently dropped by NSJSONSerialization when values came from live Mail objects. Extended the prior `|name|` / `|id|` / `|size|` quoting to **every** record key across all 5 JSON-emitting methods (#57)

## [0.4.0] - 2026-04-19

Quality and infrastructure milestone. No new MCP tools; focus on test coverage, safety, and parsing robustness.

### Added
- Test-mode safety system (`MAIL_TEST_MODE`, `MAIL_TEST_ACCOUNT`) — account-gated destructive operations are constrained to a designated test account and sends are constrained to RFC 2606 reserved domains (#19)
- Three-tier sliding-window rate limiting (general / send / expensive) replacing the previous stub (#17)
- Proper MCP elicitation for destructive operation confirmation, replacing the previous stub (#18)
- Unit tests for all 14 `server.py` MCP tool handlers, lifting coverage from 0 % to 99 % (#16)
- E2E tests exercising FastMCP tool registration, schema, and invocation — 20 in-process tests covering all 14 tools (#21)
- stdio subprocess smoke test verifying the real MCP transport layer (#50)
- Blind-agent eval framework under `evals/agent_tool_usability/` — 36 scenarios across 9 categories, runnable against any OpenRouter-accessible model (#22)
- `docs/guides/COMPLEXITY.md` — rationale and exception table for the CC ≤ 20 ceiling (#24)
- IMAP hybrid-approach research document (#15)

### Changed
- AppleScript output now emits JSON via ASObjC + `NSJSONSerialization` instead of the fragile pipe-delimited format that broke silently when any field contained `|` (#23). Finishes previously-placeholder `list_accounts` and `list_mailboxes` return shapes.
- Coverage threshold raised from 60 % to 90 % in both `pyproject.toml` and CI, matching the documented target (#20)
- Pre-commit hook now enforces version sync across `pyproject.toml`, `__init__.py`, and `.claude/CLAUDE.md` — failures block the commit locally instead of surfacing later in CI (#25)

### Fixed
- Three `NSJSONSerialization` selector-collision bugs discovered during the JSON-output migration's integration smoke: `name`, `id`, and `size` AppleScript record keys were silently dropped and are now quoted as `|name|`, `|id|`, `|size|` (#23)

## [0.3.0] - 2025-10-11

Phase 3: Smart reply and forward.

### Added
- `reply_to_message` tool with reply-all support
- `forward_message` tool with CC/BCC support
- Reply/forward security tests (body sanitization, special character escaping)

## [0.2.0] - 2025-10-11

Phase 2: Message management and attachments.

### Added
- `send_email_with_attachments` tool
- `get_attachments` tool
- `save_attachments` tool with directory validation
- `move_messages` tool with Gmail label-based workaround (`gmail_mode`)
- `flag_message` tool with color support
- `create_mailbox` tool with parent mailbox support
- `delete_messages` tool with permanent delete option
- Attachment security validation (type blocklist, size limits, filename sanitization)
- Bulk operation validation (max 100 items)

## [0.1.0] - 2025-10-11

Initial release. Phase 1: Core mail operations.

### Added
- `list_mailboxes` tool
- `search_messages` tool with sender/subject/read-status filters
- `get_message` tool with optional content inclusion
- `send_email` tool with CC/BCC support
- `mark_as_read` tool with bulk support
- AppleScript-based Mail.app integration via subprocess
- Custom exception hierarchy for Mail errors
- Input sanitization and AppleScript string escaping
- Security module with operation logging and validation
- Unit test suite with mocked AppleScript
- Integration test framework (opt-in via `--run-integration`)
