#!/bin/bash
# SessionStart hook — loads project context

CONTEXT="# Current Project State\n\n"

# Git status
GIT_STATUS=$(git status --short 2>/dev/null)
if [ -n "$GIT_STATUS" ]; then
    CONTEXT+="## Git Status\n\`\`\`\n$GIT_STATUS\n\`\`\`\n\n"
fi

# Current branch + warning
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
CONTEXT+="**Current branch:** $BRANCH\n\n"

if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
    CONTEXT+="**WARNING:** You are on $BRANCH. Create a feature branch before making changes.\n\n"
fi

# Version and milestone info
if command -v gh &> /dev/null; then
    CURRENT_VERSION=$(grep '^version = ' pyproject.toml 2>/dev/null | cut -d'"' -f2)

    if [ -n "$CURRENT_VERSION" ]; then
        CONTEXT+="**Version:** v$CURRENT_VERSION\n\n"

        # Look for next milestone (current version + patch bump)
        IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
        NEXT_PATCH=$((PATCH + 1))
        CURRENT_MILESTONE="v${MAJOR}.${MINOR}.${NEXT_PATCH}"

        MILESTONE_EXISTS=$(gh api repos/:owner/:repo/milestones --jq ".[] | select(.title == \"$CURRENT_MILESTONE\") | .number" 2>/dev/null)

        if [ -n "$MILESTONE_EXISTS" ]; then
            MILESTONE_OPEN=$(gh issue list --milestone "$CURRENT_MILESTONE" --state open --json number -q 'length' 2>/dev/null || echo "0")
            CONTEXT+="**Active Milestone:** $CURRENT_MILESTONE ($MILESTONE_OPEN open issues)\n\n"
        fi
    fi

    OPEN_ISSUES=$(gh issue list --state open --json number -q 'length' 2>/dev/null || echo "unknown")
    CONTEXT+="**Total open issues:** $OPEN_ISSUES\n\n"
fi

# Recent commits
RECENT_COMMITS=$(git log --oneline -5 2>/dev/null)
if [ -n "$RECENT_COMMITS" ]; then
    CONTEXT+="## Recent Commits\n\`\`\`\n$RECENT_COMMITS\n\`\`\`\n\n"
fi

# Return context as JSON
cat <<EOF
{
  "additionalContext": $(echo -e "$CONTEXT" | jq -Rs .)
}
EOF

# Set PYTHONPATH
if [ -n "$CLAUDE_ENV_FILE" ]; then
    echo 'export PYTHONPATH="$CLAUDE_PROJECT_DIR/src:$PYTHONPATH"' >> "$CLAUDE_ENV_FILE"
fi

exit 0
