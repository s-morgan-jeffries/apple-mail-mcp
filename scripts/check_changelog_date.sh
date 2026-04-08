#!/bin/bash
# Ensures CHANGELOG.md has an actual date (not TBD) for the given version.
# Used during release to catch stale changelog entries.
# Usage: ./scripts/check_changelog_date.sh v0.4.0
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 vX.Y.Z"
    exit 1
fi

TAG="$1"
VERSION="${TAG#v}"

echo "Checking CHANGELOG date for $VERSION..."

if [ ! -f "CHANGELOG.md" ]; then
    echo "ERROR: CHANGELOG.md not found."
    exit 1
fi

# Find the line for this version
ENTRY=$(grep "^## \[$VERSION\]" CHANGELOG.md || echo "")

if [ -z "$ENTRY" ]; then
    echo "ERROR: No CHANGELOG entry found for $VERSION."
    echo "Add an entry: ## [$VERSION] - $(date +%Y-%m-%d)"
    exit 1
fi

# Check for TBD or missing date
if echo "$ENTRY" | grep -qiE 'TBD|UNRELEASED|YYYY'; then
    echo "ERROR: CHANGELOG entry for $VERSION has a placeholder date."
    echo "Update it to: ## [$VERSION] - $(date +%Y-%m-%d)"
    exit 1
fi

# Verify date format
DATE=$(echo "$ENTRY" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || echo "")
if [ -z "$DATE" ]; then
    echo "ERROR: CHANGELOG entry for $VERSION missing date in YYYY-MM-DD format."
    exit 1
fi

echo "OK: CHANGELOG entry for $VERSION dated $DATE."
