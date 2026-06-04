# IMAP thread-discovery strategies

**Status:** Research / decision doc. Output of issue #80.
**Date:** 2026-05-02
**Outcome:** Recommendation at the bottom; two follow-up implementation issues filed.

## Background

`get_thread`'s IMAP path (shipped in #66, source: `find_thread_members` in [src/apple_mail_mcp/imap_connector.py](../../src/apple_mail_mcp/imap_connector.py)) uses a per-mailbox header-search BFS. For each mailbox in the anchor's account, it issues `SEARCH HEADER X Y` for every (known thread id) × (`Message-ID` | `In-Reply-To` | `References`) pair. This is correct everywhere — the only IMAP commands required are `LIST`, `SELECT`, `SEARCH HEADER`, and `FETCH ENVELOPE FLAGS`, all of which RFC 3501 mandates. But it scales poorly:

- **M** = number of mailboxes / labels on the account
- **N** = number of known thread ids (anchor + every entry in its `References` chain)
- **3** = the three header types
- **Round-trip count** ≈ M × N × 3 SEARCH calls + M SELECTs + up to M FETCHes

For an iCloud account with M=7 mailboxes and a thread of N=4 (anchor + 3 references), that's ~84 SEARCH round-trips per call. For Gmail with ~91 labels (each presented as a folder over IMAP) it balloons to ~1100. Issue #80 recorded a real iCloud `get_thread` measurement around 90 s, putting per-round-trip cost on iCloud's IMAP server in the ~1 s range under burst load — so the cost is real and worth optimizing for users with non-trivial mailbox counts.

This doc evaluates two server-side alternatives:

1. **RFC 5256 `THREAD REFERENCES`** — server returns the entire thread tree for the SELECTed mailbox in a single command. Capability-advertised; supported by some providers, not others.
2. **Gmail `X-GM-THRID`** — Gmail-specific extension. One `FETCH X-GM-THRID` on the anchor's UID returns its 64-bit thread ID; one `SEARCH X-GM-THRID <id>` against `[Gmail]/All Mail` then returns every member of the conversation cross-mailbox in one round-trip.

## Methodology

1. Probe every Mail.app account that has IMAP configured (i.e., a Keychain entry under `apple-mail-mcp.imap.<account>`). Capture the server's `CAPABILITY` response. Try the relevant commands directly.
2. For providers we couldn't probe live, document expected behavior from RFCs / public vendor documentation, and flag the gap explicitly.
3. Compare end-to-end cost shape (round-trips × per-round-trip cost) for each strategy on each provider category.

## What we measured

