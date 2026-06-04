# Threat Model Document (STRIDE pass) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `docs/guides/THREAT_MODEL.md` — a reviewer-oriented STRIDE pass across 5 trust boundaries (osascript, IMAP, Keychain, Filesystem, MCP/LLM-as-conduit) with each finding linked to its existing mitigation or flagged as a tracked gap. Add a one-line cross-link in `docs/SECURITY.md`.

**Architecture:** Pure docs change. No code edits, no tests run. Per-boundary structure: 1-paragraph prose intro + STRIDE table (Category | Threat | Mitigation | Gap?). All source-file references must be live links — every claim about a mitigation cites the file (and ideally function name) that implements it.

**Tech Stack:** Markdown only. No build step. GitHub renders the doc.

**Design doc:** [`docs/plans/2026-05-22-issue-213-threat-model-design.md`](2026-05-22-issue-213-threat-model-design.md)

---

## Preflight findings (baked in — do not re-discover)

These were verified during plan-writing:

- **`save_attachments` does NOT have a byte cap.** [src/apple_mail_mcp/mail_connector.py:2410-2487](../../src/apple_mail_mcp/mail_connector.py#L2410-L2487) validates the destination directory and prevents path traversal, but does not cap individual or aggregate attachment size. The DoS row for the Filesystem boundary should flag this as a real gap and **Task 9** files a follow-up issue.
- **Source files referenced by the threat model exist** at their expected paths: `utils.py`, `security.py`, `templates.py`, `mail_connector.py`, `server.py`, `keychain.py`, `imap_connector.py`. (No file is named `imap_credentials.py` — the original spec draft had this wrong; corrected in the design doc.)
- **Project convention** for design docs and impl plans: `docs/plans/YYYY-MM-DD-<topic>.md` (this file) and `docs/plans/YYYY-MM-DD-<topic>-design.md` (the spec).

---

## Task 1: Branch + scaffold the THREAT_MODEL.md file

**Files:**
- Create: `docs/guides/THREAT_MODEL.md`

- [ ] **Step 1: Create a docs branch**

Branch convention per CLAUDE.md is `{type}/issue-{num}-{description}`. Since this PR is docs-only, use `docs/` as the type prefix.

```bash
git checkout -b docs/issue-213-threat-model
```

- [ ] **Step 2: Create the file with just the top-level scaffold**

Create `docs/guides/THREAT_MODEL.md` with this exact content (sections will be filled in subsequent tasks):

```markdown
# Threat Model

> Reviewer-oriented STRIDE analysis of the trust boundaries crossed by `apple-mail-fast-mcp`. Companion to [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md) (how to build safely) and [`../SECURITY.md`](../SECURITY.md) (user-facing posture).

## Overview

_Filled in Task 2._

## Attacker model

_Filled in Task 3._

## Trust boundaries

_Filled in Tasks 4–8._

### 1. osascript / AppleScript

### 2. IMAP

### 3. Keychain

### 4. Filesystem

### 5. MCP / LLM-as-conduit

## Open gaps

_Filled in Task 9._

## References

_Filled in Task 10._
```

- [ ] **Step 3: Verify the file renders cleanly**

Run: `grep -c "^## " docs/guides/THREAT_MODEL.md`
Expected: `5` (Overview, Attacker model, Trust boundaries, Open gaps, References)

Run: `grep -c "^### " docs/guides/THREAT_MODEL.md`
Expected: `5` (the five boundaries)

- [ ] **Step 4: Stage but do not commit yet** — single combined commit at end of plan keeps the doc reviewable as one diff.

---

## Task 2: Write the Overview section

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (replace `_Filled in Task 2._` under `## Overview`)

- [ ] **Step 1: Replace the placeholder under `## Overview` with this content**

```markdown
This document answers one question: **what's the worst thing an attacker could do, and what stops them?**

It complements two existing security docs:

- [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md) — a builder's checklist (five concerns: sanitize, AppleScript escape, path-traversal-safe names, rate limiting, audit logging). Tells contributors *what to do*.
- [`../SECURITY.md`](../SECURITY.md) — user-facing best practices, privacy, compliance.

Neither doc maps the architectural attack surface explicitly. This one does, via a STRIDE pass (Spoofing / Tampering / Repudiation / Information disclosure / DoS / Elevation) per trust boundary.

**The realistic ongoing risk** is not a missing escape — those are well-covered by [`escape_applescript_string`](../../src/apple_mail_mcp/utils.py) and the JSON-only output pipeline. The realistic risk is **LLM-laundered tool calls authorized by a user who didn't read the elicitation prompt closely**. Mitigations are layered: per-tool elicitation gates, rate limits, audit log, and the user's own attention. See boundary §5.

**What this doc isn't:** not a checklist, not pentesting, not external supply-chain audit (tracked separately via #235), not formal verification.
```

- [ ] **Step 2: Verify**

Run: `wc -w docs/guides/THREAT_MODEL.md`
Expected: roughly 150–250 words in the Overview section (the file as a whole is still mostly placeholders at this point).

---

## Task 3: Write the Attacker model section

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (replace `_Filled in Task 3._` under `## Attacker model`)

- [ ] **Step 1: Replace the placeholder under `## Attacker model` with this content**

```markdown
We assume six distinct attacker types. The scope column lists whether the doc claims to defend against each.

| Actor | Capability | Scope | Rationale |
|---|---|---|---|
| **Email sender** | Crafts message bodies, subjects, headers, and attachments delivered via IMAP into Mail.app | In scope | Primary vector: prompt injection (#225), AppleScript injection via message content, malicious attachments |
| **IMAP server operator** | Controls bytes the server returns (folder listings, message bodies, flags) | In scope (data contents); OOS for cert-validated identity | TLS authenticates server identity; we do not trust the bytes |
| **Network adversary** | Passive sniff or active MITM on IMAP/SMTP | **OOS** | TLS is the boundary. If TLS is broken, same posture as any IMAP client |
| **Co-resident user-process** | Same UID as us; can read Keychain (with ACL prompt), send Apple events, read our files, set env vars | **OOS** | Same trust level as the user. Keychain ACL and the AppleScript sandbox will not save us |
| **Supply-chain attacker** | Compromises a transitive Python dependency | **OOS** | Tracked separately via pip-audit and #235 |
| **LLM (Claude)** | Receives tool responses (including email content); emits tool calls back | In scope (laundering); OOS for what happens inside Anthropic's API | The LLM acts *for* the user but is manipulable by injected content. Elicitation is the final guardrail |

**Explicit OOS statement:** A malicious process already running as the same UID is out of scope. We rely on macOS process isolation, code-signing, and the Keychain ACL prompt — none of which we control. Anyone who already owns the user account already owns this server.
```

- [ ] **Step 2: Verify the table renders**

Run: `grep -c "^| " docs/guides/THREAT_MODEL.md`
Expected: at least 8 (header + separator + 6 actor rows).

---

## Task 4: Write boundary §1 — osascript / AppleScript

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (under `### 1. osascript / AppleScript`)

- [ ] **Step 1: Insert this content after the `### 1. osascript / AppleScript` header**

```markdown
**What crosses:** Python's `_run_applescript()` builds a string and pipes it to `osascript`. The script runs with the user's full privileges and Mail.app + automation access. Any unescaped interpolation = code execution as the user. We never run user-supplied AppleScript source — only parameterized templates with sanitized placeholders.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | n/a — `osascript` runs as the user, no auth surface | — | — |
| Tampering | User input interpolated into AS source escapes the string and injects commands | [`escape_applescript_string`](../../src/apple_mail_mcp/utils.py) + [`sanitize_input`](../../src/apple_mail_mcp/utils.py); pattern documented in [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md); property tests in #214 | — |
| Tampering | Numeric / dashed UUID IDs treated as expressions when unquoted (`-` becomes subtraction) | `whose id is "{safe}"` pattern, mandatory quotes (the #86 regression) | — |
| Repudiation | — | Per-process [`operation_logger`](../../src/apple_mail_mcp/security.py) records every tool call with parameters | — |
| Information disclosure | `osascript` stdout leaks Mail content if scripts emit unstructured text | JSON-only output via [`_wrap_as_json_script`](../../src/apple_mail_mcp/mail_connector.py) + ASObjC `NSJSONSerialization` | — |
| Denial of service | `osascript` hangs on big mailboxes or a deadlocked Mail.app | `with timeout of N` wrapper inside `_wrap_as_json_script` | ⚠️ **#233** — non-wrapped AS paths bypass the timeout |
| Elevation | n/a — boundary is `process == user`, no privilege to elevate | — | — |
```

- [ ] **Step 2: Spot-check the source file references**

Run: `grep -n "def escape_applescript_string\|def sanitize_input" src/apple_mail_mcp/utils.py`
Expected: two function definitions found.

Run: `grep -n "_wrap_as_json_script\|with timeout of" src/apple_mail_mcp/mail_connector.py | head -5`
Expected: at least one match for each.

If any expected match is missing, **stop and reconcile** the doc with the actual code before continuing.

---

## Task 5: Write boundary §2 — IMAP

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (under `### 2. IMAP`)

- [ ] **Step 1: Insert this content after the `### 2. IMAP` header**

```markdown
**What crosses:** [`imap_connector.py`](../../src/apple_mail_mcp/imap_connector.py) connects to a remote IMAP server over TLS using `imapclient`. Credentials come from the macOS Keychain via [`keychain.py`](../../src/apple_mail_mcp/keychain.py). The server returns headers, bodies, flags, mailbox listings. **None of this is trusted as input** — it flows into search results, `get_message`/`get_thread` responses, and (importantly) into AppleScript fallback paths.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | Attacker poses as the legit IMAP server | TLS certificate validation via `imapclient` + stdlib SSL | — |
| Tampering | MITM or compromised server returns crafted bytes designed to break parsing or escape into AS | Server-returned strings flow through `sanitize_input` + `escape_applescript_string` before AS interpolation; property tests in #214 will fuzz this boundary | ⚠️ Audit during #214: confirm every IMAP→AS path applies escape |
| Repudiation | We don't log IMAP server identity per request | Connection-time logging in [`keychain.py`](../../src/apple_mail_mcp/keychain.py) | (low) |
| Information disclosure | Server sees our queries and credentials; plaintext IMAP would expose credentials | TLS by default; `setup-imap` requires STARTTLS or implicit TLS — plaintext is not a supported config | — |
| Denial of service | Hostile server hangs, streams gigabytes, or returns crafted message with very many attachments | Connection timeout via `imapclient`; per-call `timeout` param on the connector; bulk caps (100 items) | (low) — no global byte ceiling on server responses; file follow-up if exposure proves material |
| Elevation | Compromised server cannot directly execute code if escape paths are correct | Defense in depth: server data → escape → AS or → return JSON. We never `eval` server input | — |
```

- [ ] **Step 2: Verify the referenced files exist**

Run: `ls src/apple_mail_mcp/imap_connector.py src/apple_mail_mcp/keychain.py`
Expected: both files listed.

---

## Task 6: Write boundary §3 — Keychain

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (under `### 3. Keychain`)

- [ ] **Step 1: Insert this content after the `### 3. Keychain` header**

```markdown
**What crosses:** Per-account IMAP/SMTP passwords are stored under our service identifier in the macOS Keychain. They are read at connector init via [`keychain.py`](../../src/apple_mail_mcp/keychain.py). The Keychain's per-application ACL is the only barrier between any user-process and our credentials.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | Another process pretends to be us and reads our Keychain entry | macOS Keychain ACL — entry bound to our binary signature at write time. macOS prompts the user on first access by a different binary | — |
| Tampering | Co-resident process modifies the stored password | OOS — co-resident user-process is OOS by attacker model | — |
| Repudiation | We don't log Keychain access | Apple-side Keychain access log if the user enables it | (low) |
| Information disclosure | Password readable on ACL grant | User must approve first access; revocable via Keychain Access app | — |
| Denial of service | Keychain unavailable or user revokes access → connector fails | Connector raises clear error; AppleScript fallback paths where applicable | — |
| Elevation | n/a — read-only credential retrieval | — | — |
```

---

## Task 7: Write boundary §4 — Filesystem

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (under `### 4. Filesystem`)

- [ ] **Step 1: Insert this content after the `### 4. Filesystem` header**

```markdown
**What crosses:** Several filesystem touchpoints, each with different trust:

- **Templates:** `~/.apple_mail_mcp/templates/<name>.md` — both name and content are user-supplied
- **Save attachments:** caller supplies `dest_dir`
- **Draft attachments:** tempdir extraction during `update_draft`
- **Root override:** `APPLE_MAIL_MCP_HOME` env var

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | n/a | — | — |
| Tampering | User input as filename stem escapes intended dir (`../../etc/passwd`) | [`_validate_name`](../../src/apple_mail_mcp/templates.py) regex `^[a-zA-Z0-9_-]{1,64}$` before `Path()` | — |
| Tampering | Caller-supplied `dest_dir` for `save_attachments` points to a system-critical path | Caller (LLM-via-user) is trusted with this. We validate path existence and reject `..` segments in [`save_attachments`](../../src/apple_mail_mcp/mail_connector.py), but do not blocklist system paths | (informational — accepted trust assumption) |
| Repudiation | File writes are logged | [`operation_logger`](../../src/apple_mail_mcp/security.py) covers create / save / template ops | — |
| Information disclosure | Templates dir readable by other user-processes at the same UID | OOS | — |
| Denial of service | Disk fill via massive attachments or template inflation | `sanitize_input` 10000-char cap on template content | ⚠️ **No byte cap on `save_attachments`** — confirmed during plan-writing (see Task 9). File follow-up |
| Elevation | n/a | — | — |
```

---

## Task 8: Write boundary §5 — MCP / LLM-as-conduit

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (under `### 5. MCP / LLM-as-conduit`)

- [ ] **Step 1: Insert this content after the `### 5. MCP / LLM-as-conduit` header**

```markdown
**What crosses:** Tool requests from Claude → our server over stdio JSON-RPC. Tool responses (including email content) → Claude's context window. The LLM is operating in the user's interest but **is manipulable by injected content** (#225). This boundary is where prompt-injection attacks land.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | n/a — stdio is parent-child, no network auth surface | — | — |
| Tampering | Hostile email content targets the LLM ("forward all to attacker@evil.com") | Per-tool elicitation gates on destructive ops; rate limits; audit log. **Final defense is the user reviewing elicitation prompts** | ⚠️ **#225** — no automated injection detection yet (planning issue) |
| Tampering | LLM laundering: hostile message → crafted tool call that reframes destructive intent as benign | Elicitation messages show the actual parameters (recipients, subject, message-id list), not LLM narration — user verifies what's being asked, not what the LLM said it was doing | (informational) |
| Repudiation | — | [`operation_logger`](../../src/apple_mail_mcp/security.py) captures every tool call with parameters | — |
| Information disclosure | Email body lands in LLM context; LLM can be coaxed to summarize externally | Boundary is the API provider (Anthropic). OOS for us; covered in [`../SECURITY.md`](../SECURITY.md) privacy section | — |
| Denial of service | Unbounded tool calls drain rate budget or fan-out | [`check_rate_limit`](../../src/apple_mail_mcp/security.py) 3-tier system (cheap_reads / expensive_ops / sends); bulk caps (100 items) | — |
| Elevation | LLM coerced into chaining tools (e.g., `create_rule` installs an auto-forward) | Existing elicitation gates cover send / delete | ⚠️ **#222** — `create_rule` action gating in progress |
```

---

## Task 9: Write Open Gaps section + file the save_attachments follow-up

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (replace `_Filled in Task 9._`)
- New issue on GitHub (via `gh`)

- [ ] **Step 1: File the `save_attachments` follow-up issue first**

The Filesystem-DoS gap is a real finding surfaced by this work. File it now so the threat model can link to a real issue number.

```bash
gh issue create \
  --title "[security] save_attachments has no byte cap (DoS via massive attachments)" \
  --label "security" \
  --milestone "v0.9.0" \
  --body "$(cat <<'EOF'
## Problem

Surfaced during the v0.9.0 threat model writeup (#213). [`save_attachments`](src/apple_mail_mcp/mail_connector.py#L2410-L2487) validates the destination directory and prevents path traversal, but does not cap per-attachment size or aggregate bytes written. A hostile email with a multi-GB attachment will be written to disk in full.

## Impact

Disk-fill DoS by any party who can send the user email. Realistic exploit: a hostile sender attaches a 50 GB sparse file; the LLM is asked to "save the attachment", elicitation fires (recipients shown), user approves, disk fills.

## Suggested mitigation

- Per-attachment size cap (config-driven; default ~100 MB)
- Aggregate cap per `save_attachments` call (default ~500 MB)
- Pre-check `name of att` and `size of att` via AppleScript before issuing the save
- Return the rejected attachment names in the success payload

## Out of scope

- Malware scanning (separate concern; SECURITY.md already says we don't do this)
EOF
)"
```

Note the resulting issue number (e.g. `#236`). It will be referenced below.

- [ ] **Step 2: Replace the placeholder under `## Open gaps`**

Substitute `#NNN` with the actual issue number from Step 1.

```markdown
Findings flagged `⚠️` in the tables above, mapped to tracked issues:

| Boundary | Gap | Issue |
|---|---|---|
| osascript / AppleScript (§1) | Non-wrapped AS paths bypass `with timeout of N` | [#233](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/issues/233) |
| IMAP (§2) | Audit every IMAP→AS path applies `escape_applescript_string` | [#214](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/issues/214) (property tests) |
| Filesystem (§4) | No byte cap on `save_attachments` | [#NNN](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/issues/NNN) (filed from this work) |
| MCP / LLM-as-conduit (§5) | No automated prompt-injection detection on `get_message` responses | [#225](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/issues/225) (planning) |
| MCP / LLM-as-conduit (§5) | `create_rule` does not gate dangerous actions (move / forward / delete / copy) | [#222](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/issues/222) |

Lower-severity / informational items (not tracked as issues):

- IMAP server-response byte ceiling — no evidence of real exposure today
- Keychain access log — Apple-side, user-configurable
- `dest_dir` of `save_attachments` is caller-controlled by design
```

- [ ] **Step 3: Verify the linked issues are all open**

Run: `for n in 214 222 225 233 NNN; do gh issue view $n --json number,state,title --jq '"#\(.number) [\(.state)] \(.title)"'; done` (substituting NNN)
Expected: each line ends in `OPEN`.

---

## Task 10: Write the References section

**Files:**
- Modify: `docs/guides/THREAT_MODEL.md` (replace `_Filled in Task 10._`)

- [ ] **Step 1: Replace the placeholder under `## References`**

```markdown
- [`SECURITY.md`](../SECURITY.md) — user-facing security posture, privacy, compliance
- [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md) — per-feature builder's checklist
- [`ARCHITECTURE.md`](../reference/ARCHITECTURE.md) — system architecture
- [`APPLESCRIPT_GOTCHAS.md`](../reference/APPLESCRIPT_GOTCHAS.md) — AS quirks and string-escape patterns
- Issues tracked from this analysis: #214, #222, #225, #233, and the `save_attachments` follow-up filed in Task 9
```

---

## Task 11: Add cross-link in `docs/SECURITY.md`

**Files:**
- Modify: `docs/SECURITY.md` (insert one line at the top of the `## Attack Surface Analysis` section)

- [ ] **Step 1: Read the section header**

Run: `grep -n "^## Attack Surface Analysis" docs/SECURITY.md`
Expected: one match, around line 37.

- [ ] **Step 2: Insert the cross-link**

Use `Edit` to insert this paragraph immediately after the `## Attack Surface Analysis` line:

```markdown
> **For the canonical trust-boundary breakdown and STRIDE analysis, see [`docs/guides/THREAT_MODEL.md`](guides/THREAT_MODEL.md).** The narrative below is preserved for continuity and will be reconciled with the threat model in #220.
```

The exact edit:

```python
old_string = "## Attack Surface Analysis\n\n### 1. Prompt Injection"
new_string = """## Attack Surface Analysis

> **For the canonical trust-boundary breakdown and STRIDE analysis, see [`docs/guides/THREAT_MODEL.md`](guides/THREAT_MODEL.md).** The narrative below is preserved for continuity and will be reconciled with the threat model in #220.

### 1. Prompt Injection"""
```

- [ ] **Step 3: Verify**

Run: `grep -n "THREAT_MODEL.md" docs/SECURITY.md`
Expected: one match inside the `## Attack Surface Analysis` section.

---

## Task 12: Final verification

**No files modified — verification only.**

- [ ] **Step 1: Link audit**

Run: `grep -oE '\[[^]]+\]\([^)]+\)' docs/guides/THREAT_MODEL.md | sort -u | head -40`
Inspect manually. For each `../../src/...` link, confirm the file exists:

```bash
for f in $(grep -oE '\.\./\.\./src/[^)]+' docs/guides/THREAT_MODEL.md | sed 's/#.*//' | sort -u); do
  test -e "docs/guides/$f" && echo "OK $f" || echo "MISSING $f"
done
```

Expected: all `OK`.

- [ ] **Step 2: Section count**

Run: `grep -c "^## " docs/guides/THREAT_MODEL.md`
Expected: `5`.

Run: `grep -c "^### " docs/guides/THREAT_MODEL.md`
Expected: `5`.

- [ ] **Step 3: Word count sanity check**

Run: `wc -w docs/guides/THREAT_MODEL.md`
Expected: 1500–2500 words.

- [ ] **Step 4: Confirm no source code was modified**

Run: `git diff --stat`
Expected: only `docs/guides/THREAT_MODEL.md` (new) and `docs/SECURITY.md` (1 line added).

- [ ] **Step 5: Reviewer-acceptance check**

Read the doc top-to-bottom as if you've never seen the project. Confirm you can answer, **without reading source**:

1. What's the worst thing an attacker could do?
2. What stops them?
3. Where are the gaps, and which issues track them?

If any answer requires reading source, revise the relevant section.

---

## Task 13: Commit, push, open PR

**Files:** none modified — git operations only.

- [ ] **Step 1: Stage and commit**

```bash
git add docs/guides/THREAT_MODEL.md docs/SECURITY.md
git commit -m "$(cat <<'EOF'
docs(#213): add THREAT_MODEL.md (STRIDE pass across 5 trust boundaries)

Reviewer-oriented threat model covering osascript, IMAP, Keychain,
filesystem, and MCP/LLM-as-conduit boundaries. Each finding links to
the existing mitigation or to a tracked gap issue.

Adds a one-line cross-link in SECURITY.md pointing at the new doc;
full reconciliation deferred to #220.

Surfaced one new gap (save_attachments byte cap) filed as a follow-up.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Push**

```bash
git push -u origin docs/issue-213-threat-model
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "docs(#213): threat model document (STRIDE pass)" --body "$(cat <<'EOF'
## Summary
- Adds `docs/guides/THREAT_MODEL.md`: STRIDE pass across 5 trust boundaries (osascript, IMAP, Keychain, filesystem, MCP/LLM-as-conduit).
- Adds a one-line cross-link to the new doc in `docs/SECURITY.md`; full reconciliation deferred to #220.
- Surfaces one new gap (save_attachments byte cap), filed as a follow-up.

## Closes
Closes #213

## Test plan
- [ ] Render `docs/guides/THREAT_MODEL.md` on GitHub; confirm tables render and all internal links resolve.
- [ ] Confirm each `⚠️ Gap?` row references an open issue (`gh issue view <N>`).
- [ ] Read the doc cold — can you answer "what's the worst thing / what stops them / where are the gaps" without opening source?

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Confirm PR opens, return URL**

Expected: PR URL printed; CI should pass since no code changed.
