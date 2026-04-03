#!/bin/bash
# Claude Code pre-bash hook: prevent dangerous operations.
# Blocks: commits to main, direct git tag (use scripts/create_tag.sh).

COMMAND="$1"

# Block git commit on main/master
if echo "$COMMAND" | grep -qE 'git commit' && ! echo "$COMMAND" | grep -q 'release/'; then
    BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
    if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
        echo "BLOCKED: Cannot commit directly to $BRANCH."
        echo "Create a feature branch first: git checkout -b feature/issue-N-description"
        exit 1
    fi
fi

# Block direct git tag (enforce create_tag.sh)
if echo "$COMMAND" | grep -qE '^git tag '; then
    echo "BLOCKED: Use ./scripts/create_tag.sh vX.Y.Z instead of direct git tag."
    exit 1
fi
