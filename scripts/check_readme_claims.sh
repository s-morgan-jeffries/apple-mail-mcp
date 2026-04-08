#!/bin/bash
# Validates documentation claims against actual codebase.
# Checks: tool count, test count in README and CLAUDE.md.
set -euo pipefail

ERRORS=0

echo "Checking documentation claims..."

# Check 1: Tool count in README
echo ""
echo "Check 1: Tool count..."
README_TOOL_CLAIM=$(grep -oE 'Tools \([0-9]+\)' README.md 2>/dev/null | grep -oE '[0-9]+' || echo "")
ACTUAL_TOOLS=$(grep -c '@mcp.tool' src/apple_mail_mcp/server.py || echo "0")

if [ -n "$README_TOOL_CLAIM" ]; then
    if [ "$README_TOOL_CLAIM" != "$ACTUAL_TOOLS" ]; then
        echo "  ERROR: README claims $README_TOOL_CLAIM tools, but server.py has $ACTUAL_TOOLS @mcp.tool() decorators."
        ERRORS=$((ERRORS + 1))
    else
        echo "  OK: README tool count ($README_TOOL_CLAIM) matches server.py."
    fi
else
    echo "  SKIP: No tool count found in README."
fi

# Check 2: CLAUDE.md tool count
echo ""
echo "Check 2: CLAUDE.md tool count..."
CLAUDE_TOOL_CLAIM=$(grep -oE '[0-9]+ MCP tools' .claude/CLAUDE.md 2>/dev/null | grep -oE '[0-9]+' || echo "")

if [ -n "$CLAUDE_TOOL_CLAIM" ]; then
    if [ "$CLAUDE_TOOL_CLAIM" != "$ACTUAL_TOOLS" ]; then
        echo "  ERROR: CLAUDE.md claims $CLAUDE_TOOL_CLAIM tools, but server.py has $ACTUAL_TOOLS."
        ERRORS=$((ERRORS + 1))
    else
        echo "  OK: CLAUDE.md tool count ($CLAUDE_TOOL_CLAIM) matches server.py."
    fi
else
    echo "  SKIP: No tool count found in CLAUDE.md."
fi

# Check 3: CLAUDE.md test count (if present)
echo ""
echo "Check 3: Test counts..."
CLAUDE_TEST_CLAIM=$(grep -oE '[0-9]+ unit' .claude/CLAUDE.md 2>/dev/null | grep -oE '[0-9]+' || echo "")

if [ -n "$CLAUDE_TEST_CLAIM" ]; then
    ACTUAL_TESTS=$(uv run pytest tests/unit/ --collect-only -q --no-header 2>/dev/null | tail -1 | grep -oE '[0-9]+ test' | grep -oE '[0-9]+' || echo "unknown")
    if [ "$ACTUAL_TESTS" != "unknown" ] && [ "$CLAUDE_TEST_CLAIM" != "$ACTUAL_TESTS" ]; then
        echo "  WARNING: CLAUDE.md claims $CLAUDE_TEST_CLAIM unit tests, actual count is $ACTUAL_TESTS."
    else
        echo "  OK: Test count appears consistent."
    fi
else
    echo "  SKIP: No test count found in CLAUDE.md."
fi

echo ""
if [ $ERRORS -gt 0 ]; then
    echo "FAILED: $ERRORS documentation claim(s) are stale."
    echo ""
    echo "Fix by updating the numbers in README.md and/or .claude/CLAUDE.md."
    exit 1
else
    echo "All documentation claims verified."
fi
