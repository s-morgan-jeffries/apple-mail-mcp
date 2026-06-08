# Release Artifacts

This project ships two **derived artifacts** that must be refreshed each release (Phase 8.5 of the [release skill](../../.claude/skills/release/SKILL.md), #288) but can't be regenerated in CI — they need resources the release driver may not have:

| Artifact | Needs | Refresh |
|---|---|---|
| `tests/benchmarks/baseline.json` | a real Mail.app account (`MAIL_TEST_ACCOUNT`) | `make benchmark-baseline` |
| `evals/agent_tool_usability/results/scored_results.md` | an `OPENROUTER_API_KEY` | `make eval-tools` (+ refresh the Claude row) |

Because they're resource-gated, they're easy to defer "just this once" — and at v0.10.0 they were, silently: the eval snapshot shipped stamped `v0.9.0`, and the benchmark baseline predated the release. [`scripts/check_release_artifacts.sh`](../../scripts/check_release_artifacts.sh) (#356) closes that gap.

## The gate

Each artifact carries the **release version it was produced for**:

- `baseline.json` — a `"_version"` key, written by `--capture-baseline` (from `apple_mail_mcp.__version__`).
- `scored_results.md` — its `**Version:** vX.Y.Z` line (hand-authored when the snapshot is written).

`check_release_artifacts.sh` (run in release Phase 9; `./scripts/check_release_artifacts.sh [version]`, default version from `pyproject.toml`) **fails** when a stamp doesn't match the release being cut — so a stale artifact can't ship unnoticed.

## Resolving a failure

When the gate flags a stale artifact, do one of:

1. **Refresh it** — run the command above and commit the result (this re-stamps it). The normal path.
2. **Re-stamp** — if it's genuinely unchanged for this release (re-running would reproduce the same numbers/scores), re-run the capture, or update `scored_results.md`'s `**Version:**` line, asserting it still represents the release.
3. **Waive** — if it genuinely can't be refreshed for this release (e.g. a CI/docs-only release with no perf or tool-surface change), add a line to [`release_artifact_waivers.txt`](../../release_artifact_waivers.txt):
   ```
   v<version> <benchmark|eval> #<issue> <reason>
   ```
   The `#<issue>` is required — a waiver records a *deliberate, tracked* skip, never a silent one. Prune old entries freely.

If you can't justify a waiver with a one-line reason and an issue, the artifact probably should be refreshed.

## Why a gate (vs. a checklist)

Phase 8.5 was already marked "mandatory", but only `eval-descriptions` (#1) was enforced (by `check_docs.sh`); the benchmark baseline and eval snapshot had no freshness check, so the "mandatory" step quietly became optional. A warning that's ignored is how the v0.10.0 snapshot went stale — hence a hard gate with an explicit, tracked waiver rather than another advisory note. Same philosophy as the complexity and client/server-parity gates ([COMPLEXITY.md](COMPLEXITY.md), [CLIENT_SERVER_PARITY.md](CLIENT_SERVER_PARITY.md)).

## Checking locally

```bash
./scripts/check_release_artifacts.sh          # against pyproject's version
./scripts/check_release_artifacts.sh 0.11.0   # against an explicit version
```
