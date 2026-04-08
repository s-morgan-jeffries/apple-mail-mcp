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

echo "Git push detected. Waiting for CI to start..." >&2
sleep 10

RUN_ID=$(gh run list --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)

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
    echo "No CI run found. Check manually." >&2
fi

exit 0
