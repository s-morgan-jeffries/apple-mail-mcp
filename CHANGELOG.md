# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.2] - 2026-06-13

A bug-fix patch release for four reliability and data-integrity issues surfaced from real Claude Desktop usage on Gmail and iCloud: a full-body read that could crash the whole server, a Gmail label move that silently trashed the message, an IMAP search that silently dropped matching results, and an attachment save that hung for minutes on Gmail. No new tools (still 24).

### Added

**`save_attachments` IMAP fast path (#371):** `save_attachments` gains optional `account` + `mailbox` parameters. When supplied, the message is fetched once over IMAP and its attachment bytes are written straight to disk — avoiding the O(accounts × mailboxes) AppleScript cross-scan whose unindexed `whose message id` lookup (~20s/mailbox) ran for minutes and timed out on Gmail's many labels. Mirrors `get_attachment_content`'s fast path; without the parameters the AppleScript fallback is unchanged.

**Truncation signal on `get_messages` (#365):** a bounded body carries `content_truncated: true` and `content_original_bytes: <int>` so callers can tell when a body was clipped.

### Changed

**`get_messages` bounds and scrubs message bodies (#365):** each `content` is now capped at 1 MB of UTF-8 text (override via `APPLE_MAIL_MCP_MAX_BODY_BYTES`) and stripped of transport-hostile characters before it leaves the tool, so a single large or non-UTF8-encodable body can't blow the JSON-RPC frame.

### Deprecated

**`gmail_mode` on `update_message` (#364):** the parameter is now accepted but ignored — the move strategy is chosen automatically (IMAP relabel when configured, otherwise a verified AppleScript move). Removal is tracked for v1.0 in #369.

### Fixed

**`get_messages` full-body fetch could crash the entire server (#365):** retrieving full bodies on iCloud returned `-32000: Connection closed` and took the whole stdio server down (every tool unavailable until relaunch). An unbounded, un-scrubbed body produced a JSON-RPC frame the client rejected — outside the tool's `try/except`. Bodies are now bounded and scrubbed at the single resolve chokepoint (covers the IMAP, AppleScript, and `SELECTED` paths).

**Gmail INBOX→label move silently trashed the message (#364):** `update_message` with `gmail_mode=true` ran an AppleScript copy+delete; on Gmail `delete` always routes to Trash and Trash strips all other labels, so the destination label was lost and the message landed in `[Gmail]/Trash` only — while the tool still reported success. Moves now use a relabel (`set mailbox`) and verify the message left the source; an unconfirmed move fails loud with `error_type: "imap_required"` instead of losing mail.

**IMAP `search_messages` `limit` bounded the candidate window, not the matches (#366, #368):** with `has_attachment` set, `limit` was applied before the post-FETCH filter, so a limited search silently missed attachment-bearing messages (observed live: 2 results at `limit=5` vs 6 at `limit=20` on the same mailbox). The IMAP path now walks candidates newest-first in bounded chunks and short-circuits once `limit` *matching* rows are found. (Contributed by @jason21wc.)

**`save_attachments` hung and timed out on Gmail (#371):** see the IMAP fast path under Added — the cross-scan that caused the hang is now bypassed when `account`+`mailbox` are supplied.

## [0.10.1] - 2026-06-08

A maintenance release. The one user-facing fix lets an iCloud account whose Apple ID is a third-party address (e.g. a Gmail sign-in) resolve its IMAP login. The rest is release-engineering hardening: a gate that makes the Phase 8.5 derived-artifact refresh impossible to skip silently — the gap that shipped a stale eval snapshot at v0.10.0 — and a more robust blind-eval model list that tracks each family's latest model and fails loud on retired ids.

### Added

**Release-artifact freshness gate (#356):** [`scripts/check_release_artifacts.sh`](scripts/check_release_artifacts.sh) fails the release validation when a derived artifact (`tests/benchmarks/baseline.json`, `evals/agent_tool_usability/results/scored_results.md`) isn't stamped for the release being cut — unless an explicit, issue-tracked waiver is recorded in [`release_artifact_waivers.txt`](release_artifact_waivers.txt). The v0.10.0 release silently shipped an eval snapshot stamped `v0.9.0` because Phase 8.5 was skippable without a check; this closes that gap. See [docs/guides/RELEASE_ARTIFACTS.md](docs/guides/RELEASE_ARTIFACTS.md).

### Changed

**Blind-eval model list tracks latest-per-family and fails loud on retired ids (#358):** `make eval-tools` had pinned `mistralai/mistral-large-2411`, which OpenRouter retired — its calls 404'd and produced a silent zero-token row. The model list now uses each family's latest non-dated slug where one exists (`mistralai/mistral-large`, `deepseek/deepseek-chat`), each run records the exact version OpenRouter served (`resolved_model`), and `run_eval` pre-checks model availability against the catalog, exiting before any credits are spent if a requested id is missing.

### Fixed

**iCloud IMAP login resolution for third-party Apple IDs (#341):** `_resolve_imap_config` couldn't determine the login for an iCloud/MobileMe account whose Apple ID is a non-iCloud address (e.g. a Gmail sign-in) and whose AppleScript `email addresses` list is empty — the #299 apple-alias rule had nothing to resolve from, so the connection failed. A persisted per-account login override (`~/.apple_mail_mcp/imap_login_overrides.json`, set via the IMAP setup CLI) is now consulted first, letting these accounts connect.

## [0.10.0] - 2026-06-05

First release under the new name **`apple-mail-fast-mcp`** (#335) — the PyPI distribution, CLI command, and repo were renamed, and publishing now goes through PyPI OIDC trusted publishing. Feature-wise the theme is **richer composition and inspection**: drafts can carry an HTML body, a new tool reads an attachment's content inline without writing to disk, and IMAP credentials can come from an environment variable for uvx/headless/CI contexts. Alongside: a confirmation-prompt fix for current FastMCP, and CI/release hardening so parity drift and dependency advisories can't slip through (or hard-block a release) unnoticed.

> **Renaming note:** install as `apple-mail-fast-mcp` (e.g. `pip install apple-mail-fast-mcp` / `uvx apple-mail-fast-mcp`) and invoke the CLI as `apple-mail-fast-mcp`. The Python import package remains `apple_mail_mcp` for now, and Keychain entries remain under the `apple-mail-mcp.imap.<account>` prefix (a brand migration is tracked in #336/#337).

### Added

**HTML body support for `create_draft` / `update_draft` (#251):** a new optional `body_html` parameter builds a `multipart/alternative` draft (HTML + a plain-text alternative, derived from the HTML when no plain `body` is given) over the clean IMAP-APPEND path. Limited to fresh save-as-draft and requires IMAP credentials; combining it with `send_now` or reply/forward is rejected, and if the IMAP path can't engage the call fails (`html_requires_imap`) rather than silently downgrading to plain text. HTML is caller-trusted (not sanitized).

**`get_attachment_content` tool (#250):** read a single attachment's content inline — UTF-8 text for text-like types (`text/*`, `application/json`, …) or base64 otherwise — without writing to disk, for triage workflows. 0-based `attachment_index` matching `get_attachments` / `get_messages(include_attachments=True)`; ~25 MB inline cap (override via `APPLE_MAIL_MCP_MAX_INLINE_ATTACHMENT_BYTES`), over which it returns `attachment_too_large` and points at `save_attachments`.

**Environment-variable fallback for the IMAP password (#248):** `APPLE_MAIL_MCP_IMAP_PASSWORD_<ACCOUNT>` (account name uppercased, non-alphanumerics → `_`) supplies the IMAP password where the macOS Keychain isn't usable (uvx, Docker/CI, headless). Checked before the Keychain and composes with the name↔UUID dual-form lookup (#243). Env vars are less private than the Keychain — documented as uvx/CI-only.

**Weekly dependency-advisory workflow (#296):** a scheduled CI job (`dependency-audit.yml`) runs `pip-audit` and opens/updates (and auto-closes) a tracking issue when advisories appear, so freshly-disclosed CVEs on unchanged pins are discovered continuously and bumped on their own PRs — not mid-release.

### Changed

**Confirmation prompts pass an explicit `response_type` to `ctx.elicit` (#282):** the destructive-action confirmation gate now uses a boolean `response_type` (clearing FastMCP ≥3.3.1's `FastMCPDeprecationWarning` and the empty-form rendering bug in some clients, e.g. VS Code). Only an explicit affirmative proceeds; decline, cancel, accept-with-false, missing context, and elicit errors all block (fail-closed).

**`update_rule` confirmation is dangerous-action-aware (#280):** updating a rule only elicits confirmation when the change touches conditions/match-logic or introduces a destructive action (delete/forward/move/copy), mirroring `create_rule` (#222) — purely organizational tweaks no longer prompt.

**Clean drafts adopt the sole enabled account + warn on fallback (#321, #270):** with no explicit `from_account`, a single enabled account is adopted so the clean (no iOS cite-blockquote) IMAP-APPEND draft path can engage; save-as-draft calls that fall back to the AppleScript path now surface a warning.

**Release dependency gate split: direct vs transitive (#296):** `check_dependencies.sh` now hard-fails only on advisories in direct deps (`fastmcp`/`imapclient`); transitive advisories are warnings (handled by the scheduled workflow above), so a freshly-disclosed transitive CVE no longer hard-blocks an otherwise-ready release.

**Client/server parity check is now blocking (#277):** `check_client_server_parity.sh` fails CI when a public connector method is neither exposed as a tool nor in an intentionally-internal allowlist (and on stale allowlist entries), instead of always passing.

**Faster bulk IMAP id resolution (#316):** `update_message`'s RFC Message-ID→UID resolution now batches into a chunked `OR` SEARCH instead of per-id lookups (~6× faster bulk moves on the IMAP path).

### Fixed

**Drafts surface promptly after IMAP-APPEND (#269):** the account is synchronized after an IMAP-APPEND draft so it appears in Mail.app's local Drafts pane without waiting for Mail's background poll.

**Stricter name / draft_id validation (#325):** the forbidden-character set is now enforced in the name and `draft_id` validators.

**`check_readme_claims.sh` tool-count under the `@_tool` wrapper (#346):** the doc-claims check counted bare `@mcp.tool()` (always 0 since the `@_tool` wrapper, #217); it now counts the wrapper, and CLAUDE.md's test counts were refreshed.

**Env-var IMAP password whitespace stripped (#349):** the `APPLE_MAIL_MCP_IMAP_PASSWORD_<ACCOUNT>` value is now stripped of surrounding whitespace, so a trailing newline from a `.env` file / Docker / `export` no longer breaks IMAP login (mirrors the Keychain path's behavior).

**Reply/forward draft folder-miss no longer trips the IMAP circuit breaker (#350):** when the seed message isn't in the guessed `seed_mailbox` the call falls back to AppleScript (which resolves across all folders) without opening the 30s breaker, so a normal reply to filed mail no longer degrades subsequent IMAP reads for the account.

### Docs

**Issue-claiming convention for non-collaborators (#327):** corrected the contributing docs for how non-collaborators claim issues.

## [0.9.1] - 2026-06-03

Patch release. The theme is **IMAP-path correctness and interop robustness**: several contributor-relevant crashes and login edge cases on the IMAP fast path are fixed (concurrent-mutation FETCH, iCloud login resolution, split connect/operation timeouts), tool parameters arriving as stringified JSON from clients like Cowork are now coerced, and reply/forward drafts no longer render with an iOS cite-blockquote wrapper. Alongside the fixes: a new warn-only prompt-injection detector on read responses, an automated doc/artifact drift gate wired into CI and the release flow, and test-suite hardening (Hypothesis property tests on the escape/sanitize boundary, plus a fix for the unit suite leaking to real `osascript`).

### Added

**Warn-only prompt-injection detection on read responses (#225):** `get_message` (and other read paths) now scan returned message content for prompt-injection patterns and attach a non-blocking warning when matches are found. This is detection-only — content is never altered or withheld — giving clients a signal without changing behavior. First slice of the broader #225 planning issue.

### Changed

**Automated doc/artifact drift gate + release refresh hooks (#288):** A new `scripts/check_docs.sh` gate fails CI on tool-set coverage gaps, references to removed names, broken cross-references, and eval-description drift. The release workflow gains a mandatory artifact-refresh phase so derived snapshots (eval descriptions, benchmark baseline, blind-eval scores) can't silently rot between releases.

**Bulk-mutation benchmarks captured via a self-seeding source (#287):** The previously-skipped IMAP bulk-mutation benchmarks now run against a self-seeding source account, closing the perf-coverage gap that needed a 50+-message mailbox.

### Fixed

**IMAP `search_messages` / `get_message` survive a message vanishing mid-FETCH (#314):** Concurrent mailbox mutation could leave a FETCH response missing its `ENVELOPE`, raising `KeyError: ENVELOPE`. Both paths now tolerate a message disappearing between the search and the fetch.

**Tool parameters coerced from stringified arrays/dicts (#309):** `create_draft`'s `to` / `cc` / `bcc` (and other list/dict params) failed when a client such as Cowork serialized them as JSON strings instead of arrays. Parameters are now coerced before use, restoring interop.

**iCloud IMAP login resolves to the `@icloud.com` address (#299):** `_resolve_imap_config` could pick a third-party Apple ID as the IMAP login, producing `AUTHENTICATIONFAILED` against iCloud (the inverse of #201). Login now resolves to the `@icloud.com` address.

**Split IMAP connect vs operation timeouts (#249):** A single timeout covered both connect and operations; these are now split (3s connect, 30s operation) so a slow connect can't consume the operation budget and a slow operation isn't capped at the connect timeout.

**Reply/forward drafts written via IMAP APPEND (#292, #245 follow-up):** Saving a reply/forward as a draft still rendered as an iOS cite-blockquote; these drafts are now written via IMAP APPEND, bypassing Mail.app's compose mangling (extending the #245 fix to the reply/forward paths).

**`update_message` matches RFC 5322 Message-ID (#291):** Message lookup now matches the RFC 5322 `Message-ID` in addition to Mail's numeric id, so callers holding an RFC id can target a message directly.

**`draft_id` interpolation hardened + RFC ids resolved in extract (#294):** Defense-in-depth escaping of `draft_id` and resolution of RFC Message-ID draft ids in the extract path, closing the gap tracked from the v0.9.0 release review.

**Unit suite no longer leaks to real `osascript` (#298):** IMAP error-path unit tests were stalling ~30s each on a real socket timeout, pushing CI to ~5min; the suite is now fully isolated from real `osascript`, dropping unit-test time back to seconds.

**`/merge-and-status` robustness (#253, #268):** The status catch-net is now resilient to transient empty results (#253), and milestone selection uses a version-aware sort instead of an alphabetic one that picked v0.10.0 over v0.9.0 (#268).

### Security

**`draft_id` defense-in-depth (#294):** See Fixed — the `draft_id` interpolation hardening also closes the latent injection surface tracked from the v0.9.0 release review, even though `_validate_draft_id`'s regex already made it injection-safe.

### Tests

**Hypothesis property tests on the escape/sanitize/validate boundary (#214):** Property-based tests now fuzz the `sanitize_input` → `escape_applescript_string` → validation boundary, exercising input shapes the example-based suite didn't cover.

### Dependencies

**Consolidated dependency bump + pyjwt security fix (#235):** A consolidated transitive-dependency bump that also clears the pyjwt PYSEC-2025-183 advisory now that the fastmcp/mcp range ships a fixed version.

## [0.9.0] - 2026-06-01

Minor release. The theme is **hardening the destructive-operation surface and the IMAP fast path**: explicit user-confirmation gates now front the remaining unguarded deletes and rule mutations, `save_attachments` is bounded against disk-fill, a contributor-reported IMAP CRLF command-injection vector is closed, and a STRIDE threat model now documents the trust boundaries those defenses sit on. Alongside the security work: an opt-in read-only server mode, a new recency search filter, several IMAP correctness fixes (three of them contributor-authored), and a full documentation reconciliation to the current 23-tool surface.

Thanks to external contributors [@fmasi](https://github.com/fmasi), [@allenpan05](https://github.com/allenpan05), and [@jason21wc](https://github.com/jason21wc) for the fixes credited below.

### Added

**Read-only server mode + MCP tool annotations (#217):** Tools now carry MCP annotations (`readOnlyHint` / `destructiveHint` / `idempotentHint`) so clients can reason about each tool's effect before calling it. A new `--read-only` flag starts a split server that exposes only the non-mutating tools — a least-privilege deployment for "let the model read my mail but never change it."

**`received_within_hours` search filter (#230):** `search_messages` gains a relative-recency filter (e.g. "messages in the last 6 hours") that compiles to an AppleScript date comparison server-side, avoiding a fetch-all-then-filter pass.

### Changed

**Destructive operations now require confirmation (#239, #222):** `delete_messages` and `create_rule` (when the rule carries a dangerous action — `delete` / `forward_to` / `move_to` / `copy_to`) now route through the `_elicit_confirmation` gate, joining the deletes and rule/draft mutations already gated. As with the existing gates, the flow fails closed: an MCP client that can't elicit gets a typed `confirmation_required` error rather than a silent execution.

**`save_attachments` byte caps (#236):** Attachment saves are now bounded by per-file and total byte caps (a pre-write check plus a post-write net), defending against a malicious/oversized-attachment disk-fill DoS. The tool return now includes a `rejected` list naming any attachments skipped for exceeding a cap. Caps are configurable via `APPLE_MAIL_MCP_MAX_ATTACHMENT_BYTES` / `APPLE_MAIL_MCP_MAX_TOTAL_ATTACHMENT_BYTES`.

**Complexity and type-check CI gates are now blocking (#274):** The cyclomatic-complexity gate (CC ≤ 20) and `mypy --strict` step in PR CI lost their `continue-on-error` — a complexity or type regression now fails the build instead of being advisory. (A CC-25 regression had previously merged undetected because the gate was non-blocking.)

### Fixed

**IMAP multipart attachment enumeration drops attachments (#266, by @jason21wc):** The IMAP BODYSTRUCTURE walk failed to enumerate attachments on some multipart message shapes, so `save_attachments` / attachment listing could under-report. Fixed alongside an `rfc822`-consistency cleanup.

**`_resolve_imap_config` raises `KeyError` on accounts with no IMAP server (#267, by @jason21wc):** Accounts lacking an IMAP server property (e.g. some local/POP setups) raised `KeyError` instead of degrading gracefully; the resolver now coerces the missing value and falls back cleanly.

**Compose drafts created via IMAP APPEND (#245, fix #246 by @fmasi):** Creating a compose draft through Mail.app introduced an unwanted cite-blockquote wrapper. Drafts are now written via IMAP APPEND, bypassing Mail.app's compose-window mangling.

**Connector timeout threaded into non-JSON AppleScript paths (#233):** The `with timeout` protection added in v0.8.2 covered only `_wrap_as_json_script` call sites; the direct-AppleScript mutation paths (`mark_as_read`, `move_messages`, `update_message`, …) now also honor the configured connector timeout, closing the gap that entry tracked.

**AppleScript mailbox resolver — nested paths + Gmail custom labels (#247):** The mailbox-name resolver mishandled nested mailbox paths and Gmail custom labels; resolution now walks the hierarchy correctly.

**`search_messages` iteration order + date-literal bug (#242):** AppleScript message iteration is now reversed to return newest-first as documented, and a date-literal construction bug in the filter path is fixed.

**IMAP Keychain lookup tries both name and UUID forms (#243):** `setup-imap` may key the Keychain entry under either the account display name or its UUID; runtime lookup now tries both forms before falling back to AppleScript.

**Stale e2e elicitation harness repaired (#257):** The `delete_mailbox` / `delete_messages` e2e tests had drifted out of sync with the elicitation gates and were silently failing; repaired, and the manual-e2e policy (CI excludes e2e; `make test-e2e` is a mandatory pre-release gate) is now documented.

### Security

**IMAP CRLF command injection + attachment path traversal (#254, by @allenpan05):** Closed a CRLF-injection vector where crafted input could smuggle additional IMAP protocol commands across a line boundary, plus attachment-path traversal cases. Reported and fixed by an external contributor.

**Removed latent AppleScript-injection vector (#258):** Deleted the unused `parse_date_filter` helper, which built AppleScript from input without the standard escape path — dead code, but a live injection vector if ever wired up.

**Threat model documented (#213):** Added `docs/guides/THREAT_MODEL.md` — a STRIDE pass across the five trust boundaries (MCP client, server process, AppleScript bridge, IMAP, on-disk user data) — and cross-linked it from the per-feature security checklist.

**Release-review hardening:** The release-gate code review surfaced three pre-existing gaps (none regressions), two fixed here: (1) `create_draft`'s recipient lists now pass through the mandated `sanitize_input` → `escape_applescript_string` two-step before AppleScript interpolation, matching every other interpolation site (previously only `escape_applescript_string` was applied, so a null byte in a recipient address would reach the generated script); (2) `delete_messages` is now an account-gated operation and calls `check_test_mode_safety` when an `account` is supplied — in `MAIL_TEST_MODE` a delete aimed at a non-test account is now rejected before the connector is touched — and its success path now records to the audit trail like every other mutating tool. The third (defense-in-depth escaping of `draft_id`, which `_validate_draft_id`'s regex already makes injection-safe) is tracked as #294.

### Dependencies

Bumped three transitive dependencies to clear newly-disclosed advisories (the pins were unchanged since v0.8.2; only the advisories are new): `idna` 3.11 → 3.17, `starlette` 1.0.0 → 1.2.1, `urllib3` 2.6.3 → 2.7.0.

### Docs

**Documentation reconciled to the current surface (#220):** Major refresh of README, `ARCHITECTURE.md` (now documents the AppleScript-default + IMAP-fast-path dispatch model, dual-emit IDs, drafts lifecycle, and thread tiers), `TOOLS.md` (corrected to 23 tools, stale `get_message` examples fixed), `DEVELOPMENT.md` (rewritten as a dev-workflow guide), `APPLESCRIPT_GOTCHAS.md` (JSON-via-ASObjC patterns), `TESTING.md`, and `SECURITY.md` (reconciled with the threat model). Refreshed the blind-agent-eval baseline (#219) and the performance benchmark baseline (#216), and added the claim-by-comment convention to `CONTRIBUTING.md` (#259).

## [0.8.2] - 2026-05-20

Patch release. Three substantive bug fixes — one security regression in our own gate chain, one regression introduced at v0.8.1, and one long-latent AppleEvent timeout bug surfaced by use on slow Exchange/EWS accounts. Two of the three are contributor-authored: [@fmasi](https://github.com/fmasi) reported and fixed the v0.8.1 regression they noticed within hours of release; [@allenpan05](https://github.com/allenpan05) reported and fixed the AppleEvent timeout bug. Thanks to both.

### Fixed

**`_elicit_confirmation` fails closed on missing context / unsupported elicitation (#226):** The confirmation step in our destructive-operation gate chain had two silent-pass paths that bypassed enforcement: when `ctx` was `None` (any MCP client that didn't pass a context) and when `ctx.elicit(...)` raised (clients that don't implement the elicitation capability). Both paths returned `None`, which downstream callers interpret as "approved." Result: every gated tool that wires through the helper — `delete_rule`, `update_rule`, `delete_mailbox`, `delete_template`, plus `create_draft` / `update_draft` with `send_now=True` (via `_run_send_now_gates`) — could be invoked without confirmation from any MCP client that doesn't implement elicitation. The safety and rate-limit gates still fired, but the explicit "user confirmed this action" gate was gone.

Both bypass paths now return a typed `confirmation_required` error distinct from the existing `cancelled` (user-declined) error — MCP clients can give different UX for "user said no" vs "couldn't ask user." Both paths now also log to the audit trail with distinct statuses (`confirmation_required` for missing ctx, `confirmation_unavailable` for elicit-raise) so operators can spot bypass attempts. The `cancelled` error message changes from "User declined to send" to "User declined to continue" — the helper is used for deletes and rule mutations too, not just sends.

Surfaced via a contributor's fork patch surveyed during the v0.8.1 post-mortem.

**`find_message_by_message_id` regression introduced at v0.8.1 (#231, fix #232 by @fmasi):** v0.8.1's PR #208 added bracket-wrapping to `find_message_by_message_id` based on the (unverified) assumption that Mail.app stores `message id` with brackets per RFC 5322. fmasi sampled 81 message ids across 27 mailboxes on two accounts (iCloud + Gmail) and found **zero** stored bracketed — IMAP servers strip outer brackets per RFC 3501 when returning Message-ID via FETCH ENVELOPE, and Mail.app stores whatever IMAP gave it. The v0.8.1 wrap meant `create_draft(reply_to=<bare RFC id>)` and `create_draft(forward_of=...)` were silently failing with `MailMessageNotFoundError` on IMAP-backed accounts — the original #205 failure mode, just routed through the helper.

Fix uses a compound AppleScript clause that queries both forms in one round-trip: `whose (message id is "X" or message id is "<X>")`. Strips brackets from input first so the canonical bare form drives both arms. Robust to whatever Mail.app actually stored on any account type. PR #232 also adds three integration tests against real Mail.app — the safety net that CLAUDE.md's "if you touched AppleScript, write integration tests" rule was asking for. PR #208's unit tests passed because they asserted the generated AppleScript string but never verified Mail.app would actually match against it; the integration tests added here close that gap.

**AppleEvent timeout bypass on AppleScript paths (#227, fix #228 by @allenpan05):** AppleScript scripts emitted by `_wrap_as_json_script` lacked a `with timeout of N seconds` clause. As a result, Mail's default 60-second AppleEvent timeout fired before whatever subprocess timeout the connector was constructed with — `AppleMailConnector(timeout=N)` was effectively a no-op for any operation Mail couldn't complete in 60s. Once the AppleEvent timed out, the scripting bridge entered an unresponsive `Connection is invalid (-609)` state for ~30s. Hit reliably on Exchange/EWS accounts where per-message property fetches are server-bound rather than local.

`_wrap_as_json_script` now takes a required `timeout` keyword arg and wraps the body in `with timeout of {timeout} seconds ... end timeout`. All 12 call sites pass `self.timeout` so the in-script timeout matches the subprocess kill timer. Default-configured users (`AppleMailConnector(timeout=60)`) see no behavior change since that matches Mail's old default; users who passed higher timeouts now actually get them.

**Known gap:** ~10 other `_run_applescript` call sites (mutation paths like `mark_as_read`, `move_messages`, `update_message`) build their AppleScript directly without going through `_wrap_as_json_script` and therefore don't yet get the `with timeout` protection. These are typically small-batch operations that don't approach the 60s wall in practice, but the gap is tracked as #233 for v0.9.0.

## [0.8.1] - 2026-05-17

Patch release. Three user-facing bug fixes — two reported by external contributor [@fmasi](https://github.com/fmasi) with full reproductions — plus a refactor sweep that drops every remaining function below the CC 20 threshold (complexity allowlist now empty).

### Fixed

**`_resolve_imap_config` prefers Mail.app's `user name` for IMAP LOGIN; CLI keychain key matches runtime (#201):** `AppleMailConnector._resolve_imap_config` previously preferred `email_addresses[0]` over Mail.app's `user name` property when deriving the IMAP LOGIN. For iCloud accounts with a custom-domain Apple ID (Apple's "Custom Email Domain" / Hide-My-Email-style setup), `email_addresses[0]` is an SMTP-only From alias that iCloud's IMAP server rejects with `AUTHENTICATIONFAILED`, while `user name` (the Apple ID) is the credential the server actually accepts — and the one Mail.app itself sends. Flipped the preference to prefer `user name`, falling back to `email_addresses[0]` only when `user_name` is empty. The preference is invisible for most users (Gmail / Yahoo / icloud.com-primary accounts) because for those configurations `user name` and `email_addresses[0]` are the same value — the bug only surfaces when those two diverge, as they do for custom-domain Apple IDs.

The CLI was restructured alongside the connector fix so the keychain key written by `setup-imap` always matches what runtime uses: `cli.run_setup_imap` now resolves IMAP config upfront and uses the resolved email as the keychain key default. `--email` still overrides when explicitly provided (escape hatch for accounts where Mail.app's `user_name` is empty/wrong), but now wins for BOTH the keychain key AND the IMAP LOGIN — pre-fix the login silently switched back to the resolver's value at the last moment, which is what masked the bug originally.

**Upgrade note:** Existing users on custom-domain iCloud accounts will need to re-run `apple-mail-mcp setup-imap --account <name>` once after upgrading. Their old keychain entry (written under the SMTP alias) is unreadable by the new lookup path. IMAP fast paths gracefully fall back to AppleScript on a keychain miss, so the failure mode is "slow operations" rather than "broken operations" until they do.

Reported with reproduction steps and a verified local patch by [@fmasi](https://github.com/fmasi).

**`create_draft(reply_to=...)` accepts RFC 5322 Message-IDs from IMAP-path read tools (#205):** Since #148 (dual-emit message IDs), every read-tool row carries `id` and `rfc_message_id` — and on the IMAP path the two are deliberately equal (both are the bracketless RFC 5322 Message-ID). `create_draft(reply_to=<id>)` and `create_draft(forward_of=<id>)` used to fail with `message_not_found` because the AppleScript `whose id is "X"` clause matches Mail's internal numeric id only. Result: agents that read with the IMAP path and tried to round-trip into `create_draft` got silent failures on IMAP-backed rows (Gmail, iCloud, etc.). `get_messages` worked for the same id because it routes through the IMAP fast path with account+mailbox hints.

Fixed transparently in the connector: if `seed_id` contains `@` (the unambiguous RFC-vs-numeric discriminator — Mail's internal id is a stringified long integer), `create_draft` now resolves it to the internal id via the existing `find_message_by_message_id` resolver before building the AppleScript. Existing callers passing the internal numeric id keep working unchanged. The resolver itself is also bracket-tolerant now — wraps `<>` if missing — so callers can pass either form. Diagnosed by [@fmasi](https://github.com/fmasi) with the right primitive (`find_message_by_message_id`) identified.

**`delete_messages` resolves Trash before SELECT to avoid implicit-CLOSE on Exchange / old Dovecot (#199):** `ImapConnector.delete_messages` ran `client.select_folder(source_mailbox)` before calling `list_folders()` (twice in the convention-fallback path) to discover the Trash destination. RFC 3501 §6.3.8 permits LIST in either Authenticated or Selected state, but Exchange Online and some older Dovecot deployments issue an implicit CLOSE on certain LIST transitions, deselecting the mailbox and breaking the subsequent SEARCH with `"No mailbox selected"`. The failure was safe (orchestrator fell back to AppleScript) so users on affected servers saw degraded perf on delete operations rather than data loss. Reordered: capability check → trash resolve → SELECT → SEARCH → MOVE, with all LIST traffic finishing in AUTHENTICATED state before SELECT begins. Locked the contract with a regression test asserting `list_folders` precedes `select_folder` via `client.method_calls`. Surfaced by the feature-dev:code-reviewer agent during the v0.8.0 release review.

### Changed

**Complexity allowlist now empty — every function under CC 20 (#191–#195):** Five issues / four PRs (#194 and #195 were bundled into one) cleared the complexity allowlist that was carrying five documented exceptions at v0.8.0 release time. No user-facing behavior change; all refactors preserved 100% of existing test assertions.

- **`server.py::create_draft` (CC 36 → 19) — #191:** Extracted five helpers from the unified compose/reply/forward authoring loop: `_resolve_create_draft_seed`, `_maybe_apply_template`, `_validate_fresh_seed_fields`, `_run_send_now_gates`, `_persist_create_draft_seed`. `_run_send_now_gates` was designed for reuse by #192.
- **`server.py::update_draft` (CC 34 → 18) — #192:** Adopted `_run_send_now_gates` from #191 plus the renamed `_persist_draft_seed`; added two new helpers (`_resolve_update_subject_body` for the three-tier caller > template > state subject/body resolution, `_merge_draft_recipients` for the to/cc/bcc merge).
- **`mail_connector.py::AppleMailConnector.create_draft` (CC 25 → 13) — #193:** Extracted `_validate_create_draft_args`, `_build_attachment_block`, `_build_creation_block`. The reply and forward branches in `_build_creation_block` now share one template (verb selector) instead of two near-identical AppleScript blocks.
- **`imap_connector.py::_thread_via_xgm_per_mailbox` and `_thread_via_imap_thread` (both 21 → 16) — #194, #195:** Extracted one shared static helper `_merge_envelope_fetch_into` encapsulating the byte-identical envelope-merge loop at the end of each per-mailbox body. One PR (#210) closed both issues.

Net: the `scripts/check_complexity.sh` allowlist dict is now `{}`. Any new function above CC 20 will fail the gate without an explicit allowlist entry + documented exception in `docs/guides/COMPLEXITY.md`.

## [0.8.0] - 2026-05-13

Performance + correctness release. The headline arc is **IMAP fast paths for every single-field mutation** (#149 move / #150 delete / #151 read / #152 flag): when callers provide `account` + `source_mailbox`, the four mutation paths skip the AppleScript `whose message id is` linear scan (~57s on a 47k-message Gmail INBOX per #147's bench) and run server-side via `UID MOVE` / `UID STORE`. The dual-emit `rfc_message_id` field (#148) means callers naturally have an ID in the right form regardless of which path produced their input. Plus a clutch of bug fixes — a real `flag_color` map swap that's been silently giving callers the wrong colors (#185), a test-mode safety gap where implicit-reply sends could target real addresses (#175), a `from_account` sender path missing its sanitize_input wrap (#173), a fix to the v0.7.0-released release workflow itself (#177), and the complexity gate's silent-pass bug that was letting CC 21–40 functions slip through (#174). Net surface: no new tools, no breaking changes; the same 23 MCP tools just run much faster on the common path.

### Added

**Dual-emit `rfc_message_id` field on read-tool rows (#148):** Every row returned by `search_messages`, `get_messages`, and `get_thread` now carries an additional `rfc_message_id: str | null` field alongside the existing `id` field. The `id` field is still path-native (Mail.app internal numeric on the AppleScript path, RFC 5322 Message-ID on the IMAP path); `rfc_message_id` is always RFC 5322 (bracketless), or `null` when the message lacks a Message-ID header (drafts, malformed mail). Closes the loop on #147's original motivation: callers whose read happened to fall back to AppleScript (returning an internal numeric `id`) can now feed `rfc_message_id` to the IMAP fast paths from #149 / #150 / #151 / #152, triggering them automatically without having to know which path produced the row. AppleScript-path cost is sub-second per mailbox (per-row direct property read, confirmed in #147's bench); IMAP-path cost is zero (already extracted for `id`). `get_thread` previously emitted `rfc_message_id` to its tell script for graph-walking but stripped it before returning; now keeps it.

### Changed

**`AppleMailConnector.update_message` refactored below CC 20 (#174):** The IMAP fast-path additions in #149/#151/#152 each added a `_maybe_imap_*` call + if-check, drifting `update_message`'s cyclomatic complexity from 21 to 24. Extracted two helpers — `_try_imap_fast_paths` (composes the three single-field IMAP fast paths) and `_build_flag_actions` (translates the `flagged`/`flag_color` patch into AppleScript actions). Net CC: 24 → 17. No behavior change; all 20+ existing IMAP-delegation tests still pass.

### Fixed

**`ImapConnectionPool.close()` no longer races in-flight `session()` callers (#171):** `close()` previously snapshotted cached entries under the cache lock, then called `logout()` on each client without holding the per-entry lock. A thread inside a `session()` block would have its IMAP client logged out mid-operation. Latent today (FastMCP is single-threaded), but a real correctness hazard for any future threading and for the #127 atexit hook firing at interpreter shutdown alongside daemon threads. Fix: acquire each entry's lock before calling `logout()`, mirroring the pattern already used by `session()`'s invalidation path.

**Complexity gate now enforces CC ≤ 20 (#174):** `scripts/check_complexity.sh` was filtering radon's JSON output via `-n F`, which limits to functions with CC ≥ 41. Functions in the dangerous CC 21–40 range silently passed the gate. Switched the filter to `-n D` (CC ≥ 21) so the threshold is actually meaningful, and added a per-function allowlist with per-entry CC ceilings: documented exceptions stay green at their current levels, new code over CC 20 fails the gate, and any allowlisted function that creeps higher than its ceiling fails as a regression. Five long-standing exceptions are listed in the allowlist pending dedicated refactor PRs: `server.py::create_draft` (36), `server.py::update_draft` (34), `mail_connector.py::AppleMailConnector.create_draft` (25), and the two threading helpers `_thread_via_xgm_per_mailbox` and `_thread_via_imap_thread` (21 each). See [`docs/guides/COMPLEXITY.md`](docs/guides/COMPLEXITY.md) for the allowlist mechanism and ratchet semantics.


**`from_account` sender path now applies sanitize_input before escape_applescript_string (#173):** The security checklist's two-step convention for AppleScript interpolation (sanitize then escape) was missing from the sender clause in `create_draft` / `update_draft`. Low practical risk — the value comes from Mail.app's own account list, not direct user input — but the convention exists so we don't have to risk-assess each interpolation site individually. The Display-Name <email> format from #158 broadened what characters can appear here, making the convention more relevant. Includes a regression test asserting null bytes embedded in the resolver's output are stripped before reaching the AppleScript.

**Test-mode safety gap on implicit-reply `send_now` (#175):** In test mode (`MAIL_TEST_MODE=true`), `create_draft(reply_to=X, send_now=True)` and the analogous `update_draft` path bypassed the reserved-domain safety check when no explicit `to`/`cc`/`bcc` overrides were supplied — Mail.app derived recipients from the original message at send time, so the server's pre-flight gate (which only fired on non-empty recipient lists) was skipped. The gap let test-mode replies target real addresses without surfacing as safety violations. Fixed at two layers: server-tool wrappers now always call `check_test_mode_safety` on `send_now=True` (even with empty recipients), and `check_test_mode_safety` itself now treats empty/None recipients on a `SEND_OPERATIONS` call in test mode as a `safety_violation`. The fix forces explicit recipients for any test-mode send. Surfaced during the v0.7.0 release-review documentation pass; analog of the v0.6 `reply_to_message` hardcoded block that was dropped when the drafts lifecycle (#134) replaced the four old send tools.

**Flag color labels in `update_message(flag_color=...)` (#185):** The map from color name to AppleScript flag index in [`utils.py:get_flag_index`](src/apple_mail_mcp/utils.py) had two pairs of swapped labels. Empirical testing (Gmail/Mail.app, 2026-05-12) confirmed that callers passing certain colors got a different color in Mail.app's UI than they asked for:

- `flag_color="orange"` previously rendered as **red**; now renders as **orange**.
- `flag_color="red"` previously rendered as **orange**; now renders as **red**.
- `flag_color="blue"` previously rendered as **green**; now renders as **blue**.
- `flag_color="green"` previously rendered as **blue**; now renders as **green**.
- `yellow`, `purple`, `gray`, `none` were correctly labeled and unchanged.

The AppleScript-path default for `update_message(flagged=True)` (no `flag_color`) was also adjusted from `get_flag_index('orange')` to `get_flag_index('red')` so the no-color rendering remains red (matching #152's IMAP fast path which sets bare `\Flagged`). Net behavior: `update_message(flagged=True)` still produces a red flag, regardless of which path runs.

Callers who were relying on the buggy mapping should update their calls to use the color name they actually want.

### Performance

**IMAP fast path for flag-only `update_message` (#152):** When `update_message` is called with only `flagged` set (no `flag_color`, `read_status`, or `destination_mailbox`) plus `account` + `source_mailbox`, the flag/unflag mutation now runs server-side via IMAP `UID STORE +/-FLAGS (\Flagged)` — single round-trip after Message-ID resolution. `\Flagged` is base IMAP (RFC 3501), universal across all servers; no capability check, no fallback variants. Verified empirically (Gmail/Mail.app, 2026-05-12) that bare `\Flagged` produces identical visual state to today's AppleScript path's `set flag index = 0` — no UI difference. Color-specifying calls (`flag_color="red"` etc.) still route through AppleScript since Mail.app's color encoding (`$MailFlagBit*` user keywords) is out of IMAP scope. **Caveat on unflag:** calling `flagged=False` via this fast path on a message that was previously color-flagged removes `\Flagged` but does NOT remove the `$MailFlagBit*` color keyword Mail.app set. Standard IMAP clients show no flag; Mail.app may resurface the color on next sync. To clean both: either omit `source_mailbox` (forces the AppleScript path which also clears `flag index`), or pass `flag_color="none"` instead of `flagged=False`. **This closes the IMAP-fast-paths-for-mutations arc** (#149 move, #150 delete, #151 read, #152 flag) — every single-field mutation on `update_message` and `delete_messages` now has an IMAP path on the common path.

**IMAP fast path for read-status-only `update_message` (#151):** When `update_message` is called with only `read_status` set (no `flagged`, `flag_color`, or `destination_mailbox`) plus `account` + `source_mailbox`, the read/unread mutation now runs server-side via IMAP `UID STORE +/-FLAGS (\Seen)` — single round-trip after Message-ID resolution. `\Seen` is base IMAP (RFC 3501), universal across all servers; no capability check needed, no fallback variants. Resolves the dual-ID problem from #147 for the read-status path. Combined patches (read + move, read + flag) still run via AppleScript pending #152. With #149/#150 already landed, the IMAP-fast-paths-for-mutations arc now covers move-only, delete, and read-status-only on the common path; `flag_message` (#152) is the last domino.

**IMAP fast path for `delete_messages` (#150):** When invoked with `account` and `source_mailbox`, `delete_messages` now runs server-side via IMAP `UID MOVE` to the account's Trash folder (RFC 6851), atomic and single round-trip after Message-ID resolution. Resolves the same dual-ID problem from #147 that #149 fixed for moves: callers feeding RFC 5322 Message-IDs from `search_messages`'s IMAP path no longer pay the AppleScript `whose message id is` linear scan (~57s on a 47k-message Gmail INBOX). Trash folder is resolved via RFC 6154 SPECIAL-USE `\Trash` flag; falls back to conventional names (`Trash`, `[Gmail]/Trash`, `Deleted Messages`, `Deleted Items`) when SPECIAL-USE isn't advertised. Capability fallback chain: `MOVE` → `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` (UIDPLUS only) → AppleScript. Servers without either capability, or without a discoverable Trash folder, fall through transparently. Combined with #149, the IMAP-fast-paths-for-mutations arc now covers move and delete on the common path; `mark_as_read` (#151) and `flag_message` (#152) still pending.

**IMAP fast path for move-only `update_message` (#149):** When a caller invokes `update_message` with only `destination_mailbox` set (no `read_status`, `flagged`, or `flag_color`) and provides `source_mailbox`, the move now runs server-side via IMAP `UID MOVE` (RFC 6851) — atomic, single round-trip after Message-ID resolution. Resolves the dual-ID problem from #147: callers feeding RFC 5322 Message-IDs from `search_messages`'s IMAP path no longer pay the AppleScript `whose message id is` linear scan (~57s on a 47k-message Gmail INBOX). Capability detection prefers `MOVE`, falls back to `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` when only `UIDPLUS` (RFC 4315) is advertised — the scoped `UID EXPUNGE` is safe (removes only the just-moved UIDs, not other `\Deleted`-flagged messages in the mailbox). Servers advertising neither MOVE nor UIDPLUS fall through to AppleScript via the existing graceful-degradation path. Combined patches (move + read/flag in one call) still run via AppleScript pending #150 / #151 / #152. Requires `source_mailbox` — without it, IMAP would have to SEARCH every mailbox per Message-ID, defeating the speed win.

### Tooling

**Release workflow no longer fails when CHANGELOG contains backticks (#177):** The auto-release workflow used `gh release create --notes "${{ steps.changelog.outputs.notes }}"`. GitHub Actions interpolates `${{ ... }}` *before* bash parses the line, so backtick-wrapped identifiers in the CHANGELOG body became shell command substitution at release time. v0.7.0's release failed at this step and was patched manually. Fixed by writing the notes to a workspace file and using `--notes-file`, sidestepping shell interpolation entirely.

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
