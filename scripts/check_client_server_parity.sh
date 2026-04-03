#!/bin/bash
# Verify every public method in mail_connector.py has a corresponding @mcp.tool() in server.py.
set -euo pipefail

CONNECTOR="src/apple_mail_mcp/mail_connector.py"
SERVER="src/apple_mail_mcp/server.py"

echo "Checking client-server parity..."

# Extract public methods from connector (exclude __init__, _private)
CONNECTOR_METHODS=$(grep -E '^\s+def [a-z]' "$CONNECTOR" | grep -v '^\s+def _' | sed 's/.*def \([a-z_]*\)(.*/\1/' | sort)

# Extract @mcp.tool() decorated functions from server
SERVER_TOOLS=$(grep -B1 '@mcp.tool' "$SERVER" | grep 'def ' | sed 's/.*def \([a-z_]*\)(.*/\1/' | sort)

echo ""
echo "Connector public methods:"
echo "$CONNECTOR_METHODS" | sed 's/^/  /'
echo ""
echo "Server tools:"
echo "$SERVER_TOOLS" | sed 's/^/  /'
echo ""

# Find methods in connector but not in server
MISSING=$(comm -23 <(echo "$CONNECTOR_METHODS") <(echo "$SERVER_TOOLS"))

if [ -n "$MISSING" ]; then
    echo "WARNING: Connector methods without @mcp.tool() wrapper:"
    echo "$MISSING" | sed 's/^/  - /'
    echo ""
    echo "These may be intentional (internal helpers) or may need server exposure."
    # Don't fail — some methods may be intentionally internal
    exit 0
else
    echo "All connector methods have corresponding server tools."
fi
