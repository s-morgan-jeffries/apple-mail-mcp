# Cyclomatic Complexity

This project enforces a cyclomatic complexity (CC) ceiling via `./scripts/check_complexity.sh`, run as part of `make check-all` and in CI.

## Threshold

**CC ≤ 20** per function / method. Any function with CC > 20 fails the build.

The ceiling is intentionally generous. The goal is not to chase a low complexity score for its own sake — it's to flag functions whose branching has grown beyond what can be reasoned about while reviewing. A CC of 20 is roughly the upper bound of "I can hold this whole control-flow graph in my head." Beyond that, extract.

Why not CC ≤ 10 or CC ≤ 15? Several MCP tool functions in `server.py` naturally reach CC 11–16 because they chain independent validation gates (safety gate, rate limit, input validation, file existence, elicitation, connector call). Each gate is a single `if X: return error` — simple in isolation but additive in CC. Splitting them would fragment the linear gate-then-act pattern that makes server tools readable.

## Currently complex functions (CC ≥ 11, all below threshold)

The functions below sit above CC 10 intentionally. When touching them, prefer adding one more gate over restructuring. If a change would push any of them above 20, extract a helper first.

| File | Function | CC | Why it's complex |
|---|---|---|---|
| [`server.py`](../../src/apple_mail_mcp/server.py) | `send_email_with_attachments` | 16 | Seven sequential gates before sending: test-mode safety, rate limit, path→Path conversion, recipient validation, attachment-existence check, elicitation, connector call. Each is a single `if`. |
| [`security.py`](../../src/apple_mail_mcp/security.py) | `check_test_mode_safety` | 12 | Three distinct safety categories (reply-message block, account-gated operations, send-to-reserved-domain), each with sub-conditions. Splitting would hide the unified "is this safe?" question. |
| [`mail_connector.py`](../../src/apple_mail_mcp/mail_connector.py) | `send_email_with_attachments` | 12 | Per-attachment size/type/existence validation loop plus three recipient-list builders (to/cc/bcc) plus the AppleScript `f"""..."""` assembly. |
| [`mail_connector.py`](../../src/apple_mail_mcp/mail_connector.py) | `forward_message` | 12 | Optional `body`, `cc`, `bcc` each add a branch; elicitation and connector-call paths are shared. |
| [`server.py`](../../src/apple_mail_mcp/server.py) | `send_email` | 11 | Same gate chain as `send_email_with_attachments` minus the attachment validation. |

Accepted because: each is a sequence of orthogonal gates or optional-parameter branches, not tangled logic. They read top-to-bottom and each branch has a clear exit.

## Adding a new documented exception

If a legitimately complex new function needs to exceed CC 20 (rare), do this in the same PR as the function:

1. Add a row to the table above: file, function, CC, and a one-sentence "why it's complex" that names the specific structural reason.
2. If CC is > 20, update `THRESHOLD` in [`scripts/check_complexity.sh`](../../scripts/check_complexity.sh) — this affects all functions, so prefer extracting a helper instead.
3. Mention the exception in the PR description so reviewers see it.

If you can't write a one-sentence justification, the function probably needs refactoring, not documentation.

## Checking complexity locally

```bash
./scripts/check_complexity.sh        # Gate check (CC > 20 fails)
uv run radon cc src/apple_mail_mcp -n B -s   # See all functions rated B (CC 6+) or worse
uv run radon cc src/apple_mail_mcp -n C -s   # See all functions rated C (CC 11+) or worse
```
