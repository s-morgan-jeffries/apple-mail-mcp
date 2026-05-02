"""macOS Keychain password storage / retrieval for IMAP credentials.

Entries live under service name
``apple-mail-mcp.imap.<mail_app_account_name>`` keyed by the account's
email. The ``apple-mail-mcp setup-imap`` CLI is the supported way to
write entries; this module also exposes set/delete helpers that the
CLI uses, plus the read helper used by the IMAP fallback path at
runtime.

See ``docs/research/imap-auth-options-decision.md`` for the chosen
auth path and the service-name convention, and
``docs/plans/2026-04-23-imap-connector-design.md`` for module-level
design decisions.
"""

from __future__ import annotations

import subprocess

from apple_mail_mcp.exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)

SERVICE_NAME_PREFIX = "apple-mail-mcp.imap."

_EXIT_ITEM_NOT_FOUND = 44
_EXIT_INTERACTION_NOT_ALLOWED = 128
_ACCESS_DENIED_MARKERS = ("-25308", "-128", "not allowed", "user canceled")


def get_imap_password(mail_app_account: str, email: str) -> str:
    """Return the app-specific password stored in Keychain.

    Args:
        mail_app_account: Mail.app account name (e.g. "iCloud", "Gmail").
        email: Email address the password is keyed to.

    Returns:
        The password, as stored (trailing newline from ``security -w`` stripped).

    Raises:
        MailKeychainEntryNotFoundError: No matching Keychain item.
        MailKeychainAccessDeniedError: ACL or user denial.
        MailKeychainError: Any other ``security(1)`` failure.
    """
    service = SERVICE_NAME_PREFIX + mail_app_account
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                service,
                "-a",
                email,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MailKeychainError(f"`security` binary not found: {exc}") from exc

    if result.returncode == 0:
        return result.stdout.rstrip("\n")

    stderr = result.stderr or ""

    if result.returncode == _EXIT_ITEM_NOT_FOUND:
        raise MailKeychainEntryNotFoundError(
            f"No Keychain entry for service={service!r}, account={email!r}."
        )

    if result.returncode == _EXIT_INTERACTION_NOT_ALLOWED or any(
        marker in stderr for marker in _ACCESS_DENIED_MARKERS
    ):
        raise MailKeychainAccessDeniedError(
            f"Keychain access denied for service={service!r}, account={email!r}: "
            f"{stderr.strip()}"
        )

    raise MailKeychainError(
        f"security find-generic-password failed (exit {result.returncode}): "
        f"{stderr.strip()}"
    )


def set_imap_password(
    mail_app_account: str, email: str, password: str
) -> None:
    """Write or update an IMAP app password to Keychain.

    Uses ``security add-generic-password ... -U`` so re-running with a
    new password updates the existing entry instead of failing with a
    duplicate-item error. The password is passed as an argument to
    ``security`` (no shell interpolation, no env var); ``subprocess.run``
    keeps it out of any shell history.

    Args:
        mail_app_account: Mail.app account name (e.g. "iCloud", "Gmail").
        email: Email address the password is keyed to.
        password: The app-specific password to store.

    Raises:
        MailKeychainAccessDeniedError: ACL or user denial.
        MailKeychainError: Any other ``security(1)`` failure.
    """
    service = SERVICE_NAME_PREFIX + mail_app_account
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                service,
                "-a",
                email,
                "-w",
                password,
                "-U",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MailKeychainError(f"`security` binary not found: {exc}") from exc

    if result.returncode == 0:
        return

    stderr = result.stderr or ""
    if result.returncode == _EXIT_INTERACTION_NOT_ALLOWED or any(
        marker in stderr for marker in _ACCESS_DENIED_MARKERS
    ):
        raise MailKeychainAccessDeniedError(
            f"Keychain access denied for service={service!r}, account={email!r}: "
            f"{stderr.strip()}"
        )

    raise MailKeychainError(
        f"security add-generic-password failed (exit {result.returncode}): "
        f"{stderr.strip()}"
    )


def delete_imap_password(mail_app_account: str, email: str) -> None:
    """Remove the Keychain entry for an account.

    Args:
        mail_app_account: Mail.app account name.
        email: Email address the password was keyed to.

    Raises:
        MailKeychainEntryNotFoundError: No matching Keychain item to delete.
        MailKeychainAccessDeniedError: ACL or user denial.
        MailKeychainError: Any other ``security(1)`` failure.
    """
    service = SERVICE_NAME_PREFIX + mail_app_account
    try:
        result = subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                service,
                "-a",
                email,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MailKeychainError(f"`security` binary not found: {exc}") from exc

    if result.returncode == 0:
        return

    stderr = result.stderr or ""
    if result.returncode == _EXIT_ITEM_NOT_FOUND:
        raise MailKeychainEntryNotFoundError(
            f"No Keychain entry for service={service!r}, account={email!r}."
        )
    if result.returncode == _EXIT_INTERACTION_NOT_ALLOWED or any(
        marker in stderr for marker in _ACCESS_DENIED_MARKERS
    ):
        raise MailKeychainAccessDeniedError(
            f"Keychain access denied for service={service!r}, account={email!r}: "
            f"{stderr.strip()}"
        )

    raise MailKeychainError(
        f"security delete-generic-password failed (exit {result.returncode}): "
        f"{stderr.strip()}"
    )
