#!/bin/bash
# Claude Code post-bash hook: monitor CI after git push.

COMMAND="$1"

# Detect git push and monitor CI
if echo "$COMMAND" | grep -qE 'git push'; then
    echo "Push detected. Checking for CI runs..."

    # Wait briefly for CI to start
    sleep 3

    # Check if gh is available
    if command -v gh &> /dev/null; then
        RUN_ID=$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || echo "")
        if [ -n "$RUN_ID" ]; then
            echo "CI run started: https://github.com/$(gh repo view --json nameWithOwner --jq '.nameWithOwner')/actions/runs/$RUN_ID"
            echo "Watching CI..."
            gh run watch "$RUN_ID" --exit-status 2>&1 || {
                echo ""
                echo "CI FAILED. Check: gh run view $RUN_ID --web"
            }
        fi
    fi
fi
