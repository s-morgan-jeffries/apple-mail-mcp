"""CLI subcommands for the ``apple-mail-mcp`` entry point.

Today the only subcommand is ``setup-imap`` (issue #76). The default
invocation (no subcommand) starts the MCP server — that path lives in
``server.main()`` and is unaffected by this module.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable

from imapclient.exceptions import IMAPClientError, LoginError

from .exceptions import (
    MailAccountNotFoundError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)
from .imap_connector import ImapConnector
from .keychain import (
    delete_imap_password,
    get_imap_password,
    set_imap_password,
)
from .mail_connector import AppleMailConnector


def _print_available_accounts(accounts: list[dict[str, object]]) -> None:
    if not accounts:
        print("  (no accounts found in Mail.app)", file=sys.stderr)
        return
    for acc in accounts:
        name = acc.get("name") or "(unnamed)"
        print(f"  - {name}", file=sys.stderr)


def _resolve_account(
    accounts: list[dict[str, object]], requested_name: str
) -> dict[str, object] | None:
    for acc in accounts:
        if acc.get("name") == requested_name:
            return acc
    return None


def _resolve_email(
    account: dict[str, object], cli_email: str | None
) -> str | None:
    if cli_email:
        return cli_email
    emails = account.get("email_addresses") or []
    if isinstance(emails, list) and emails:
        first = emails[0]
        if isinstance(first, str) and first:
            return first
    return None


def run_setup_imap(
    *,
    account_name: str,
    cli_email: str | None,
    uninstall: bool,
    connector_factory: Callable[[], AppleMailConnector] | None = None,
    getpass_fn: Callable[[str], str] | None = None,
    imap_factory: Callable[
        [str, int, str, str], ImapConnector
    ] | None = None,
) -> int:
    """Run the ``setup-imap`` subcommand. Returns the desired exit code.

    The ``*_factory`` and ``*_fn`` keyword-only arguments are injection
    seams for unit tests. Production callers omit them and get the real
    implementations.
    """
    mail = (connector_factory or AppleMailConnector)()
    accounts = mail.list_accounts()
    matched = _resolve_account(accounts, account_name)

    if matched is None:
        print(
            f"ERROR: No Mail.app account named {account_name!r}. "
            "Available accounts:",
            file=sys.stderr,
        )
        _print_available_accounts(accounts)
        return 1

    email = _resolve_email(matched, cli_email)
    if not email:
        print(
            f"ERROR: Account {account_name!r} has no email addresses in "
            "Mail.app. Pass --email <email> to set one explicitly.",
            file=sys.stderr,
        )
        return 1

    if uninstall:
        try:
            delete_imap_password(account_name, email)
        except MailKeychainEntryNotFoundError:
            print(
                f"No Keychain entry to remove for {account_name!r} "
                f"({email}).",
                file=sys.stderr,
            )
            return 1
        except MailKeychainError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Removed Keychain entry for {account_name!r} ({email}).")
        return 0

    print(
        f"Found Mail.app account {account_name!r} (email: {email})."
    )

    prompt = "Enter app-specific password: "
    try:
        password = (getpass_fn or getpass.getpass)(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1
    if not password:
        print("ERROR: empty password.", file=sys.stderr)
        return 1

    try:
        set_imap_password(account_name, email, password)
    except MailKeychainError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Stored in Keychain as 'apple-mail-mcp.imap.{account_name}'."
    )

    # Verification: derive host/port from Mail.app and try a real LOGIN.
    try:
        host, port, resolved_email = mail._resolve_imap_config(account_name)
    except MailAccountNotFoundError:
        # Shouldn't happen — we just listed it — but surface clearly.
        print(
            f"ERROR: Mail.app stopped recognizing {account_name!r} "
            "between the account list and the IMAP config lookup.",
            file=sys.stderr,
        )
        return 1

    # Use the email Mail.app actually expects for IMAP LOGIN, which may
    # differ from `--email` for iCloud (Apple ID alias vs. @icloud.com).
    verify_email = resolved_email or email

    print(f"Testing IMAP connection to {host}:{port}...")
    imap = (imap_factory or ImapConnector)(host, port, verify_email, password)
    try:
        # Use a cheap read-only call; search_messages with limit=1 is enough
        # to exercise login + folder select without paging much data.
        imap.search_messages(mailbox="INBOX", limit=1)
    except LoginError as exc:
        # Bad password — roll the entry back so the user can retry without
        # leaving a broken Keychain item that get_imap_password would
        # happily return.
        try:
            delete_imap_password(account_name, email)
        except MailKeychainError:
            pass
        print(
            f"ERROR: IMAP login was rejected ({exc}). The Keychain entry "
            "has been removed; please re-run with the correct password.",
            file=sys.stderr,
        )
        return 1
    except (OSError, IMAPClientError) as exc:
        # Network / protocol error. The password may be fine but we can't
        # verify right now. Keep the entry; warn explicitly.
        print(
            f"WARNING: IMAP verification could not complete ({exc}). The "
            "Keychain entry has been written but was not verified against "
            "the server. Re-run setup-imap later or test live to confirm.",
            file=sys.stderr,
        )
        return 0

    print(f"OK (connected to {host}:{port})")
    print("Setup complete.")
    return 0


__all__ = ["run_setup_imap", "get_imap_password"]
