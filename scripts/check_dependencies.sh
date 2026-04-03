#!/bin/bash
# Check for known vulnerabilities in dependencies using pip-audit.
set -euo pipefail

echo "Checking dependencies for vulnerabilities..."

if ! command -v pip-audit &> /dev/null; then
    if command -v uv &> /dev/null; then
        echo "Installing pip-audit..."
        uv pip install pip-audit -q
    else
        echo "Error: pip-audit not found. Install with: pip install pip-audit"
        exit 1
    fi
fi

pip-audit 2>&1 || {
    echo ""
    echo "WARNING: Vulnerability scan found issues. Review and update dependencies."
    exit 1
}

echo ""
echo "No known vulnerabilities found."
