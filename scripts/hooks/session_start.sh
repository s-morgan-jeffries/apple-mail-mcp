#!/bin/bash
# Claude Code session start hook: load project context.

echo "=== Apple Mail MCP Server ==="

# Current branch and status
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "detached")
echo "Branch: $BRANCH"
if [ "$BRANCH" = "main" ]; then
    echo "WARNING: You are on main. Create a feature branch before making changes."
fi

# Version from pyproject.toml
VERSION=$(grep '^version = ' pyproject.toml 2>/dev/null | head -1 | sed 's/version = "\(.*\)"/\1/')
echo "Version: $VERSION"

# Active milestone (if gh available)
if command -v gh &> /dev/null; then
    MILESTONE=$(gh api repos/:owner/:repo/milestones --jq '.[0] | "\(.title) — \(.open_issues) open issues"' 2>/dev/null || echo "none")
    echo "Milestone: $MILESTONE"
fi

# Recent commits
echo ""
echo "Recent commits:"
git log --oneline -5 2>/dev/null | sed 's/^/  /'

# Git status summary
CHANGED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
if [ "$CHANGED" -gt 0 ]; then
    echo ""
    echo "Working directory: $CHANGED changed file(s)"
fi

echo ""
