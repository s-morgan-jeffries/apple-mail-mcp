# Benchmarking

The benchmark suite establishes timing baselines for the project's expensive operations and detects regressions at a 5x threshold. Benchmarks are opt-in: they require real Mail.app, take 30+ seconds to run, and are excluded from CI.

## What's covered

| Benchmark | What it times |
|-----------|---------------|
| `search_messages_no_filter` | List-style search (no filters, just a limit) |
| `search_messages_with_sender_filter` | Filtered search with a permissive matcher — most messages match, exercises per-message AppleScript IF-filter machinery |
| `search_messages_with_zero_matches` | Filtered search with a no-match sentinel — full-scan worst case (no early exit; bounded only by mailbox size) |
| `save_attachments_one_file` | Saving attachments from one message to a tmp dir |
| `mark_as_read_50_msgs` | Bulk mark-read on 50 messages — the key scaling-pattern signal |

`move_messages` is intentionally absent from v1; IMAP UID semantics make the round-trip-then-revert pattern fragile. Will land alongside a documented stable-fixture-mailbox setup.

## Running

```bash
# Compare current timings against committed baselines
make benchmark

# Capture fresh baselines (use after intentional perf changes)
make benchmark-baseline
```

Both targets set `MAIL_TEST_MODE=true` automatically. The test account is whatever `MAIL_TEST_ACCOUNT` resolves to — default `iCloud`.

