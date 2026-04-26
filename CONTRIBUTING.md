# Contributing to Apple Mail MCP

## A note on early contributor PRs

Between December 2025 and April 2026, several PRs from external contributors — including @ericboehs, @kemotaha, @tylew, and @jpgrosen — were closed without comment or merge. The honest explanation is that Claude Code, running on my behalf, was aggressively closing PRs without surfacing them for my review, and I didn't have a workflow that would catch them. That's a process failure I own. Several of those PRs correctly diagnosed bugs that were re-fixed independently on `main` weeks later than they could have been.

To everyone whose work got that treatment: I'm sorry. The project is healthier because you flagged these issues, even when the credit didn't follow.

The process changes shipping in this milestone (#87, #88, #90) — issue-first guidance below, an updated PR template, and a post-merge workflow that surfaces open contributor PRs — are aimed at making sure future PRs get a real evaluation: merged, redirected, or closed with clear reasoning. Never silently.

## Setup

```bash
git clone https://github.com/s-morgan-jeffries/apple-mail-mcp.git
cd apple-mail-mcp
uv sync --dev
./scripts/install-git-hooks.sh
```

## Development Workflow

0. **Before you start coding,** open an issue (or comment on an existing one) describing what you plan to fix or build. This lets us flag duplicate or in-flight work and saves you from rebases or wasted effort.
1. Create a branch: `git checkout -b feature/issue-N-description`
2. Write tests first (TDD): RED -> GREEN -> REFACTOR
3. Implement backend (`mail_connector.py`) and frontend (`server.py`) together
4. Run checks: `make check-all`
5. Open a PR against `main`

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