Only one account on this machine has IMAP delegation set up: **iCloud** (host `p42-imap.mail.me.com`). MobileMe, Gmail, Yahoo, and a Pitt account exist in Mail.app but have no Keychain entries, so they can't be queried over IMAP without credentials we don't have (running `apple-mail-fast-mcp setup-imap --account <name>` would unblock them; any future revision of this doc should re-run on whatever's available).

### iCloud — `CAPABILITY` advertisement

```
CONDSTORE, CONTEXT=SORT, ENABLE, ESEARCH, ESORT, ID, IDLE, IMAP4,
IMAP4REV1, LIST-STATUS, NAMESPACE, QRESYNC, QUOTA, SASL-IR, SORT,
UIDPLUS, UNSELECT, WITHIN, X-APPLE-REMOTE-LINKS, XAPPLELITERAL,
XAPPLEPUSHSERVICE
```

**No `THREAD=*` advertised.** No Gmail extensions either (expected; iCloud isn't Gmail). Issuing `THREAD REFERENCES UTF-8 ALL` against iCloud returns immediately with `imapclient.exceptions.CapabilityError: The server does not support b'REFERENCES' threading algorithm` — measured at ~90 ms, so cheap to detect at runtime and fall back from. **For iCloud users, RFC 5256 THREAD is not an option.** The current per-mailbox header-search BFS, or the AppleScript fallback, are the only available paths.

This contradicts the speculation in #80's body table that listed iCloud as a THREAD supporter. Updated below.

### iCloud — mailbox shape

Seven folders (`INBOX`, `Drafts`, `Sent Messages`, `Archive`, `Junk`, `Deleted Messages`, `Notes`). Account on this machine is sparsely populated (only 2 messages in `Sent Messages`, all others empty), which is why we don't reproduce the issue's ~90 s baseline figure here — there's nothing to thread. The cost-shape analysis (M × N × 3 round-trips) stands regardless.

## What we couldn't measure (documentary survey)

### Gmail

Per Google's IMAP documentation and public `CAPABILITY` traces:

- Advertises **`THREAD=REFERENCES`** and **`THREAD=REFS`**.
- Advertises **`X-GM-EXT-1`** — the marker indicating support for `X-GM-MSGID`, `X-GM-THRID`, and `X-GM-LABELS`.
- `[Gmail]/All Mail` is a virtual folder containing every message in the account regardless of label. A single `SEARCH X-GM-THRID <thrid>` against `[Gmail]/All Mail` returns every UID in the conversation across all labels.

Cost shape:
- **THREAD path**: per-mailbox `THREAD REFERENCES ALL` × M mailboxes (M can be ~91 for power users with many labels).
- **X-GM-THRID path**: 1 SELECT (`All Mail`) + 1 FETCH (anchor's `X-GM-THRID`) + 1 SEARCH + 1 FETCH for envelopes. Total: ~4 round-trips, mailbox-count-independent.

Net: **X-GM-THRID is dramatically faster on Gmail than either THREAD or header-search BFS**, and matches Gmail's UI's "conversation" notion exactly. The fidelity caveat the issue flagged (THREAD groups by RFC 5322 references; Gmail UI groups by `X-GM-THRID`) is real but operationally unimportant — users who use `get_thread` on a Gmail message expect Gmail's conversation, not a strict RFC 5322 reconstruction.

### Fastmail

Public docs and CAPABILITY traces show **`THREAD=ORDEREDSUBJECT`** and **`THREAD=REFERENCES`** advertised. Cyrus IMAPd (the upstream Fastmail uses) implements both. Cost shape: per-mailbox THREAD × M, M typically much smaller than Gmail (~10).

### Dovecot (self-hosted, ProtonMail Bridge, etc.)

`THREAD=REFS` is in the Dovecot core; `THREAD=REFERENCES` available via the `imap_thread` plugin. Whether a given deployment advertises it is at the operator's discretion.

### Yahoo, AOL, generic IMAP servers

Spotty. Yahoo's IMAP capability list isn't well documented and has been deprecating app passwords; we couldn't probe live. Generic IMAP servers vary by deployment. Capability-detection at runtime is the only safe answer.

## Capability matrix (revised from issue body)

| Provider | THREAD advertised? | X-GM-EXT-1? | Notes |
|----------|--------------------|--------------|-------|
| iCloud | **NO** (verified empirically 2026-05-02) | NO | Per-mailbox header-search BFS or AppleScript fallback only. |
| Gmail | YES (per public docs) | YES | X-GM-THRID is the best path; ~4 round-trips, mailbox-count-independent. |
| Fastmail | YES (per public docs) | NO | THREAD per-mailbox is the win here. |
| Dovecot-based | DEPENDS | NO | Capability detection per-server. |
| Yahoo / AOL | UNCLEAR | NO | No reliable docs; runtime detection only. |
| Generic IMAP (RFC 3501) | DEPENDS | NO | Runtime detection. |

## Cost comparison (round-trips per `get_thread` call)

For an N=4 thread (anchor + 3 references):

| Provider (M=mailboxes) | Header-search BFS | RFC 5256 THREAD | X-GM-THRID |
|------------------------|-------------------|-----------------|------------|
| iCloud (M=7) | ~84 SEARCH + 7 SELECT + 7 FETCH ≈ 100 RTs | not supported | not supported |
| Fastmail (M=10) | ~120 RTs | ~10 SELECT + 10 THREAD ≈ 20 RTs | not supported |
| Gmail (M=91) | ~1100 RTs | ~91 SELECT + 91 THREAD ≈ 180 RTs | ~4 RTs |

The savings scale linearly with M (mailbox count) for THREAD, and collapse to essentially constant for X-GM-THRID. **Gmail is the strongest case for both alternatives.**

## What `find_thread_members` already does well

Worth flagging since the optimization plans below build on it:

- The header-search BFS exploits the well-formed-replies-copy-References invariant — searching on `<anchor-id>` against the `References` header naturally captures every descendant regardless of tree depth, in one pass.
- It's already correct cross-mailbox without merging logic.
- It already gracefully skips mailboxes that reject `SELECT` (e.g. Gmail smart labels), per [imap_connector.py:733-741](../../src/apple_mail_mcp/imap_connector.py#L733-L741).
- It composes with the rest of the IMAP fallback story (#75 pool + #118 breaker + AppleScript fallback) without changes.

So whatever we do, the BFS stays as the **universal fallback** for capability-rejecting servers.

## Recommendation

**Tiered, capability-detected dispatch.** In priority order:

### Tier 1: `X-GM-THRID` for Gmail (filed as follow-up issue)

When the connector connects to a Gmail account (detect via `X-GM-EXT-1` in CAPABILITY):

1. `SELECT [Gmail]/All Mail` (readonly).
2. `SEARCH HEADER Message-ID <anchor-id>` to find anchor's UID.
3. `FETCH <uid> X-GM-THRID` to get the conversation ID.
4. `SEARCH X-GM-THRID <thrid>` to get every UID in the conversation.
5. `FETCH <uids> ENVELOPE FLAGS` to populate the result.

~5 round-trips, mailbox-count-independent. Replaces ~1100 RTs on a 91-label Gmail with ~5. **Biggest single perf win available**, and matches Gmail UI's notion of "conversation."

### Tier 2: `THREAD=REFERENCES` for capability-advertising providers (filed as follow-up issue)

When `THREAD=REFERENCES` is in CAPABILITY (Fastmail, some Dovecot deployments, etc., but **not** iCloud):

1. For each mailbox: `SELECT` then `THREAD REFERENCES UTF-8 ALL` once.
2. Walk the returned nested-tuple thread tree. Find the cluster containing the anchor's `Message-ID`. Collect siblings' UIDs.
3. `FETCH ENVELOPE FLAGS` on the collected UIDs.

~M × 2 round-trips replaces M × 3 × N. The win shrinks for short threads (small N) and grows for deep ones.

### Tier 3: header-search BFS (current path, no changes)

The universal fallback when neither extension is advertised. Stays as-is. **iCloud's only IMAP path** for the foreseeable future, since the server doesn't support THREAD.

### What does NOT change

- AppleScript fallback unchanged. Still the universal baseline when IMAP isn't configured or fails.
- `_IMAP_FALLBACK_EXCS` and the fallback-on-error semantics. Capability rejections from Tier 1/2 attempts should fall through to Tier 3 (header-search BFS), not to AppleScript — the IMAP connection is healthy, just the optimization path isn't available.
- `get_thread`'s tool surface. `account` and `mailbox` aren't even consulted today on the IMAP path; that stays the same.

## Open questions deferred to follow-ups

- **Tier 1 mailbox-spanning fidelity.** Does `SEARCH X-GM-THRID` against `[Gmail]/All Mail` reliably return UIDs that we can then `FETCH ENVELOPE FLAGS` on? The UIDs are scoped to `[Gmail]/All Mail`, not to the user's chosen labels — so the result dicts wouldn't carry per-label context. For `get_thread`'s callers this is fine (they want envelope+flags, not labels), but if we ever want to surface labels, that's a separate FETCH on `X-GM-LABELS`.
- **Tier 2 cross-mailbox merge.** RFC 5256 THREAD is per-mailbox. If a thread spans INBOX + Archive (common when a user files some replies and not others), Tier 2 needs to merge results across mailboxes — which the current header-search BFS does naturally (the merge falls out of dedup-by-Message-ID at the end). Same dedup logic should drop in.
- **THREAD command syntax variants.** `THREAD=REFS` (Gmail's preferred form) vs `THREAD=REFERENCES` (RFC 5256) — need to query both and pick whichever is advertised.

## Follow-up issues filed

- **#122 — `[perf] Use X-GM-THRID for get_thread on Gmail accounts`** — implements Tier 1. **Shipped.**
- **#125 — `[perf] X-GM-THRID per-mailbox iteration when [Gmail]/All Mail is hidden from IMAP`** — implements Tier 1.5. **Shipped.** Smoke-verified against real Gmail (92 folders): 25.5s vs ~100s for Tier 3 BFS, ~4× win. Triggered when `X-GM-EXT-1` is advertised but the `\\All` SPECIAL-USE flag is not present in the folder listing.
- **#123 — `[perf] Use RFC 5256 THREAD for get_thread when server advertises capability`** — implements Tier 2. **Shipped.** Unit-tested only (no Fastmail/Dovecot account configured for live verification — first real-server use will surface any quirks). Triggered when `THREAD=REFERENCES` or `THREAD=REFS` is advertised.

All three tiers are now in production for the connector's `find_thread_members`. iCloud users get no benefit from any of them; their best win remains improving the BFS itself (e.g. parallelizing across mailboxes, bounding by `\\Trash` / `\\Junk` mailbox skip lists, or accepting partial coverage on huge accounts) — but that's a separate research question and is **not** the same as the THREAD/X-GM-THRID question this doc answers.

## Provenance

- iCloud CAPABILITY + THREAD probe: live measurement against `p42-imap.mail.me.com:993` on 2026-05-02.
- Gmail / Fastmail / Dovecot capability claims: public vendor docs and standards (RFC 5256, Gmail IMAP extensions docs at <https://developers.google.com/gmail/imap/imap-extensions>).
- Round-trip-cost estimates: derived from current code shape (`find_thread_members` in `imap_connector.py`) and measured per-round-trip cost on iCloud (~150-300 ms typical, occasional ~1 s under burst load).

---

## Addendum: live Gmail observations (2026-05-03, post #122 setup)

Once the user configured Gmail IMAP delegation, we re-ran the capability probe directly. **One prediction in this doc was wrong** and worth correcting:

```
caps (21): [APPENDLIMIT=35651584, CHILDREN, COMPRESS=DEFLATE, CONDSTORE,
            ENABLE, ESEARCH, ID, IDLE, IMAP4REV1, LIST-EXTENDED,
            LIST-STATUS, LITERAL-, MOVE, NAMESPACE, QUOTA, SPECIAL-USE,
            UIDPLUS, UNSELECT, UTF8=ACCEPT, X-GM-EXT-1, XLIST]
THREAD variants: []           # ← NOT advertised
X-GM-EXT-1 advertised? True   # ← advertised
folder count: 92
```

- **`X-GM-EXT-1` confirmed** — Tier 1 (X-GM-THRID) is available, as predicted.
- **`THREAD=*` is NOT advertised** on this Gmail account, contradicting the public-docs claim earlier in this doc. So Tier 2 (RFC 5256 THREAD) wouldn't fire on Gmail anyway — even if we implemented it, capability detection would skip it. **Tier 1 is the only IMAP optimization Gmail will accept**, and the BFS is the only fallback.
- **92 folders** confirms the magnitude: 92 × N=4 × 3 ≈ 1100 SEARCH round-trips for the BFS path on this account. Tier 1 collapses that to ~5 round-trips, mailbox-count-independent.

This finding makes #122 (Tier 1) the single highest-impact perf optimization for Gmail users. #123 (Tier 2) remains valuable for Fastmail / Dovecot deployments, where neither X-GM-EXT-1 nor a Gmail-class folder count applies.

Capability matrix updated based on live probe:

| Provider | THREAD advertised? | X-GM-EXT-1? | Notes |
|----------|--------------------|--------------|-------|
| iCloud | NO (verified 2026-05-02) | NO | BFS or AppleScript only. |
| Gmail | **NO (verified 2026-05-03)** ← was YES per docs | YES | X-GM-THRID is the only IMAP optimization that fires here. |
| Fastmail | YES (per public docs) | NO | THREAD per-mailbox is the win — no live verification yet. |
| Dovecot-based | DEPENDS | NO | Capability detection per-server. |

### Sub-finding: `[Gmail]/All Mail` is opt-in over IMAP

The first cut of #122 (Tier 1) used SPECIAL-USE's `\\All` flag to find `[Gmail]/All Mail`, then ran one `SEARCH HEADER Message-ID` + one `FETCH X-GM-THRID` + one `SEARCH X-GM-THRID` against it. Total: 5 round-trips, mailbox-count-independent. Beautiful in theory.

**In practice on the user's account, the folder isn't there.** The post-login folder listing (92 folders) had no entry with the `\\All` flag at all. Live `LIST` output shows only `\\Drafts`, `\\Important`, `\\Sent`, `\\Junk` (Spam), `\\Starred`, `\\Trash` — no `\\All Mail`. Manually trying `SELECT [Gmail]/All Mail` returned `[NONEXISTENT] Unknown Mailbox`.

Cause: Gmail Settings → "Forwarding and POP/IMAP" → "Folder size limits" → the option **"Do not show this folder in IMAP"** can be toggled per-folder. Many Gmail users hide All Mail from IMAP because it's enormous and otherwise appears in every IMAP client's mailbox list. When hidden, the folder simply isn't enumerated by `LIST` and isn't selectable.

This makes Tier 1 (as designed) a no-op for any Gmail user who has hidden All Mail. The dispatcher correctly falls through to Tier 3 (BFS), so correctness is fine — but the ~5-round-trip perf win never materializes.

**For users who can enable All Mail in IMAP**: the Gmail Settings flip is one click; #122's Tier 1 then activates fully. Worth documenting in a setup-guide or README note.

**For users who can't or won't**: needs a per-mailbox X-GM-THRID variant. SELECT INBOX (or any folder where the anchor lives), FETCH X-GM-THRID for the anchor's UID, then iterate selectable folders running `SEARCH X-GM-THRID <thrid>` per mailbox. Cost: M × 2 round-trips (vs. ~5 for the All-Mail path; vs. M × N × 3 for BFS). Still a 6× win over BFS on a 92-folder account. Filed as **#125**.

This sub-finding doesn't invalidate #122 — Tier 1 still fires for Gmail users with All Mail exposed, which is a meaningful subset. It just means the universal X-GM-THRID path is a separate, more general optimization.