To also run the **Gmail-variant** benchmarks (#101), set `MAIL_TEST_ACCOUNT_GMAIL` to the Mail.app account name of a configured Gmail account:

```bash
MAIL_TEST_ACCOUNT_GMAIL=Gmail make benchmark
MAIL_TEST_ACCOUNT_GMAIL=Gmail make benchmark-baseline
```

When `MAIL_TEST_ACCOUNT_GMAIL` is unset, Gmail variants skip cleanly with a clear message; the standard (iCloud) benchmarks still run.

## Test data requirements

Benchmarks measure real Mail.app behavior, so they need real data:

| Benchmark | Requires |
|-----------|----------|
| `search_messages_*` | At least 1 message in INBOX, Archive, or Sent Messages |
| `save_attachments_*` | At least 1 message with an attachment in those mailboxes |
| `mark_as_read_50_msgs`, `move_messages_50_msgs`, `update_message_move_50_msgs_imap` | Keychain IMAP credentials for `MAIL_TEST_ACCOUNT` — the source pool **self-seeds** synthetic messages (no real ≥50-msg mailbox needed; #287) |
| `search_messages_*_gmail` | A Gmail account configured in Mail.app + Keychain IMAP credentials (#73 opt-in) |
| `move_messages_50_msgs_gmail` | Same as above. Exercises the `gmail_mode=True` copy+delete path |

If a precondition isn't met, the benchmark skips with a clear message rather than failing. This is by design — running a smaller bulk benchmark on 5 messages would defeat the point (the scaling signal is what matters).

### Bulk fixtures use synthetic data (both account families)

The bulk benchmarks never touch your real mail. Both the generic
(`MAIL_TEST_ACCOUNT`) and Gmail (`MAIL_TEST_ACCOUNT_GMAIL`) families seed a
dedicated source mailbox with 50 synthetic RFC 5322 messages via IMAP `APPEND`
(subject `ZZZ-AMM-BENCH Synthetic Message NNN`), idempotently (re-runs only
append what's missing):

- Generic: `[apple-mail-mcp-bench-source]` → `[apple-mail-mcp-bench]` (#287).
- Gmail: `[apple-mail-mcp-bench-gmail-source]` → `[apple-mail-mcp-bench-gmail]`, moved with `gmail_mode=True` (copy+delete).

The move-target is populated per-test from the source and drained back on
teardown. All four fixture mailboxes persist across runs (cheaper than
re-creating per-run, and easy to spot from the prefix). Use `delete_mailbox`
to clean them up if desired.

## Bulk-move: IMAP fast path vs AppleScript (#287)

The bulk benchmarks are captured against a self-seeding synthetic source (above), so they no longer depend on a real ≥50-message mailbox. Captured medians (50 messages, iCloud):

| Benchmark | Path | Median |
|-----------|------|--------|
| `mark_as_read_50_msgs` | flag toggle | ~0.8s |
| `move_messages_50_msgs` | AppleScript bulk move (one direction ≈ half) | ~4.0s |
| `update_message_move_50_msgs_imap` | `update_message` IMAP move-only fast path (#149) | ~23.6s |

**Surprising result:** the IMAP "fast path" is **~6× *slower*** than the AppleScript move. The likely cause is that resolving 50 RFC 5322 Message-IDs to IMAP UIDs issues ~50 individual `SEARCH HEADER Message-ID` round-trips (unindexed on most servers), which dominates the move itself. This is tracked for investigation in a follow-up — the benchmark exists precisely to surface this kind of regression-vs-intuition.

(Historical note: these benchmarks previously skipped because the fixture needed a real ≥50-message mailbox and the AppleScript `move_messages` setup was slow on many-mailbox accounts, #103. The self-seeding source + the IMAP-search robustness fix #314 unblocked capture.)

## How baselines are stored

`tests/benchmarks/baseline.json` holds one number per benchmark — the median wall-clock seconds observed at capture time:

```json
{
  "search_messages_no_filter": 1.398,
  "search_messages_with_sender_filter": 1.409
}
```

A benchmark with no entry in `baseline.json` skips its assertion (rather than failing) when run. Capture a baseline on your machine with `make benchmark-baseline` to enable comparison.

## The 5x threshold

The harness fails a benchmark when its median run is more than 5x the baseline. This is calibrated for real-machine noise — typical CV (coefficient of variation) across runs is 5-10%, occasionally up to 30% under transient load. 5x is generous enough that a slow laptop won't false-positive against a baseline captured on a fast one, while still catching real regressions (a 5x slowdown on a 1.4s operation is ~7s, well outside any normal variance).

If you regularly see passing benchmarks at 3-4x baseline, that's a signal to either (a) re-capture baselines on your machine, or (b) investigate whether something genuinely got slower.

## When to update baselines

- **After an intentional perf improvement.** Run `make benchmark-baseline` to capture the new lower numbers; commit with a note explaining the speedup.
- **After hardware change.** A new dev machine may have consistently different numbers.
- **After upstream changes** that legitimately affect timings (macOS update, Mail.app version, IMAP server change). Note the cause in the commit message.

Don't update baselines just to silence a failing test — investigate first.

## Methodology notes

- **Median, not mean.** Each benchmark runs 5 times; the median is the headline number. Tolerates one slow outlier without skewing.
- **Cold-start detection.** The first run of each benchmark is flagged separately if it's >2x the median of the rest — common with operations that warm up Mail.app state. Cold-start runs are still part of the median (they happen in real use too).
- **No subprocess mocking.** The performance-patterns skill identifies subprocess overhead (100-300ms per `osascript` call) as the project's main perf concern. Mocking subprocess defeats the purpose.

## Why benchmarks aren't in CI

CI runners don't have Mail.app. There's no path to running these in GitHub Actions short of provisioning a macOS runner with a configured Mail account, which is more cost and complexity than the regression risk warrants for a pre-1.0 project. Local-only is the right tradeoff.

## Adding a new benchmark

1. Pick a name like `<operation>_<scale>` (e.g., `flag_message_50_msgs`).
2. Write the test in the appropriate `test_*.py` file, calling `measure_median(...)` then `assert_within_baseline(...)`.
3. Use the `baselines: dict[str, float]` and `capture_mode: bool` fixtures to plug into the harness.
4. Run `make benchmark-baseline` to seed the new entry in `baseline.json`.
5. In the same PR: commit the test + the new baseline entry together.

The harness is in [`tests/benchmarks/conftest.py`](../../tests/benchmarks/conftest.py).
