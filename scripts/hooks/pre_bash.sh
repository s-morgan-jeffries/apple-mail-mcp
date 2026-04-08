#!/bin/bash
# PreToolUse hook for Bash commands
# Checks: branch protection, tag creation enforcement

# Read JSON input from stdin
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')

# ===================================================
# CHECK: Prevent commits to main branch
# ===================================================
check_no_commits_to_main() {
    if ! echo "$COMMAND" | grep -qE "^git commit"; then
        return 0
    fi

    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

    # Allow release branches
    if [[ "$CURRENT_BRANCH" =~ ^release/ ]]; then
        return 0
    fi

    if [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
        if ! echo "$COMMAND" | grep -qiE "hotfix|emergency"; then
            echo "Cannot commit directly to $CURRENT_BRANCH. Create a feature branch first." >&2
            return 2
        fi
    fi

    return 0
}

# ===================================================
# CHECK: Enforce wrapper script for tag creation
# ===================================================
check_tag_creation_workflow() {
    if ! echo "$COMMAND" | grep -qE "^git tag"; then
        return 0
    fi

    echo "Use ./scripts/create_tag.sh <tag-name> instead of direct git tag commands." >&2
    return 2
}

# ===================================================
# Run all checks
# ===================================================
CHECKS=(
    check_no_commits_to_main
    check_tag_creation_workflow
)

for check in "${CHECKS[@]}"; do
    $check
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        exit $EXIT_CODE
    fi
done

exit 0
