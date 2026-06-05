#!/bin/bash
# Verify every public method in mail_connector.py is either exposed as a
# server tool (@_tool / @mcp.tool) OR listed in the intentionally-internal
# ALLOWLIST below. A new public connector method that is neither fails the
# gate (exit 1). See docs/guides/CLIENT_SERVER_PARITY.md.
set -euo pipefail

CONNECTOR="src/apple_mail_mcp/mail_connector.py"
SERVER="src/apple_mail_mcp/server.py"

echo "Checking client-server parity..."

# Extract public methods from connector (exclude __init__, _private)
CONNECTOR_METHODS=$(grep -E '^\s+def [a-z]' "$CONNECTOR" | grep -v '^\s+def _' | sed 's/.*def \([a-z_]*\)(.*/\1/' | sort)

# Extract decorated tool functions from server. Matches both the bare
# @mcp.tool() decorator (legacy) and the @_tool(...) helper that wraps it
# (#217 — annotation-aware decorator that gates registration on
# --read-only). Once a decorator line is seen, the next `def`/`async def`
# line is the tool's function name. BSD-awk friendly.
SERVER_TOOLS=$(awk '
    /^@_tool\(/ || /^@mcp\.tool\(/ { in_dec=1; next }
    in_dec && /^(async )?def / {
        sub(/^(async )?def /, ""); sub(/\(.*$/, ""); print; in_dec=0
    }
' "$SERVER" | sort)

# Find public connector methods not exposed as a server tool.
MISSING=$(comm -23 <(echo "$CONNECTOR_METHODS") <(echo "$SERVER_TOOLS"))

# Classify MISSING against the intentionally-internal allowlist. A public
# method that is neither a tool nor allowlisted fails the gate; stale
# allowlist entries (now exposed, or removed) also fail so the list stays a
# shrinking, honest ratchet. Mirrors scripts/check_complexity.sh.
echo "$MISSING" | python3 -c '
import sys

# Intentionally-internal connector methods: name -> reason. These are public
# on the connector but deliberately NOT exposed as MCP tools (subsumed by a
# CRUD-style tool, or used only as internal helpers). Adding a genuinely new
# internal method? Add it here WITH a reason. Exposing one as a tool? Remove
# its entry. See docs/guides/CLIENT_SERVER_PARITY.md.
ALLOWLIST = {
    "mark_as_read": "Subsumed by update_message(read_status=...).",
    "move_messages": "Subsumed by update_message(destination_mailbox=...).",
    "flag_message": "Subsumed by update_message(flagged=/flag_color=...).",
    "set_rule_enabled": "Subsumed by update_rule(enabled=...).",
    "get_message": "Singular fetch; the tool is get_messages (plural).",
    "get_attachments": "Metadata surfaced via get_messages(include_attachments=True).",
    "get_selected_messages": "Mail.app UI selection; surfaced via the read tools (#92), not standalone.",
    "get_draft_state": "Reads a draft\x27s current fields for update_draft\x27s merge.",
    "find_message_by_message_id": "Internal RFC-id->Mail-id lookup (e.g. update_draft seed recovery).",
    "extract_draft_attachments": "Helper for update_draft delete-and-recreate attachment preservation.",
    "auto_template_vars": "Auto-fills template variables; used by the template/draft path, not standalone.",
}

missing = {line.strip() for line in sys.stdin if line.strip()}
unexpected = sorted(missing - ALLOWLIST.keys())
stale = sorted(ALLOWLIST.keys() - missing)

problems = False
if unexpected:
    problems = True
    print("FAIL: public connector method(s) not exposed as a tool and not allowlisted:")
    for m in unexpected:
        print(f"  - {m}: add an @_tool wrapper in server.py, OR add it to "
              f"ALLOWLIST (with a reason) in scripts/check_client_server_parity.sh")
if stale:
    problems = True
    print("FAIL: stale ALLOWLIST entr(y/ies) — now exposed as a tool, or no "
          "longer a public connector method:")
    for m in stale:
        print(f"  - {m}: remove it from ALLOWLIST in "
              f"scripts/check_client_server_parity.sh")

if problems:
    print("")
    print("See docs/guides/CLIENT_SERVER_PARITY.md.")
    sys.exit(1)

print(f"Parity OK: {len(missing)} intentionally-internal method(s), all allowlisted.")
'
