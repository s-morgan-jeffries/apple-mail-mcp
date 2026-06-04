# Contributing to Apple Mail MCP

## A note on early contributor PRs

Between December 2025 and April 2026, several PRs from external contributors — including @ericboehs, @kemotaha, @tylew, and @jpgrosen — were closed without comment or merge. The honest explanation is that Claude Code, running on my behalf, was aggressively closing PRs without surfacing them for my review, and I didn't have a workflow that would catch them. That's a process failure I own. Several of those PRs correctly diagnosed bugs that were re-fixed independently on `main` weeks later than they could have been.

To everyone whose work got that treatment: I'm sorry. The project is healthier because you flagged these issues, even when the credit didn't follow.

The process changes shipping in this milestone (#87, #88, #90) — issue-first guidance below, an updated PR template, and a post-merge workflow that surfaces open contributor PRs — are aimed at making sure future PRs get a real evaluation: merged, redirected, or closed with clear reasoning. Never silently.

## Setup

```bash
git clone https://github.com/s-morgan-jeffries/apple-mail-fast-mcp.git
cd apple-mail-fast-mcp
uv sync --dev
./scripts/install-git-hooks.sh
```

## Development Workflow

0. **Before you start coding,** open an issue (or comment on an existing one) describing what you plan to fix or build. This lets us flag duplicate or in-flight work and saves you from rebases or wasted effort.

   **Claiming an existing issue:** if you want to take an existing issue, leave a comment saying so. If the issue has **no assignee** AND no comment indicating someone's already on it, you can start work in parallel — you don't need to wait for a maintainer response before beginning. The maintainer typically replies within ~24h to acknowledge your claim; if you don't hear back within ~3 days, feel free to @-mention `@s-morgan-jeffries` directly and keep going. GitHub only lets repository collaborators be set as assignees, so for external contributors the canonical "this is taken" signal is a **maintainer comment acknowledging the claim** (an assignee is used when the claimant is a collaborator). Either way, please respect an issue someone has already claimed.
1. Create a branch: `git checkout -b feature/issue-N-description`
2. Write tests first (TDD): RED -> GREEN -> REFACTOR
3. Implement backend (`mail_connector.py`) and frontend (`server.py`) together
4. Run checks: `make check-all`
5. **If your PR touches IMAP or AppleScript code paths** — `imap_connector.py`, `mail_connector.py` AppleScript bodies/wrappers, or tools gated by `_elicit_confirmation` — also run `make test-e2e` before pushing (requires `MAIL_TEST_MODE=true`; the happy-path dispatch tests are mocked and need no account, but the full suite needs a test Mail.app account). **CI does not run e2e** — they need Mail.app — so a stale e2e failure on `main` is only caught by someone running this locally.
6. Open a PR against `main`

## Branch Convention

`{type}/issue-{num}-{description}` — always tied to an issue.

Types: `feature/`, `fix/`, `docs/`

## Make Targets

```bash
make test              # Unit tests
make lint              # Ruff linting
make format            # Ruff formatting
make typecheck         # Mypy strict mode
make check-all         # All checks
make coverage          # Coverage report
make test-integration  # Real Mail.app tests
```

## Pull Request Process

1. All CI checks pass (`make check-all`)
2. Tests for new code:
   - **New features:** unit tests covering the happy path and error branches.
   - **Bug fixes:** include a regression test that fails before your fix and passes after.
   - **AppleScript changes:** an integration test under `tests/integration/`.
3. Update `docs/reference/TOOLS.md` if you added/changed a tool
4. PR description references the issue (`Closes #N`)

## Coding Standards

- **Type annotations** on all functions (mypy strict mode)
- **Docstrings** on all public functions (Args, Returns, Raises)
- **ruff** for linting and formatting (line length: 100)
- **Structured responses**: `{"success": bool, "error": str, "error_type": str}`
- **Security checklist** for every new feature (see [`docs/guides/SECURITY_CHECKLIST.md`](docs/guides/SECURITY_CHECKLIST.md))
- **Cyclomatic complexity** ceiling of CC ≤ 20 per function (see [`docs/guides/COMPLEXITY.md`](docs/guides/COMPLEXITY.md))

## Testing Requirements

- Unit tests mock at `_run_applescript()` boundary
- Integration tests run against real Mail.app (opt-in via `--run-integration`)
- Coverage enforced: `fail_under = 90`

## Release Process

Releases follow a 12-phase process documented in `.claude/skills/release/SKILL.md`. CHANGELOG is only updated on release branches.
