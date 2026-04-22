#!/bin/bash
# Probe whether Mail.app's IMAP credentials are retrievable from the macOS Keychain.
#
# Background: docs/research/imap-hybrid-approach.md proposed retrieving IMAP
# credentials via `security find-internet-password -s imap.<provider>.com`.
# This script tests that assumption on the running machine. See issue #39
# and the "Spike Findings" section of imap-hybrid-approach.md for context.
#
# Exits 0 regardless of findings — this is a diagnostic, not a gate.
set -uo pipefail

echo "=================================================================="
echo "Keychain / IMAP credential probe"
echo "macOS: $(sw_vers -productName) $(sw_vers -productVersion) (Darwin $(uname -r))"
echo "Date:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=================================================================="
echo ""

KEYCHAINS=$(security list-keychains | tr -d '"' | xargs)

echo "--- Keychains in search list ---"
echo "$KEYCHAINS" | tr ' ' '\n' | sed 's/^/  /'
echo ""

echo "--- Check 1: items with ptcl=imap or ptcl=imps ---"
IMAP_PROTO_COUNT=0
for kc in $KEYCHAINS; do
    count=$(security dump-keychain "$kc" 2>/dev/null \
        | grep -cE '"ptcl"<uint32>="(imap|imps)"' || true)
    echo "  $kc: $count items"
    IMAP_PROTO_COUNT=$((IMAP_PROTO_COUNT + count))
done
echo "  TOTAL: $IMAP_PROTO_COUNT"
echo ""

echo "--- Check 2: items matching common IMAP server hostnames ---"
SERVERS=(
    "imap.gmail.com"
    "imap.mail.me.com"
    "imap.mail.yahoo.com"
    "outlook.office365.com"
    "imap-mail.outlook.com"
    "imap.fastmail.com"
    "imap.aol.com"
)
SERVER_MATCH_COUNT=0
for srv in "${SERVERS[@]}"; do
    out=$(security find-internet-password -s "$srv" 2>&1 || true)
    if echo "$out" | grep -q "could not be found"; then
        echo "  $srv: not found"
    else
        echo "  $srv: FOUND"
        SERVER_MATCH_COUNT=$((SERVER_MATCH_COUNT + 1))
    fi
done
echo "  TOTAL MATCHES: $SERVER_MATCH_COUNT / ${#SERVERS[@]}"
echo ""

echo "--- Check 3: OAuth tokens (Gmail/Google) ---"
GOOGLE_OAUTH_COUNT=$(security dump-keychain 2>/dev/null \
    | grep -cE '"gena"<blob>="Google OAuth"' || true)
echo "  Items with gena=\"Google OAuth\": $GOOGLE_OAUTH_COUNT"
echo ""

echo "--- Check 4: Accounts framework DB readability (TCC) ---"
ACCOUNTS_DIR="$HOME/Library/Accounts"
if ls "$ACCOUNTS_DIR" >/dev/null 2>&1; then
    echo "  $ACCOUNTS_DIR: readable"
    if [ -f "$ACCOUNTS_DIR/Accounts4.sqlite" ] \
        && sqlite3 "$ACCOUNTS_DIR/Accounts4.sqlite" ".tables" >/dev/null 2>&1; then
        echo "  Accounts4.sqlite: readable"
    else
        echo "  Accounts4.sqlite: NOT readable (likely TCC-protected)"
    fi
else
    echo "  $ACCOUNTS_DIR: NOT readable (TCC-protected — grant Full Disk Access to test)"
fi
echo ""

echo "--- Check 5: Mail.app account inventory (for reference) ---"
osascript <<'APPLESCRIPT' 2>&1 || echo "  (unable to query Mail.app — may not be running or automation not authorized)"
tell application "Mail"
    set out to ""
    repeat with a in every account
        set out to out & "  " & (name of a) & " [" & (account type of a as string) & "]" & linefeed
    end repeat
    return out
end tell
APPLESCRIPT
echo ""

echo "=================================================================="
echo "SUMMARY"
echo "=================================================================="
echo "  IMAP-protocol Keychain items:     $IMAP_PROTO_COUNT"
echo "  Common IMAP hostnames matched:    $SERVER_MATCH_COUNT / ${#SERVERS[@]}"
echo "  Google OAuth items in Keychain:   $GOOGLE_OAUTH_COUNT"
echo ""
if [ "$IMAP_PROTO_COUNT" -eq 0 ] && [ "$SERVER_MATCH_COUNT" -eq 0 ]; then
    echo "  VERDICT: Mail.app's IMAP credentials are NOT retrievable via the"
    echo "  login/System keychain on this machine. The assumption in"
    echo "  docs/research/imap-hybrid-approach.md is falsified here."
else
    echo "  VERDICT: Some IMAP credentials appear retrievable. Inspect the"
    echo "  matching items above to determine whether they are usable."
fi
echo "=================================================================="
