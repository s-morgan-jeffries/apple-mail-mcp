#!/bin/bash
# PostToolUse hook for Bash commands
# Monitors CI after git push

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')
EXIT_CODE=$(echo "$INPUT" | jq -r '.tool_response.exit_code // 0')

# Only check successful git push commands
if ! echo "$COMMAND" | grep -qE "^git push" || [ "$EXIT_CODE" != "0" ]; then
    exit 0
fi

# Check if gh CLI is available
if ! command -v gh &> /dev/null; then
    echo "gh CLI not found. Check CI manually." >&2
    exit 0
fi

# Resolve the SHA that was just pushed so the run lookup below can be
# scoped to it. Without this, `gh run list --limit 1` would return the
# most recent run repo-wide — potentially an unrelated run on another
# branch — and the hook would misreport its status as the result of
# this push. (#179)
PUSHED_SHA=$(git rev-parse HEAD 2>/dev/null)
if [ -z "$PUSHED_SHA" ]; then
    echo "Could not resolve pushed commit SHA; skipping CI watch." >&2
    exit 0
fi

echo "Git push detected. Waiting for CI to start..." >&2
sleep 10

RUN_ID=$(gh run list --commit "$PUSHED_SHA" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)

if [ -n "$RUN_ID" ]; then
    echo "Watching CI run #$RUN_ID..." >&2
    gh run watch "$RUN_ID" --exit-status 2>&1 >&2
    WATCH_EXIT=$?

    if [ $WATCH_EXIT -ne 0 ]; then
        RUN_URL=$(gh run view "$RUN_ID" --json url -q .url 2>/dev/null)
        echo "CI failed. Details: $RUN_URL" >&2
        echo "Fetch logs: gh run view $RUN_ID --log-failed" >&2
        exit 2
    fi
    echo "CI passed." >&2
else
    echo "No CI run found for $PUSHED_SHA (workflows may not fire on branch pushes; check after opening the PR)." >&2
fi

exit 0
