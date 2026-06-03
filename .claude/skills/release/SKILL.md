---
name: release
description: Use when the user wants to release a new version. Orchestrates the full release workflow including milestone check, version bump, changelog generation, validation, tagging, and PR creation. Also use when discussing release planning or version management.
---

# Release Workflow (12 Phases)

## Phase 1: Milestone Check
- List open milestones via `gh api`
- Verify 0 open issues in target milestone
- Stop if issues remain (ask user to move/close)

## Phase 2: Version Number
- Read current version from pyproject.toml (authoritative)
- Milestone title = target version
- Confirm with user

## Phase 3: Create Release Branch
- `git checkout -b release/vX.Y.Z` from main

## Phase 4: Bump Version
Update ALL files (pyproject.toml is authoritative):
1. `pyproject.toml` — `version = "X.Y.Z"`
2. `src/apple_mail_mcp/__init__.py` — `__version__ = "X.Y.Z"`
3. `.claude/CLAUDE.md` — `**Version:** vX.Y.Z`
4. `README.md` — all version references

## Phase 5: Generate CHANGELOG
- Commits since last tag: `git log v{prev}..HEAD --oneline`
- PRs since last release: `gh pr list --state merged --search "merged:>YYYY-MM-DD"`
- Keep a Changelog format: `## [X.Y.Z] - YYYY-MM-DD`
- Categories: Added, Changed, Fixed, Removed

## Phase 6: Test Coverage Review
- Run coverage: `make coverage`
- Compare against `fail_under` in pyproject.toml
- Audit changed files for adequate coverage

## Phase 7: Code Review
- Launch `superpowers:code-reviewer` against cumulative diff
- Critical issues block release

## Phase 8: Documentation Review
- README, CLAUDE.md, CHANGELOG, docs/**, tool docstrings, skills
- The content-drift gate (`check_docs.sh`, Phase 9) automates the tool-set /
  removed-name / cross-ref / eval-description-sync parts; this phase is the
  human read for things it can't check (accuracy, tone, completeness).

## Phase 8.5: Refresh derived artifacts (mandatory, #288)
Derived artifacts rot silently between releases — refresh them against the
release commit so a stale snapshot can't ship:
1. `make eval-descriptions` — regenerate the blind-eval tool descriptions;
   commit if changed. (`check_docs.sh` also fails on drift here.)
2. Re-capture the benchmark baseline (#216): `MAIL_TEST_MODE=true
   MAIL_TEST_ACCOUNT=<acct> uv run pytest tests/benchmarks/ --run-benchmark
   --capture-baseline` (needs real Mail.app); commit the refreshed
   `baseline.json`.
3. Re-run the blind agent eval (#219) and refresh its scored snapshot (needs
   `OPENROUTER_API_KEY`); commit.

## Phase 9: Validation
Run ALL checks (stop on failure):
1. `./scripts/check_version_sync.sh`
2. `./scripts/check_client_server_parity.sh`
3. `./scripts/check_complexity.sh`
4. `make test`
5. `make test-e2e` — **mandatory** (requires `MAIL_TEST_MODE=true` + a test Mail.app account). CI excludes e2e, so this is the only gate that catches a stale e2e failure. A pre-existing failure on `main` is a **release-blocker**, not a known issue to ship around (#257).
6. `./scripts/check_dependencies.sh`
7. `./scripts/check_applescript_safety.sh`
8. `./scripts/check_docs.sh` — doc/artifact drift gate (tool-set coverage, removed-name, cross-refs, eval-description sync) (#288)

## Phase 10: Commit, Push, PR
- Commit: `"release: vX.Y.Z"`
- Push: `-u origin release/vX.Y.Z`
- PR to main via `gh pr create`

## Phase 11: Merge, Tag, Push Tag
- Rebase merge: `gh pr merge NNN --rebase --delete-branch`
- Tag on main: `./scripts/create_tag.sh vX.Y.Z`
- Push tag: `git push origin vX.Y.Z`
- Verify: `git describe --tags --abbrev=0`

## Phase 12: Close Milestone
- `gh api -X PATCH repos/{owner}/{repo}/milestones/{number} -f state=closed`

## Notes
- CHANGELOG only updated on release branches
- Tags created on main AFTER PR merge
- Use rebase merge (linear history required)
- Each phase has explicit stop conditions — if it fails, stop and report
