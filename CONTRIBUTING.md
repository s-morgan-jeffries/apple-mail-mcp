# Contributing to Apple Mail MCP

## Setup

```bash
git clone https://github.com/s-morgan-jeffries/apple-mail-mcp.git
cd apple-mail-mcp
uv sync --dev
./scripts/install-git-hooks.sh
```

## Development Workflow

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
2. Tests cover new code (aim for 100% on new features)
3. If you modified AppleScript, include integration tests
4. Update `docs/reference/TOOLS.md` if you added/changed a tool
5. PR description references the issue (`Closes #N`)

## Coding Standards

- **Type annotations** on all functions (mypy strict mode)
- **Docstrings** on all public functions (Args, Returns, Raises)
- **ruff** for linting and formatting (line length: 100)
- **Structured responses**: `{"success": bool, "error": str, "error_type": str}`
- **Security checklist** for every new feature (see `.claude/CLAUDE.md`)

## Testing Requirements

- Unit tests mock at `_run_applescript()` boundary
- Integration tests run against real Mail.app (opt-in via `--run-integration`)
- Coverage enforced: `fail_under = 60` (target: 90%)

## Release Process

Releases follow a 12-phase process documented in `.claude/skills/release/SKILL.md`. CHANGELOG is only updated on release branches.
