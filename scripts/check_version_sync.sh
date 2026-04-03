#!/bin/bash
# Verify version string is consistent across all authoritative files.
# Authoritative source: pyproject.toml
set -euo pipefail

echo "Checking version synchronization..."

# Extract version from pyproject.toml (authoritative)
PYPROJECT_VERSION=$(grep '^version = ' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')

if [ -z "$PYPROJECT_VERSION" ]; then
    echo "ERROR: Could not extract version from pyproject.toml"
    exit 1
fi

echo "  pyproject.toml: $PYPROJECT_VERSION (authoritative)"

ERRORS=0

# Check __init__.py
INIT_VERSION=$(grep '__version__' src/apple_mail_mcp/__init__.py | sed 's/__version__ = "\(.*\)"/\1/')
echo "  __init__.py:    $INIT_VERSION"
if [ "$INIT_VERSION" != "$PYPROJECT_VERSION" ]; then
    echo "  ERROR: __init__.py version mismatch!"
    ERRORS=$((ERRORS + 1))
fi

# Check CLAUDE.md
if [ -f ".claude/CLAUDE.md" ]; then
    CLAUDE_VERSION=$(grep '^\*\*Version:\*\*' .claude/CLAUDE.md | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | sed 's/^v//')
    if [ -n "$CLAUDE_VERSION" ]; then
        echo "  CLAUDE.md:      $CLAUDE_VERSION"
        if [ "$CLAUDE_VERSION" != "$PYPROJECT_VERSION" ]; then
            echo "  ERROR: CLAUDE.md version mismatch!"
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo "  CLAUDE.md:      (no version found - OK if not yet formatted)"
    fi
fi

# Check CHANGELOG.md
if [ -f "CHANGELOG.md" ]; then
    CHANGELOG_VERSION=$(grep '^\## \[' CHANGELOG.md | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
    if [ -n "$CHANGELOG_VERSION" ]; then
        echo "  CHANGELOG.md:   $CHANGELOG_VERSION (latest entry)"
        if [ "$CHANGELOG_VERSION" != "$PYPROJECT_VERSION" ] && [ "$CHANGELOG_VERSION" != "Unreleased" ]; then
            echo "  WARNING: CHANGELOG.md latest entry doesn't match (may be OK if unreleased changes exist)"
        fi
    fi
fi

echo ""

if [ $ERRORS -gt 0 ]; then
    echo "FAILED: $ERRORS version mismatch(es) found."
    exit 1
else
    echo "All versions synchronized."
fi
