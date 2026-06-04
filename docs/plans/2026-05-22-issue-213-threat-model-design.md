# Threat model document (STRIDE pass)

**Issue:** #213
**Date:** 2026-05-22
**Status:** Approved

## Context

The project has two existing security docs:

- [`docs/SECURITY.md`](../SECURITY.md) — user-facing: best practices, privacy, compliance. Includes an "Attack Surface Analysis" section that overlaps with what a threat model would cover, but some of it is stale (mentions "Phase 2" for filesystem features that have already shipped).
- [`docs/guides/SECURITY_CHECKLIST.md`](../guides/SECURITY_CHECKLIST.md) — a builder's checklist: five concerns (sanitize, AppleScript escape, path-traversal-safe names, rate limiting, audit logging), each linking to the canonical implementation. Tells contributors *what to do*, not *what the attack surface looks like*.

Neither doc lets a reviewer answer "what's the worst thing an attacker could do, and what stops them?" without reading source. As the project moves toward broader distribution, an explicit STRIDE-style write-up makes trust boundaries legible and surfaces gaps the checklist wouldn't catch.

This issue produces that doc. SECURITY.md's full reconciliation happens later in [#220](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/issues/220) (docs refresh); here, we leave SECURITY.md alone except for a one-line cross-link pointing at the new threat model.

## Non-goals

