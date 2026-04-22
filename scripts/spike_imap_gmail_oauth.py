#!/usr/bin/env python3
"""Spike for issue #68, option (b): extract Google OAuth token from Keychain + XOAUTH2.

Tests whether the Google OAuth refresh tokens that Mail.app stores (as
`gena="Google OAuth"` generic-password items) can be extracted and used to
authenticate to `imap.gmail.com` via XOAUTH2 — which would let Gmail users who
set up Mail.app via "Google" in Internet Accounts avoid creating a separate
app password.

This runs in four stages and stops at whichever stage fails. The stage where
it fails IS the useful output.

Stage 1: locate Keychain items where `gena="Google OAuth"`.
Stage 2: retrieve one item's password content (may prompt user; may refuse).
Stage 3: inspect the token and determine whether it is directly usable. An
         OAuth refresh token requires a `client_id` + `client_secret` to
         exchange for an access token. Mail.app's Google client credentials
         are private to Apple — we cannot legitimately exchange the token.
Stage 4: if stage 3 produces an access token, construct XOAUTH2 SASL string
         and attempt AUTHENTICATE against `imap.gmail.com:993`.

Requires the `research` optional dependency group:
    uv pip install -e '.[research]'

This script is a research artifact. It does not ship.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class KeychainItem:
    service: str
    account: str


def stage1_locate_google_oauth_items() -> list[KeychainItem]:
    """Parse `security dump-keychain` and return all items with gena='Google OAuth'."""
    print("=" * 70)
    print("STAGE 1: Locate 'gena=Google OAuth' Keychain items")
    print("=" * 70)
    proc = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"  FAIL: security dump-keychain returned {proc.returncode}")
        return []

    # Items are delimited by 'keychain:' blocks. Within each block, attributes
    # appear on indented lines. We look for any block whose attributes contain
    # both gena="Google OAuth" and we capture its svce ("service") and acct.
    blocks: list[str] = []
    current: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("keychain:") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    items: list[KeychainItem] = []
    for block in blocks:
        if '"gena"<blob>="Google OAuth"' not in block:
            continue
        svce_match = re.search(r'"svce"<blob>="([^"]*)"', block)
        acct_match = re.search(r'"acct"<blob>="([^"]*)"', block)
        svce = svce_match.group(1) if svce_match else "<unknown>"
        acct = acct_match.group(1) if acct_match else "<unknown>"
        items.append(KeychainItem(service=svce, account=acct))

    print(f"  Found {len(items)} item(s):")
    for item in items:
        print(f"    service={item.service!r}  account={item.account!r}")
    if not items:
        print("  VERDICT: no Google OAuth items in Keychain — stage 2 skipped.")
    return items


def stage2_retrieve_token(item: KeychainItem) -> str | None:
    """Attempt to read the password content for the given Keychain item.

    On modern macOS, `security find-generic-password -w` for a Mail.app-owned
    item typically triggers a Keychain access prompt (the item's ACL limits
    which callers can read without user confirmation). If the user clicks
    "Allow" the password is returned; "Deny" (or dismiss) yields an error.
    """
    print()
    print("=" * 70)
    print("STAGE 2: Retrieve token content")
    print("=" * 70)
    print(f"  Attempting: security find-generic-password -w -s {item.service!r} -a {item.account!r}")
    print("  (If a Keychain prompt appears, click 'Deny' unless you're sure —")
    print("   this spike does not need the actual token value to draw its conclusion.)")
    print()
    proc = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            item.service,
            "-a",
            item.account,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        print(f"  FAIL: security exit {proc.returncode}: {stderr}")
        if "25308" in stderr or "interaction" in stderr.lower():
            print("  Interpretation: ACL-restricted — Keychain blocked read without user confirmation.")
        elif "deny" in stderr.lower() or "canceled" in stderr.lower():
            print("  Interpretation: user denied the Keychain access prompt.")
        else:
            print("  Interpretation: unexpected failure mode.")
        return None
    token = proc.stdout.rstrip("\n")
    print(f"  OK: retrieved {len(token)} characters")
    return token


def stage3_inspect_token(token: str) -> bool:
    """Inspect the token format and determine whether it is usable for XOAUTH2.

    Returns True iff we have a bearer access token that can be fed directly
    into an IMAP AUTHENTICATE XOAUTH2 SASL string. Refresh tokens require
    exchanging via Google's OAuth2 endpoint, which requires the Mail.app
    client_id+client_secret pair. Those creds are private to Apple.
    """
    print()
    print("=" * 70)
    print("STAGE 3: Inspect token format")
    print("=" * 70)

    # Google OAuth tokens have well-known shapes:
    # - Refresh tokens: start with "1//" and are ~100 chars
    # - Access tokens:  start with "ya29." and are ~200 chars, short-lived (~1h)
    # Apple may store either, or a JSON blob containing both.
    preview = token[:30].replace("\n", "\\n")
    print(f"  Length: {len(token)}  Preview: {preview!r}...")

    if token.startswith("ya29."):
        print("  Interpretation: looks like an OAuth2 access token (short-lived).")
        print("  Usable for XOAUTH2? YES — but lifespan is ~1 hour, so this would")
        print("  only work immediately after Mail.app refreshes the account.")
        return True
    if token.startswith("1//"):
        print("  Interpretation: looks like an OAuth2 refresh token.")
        print("  Usable for XOAUTH2? NO — requires exchange for an access token.")
        print("  The exchange needs Mail.app's Google client_id + client_secret,")
        print("  which are private to Apple. We cannot legitimately exchange it.")
        return False
    if token.startswith("{") or token.startswith("["):
        print("  Interpretation: JSON blob (may contain access_token, refresh_token,")
        print("  expires_at, etc.). The access_token sub-field, if present and")
        print("  unexpired, could be used. If only a refresh_token is present,")
        print("  same issue as above.")
        return False
    print("  Interpretation: unknown format — manual inspection required.")
    return False


def stage4_xoauth2_login(email: str, access_token: str) -> bool:
    """Attempt XOAUTH2 AUTHENTICATE against imap.gmail.com."""
    print()
    print("=" * 70)
    print("STAGE 4: XOAUTH2 AUTHENTICATE against imap.gmail.com:993")
    print("=" * 70)
    try:
        from imapclient import IMAPClient  # type: ignore[import-not-found]
    except ImportError:
        print("  SKIP: imapclient not installed. Install with `uv pip install -e '.[research]'`.")
        return False

    client = IMAPClient("imap.gmail.com", port=993, ssl=True)
    try:
        client.oauth2_login(email, access_token)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
    print("  OK: XOAUTH2 authenticated. Option (b) is viable for this token.")
    return True


def main() -> int:
    items = stage1_locate_google_oauth_items()
    if not items:
        print()
        print("VERDICT: option (b) not testable — no Google OAuth items found.")
        return 1

    # Try the first item. If there are multiple accounts, the user can re-run
    # with different targets — for the spike, one is enough.
    item = items[0]
    token = stage2_retrieve_token(item)
    if token is None:
        print()
        print("VERDICT: option (b) blocked at stage 2 — token not retrievable without")
        print("         user involvement in a way that scales. The ACL model would")
        print("         require the user to click 'Always Allow' per account, which")
        print("         is acceptable UX only if subsequent stages actually work.")
        return 2

    usable = stage3_inspect_token(token)
    if not usable:
        print()
        print("VERDICT: option (b) blocked at stage 3 — token is not a directly")
        print("         usable access token. Exchanging it requires Mail.app's")
        print("         private OAuth client credentials.")
        return 3

    ok = stage4_xoauth2_login(item.account, token)
    print()
    if ok:
        print("VERDICT: option (b) WORKS end-to-end, with the caveat that the")
        print("         access token expires in ~1 hour. Production use would")
        print("         require either (i) refreshing via Mail.app's private")
        print("         client creds — not possible — or (ii) the user registering")
        print("         their own Google OAuth client, which is heavier setup")
        print("         than option (a).")
        return 0
    print("VERDICT: option (b) failed at stage 4. See error above.")
    return 4


if __name__ == "__main__":
    sys.exit(main())
