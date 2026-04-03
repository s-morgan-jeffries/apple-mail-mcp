#!/bin/bash
# Create an annotated git tag with validation.
# Usage: ./scripts/create_tag.sh vX.Y.Z
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 vX.Y.Z"
    exit 1
fi

TAG="$1"

# Validate tag format
if ! echo "$TAG" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$'; then
    echo "ERROR: Invalid tag format '$TAG'. Expected: vX.Y.Z or vX.Y.Z-rcN"
    exit 1
fi

# Check we're on the right branch
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)

if echo "$TAG" | grep -q 'rc'; then
    # RC tags must be on release/* branches
    if ! echo "$BRANCH" | grep -q '^release/'; then
        echo "ERROR: RC tags must be created on release/* branches (current: $BRANCH)"
        exit 1
    fi
else
    # Final tags must be on main
    if [ "$BRANCH" != "main" ]; then
        echo "ERROR: Final tags must be created on main (current: $BRANCH)"
        exit 1
    fi
fi

# Check tag doesn't already exist
if git tag -l "$TAG" | grep -q "$TAG"; then
    echo "ERROR: Tag $TAG already exists"
    exit 1
fi

# Run version sync check
echo "Verifying version sync..."
./scripts/check_version_sync.sh || {
    echo "ERROR: Version sync check failed. Fix before tagging."
    exit 1
}

# Create annotated tag
VERSION="${TAG#v}"
echo ""
echo "Creating tag $TAG on branch $BRANCH..."
git tag -a "$TAG" -m "Release $VERSION"

echo ""
echo "Tag $TAG created. Push with:"
echo "  git push origin $TAG"
