#!/usr/bin/env python3
"""Spike for issue #68, option (a): user-supplied Keychain item + IMAPClient.

Reads a user-populated Keychain entry for iCloud Mail, connects to iCloud's IMAP
endpoint, selects INBOX, searches for unread messages, and fetches envelope
headers for the first 5 matches. The purpose is to validate the end-to-end flow
"user creates a Keychain entry with a known service name → server reads it →
IMAPClient authenticates → mailbox operations work" against a real provider.

Requires the `research` optional dependency group:
    uv pip install -e '.[research]'

Precondition: generate an app-specific password at appleid.apple.com and store
it in the Keychain:
    security add-generic-password \\
        -s "apple-mail-mcp.imap.iCloud" \\
        -a "<your-icloud-email>" \\
        -w "<APP_PASSWORD>" \\
        -T "" -U
If the Keychain entry is missing, the script prints this exact command and
exits non-zero.

This script is a research artifact. It is not imported by the package, has no
tests, and is not shipped.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

DEFAULT_ACCOUNT_NAME = "iCloud"
DEFAULT_EMAIL = "s.morgan.jeffries@icloud.com"
DEFAULT_HOST = "imap.mail.me.com"
DEFAULT_PORT = 993
SERVICE_NAME_PREFIX = "apple-mail-mcp.imap."


@dataclass
class Timings:
    keychain_lookup: float = 0.0
    connect: float = 0.0
    login: float = 0.0
    select: float = 0.0
    search: float = 0.0
    fetch: float = 0.0


def lookup_password(service: str, account: str) -> str:
    """Return the password stored in Keychain, or raise a helpful error."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"Keychain entry not found for service={service!r}, account={account!r}.\n"
            f"\n"
            f"To create it, generate an app-specific password at "
            f"https://appleid.apple.com/account/manage, then run:\n"
            f"\n"
            f"    security add-generic-password \\\n"
            f'        -s "{service}" \\\n'
            f'        -a "{account}" \\\n'
            f'        -w "<APP_PASSWORD>" \\\n'
            f'        -T "" -U\n'
            f"\n"
            f"security stderr: {result.stderr.strip()}"
        )
        raise RuntimeError(msg)
    return result.stdout.rstrip("\n")


def format_envelope(envelope: object) -> str:
    """Render an imapclient Envelope as `date / from / subject`."""
    date_raw = getattr(envelope, "date", None)
    subject_raw = getattr(envelope, "subject", None)
    from_list = getattr(envelope, "from_", None) or ()

    if date_raw is None:
        date_str = "<no date>"
    elif isinstance(date_raw, (bytes, str)):
        try:
            date_str = parsedate_to_datetime(
                date_raw.decode() if isinstance(date_raw, bytes) else date_raw
            ).isoformat()
        except (TypeError, ValueError):
            date_str = str(date_raw)
    else:
        date_str = date_raw.isoformat() if hasattr(date_raw, "isoformat") else str(date_raw)

    if isinstance(subject_raw, bytes):
        subject = subject_raw.decode("utf-8", errors="replace")
    else:
        subject = subject_raw or "<no subject>"

    if from_list:
        first = from_list[0]
        name = getattr(first, "name", None)
        mailbox = getattr(first, "mailbox", b"")
        host = getattr(first, "host", b"")
        if isinstance(mailbox, bytes):
            mailbox = mailbox.decode("utf-8", errors="replace")
        if isinstance(host, bytes):
            host = host.decode("utf-8", errors="replace")
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        sender = f"{name} <{mailbox}@{host}>" if name else f"{mailbox}@{host}"
    else:
        sender = "<no sender>"

    return f"  {date_str} | {sender} | {subject}"


def run(email: str, account_name: str, host: str, port: int) -> int:
    service = SERVICE_NAME_PREFIX + account_name
    print(f"Service:  {service}")
    print(f"Account:  {email}")
    print(f"Server:   {host}:{port}")
    print()

    timings = Timings()

    t0 = time.perf_counter()
    try:
        password = lookup_password(service, email)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    timings.keychain_lookup = time.perf_counter() - t0
    print(f"[{timings.keychain_lookup*1000:6.1f} ms] Keychain lookup OK (password length {len(password)})")

    t0 = time.perf_counter()
    try:
        client = IMAPClient(host, port=port, ssl=True)
    except OSError as exc:
        print(f"ERROR: connection failed: {exc}", file=sys.stderr)
        return 3
    timings.connect = time.perf_counter() - t0
    print(f"[{timings.connect*1000:6.1f} ms] TLS connect OK")

    try:
        t0 = time.perf_counter()
        try:
            client.login(email, password)
        except LoginError as exc:
            print(f"ERROR: LOGIN rejected: {exc}", file=sys.stderr)
            print(
                "Note: iCloud requires an app-specific password, not your Apple ID "
                "password. Generate one at appleid.apple.com if you haven't.",
                file=sys.stderr,
            )
            return 4
        timings.login = time.perf_counter() - t0
        print(f"[{timings.login*1000:6.1f} ms] LOGIN OK")

        t0 = time.perf_counter()
        select_info = client.select_folder("INBOX")
        timings.select = time.perf_counter() - t0
        exists = select_info.get(b"EXISTS", b"?")
        print(f"[{timings.select*1000:6.1f} ms] SELECT INBOX OK ({exists} messages total)")

        t0 = time.perf_counter()
        uids = client.search(["UNSEEN"])
        timings.search = time.perf_counter() - t0
        print(f"[{timings.search*1000:6.1f} ms] SEARCH UNSEEN OK ({len(uids)} unread)")

        if not uids:
            t0 = time.perf_counter()
            uids = client.search(["ALL"])
            extra = time.perf_counter() - t0
            print(f"[{extra*1000:6.1f} ms] fallback SEARCH ALL OK ({len(uids)} total)")
            if not uids:
                print("\nNo messages in INBOX to fetch. Spike considered successful through SEARCH.")
                return 0

        target_uids = uids[-5:]
        t0 = time.perf_counter()
        fetched = client.fetch(target_uids, ["ENVELOPE", "FLAGS"])
        timings.fetch = time.perf_counter() - t0
        print(f"[{timings.fetch*1000:6.1f} ms] FETCH envelope+flags for {len(target_uids)} UIDs OK")
        print()
        print(f"Last {len(target_uids)} message(s):")
        for uid in target_uids:
            envelope = fetched[uid][b"ENVELOPE"]
            print(format_envelope(envelope))
    finally:
        client.logout()

    print()
    print("VERDICT: option (a) — user-supplied Keychain item + IMAPClient — WORKS")
    print(
        f"Total time: "
        f"{sum(vars(timings).values())*1000:.1f} ms "
        f"(keychain {timings.keychain_lookup*1000:.0f}, "
        f"connect {timings.connect*1000:.0f}, "
        f"login {timings.login*1000:.0f}, "
        f"select {timings.select*1000:.0f}, "
        f"search {timings.search*1000:.0f}, "
        f"fetch {timings.fetch*1000:.0f} ms)"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default=DEFAULT_EMAIL,
                        help=f"iCloud email address (default: {DEFAULT_EMAIL})")
    parser.add_argument("--account-name", default=DEFAULT_ACCOUNT_NAME,
                        help=f"Mail.app account name (default: {DEFAULT_ACCOUNT_NAME})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"IMAP host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"IMAP port (default: {DEFAULT_PORT})")
    args = parser.parse_args()
    return run(args.email, args.account_name, args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
