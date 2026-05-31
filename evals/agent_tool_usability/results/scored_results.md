# Blind Agent Eval Results

**Date:** 2026-05-31
**Scenarios:** 42 (3 under-specified / MANUAL: #32, #33, #34 → not scored)
**Version:** v0.9.0 (23 tools — drafts lifecycle, templates, rule CRUD, IMAP fast paths)
**Runs:** Claude 1 run (Claude Code subagent, deterministic). OpenRouter models 5 runs each @ temperature=0.
**Scoring:** PASS=2, PARTIAL=1, FAIL=0, MANUAL=not scored. Rule-based regex scorer
(`score_response_regex`). Max per run over 39 scored scenarios = 78.
**Context:** Models receive *only* the server instructions + tool descriptions
(`tool_descriptions.md`, generated from the live FastMCP server — see `generate_descriptions.py`).

## Summary

| Model | Score (MANUAL excl.) | PASS% | PARTIAL | FAIL | Notes |
|-------|----------------------|-------|---------|------|-------|
| DeepSeek V3 0324 (5 runs) | 387/390 | 99% (193/195) | 1 | 1 | |
| Claude Sonnet 4.6 (subagent, 1 run) | 76/78 | 97% (38/39) | 0 | 1 | deterministic |
| Llama 3.3 70B Instruct (5 runs) | 371/390 | 92% (179/195) | 13 | 3 | |
| Qwen 2.5 72B Instruct (5 runs) | 367/390 | 91% (177/195) | 13 | 5 | |
| Mistral Large 2411 (5 runs) | 361/394 | 90% (177/197) | 7 | 4 | |

All five models land **90–99%** — the v0.9.0 tool descriptions are clear enough for a blind,
unbriefed model to pick the right tool in nearly every scenario.

## Key Findings

**1. Descriptions ship clear.** Run blind (server instructions + tool descriptions only, no codebase
access), every model selects the correct tool and critical parameters on ~9 of every 10 scenarios;
DeepSeek and Claude are essentially perfect. The descriptions are now **generated from the live
server** (`generate_descriptions.py` / `make eval-descriptions`) so they can't silently drift from
the shipped surface again — the previous hand-maintained copy had rotted to ~9 of 23 tools.

**2. The one cross-model miss is scenario #3** ("which account has the most unread"): all models
reach for `search_messages(read_status=false)` per account rather than reading `unread_count` from
`list_mailboxes` (the cheaper, intended path — no message fetch). Both are workable; the divergence
suggests `list_mailboxes`' `unread_count` field could be surfaced more prominently in its
description. Not a tool-selection error so much as an efficiency one. (It's also stochastic — the
open models pass it on most runs.)

**3. Sub-PASS is mostly parameter-formatting PARTIALs**, not tool-selection errors — Llama and Qwen
(~13 PARTIAL over 5×42) and Mistral (7) occasionally format a parameter loosely. Tool *selection* is
near-perfect across the board.

**4. MANUAL (correct behavior):** #32 ("delete all my old emails"), #33 ("email John" — ambiguous
recipient), #34 ("archive everything") are under-specified; the right move is to ask for
clarification, which the scorer treats as not-scored.

## Notes
- Claude scored via Claude Code subagent (Sonnet 4.6), blind: given only the generated descriptions,
  forbidden from reading the repo. OpenRouter models run at `temperature=0`, 5 runs each.
- Raw `raw_*.json` dumps stay git-ignored; only this summary is committed.
- Re-run anytime: `make eval-descriptions` (refresh inputs) then `make eval-tools` (open-weight
  models via OpenRouter; needs the `apple-mail-mcp-evals` / `openrouter` Keychain key). The Claude
  column is produced separately via subagent.
