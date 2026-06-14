"""CLI subcommands for the ``apple-mail-fast-mcp`` entry point.

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
from .imap_overrides import delete_login_override, set_login_override
from .keychain import (
    delete_imap_password,
    get_imap_password,
    set_imap_password,
)
from .mail_connector import AppleMailConnector
from .utils import is_apple_hosted_address, is_icloud_imap_host


def _print_available_accounts(accounts: list[dict[str, object]]) -> None:
    if not accounts:
        print("  (no accounts found in Mail.app)", file=sys.stderr)
        return
    for acc in accounts:
        name = acc.get("name") or "(unnamed)"
        print(f"  - {name}", file=sys.stderr)


def _account_exists(
    accounts: list[dict[str, object]], requested_name: str
) -> bool:
    return any(acc.get("name") == requested_name for acc in accounts)


def _maybe_print_icloud_login_hint(
    cli_email: str | None, host: str, email: str
) -> None:
    """On an iCloud login failure, if no `--email` was given and the login
    we tried isn't an Apple-hosted address, this is the classic #341 shape
    (third-party Apple ID, no @icloud.com alias in Mail.app). Point the user
    at the override path. No-op otherwise (incl. legitimate #201 custom-domain
    accounts, whose login succeeds and never reaches here)."""
    if (
        not cli_email
        and is_icloud_imap_host(host)
        and not is_apple_hosted_address(email)
    ):
        print(
            "  Hint: this looks like an iCloud account whose Apple ID is "
            "a third-party email. Re-run with `--email <your "
            "@icloud.com/@me.com address>` to set the correct IMAP login.",
            file=sys.stderr,
        )


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

    if not _account_exists(accounts, account_name):
        print(
            f"ERROR: No Mail.app account named {account_name!r}. "
            "Available accounts:",
            file=sys.stderr,
        )
        _print_available_accounts(accounts)
        return 1

    # Resolve IMAP config upfront. The connector calls _resolve_imap_config
    # at every runtime IMAP request to pick the keychain key — so we use
    # the same email here as the keychain key, otherwise setup writes one
    # entry and runtime looks for another. (#201)
    try:
        host, port, resolved_email = mail._resolve_imap_config(account_name)
    except MailAccountNotFoundError:
        print(
            f"ERROR: Mail.app stopped recognizing {account_name!r} "
            "between the account list and the IMAP config lookup.",
            file=sys.stderr,
        )
        return 1

    # `--email` is the escape hatch for the rare case where Mail.app's
    # `user name` is empty or wrong; default to the resolved value so
    # setup, uninstall, and runtime all share one keychain key.
    email = cli_email or resolved_email
    if not email:
        print(
            f"ERROR: Account {account_name!r} has no IMAP login username "
            "in Mail.app (neither `user name` nor `email addresses`). "
            "Pass --email <email> to set one explicitly.",
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
        # Also drop any persisted login override (#341) so a re-setup starts
        # from Mail.app's derived login rather than a stale override.
        delete_login_override(account_name)
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
        f"Stored in Keychain as 'apple-mail-fast-mcp.imap.{account_name}'."
    )
    # Persist an explicit --email as a login override so runtime resolution
    # (_resolve_imap_config) uses the same login this setup verifies — without
    # it, runtime re-derives the login from Mail.app and ignores --email (#341).
    if cli_email:
        set_login_override(account_name, email)

    print(f"Testing IMAP connection to {host}:{port}...")
    imap = (imap_factory or ImapConnector)(host, port, email, password)
    try:
        # Use a cheap read-only call; search_messages with limit=1 is enough
        # to exercise login + folder select without paging much data.
        imap.search_messages(mailbox="INBOX", limit=1)
    except LoginError as exc:
        # Bad password — roll the entry back so the user can retry without
        # leaving a broken Keychain item that get_imap_password would
        # happily return. Mirror the rollback for any override just written.
        try:
            delete_imap_password(account_name, email)
        except MailKeychainError:
            pass
        if cli_email:
            delete_login_override(account_name)
        print(
            f"ERROR: IMAP login was rejected ({exc}). The Keychain entry "
            "has been removed; please re-run with the correct password.",
            file=sys.stderr,
        )
        _maybe_print_icloud_login_hint(cli_email, host, email)
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
