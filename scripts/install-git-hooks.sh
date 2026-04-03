#!/bin/bash
# Install git hooks by symlinking from scripts/git-hooks/ to .git/hooks/
set -euo pipefail

HOOK_DIR="scripts/git-hooks"
GIT_HOOK_DIR=".git/hooks"

if [ ! -d "$GIT_HOOK_DIR" ]; then
    echo "ERROR: .git/hooks directory not found. Are you in the repo root?"
    exit 1
fi

for hook in "$HOOK_DIR"/*; do
    hook_name=$(basename "$hook")
    target="$GIT_HOOK_DIR/$hook_name"

    if [ -f "$target" ] && [ ! -L "$target" ]; then
        echo "Backing up existing $hook_name to $hook_name.bak"
        mv "$target" "$target.bak"
    fi

    ln -sf "../../$hook" "$target"
    echo "Installed $hook_name"
done

echo ""
echo "Git hooks installed. Run 'ls -la .git/hooks/' to verify."
