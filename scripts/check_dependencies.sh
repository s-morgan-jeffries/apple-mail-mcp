#!/bin/bash
# Check for known vulnerabilities in dependencies using pip-audit.
set -euo pipefail

echo "Checking dependencies for vulnerabilities..."

# pip-audit is declared in pyproject.toml's dev dep group, so `uv sync --dev`
# installs it into .venv. `uv run` resolves it from there — calling bare
# `pip-audit` (or trying to install it ad-hoc) doesn't work because the
# .venv bin isn't on the script-runtime PATH.
uv run pip-audit 2>&1 || {
    echo ""
    echo "WARNING: Vulnerability scan found issues. Review and update dependencies."
    exit 1
}

echo ""
echo "No known vulnerabilities found."
