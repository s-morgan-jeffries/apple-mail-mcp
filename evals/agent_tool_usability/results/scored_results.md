# Blind Agent Eval Results

**Date:** 2026-06-07
**Scenarios:** 45 (3 under-specified / MANUAL: #32, #33, #34 → not scored). Adds v0.10.0 coverage: #43/#44 `get_attachment_content` (#250), #45 HTML draft `body_html` (#251).
**Version:** v0.10.0 (24 tools — adds `get_attachment_content` + HTML draft bodies on top of v0.9.x).
**Runs:** Claude 1 run (Claude Code subagent, deterministic). OpenRouter models 5 runs each @ temperature=0.
**Scoring:** PASS=2, PARTIAL=1, FAIL=0, MANUAL=not scored. Rule-based regex scorer
(`score_response_regex`). Max per run over 42 scored scenarios = 84.
**Context:** Models receive *only* the server instructions + tool descriptions
(`tool_descriptions.md`, generated from the live FastMCP server — see `generate_descriptions.py`).

## Summary

Score = points (PASS=2, PARTIAL=1, FAIL=0) ÷ max, with MANUAL scenarios excluded from both.

| Model | Score | % | PASS | PARTIAL | FAIL |
|-------|-------|---|------|---------|------|
| DeepSeek V3 0324 (5 runs) | 412/420 | 98.1% | 206 | 0 | 4 |
| Claude Sonnet 4.6 (subagent, 1 run) | 81/84 | 96.4% | 40 | 1 | 1 |
| Llama 3.3 70B Instruct (5 runs) | 401/420 | 95.5% | 194 | 13 | 3 |
| Qwen 2.5 72B Instruct (5 runs) | 394/420 | 93.8% | 189 | 16 | 5 |

All models land **94–98%** on the v0.10.0 surface — blind, unbriefed models pick the right tool in
nearly every scenario, including the two new features.

> **Mistral Large 2411 excluded this cycle.** `mistralai/mistral-large-2411` now returns
> `404 No endpoints found` on OpenRouter (the model was retired), so all 225 of its calls errored —
> no scoreable data. The `make eval-tools` model list needs updating to a current Mistral id (or a
> replacement); tracked as a follow-up.

## Key Findings

**1. The new v0.10.0 tools read clearly blind — the point of this refresh.** Across the three
working open models + Claude, the new-tool scenarios score essentially perfectly:
`get_attachment_content` (#43 "read inline, don't save"; #44 "summarize the PDF") and the HTML draft
`body_html` (#45) are PASS on 5/5 runs for DeepSeek and Qwen and for Claude. Models correctly prefer
`get_attachment_content` over `save_attachments` when the user doesn't want a disk copy, and set
`body_html` (not plain `body`) with `send_now` left off for an HTML *draft*. The only new-tool miss
is Llama on #44 (1 of 5 runs) — the indirect "summarize the PDF" framing, where it reached for a
different read path once.

**2. Descriptions ship clear overall.** Run blind (server instructions + tool descriptions only, no
codebase access), every model selects the correct tool and critical parameters on ~9–10 of every 10
scenarios; DeepSeek and Claude are essentially perfect. Descriptions are generated from the live
server (`make eval-descriptions`), so they can't silently drift from the shipped surface.

**3. The one cross-model miss is scenario #3** ("which account has the most unread"): models reach
for `search_messages(read_status=false)` per account rather than reading `unread_count` from
`list_mailboxes` (the cheaper, intended path). Both work; the divergence suggests `list_mailboxes`'
`unread_count` could be surfaced more prominently. An efficiency divergence, not a tool-selection
error — and stochastic (open models pass it on most runs).

**4. Sub-PASS is mostly parameter-formatting PARTIALs**, not tool-selection errors — Llama (13) and
Qwen (16, over 5×42) occasionally format a parameter loosely. Tool *selection* is near-perfect.

**5. MANUAL (correct behavior):** #32 ("delete all my old emails"), #33 ("email John" — ambiguous
recipient), #34 ("archive everything") are under-specified; the right move is to ask for
clarification, which the scorer treats as not-scored.

## Notes
- Claude scored via Claude Code subagent (Sonnet 4.6), blind: given only the generated descriptions
  + server instructions, forbidden from reading the repo. OpenRouter models run at `temperature=0`,
  5 runs each.
- Raw `raw_*.json` dumps stay untracked; only this summary is committed.
- Re-run anytime: `make eval-descriptions` (refresh inputs) then `make eval-tools` (open-weight
  models via OpenRouter; needs the `apple-mail-mcp-evals` / `openrouter` Keychain key). The Claude
  column is produced separately via subagent.
