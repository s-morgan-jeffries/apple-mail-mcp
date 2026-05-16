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

# Check for functions exceeding threshold.
#
# IMPORTANT: pass -n D (rank D = CC ≥ 21) to radon so the JSON contains
# every function in the dangerous range. Until #174, this was -n F (CC
# ≥ 41), meaning anything from CC 21 to 40 silently passed the
# `> THRESHOLD` check below.
FAILURES=$(radon cc "$SRC_DIR" -n D -j 2>&1 | python3 -c "
import json, sys

THRESHOLD = $THRESHOLD

# Per-function allowlist (max CC). Functions exceeding their entry's
# value, OR new functions over THRESHOLD not in this list, fail the
# gate. Entries are documented exceptions in docs/guides/COMPLEXITY.md;
# refactor follow-ups are tracked per function.
#
# Lowering an entry (after a successful refactor) is encouraged — it's
# a one-way ratchet. NEVER raise without updating COMPLEXITY.md and
# justifying the structural reason in the PR.
ALLOWLIST = {
    ('mail_connector.py', 'AppleMailConnector.create_draft'): 25,
}

data = json.load(sys.stdin)
new_violations = []
regressions = []
for filepath, functions in data.items():
    fname = filepath.rsplit('/', 1)[-1]
    for func in functions:
        if func['complexity'] <= THRESHOLD:
            continue
        # radon's JSON splits class qualifier (classname) from name;
        # compose them for the allowlist key so methods read like
        # 'AppleMailConnector.create_draft' instead of bare 'create_draft'.
        cls = func.get('classname') or ''
        qualified = f'{cls}.{func[\"name\"]}' if cls else func['name']
        key = (fname, qualified)
        ceiling = ALLOWLIST.get(key)
        if ceiling is None:
            new_violations.append(
                f\"  {filepath}:{func['lineno']} {qualified} \"
                f\"(CC={func['complexity']}) — not in allowlist; \"
                f\"refactor or add to ALLOWLIST in scripts/check_complexity.sh\"
            )
        elif func['complexity'] > ceiling:
            regressions.append(
                f\"  {filepath}:{func['lineno']} {qualified} \"
                f\"(CC={func['complexity']}, allowlist max={ceiling}) \"
                f\"— regressed; lower the function's CC, or update \"
                f\"docs/guides/COMPLEXITY.md and justify in the PR\"
            )

problems = new_violations + regressions
if problems:
    print('Functions exceeding threshold:')
    for p in problems:
        print(p)
    sys.exit(1)
else:
    print('All functions within threshold (or below their allowlist ceiling).')
" 2>&1) || {
    echo "$FAILURES"
    exit 1
}

echo "$FAILURES"
