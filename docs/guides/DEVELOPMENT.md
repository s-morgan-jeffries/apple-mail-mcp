# Development Workflow

How to develop on the Apple Mail MCP server. For *installing/configuring* the server as a user, see
the [README](../../README.md). For coding standards and the PR process, see
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## Environment

The project uses [`uv`](https://docs.astral.sh/uv/). One command sets up a virtualenv with all dev
dependencies:

```bash
uv sync --dev
```

Run anything in that environment with `uv run …` (e.g. `uv run pytest`, `uv run python -m
apple_mail_mcp.server`). `uv sync` also installs the `apple-mail-fast-mcp` console script into
`.venv/bin/`.

## The canonical check: `make check-all`

`make check-all` is the single pre-push gate. It runs, stopping on the first failure:

| Step | Command | Notes |
|------|---------|-------|
| Lint | `uv run ruff check src/ tests/` | blocking |
| Type check | `uv run mypy src/` | blocking (strict) |
| Unit tests | `uv run pytest -m "not integration and not e2e and not benchmark"` | blocking; coverage ≥ 90% |
| Complexity | `./scripts/check_complexity.sh` | blocking — see below |
| Version sync | `./scripts/check_version_sync.sh` | versions consistent across files |
| Client/server parity | `./scripts/check_client_server_parity.sh` | every public connector method is exposed |

CI runs the same lint / type / unit / complexity / version / parity gates (`.github/workflows/test.yml`).
**Integration, e2e, and benchmark tests are *not* in CI** (they need real Mail.app) — run those
locally; see [TESTING.md](TESTING.md).

## Core conventions

- **TDD always** — RED → GREEN → REFACTOR. Write the failing test first.
- **Backend + frontend together** — a feature touches *both* `mail_connector.py` (the connector
  method) and `server.py` (the MCP tool). The parity script (`check_client_server_parity.sh`) enforces
  that every public connector method is exposed as a tool.
- **Structured responses** — every tool returns `{"success": bool, …}`; errors carry `error` +
  `error_type`. No exception reaches the LLM.
- **Sanitize twice** — all user input goes through `sanitize_input()` then
  `escape_applescript_string()` before it touches AppleScript. See
  [SECURITY_CHECKLIST.md](SECURITY_CHECKLIST.md).
- **Touched AppleScript → integration test** — unit tests mock `_run_applescript()` and cannot catch
  AppleScript bugs (see [TESTING.md](TESTING.md)).

## Cyclomatic-complexity gate

`./scripts/check_complexity.sh` fails any function over **CC 20** (via `radon`, run through `uv`).
Genuinely-complex functions can be admitted via the per-function `ALLOWLIST` in that script — a
`(filename, qualified_name) → max_CC` map that ratchets (lowering an entry after a refactor is free;
raising one needs justification). See [COMPLEXITY.md](COMPLEXITY.md).

## Branch & changelog conventions

- Branch names: `{type}/issue-{num}-{description}` — e.g. `fix/issue-99-timeout`,
  `feature/issue-42-thread-support`.
- **`CHANGELOG.md` is only updated on release branches**, never on feature branches (the release
  skill owns it).

## Common make targets

```bash
make test              # unit tests (~1s, mocked AppleScript)
make test-integration  # real Mail.app (needs a test account; MAIL_TEST_MODE=true)
make test-e2e          # MCP tool dispatch tests
make lint / format / typecheck
make coverage
make benchmark         # perf benchmarks (opt-in; needs real Mail) — see BENCHMARKING.md
make eval-descriptions # regenerate the blind-eval tool descriptions from the live server
```

## See also

- [ARCHITECTURE.md](../reference/ARCHITECTURE.md) — dispatch model, dual-emit IDs, drafts lifecycle, thread tiers
- [APPLESCRIPT_GOTCHAS.md](../reference/APPLESCRIPT_GOTCHAS.md) — JSON-via-ASObjC, escaping, known limits
- [TESTING.md](TESTING.md) — test categories, the manual-e2e policy, benchmarks, blind evals
- [TOOLS.md](../reference/TOOLS.md) — full tool reference
