#!/bin/bash
# Check cyclomatic complexity of Python source files using radon.
# Threshold: CC <= 20 for new code. Documented exceptions allowed.
set -euo pipefail

THRESHOLD=20
SRC_DIR="src/apple_mail_mcp"

if ! command -v radon &> /dev/null; then
    if command -v uv &> /dev/null; then
        echo "Installing radon..."
        uv pip install radon -q
    else
        echo "Error: radon not found. Install with: pip install radon"
        exit 1
    fi
fi

echo "Checking cyclomatic complexity (threshold: CC <= $THRESHOLD)..."
echo ""

# Get complexity report
REPORT=$(radon cc "$SRC_DIR" -n C -s 2>&1) || true

if [ -z "$REPORT" ]; then
    echo "All functions have complexity <= B (acceptable)."
    exit 0
fi

echo "$REPORT"
echo ""

# Check for functions exceeding threshold
FAILURES=$(radon cc "$SRC_DIR" -n F -j 2>&1 | python3 -c "
import json, sys
data = json.load(sys.stdin)
failures = []
for filepath, functions in data.items():
    for func in functions:
        if func['complexity'] > $THRESHOLD:
            failures.append(f\"  {filepath}:{func['lineno']} {func['name']} (CC={func['complexity']})\")
if failures:
    print('Functions exceeding threshold:')
    for f in failures:
        print(f)
    sys.exit(1)
else:
    print('All functions within threshold.')
" 2>&1) || {
    echo "$FAILURES"
    exit 1
}

echo "$FAILURES"
