# MCP Connector Playbook

> **Purpose:** Project-agnostic guide for building MCP connectors that bridge Claude and macOS applications via AppleScript/native frameworks.
>
> **Derived from:** OmniFocus MCP (v0.13.2) and Apple Calendar MCP (v0.9.0) — two mature, production MCP connectors.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Project Structure](#2-project-structure)
3. [Testing Strategy](#3-testing-strategy)
4. [Security](#4-security)
5. [CI/CD & Release](#5-cicd--release)
6. [CLAUDE.md & Skills](#6-claudemd--skills)
7. [Code Quality](#7-code-quality)
8. [Performance](#8-performance)
9. [Documentation](#9-documentation)
10. [Divergences & Recommendations](#10-divergences--recommendations)
11. [Golden Rules](#11-golden-rules)

---

## 1. Architecture

### Two-File Separation

Every MCP connector has exactly two source files with clear responsibilities:

| File | Responsibility | Thickness |
|------|---------------|-----------|
| `server.py` / `server_fastmcp.py` | MCP plumbing: tool registration, input validation, output formatting, error wrapping | Thin |
| `{domain}_connector.py` | Domain logic: all business logic, script generation, query building, filtering, safety checks | Thick |

**The server never contains business logic. The connector never formats for LLMs.**

### Supporting Modules

| Module | Responsibility | Dependencies |
|--------|---------------|-------------|
| `exceptions.py` | Custom exception hierarchy | None |
| `utils.py` | Pure functions: escaping, parsing, validation | None (stdlib only) |
| `security.py` | Validation, audit logging, confirmation flows | `utils.py`, `exceptions.py` |

**Rule:** Dependency arrows point inward. `utils.py` and `exceptions.py` are leaf nodes.

### Entry Point Pattern

```python
# server.py
from fastmcp import FastMCP
from .{domain}_connector import {Domain}Connector

mcp = FastMCP("{project-name}", instructions="""
Domain-specific context that helps LLMs make informed decisions.
""")

connector = {Domain}Connector()  # Lazy singleton

def main() -> None:
    mcp.run()
```

```toml
# pyproject.toml
[project.scripts]
{project-name} = "{package}.server:main"
```

### Tool Registration Pattern

```python
@mcp.tool()
def tool_name(
    required_param: str,
    optional_param: str | None = None,
) -> dict[str, Any]:
    """Tool description (shown to LLM).

    Args:
        required_param: Description
        optional_param: Description (optional)
    """
    try:
        result = connector.backend_method(required_param, optional_param)
        return {"success": True, "result": result}
    except DomainError as e:
        return {"success": False, "error": str(e), "error_type": "domain_error"}
```

### Response Format (Universal)

**Success:** `{"success": true, "result": ..., "<context_fields>": ...}`
**Error:** `{"success": false, "error": "Human-readable message", "error_type": "category"}`

**No exceptions reach the LLM.** All errors caught at server level, converted to structured dicts.

### Single Point of External I/O

All subprocess execution goes through one method (e.g., `_run_applescript(script: str) -> str`). This is:
- The mock boundary for unit tests
- The single place where timeout, error parsing, and subprocess management happen
- Never called directly from the server layer

---

## 2. Project Structure

### Canonical Directory Layout

```
{project-name}/
├── .claude/
│   ├── CLAUDE.md                    # Dense reference card
│   ├── settings.json                # Claude Code permissions
│   ├── commands/
│   │   └── merge-and-status.md      # Merge + milestone status
│   └── skills/
│       ├── release/SKILL.md
│       ├── applescript-{domain}/SKILL.md
│       ├── api-design/SKILL.md
│       └── integration-testing/SKILL.md
├── .github/
│   ├── dependabot.yml
│   ├── ISSUE_TEMPLATE/
│   │   ├── config.yml               # Require templates, no blank issues
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── workflows/
│       ├── test.yml                  # CI: unit tests on push/PR
│       ├── release.yml               # CD: GitHub Release on final tag
│       └── release-hygiene.yml       # Quality gate on RC tags
├── src/{package_name}/
│   ├── __init__.py                   # __version__ + exports
│   ├── server.py                     # FastMCP tools (thin)
│   ├── {domain}_connector.py         # Domain logic (thick)
│   ├── exceptions.py                 # Exception hierarchy
│   ├── security.py                   # Validation, audit, confirmation
│   └── utils.py                      # Pure utility functions
├── tests/
│   ├── conftest.py                   # Fixtures, markers, CLI options
│   ├── unit/                         # Mocked tests
│   ├── integration/                  # Real app tests
│   ├── e2e/                          # Full MCP stack tests
│   └── benchmarks/                   # Performance tests
├── evals/
│   └── agent_tool_usability/         # Blind LLM eval scenarios
├── scripts/
│   ├── check_complexity.sh
│   ├── check_version_sync.sh
│   ├── check_client_server_parity.sh
│   ├── check_dependencies.sh
│   ├── check_applescript_safety.sh
│   ├── create_tag.sh
│   ├── install-git-hooks.sh
│   ├── git-hooks/{pre-commit,pre-push,pre-tag}
│   └── hooks/{pre_bash.sh,post_bash.sh,session_start.sh}
├── docs/
│   ├── guides/                       # How-to guides
│   └── reference/                    # Technical reference
├── Makefile
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── SECURITY.md
├── pyproject.toml
└── uv.lock
```

### Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Project directory | kebab-case | `apple-mail-mcp` |
| Python package | snake_case | `apple_mail_mcp` |
| Source modules | snake_case | `mail_connector.py` |
| Test files | `test_` prefix + feature | `test_attachments.py` |
| Test classes | `Test` + PascalCase | `TestMoveMessages` |
| Exceptions | PascalCase + Error | `MailAccountNotFoundError` |
| Scripts | `check_*.sh` (validation), `setup_*.sh` (init) | `check_complexity.sh` |
| Branch names | `{type}/issue-{num}-{description}` | `feature/issue-42-batch-tags` |

---

## 3. Testing Strategy

### Five Test Levels

| Level | Location | What it validates | When to run |
|-------|----------|-------------------|-------------|
| **Unit** (mocked) | `tests/unit/` | Python logic, parsing, validation | Every change (`make test`) |
| **Integration** (real app) | `tests/integration/` | AppleScript correctness, app API behavior | New AppleScript code (`make test-integration`) |
| **E2E** (full MCP) | `tests/e2e/` | Tool registration, parameter passing, end-to-end flow | New/modified tools (`make test-e2e`) |
| **Benchmarks** | `tests/benchmarks/` | Performance regression detection | Changes to fetch/filter paths |
| **Blind Agent Eval** | `evals/agent_tool_usability/` | Tool usability by LLMs | Before release if descriptions changed |

### Mocking Approach

Unit tests mock at the AppleScript boundary:

```python
# Mock _run_applescript for connector-level tests
@patch.object(Connector, "_run_applescript")
def test_method(self, mock_run, connector):
    mock_run.return_value = "expected|output|format"
    result = connector.some_method("param")
    assert result == expected

# Mock get_client() for server-level tests
@patch('{package}.server.connector')
def test_tool(self, mock_connector):
    mock_connector.some_method.return_value = {"key": "value"}
    result = tool_name("param")
    assert result["success"] is True
```

### Coverage Requirements

- **Enforced in CI:** `fail_under` in pyproject.toml (start at current baseline, ramp to 90%)
- **Per-file minimum:** 70%
- **New features:** 100% coverage
- **server.py must have its own tests** — testing through the connector only leaves the tool layer uncovered

### Hard Rule

**If you wrote or modified an AppleScript string, integration tests must cover that operation before merge.**

---

## 4. Security

### Input Sanitization Pipeline

Every user input goes through two functions before AppleScript insertion:

```python
# 1. Remove null bytes, enforce length limit
sanitized = sanitize_input(user_input)

# 2. Escape for AppleScript string context
safe = escape_applescript_string(sanitized)
```

### Security Checklist (Per Feature)

- [ ] All inputs validated and sanitized
- [ ] AppleScript strings properly escaped
- [ ] File paths validated (no traversal)
- [ ] Size limits enforced
- [ ] User confirmation for destructive ops
- [ ] Operation logged
- [ ] Error messages don't leak sensitive info
- [ ] Security tests written

### Test Database/Account Safety

Destructive operations require environment variables:
```bash
{DOMAIN}_TEST_MODE=true
{DOMAIN}_TEST_{TARGET}={name}
```

Each destructive operation verifies the target via the native API BEFORE proceeding. Production data loss is prevented by architecture, not discipline.

---

## 5. CI/CD & Release

### Three Workflows

1. **`test.yml`** — On push/PR to main: lint, typecheck, test with coverage, validation scripts
2. **`release.yml`** — On final tag (`v*`, not `v*-rc*`): create GitHub Release, close milestone
3. **`release-hygiene.yml`** — On RC tag (`v*-rc*`): full validation suite, milestone status check

### Validation Scripts

| Script | Purpose |
|--------|---------|
| `check_version_sync.sh` | Version matches across pyproject.toml, `__init__.py`, CLAUDE.md |
| `check_complexity.sh` | Cyclomatic complexity gate via radon |
| `check_client_server_parity.sh` | Every connector method has an @mcp.tool() |
| `check_dependencies.sh` | Dependency vulnerability scan |
| `check_applescript_safety.sh` | Unsafe AppleScript pattern detection |

**Rule:** If it can be checked automatically, it runs in CI.

### Makefile as Developer Interface

Nobody should need to remember pytest flags. Standard targets:
`make test`, `make test-integration`, `make test-e2e`, `make lint`, `make typecheck`, `make check-all`, `make coverage`, `make clean`

### Release Process

```
Feature Development → PR → main
                              ↓
                    Create release/vX.Y.Z branch
                    Bump version + CHANGELOG
                    Tag vX.Y.Z-rc1
                              ↓
                    release-hygiene.yml validates
                              ↓ (fix + rc2 if needed)
                    PR to main → merge (rebase)
                    Tag vX.Y.Z on main
                              ↓
                    release.yml creates GitHub Release
                    Close milestone, create next
```

**Key rules:**
- CHANGELOG only updated on release branches, never feature branches
- Tags created on main AFTER merge (ensures `git describe` works)
- Rebase merge for release PRs (linear history)

### Branch Convention

`{type}/issue-{num}-{description}` — always tied to an issue.

### Milestone-Driven Development

- Each version gets a GitHub milestone
- Issues assigned to milestones
- Release skill checks milestone status
- CI auto-closes milestones on tag push and creates the next one

---

## 6. CLAUDE.md & Skills

### CLAUDE.md Format

Dense, reference-card format optimized for Claude Code's context window:

1. **Header** — project name, stack, version, test count, coverage %
2. **Commands** — make targets (most frequently needed)
3. **API Surface** — tool names grouped by category
4. **Core Principles** — bullet points, not paragraphs
5. **Domain Gotchas** — hard-won platform knowledge
6. **Performance Constraints** — timing data, bottleneck identification
7. **Skills** — available skills and when to load them
8. **Key Files** — where to look for important code

### Skill Architecture

Skills live in `.claude/skills/{skill-name}/SKILL.md`. They encode hard-won knowledge that can't be derived from reading the code.

**Required skills for every MCP connector:**
1. **release** — 12-phase release orchestration
2. **applescript-{domain}** — Platform-specific bugs and workarounds
3. **api-design** — Decision tree for when to add new functions vs extend existing
4. **integration-testing** — Why mocked tests miss bugs, test setup

**Skill design principles:**
- Trigger-rich descriptions (multiple "Use when..." scenarios)
- Bug stories over rules (explain WHY via the bug that created the rule)
- Concrete BAD/GOOD code pairs
- Self-contained (no cross-referencing)
- End with checklists or decision trees

---

## 7. Code Quality

### Tools

| Tool | Purpose | Config Location |
|------|---------|----------------|
| ruff | Linting (replaces flake8+isort) | `pyproject.toml [tool.ruff]` |
| mypy or pyright | Type checking | `pyproject.toml` |
| radon | Cyclomatic complexity | `scripts/check_complexity.sh` |
| pip-audit | Dependency vulnerabilities | `scripts/check_dependencies.sh` |

### Type Annotations

All functions must have type annotations. Strict mode preferred.

### Logging

Standard Python `logging` module. `logger.info()` for operations, `logger.error()` before error returns.

### Audit Logging

Separate from Python logging. `OperationLogger` singleton tracks all tool operations with timestamps for audit trail.

---

## 8. Performance

### The Core Insight

**The bottleneck is per-property IPC cost.** Each AppleScript property read costs ~17ms (inter-process communication via subprocess). All performance work reduces round-trips.

### Patterns

1. **Eliminate N+1 queries** — Batch fetch, not per-item fetch
2. **Use `whose` clauses** — Server-side filtering is 20-30x faster than manual iteration
3. **Minimize data per call** — Fetch only needed properties
4. **Single script per operation** — One subprocess call for N items

### Caching

Minimal by design. Application data is mutable; caching risks stale data. Only cache within a single operation.

### Benchmarking

```python
class BenchmarkResult:
    """mean, stdev, min, max, median, CV%, cold start detection"""

# 5 iterations, 5x threshold over documented baseline
assert br.mean < baseline * 5, f"Regression: {br.mean:.2f}s"
```

---

## 9. Documentation

### Required Files

| File | Audience | Content |
|------|----------|---------|
| `README.md` | Users/contributors | Badges, features, install, config, architecture |
| `CHANGELOG.md` | All | Keep a Changelog format, categories: Added/Changed/Fixed/Removed |
| `CONTRIBUTING.md` | Contributors | Dev workflow, Makefile targets, PR process |
| `SECURITY.md` | Security | Policy, reporting, SLAs |
| `docs/guides/` | Developers | How-to guides (development, testing) |
| `docs/reference/` | Developers | Technical reference (architecture, gotchas) |

### Version Synchronization

Version appears in three files (must be synced via `check_version_sync.sh`):
1. `pyproject.toml` (authoritative source)
2. `src/{package}/__init__.py`
3. `.claude/CLAUDE.md`

---

## 10. Divergences & Recommendations

Where the sibling projects differ, these are the recommended choices:

| Decision | OmniFocus | Calendar | Recommendation | Rationale |
|----------|-----------|----------|----------------|-----------|
| Package manager | uv | pip | **uv** | Faster, deterministic lockfile |
| Build backend | setuptools | hatchling | **hatchling** | Simpler config, already working in most projects |
| Type checker | pyright (basic) | mypy (strict) | **mypy** | Stricter by default, better CI integration |
| Server filename | `server_fastmcp.py` | `server_fastmcp.py` | **`server.py`** acceptable | Avoid touching application imports on existing projects |
| Response format | Human-readable strings | Structured dicts | **Structured dicts** | More machine-friendly, easier to parse |
| Coverage threshold | 90% | 90% | **Start at current baseline** | Ramp to 90% over time, don't block CI immediately |

---

## 11. Golden Rules

1. **TDD Always.** Write tests first (RED), implement minimal code (GREEN), refactor (REFACTOR). Never skip tests before implementation.

2. **Two-File Architecture.** Server handles MCP plumbing (thin). Connector handles domain logic (thick). The server never contains business logic; the connector never formats for LLMs.

3. **Backend + Frontend Together.** Every feature touches the connector AND the server. Never implement one without the other. Verify with `check_client_server_parity.sh`.

4. **If You Touched AppleScript, Write Integration Tests.** Unit tests mock the subprocess boundary and CANNOT catch AppleScript bugs. Integration tests against the real app are mandatory.

5. **Sanitize Everything Twice.** All user input goes through `sanitize_input()` then `escape_applescript_string()` before script insertion. No exceptions.

6. **Single Point of External I/O.** All subprocess execution through one method. This is the mock boundary for unit tests.

7. **Safety by Default.** Destructive operations verify target via native API before proceeding. Test mode requires explicit environment variables.

8. **Consistent Response Format.** Every tool returns `{"success": true/false, ...}`. Errors include `error` (message) and `error_type` (category). Always structured data, never formatted text strings.

9. **Errors Never Reach the LLM as Exceptions.** All errors caught at server level, converted to structured responses.

10. **Skills Encode Hard-Won Knowledge.** Skills capture decision procedures, anti-patterns, and bug stories that can't be derived from code.

11. **Milestone-Driven Releases.** Each version gets a GitHub milestone. Release skill checks status. CI auto-manages milestones.

12. **Validation Scripts Run in CI.** Version sync, complexity, parity, dependency audit, AppleScript safety — all automated.

13. **Coverage Is Enforced, Not Aspirational.** `fail_under` in pyproject.toml. CI fails if coverage drops.

14. **Makefile Is the Developer Interface.** `make test`, `make lint`, `make check-all`. Nobody remembers pytest flags.

15. **Blind Agent Evals Prove Usability.** Test tool names and descriptions against multiple LLMs before release. Bad descriptions = tools LLMs can't use.