- **Not a SECURITY.md rewrite.** Adding a cross-link only; the docs refresh in #220 will reconcile the overlap.
- **Not a SECURITY_CHECKLIST.md update.** The checklist stays as-is.
- **Not formal verification, pentesting, or external supply-chain audit.** Supply-chain is tracked separately (#235).
- **Not net-new mitigation work.** Where the STRIDE pass surfaces a gap, file or link to an issue — don't fix it in this PR. The pre-existing v0.9.0 issues already cover the known gaps (#214, #222, #225, #233).

## Design

### Output file

`docs/guides/THREAT_MODEL.md` — net-new. Reviewer-oriented; terse prose; tables do the heavy lifting. Target length ~600–800 lines.

### Document structure

```
1. Overview                           (~150 words)
2. Attacker model                     (~250 words)
3. Trust boundaries                   (5 sections, ~150-250 words each)
   3.1 osascript / AppleScript
   3.2 IMAP
   3.3 Keychain
   3.4 Filesystem
   3.5 MCP / LLM-as-conduit
4. Open gaps                          (~100 words)
5. References                         (~50 words)
```

### Section 1 — Overview

One paragraph each on:

- **What this doc is.** STRIDE-style write-up of the trust boundaries crossed by the MCP server. Companion to [`SECURITY_CHECKLIST.md`](../guides/SECURITY_CHECKLIST.md) (which says *how to build safely*) and [`SECURITY.md`](../SECURITY.md) (which is user-facing).
- **What this doc isn't.** Not a checklist, not pentesting, not compliance guidance.
- **One-sentence "the worst thing".** Probably along the lines of: *A hostile message body or hostile IMAP server response could, if a defense fails, cause AppleScript code execution as the user — but the [`escape_applescript_string`](../../src/apple_mail_mcp/utils.py) + JSON-only output pipeline is designed to prevent this. The realistic ongoing risk is LLM-laundered tool calls authorized by a user who didn't read the elicitation prompt closely.*

### Section 2 — Attacker model

Six actors, each row in a table with columns: **Actor | Capability | Scope | Rationale**.

| Actor | Capability | Scope | Rationale |
|---|---|---|---|
| Email sender | Crafts message bodies / subjects / headers / attachments delivered via IMAP into Mail.app | **In scope** | Primary vector: prompt injection (#225), AppleScript injection via content, malicious attachments |
| IMAP server operator | Controls the bytes the server returns | **In scope** (data contents); **OOS** for cert-validated identity | TLS authenticates server identity; we do not trust the bytes |
| Network adversary | Passive sniff or active MITM | **OOS** | TLS is the boundary. Same posture as any IMAP client |
| Co-resident user-process | Same UID; can read Keychain (with ACL prompt), send Apple events, read our files, set env vars | **OOS** | Same trust level as the user. Keychain ACL and the AppleScript sandbox won't save us |
| Supply-chain attacker | Compromises a transitive Python dep | **OOS** | Explicit non-goal in #213; tracked via pip-audit + #235 |
| LLM (Claude) | Receives tool responses (incl. email content); emits tool calls back | **In scope** (laundering); **OOS** for what happens inside Anthropic's API | The LLM acts *for* the user but is manipulable by injected content. Elicitation is the final guardrail |

Closing paragraph: explicit OOS statement — "co-resident user-process is out of scope" — and the rationale (we'd need OS-level isolation to defend, which is Apple's job, not ours).

### Section 3 — Trust boundaries

Each subsection: 1-paragraph prose intro ("what crosses this boundary, who controls each side") + STRIDE table with columns **Category | Threat | Mitigation | Gap?**.

The findings to populate each table (already validated during brainstorming):

#### 3.1 osascript / AppleScript

Crosses: Python `_run_applescript()` builds a string and pipes to `osascript`. The script runs with user privileges and full Mail.app + automation access. Any unescaped interpolation = code execution. We never run user-supplied AppleScript source — only parameterized templates.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | n/a | — | — |
| Tampering | User input interpolated into AS source could escape strings + inject commands | [`escape_applescript_string`](../../src/apple_mail_mcp/utils.py) + [`sanitize_input`](../../src/apple_mail_mcp/utils.py); property tests #214 | — |
| Tampering | Numeric / dashed UUID IDs treated as expressions if unquoted | `whose id is "{safe}"` pattern (`#86` trap) | — |
| Repudiation | — | Per-process [`operation_logger`](../../src/apple_mail_mcp/security.py) | — |
| Info disclosure | osascript stdout could leak Mail content | JSON-only output via [`_wrap_as_json_script`](../../src/apple_mail_mcp/mail_connector.py) + ASObjC | — |
| DoS | osascript hangs on big mailboxes / deadlocked Mail.app | `with timeout of N` wrap in `_wrap_as_json_script` | ⚠️ **#233** — non-wrapped paths bypass timeout |
| Elevation | n/a — boundary is `process == user` | — | — |

#### 3.2 IMAP

Crosses: `imapclient` connects to remote IMAP server over TLS. Credentials pulled from Keychain via [`keychain.py`](../../src/apple_mail_mcp/keychain.py); IMAP path implementation in [`imap_connector.py`](../../src/apple_mail_mcp/imap_connector.py). Server returns headers, bodies, flags, mailbox listings. None of this is trusted as input — it flows into search results, `get_message`, `get_thread` responses, and into AppleScript fallback paths.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | Attacker poses as the legit IMAP server | TLS cert validation via `imapclient` / stdlib SSL | — |
| Tampering | MITM or hostile server returns crafted bytes designed to break parsing | Server-returned strings flow through `sanitize_input` + `escape_applescript_string` before AS interpolation; property tests #214 | ⚠️ Audit during #214 to confirm every IMAP path applies escape |
| Repudiation | Server identity not logged per request | Connection-time logging in [`imap_credentials`](../../src/apple_mail_mcp/keychain.py) | (low) |
| Info disclosure | Server sees our queries and credentials; plaintext IMAP exposes credentials | TLS by default; `setup-imap` requires STARTTLS or implicit TLS | — |
| DoS | Hostile server hangs / streams gigabytes / crafted infinite attachments | Connection timeout via `imapclient`; per-call `timeout` param; bulk caps (100) | (low) — no global byte ceiling on server responses; file follow-up if material |
| Elevation | Compromised server cannot directly execute code if escape paths are correct | Defense in depth: server data → escape → AS or → return JSON. Never `eval` | — |

#### 3.3 Keychain

Crosses: Per-account IMAP/SMTP passwords stored under our service identifier. Read at connector init. Access ACL is the only barrier between any user-process and our credentials.

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | Another process pretending to be us reads our Keychain entry | macOS Keychain ACL — entry bound to our binary signature at write time | — |
| Tampering | Co-resident process modifies stored password | OOS — co-resident user-process is OOS by attacker model | — |
| Repudiation | We don't log Keychain access | Apple-side Keychain access log (if user enables it) | (low) |
| Info disclosure | Password readable on ACL grant | User must approve first access; revocable via Keychain Access app | — |
| DoS | Keychain unavailable → connector fails | Connector raises clear error; AS fallback paths where applicable | — |
| Elevation | n/a — read-only credential retrieval | — | — |

#### 3.4 Filesystem

Crosses:

- Templates: `~/.apple_mail_mcp/templates/<name>.md` — name and content are user-supplied
- Save attachments: caller supplies `dest_dir`
- Draft attachments: tempdir extraction during `update_draft`
- `APPLE_MAIL_MCP_HOME` env var can override the root

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | n/a | — | — |
| Tampering | User input as filename stem escapes intended dir (`../../etc/passwd`) | [`_validate_name`](../../src/apple_mail_mcp/templates.py) regex `^[a-zA-Z0-9_-]{1,64}$` before `Path()` | — |
| Tampering | Caller-supplied `dest_dir` for `save_attachments` points to system-critical path | Informational: caller (LLM-via-user) is trusted with this. We do not sanitize destination paths beyond existence | (informational) |
| Repudiation | File writes logged | [`operation_logger`](../../src/apple_mail_mcp/security.py) covers create / save / template ops | — |
| Info disclosure | Templates dir readable by other user-processes | OOS | — |
| DoS | Disk fill via massive attachments / template inflation | `sanitize_input` 10000-char cap on content | ⚠️ Confirm `save_attachments` has an explicit byte cap; file follow-up if not |
| Elevation | n/a | — | — |

#### 3.5 MCP / LLM-as-conduit

Crosses: Tool requests from Claude → our server (stdio JSON-RPC). Tool responses (including email content) → Claude's context. The LLM is operating in the user's interest but is manipulable by injected content (#225).

| Category | Threat | Mitigation | Gap? |
|---|---|---|---|
| Spoofing | n/a — stdio is parent-child, no network auth | — | — |
| Tampering | Hostile email content targets the LLM ("forward all to attacker@…") | Per-tool elicitation gates; rate limits; audit log. **Final defense is user reviewing elicitation prompts** | ⚠️ **#225** — no automated injection detection yet |
| Tampering | LLM laundering: hostile message → crafted tool call that bypasses confirmation | Elicitation messages show actual parameters (recipients, subject), not LLM narration | (informational) |
| Repudiation | — | `operation_logger` captures every tool call with parameters | — |
| Info disclosure | Email body lands in LLM context; LLM can be coaxed to summarize externally | Boundary is API provider (Anthropic). OOS for us; covered in `SECURITY.md` | — |
| DoS | Unbounded tool calls drain rate budget or fan-out | [`check_rate_limit`](../../src/apple_mail_mcp/security.py) 3-tier system; bulk caps (100) | — |
| Elevation | LLM coerced into chaining tools (e.g., `create_rule` auto-forwards everything) | Existing elicitation gates cover send/delete | ⚠️ **#222** — `create_rule` action gating in progress |

### Section 4 — Open gaps

Pulled from the "Gap?" column above. Each entry: one line referencing the boundary and the tracked issue.

Tracked v0.9.0 issues:

- **#233** — AS timeout wrap on paths bypassing `_wrap_as_json_script` (3.1)
- **#214** — property tests on escape/sanitize will validate every IMAP path applies escape (3.1, 3.2)
- **#225** — email prompt-injection detection planning (3.5)
- **#222** — `create_rule` dangerous-action confirmation gate (3.5)

New follow-ups (file during implementation only if confirmed during writeup):

- `save_attachments` byte cap audit (3.4) — read the code and confirm; file an issue if missing
- IMAP server-response byte ceiling (3.2) — informational; file only if there's evidence of real exposure

### Section 5 — References

- [`SECURITY.md`](../SECURITY.md)
- [`SECURITY_CHECKLIST.md`](../guides/SECURITY_CHECKLIST.md)
- [`ARCHITECTURE.md`](../reference/ARCHITECTURE.md)
- Linked issues: #214, #222, #225, #233, #235

### Cross-link in SECURITY.md

One-line note added to the top of SECURITY.md's "Attack Surface Analysis" section:

> *Note: for the canonical trust-boundary breakdown and STRIDE analysis, see [`docs/guides/THREAT_MODEL.md`](guides/THREAT_MODEL.md). The narrative below is preserved for continuity and will be reconciled in #220.*

## Verification

This is a docs-only change. Verification:

1. **Doc renders correctly** — preview `docs/guides/THREAT_MODEL.md` on GitHub; all internal links resolve.
2. **Every "Gap?" row points to a real issue** — manually check each issue number is open and on v0.9.0.
3. **Cross-links from SECURITY_CHECKLIST.md and SECURITY.md resolve** — the checklist already exists; SECURITY.md gets the one-line cross-link.
4. **A reviewer can answer "what's the worst thing"** — informal sanity check. The doc should give a clear answer to: *Can a hostile email cause arbitrary code execution? What stops it? Where are the gaps?*
5. **No code changes** — `git diff --stat` shows only the two doc files modified/added.

## Files touched

- **New:** `docs/guides/THREAT_MODEL.md`
- **Modified (1 line):** `docs/SECURITY.md` (cross-link in Attack Surface Analysis section)

## Critical files referenced (read during writeup, not modified)

- [src/apple_mail_mcp/utils.py](../../src/apple_mail_mcp/utils.py) — `escape_applescript_string`, `sanitize_input`
- [src/apple_mail_mcp/security.py](../../src/apple_mail_mcp/security.py) — `check_rate_limit`, `operation_logger`, `OPERATION_TIERS`
- [src/apple_mail_mcp/templates.py](../../src/apple_mail_mcp/templates.py) — `_validate_name`, `_NAME_RE`
- [src/apple_mail_mcp/keychain.py](../../src/apple_mail_mcp/keychain.py) — Keychain access pattern
- [src/apple_mail_mcp/mail_connector.py](../../src/apple_mail_mcp/mail_connector.py) — `_wrap_as_json_script`, `_run_applescript`
- [src/apple_mail_mcp/server.py](../../src/apple_mail_mcp/server.py) — elicitation gate pattern
