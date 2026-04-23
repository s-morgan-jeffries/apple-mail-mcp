"""macOS Keychain password retrieval for IMAP credentials.

Users populate Keychain entries via ``security add-generic-password``
with service name ``apple-mail-mcp.imap.<mail_app_account_name>`` and
the account's email as the key. This module retrieves them.

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
