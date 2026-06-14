"""macOS Keychain password storage / retrieval for IMAP credentials.

Entries live under service name
``apple-mail-fast-mcp.imap.<mail_app_account_name>`` keyed by the account's
email. The ``apple-mail-fast-mcp setup-imap`` CLI is the supported way to
write entries; this module also exposes set/delete helpers that the
CLI uses, plus the read helper used by the IMAP fallback path at
runtime.

Read-through fallback (#337): the brand was renamed in #335. Reads and
deletes prefer the new ``apple-mail-fast-mcp.imap.`` prefix and fall back to
the pre-rename ``apple-mail-mcp.imap.`` prefix on a NotFound miss, so existing
entries keep working with zero user action. Writes go to the new prefix only.
The fallback is dropped at 1.0.0 (documented breaking change — re-run
``setup-imap``).

See ``docs/research/imap-auth-options-decision.md`` for the chosen
auth path and the service-name convention, and
``docs/plans/2026-04-23-imap-connector-design.md`` for module-level
design decisions.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import NoReturn

from apple_mail_mcp.exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)

logger = logging.getLogger(__name__)

SERVICE_NAME_PREFIX = "apple-mail-fast-mcp.imap."

# Read/delete fallback for entries written before the #335 rebrand.
# Drop at 1.0.0 (the breaking-change follow-up to #337).
_LEGACY_SERVICE_NAME_PREFIX = "apple-mail-mcp.imap."

# Env-var fallback for the IMAP password (#248). Convention:
# APPLE_MAIL_MCP_IMAP_PASSWORD_<SUFFIX>, where <SUFFIX> is the account name
# uppercased with runs of non-[A-Z0-9] collapsed to a single underscore and
# leading/trailing underscores trimmed (e.g. "My Gmail" -> "MY_GMAIL").
IMAP_PASSWORD_ENV_PREFIX = "APPLE_MAIL_MCP_IMAP_PASSWORD_"

_ENV_SUFFIX_SEP_RE = re.compile(r"[^A-Z0-9]+")

_EXIT_ITEM_NOT_FOUND = 44
_EXIT_INTERACTION_NOT_ALLOWED = 128
_ACCESS_DENIED_MARKERS = ("-25308", "-128", "not allowed", "user canceled")


def _env_var_name(mail_app_account: str) -> str | None:
    """Return the env-var name an account's IMAP password may be read from,
    or ``None`` when the account name has no ASCII alphanumerics to build a
    usable suffix from (e.g. an all-non-ASCII name — use Keychain instead).

    The mapping is not injective: ``"Yahoo!"`` and ``"Yahoo"`` both yield
    ``YAHOO``. This is documented; distinct accounts that collide must use
    Keychain. (#248)
    """
    suffix = _ENV_SUFFIX_SEP_RE.sub("_", mail_app_account.upper()).strip("_")
    if not suffix:
        return None
    return IMAP_PASSWORD_ENV_PREFIX + suffix


def _run_security(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``security`` capturing output; map a missing binary to
    ``MailKeychainError`` (the only failure not signalled via exit code)."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MailKeychainError(f"`security` binary not found: {exc}") from exc


def _raise_security_failure(
    result: subprocess.CompletedProcess[str],
    service: str,
    email: str,
    action: str,
) -> NoReturn:
    """Map a non-zero ``security`` exit to the right exception. NotFound is
    handled by callers (it drives the legacy-prefix fallback); this raises
    AccessDenied vs. a generic Keychain error for everything else."""
    stderr = result.stderr or ""
    if result.returncode == _EXIT_INTERACTION_NOT_ALLOWED or any(
        marker in stderr for marker in _ACCESS_DENIED_MARKERS
    ):
        raise MailKeychainAccessDeniedError(
            f"Keychain access denied for service={service!r}, account={email!r}: "
            f"{stderr.strip()}"
        )
    raise MailKeychainError(
        f"security {action} failed (exit {result.returncode}): "
        f"{stderr.strip()}"
    )


def _find_password(service: str, email: str) -> str:
    """Read one Keychain entry for an exact service name. Raises
    ``MailKeychainEntryNotFoundError`` on a miss (lets the caller fall back to
    the legacy prefix), or AccessDenied / generic errors via
    ``_raise_security_failure``."""
    result = _run_security(
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            service,
            "-a",
            email,
        ]
    )
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode == _EXIT_ITEM_NOT_FOUND:
        raise MailKeychainEntryNotFoundError(
            f"No Keychain entry for service={service!r}, account={email!r}."
        )
    _raise_security_failure(result, service, email, "find-generic-password")


def _delete_password(service: str, email: str) -> None:
    """Delete one Keychain entry for an exact service name. Raises
    ``MailKeychainEntryNotFoundError`` on a miss (lets the caller fall back to
    the legacy prefix), or AccessDenied / generic errors."""
    result = _run_security(
        [
            "security",
            "delete-generic-password",
            "-s",
            service,
            "-a",
            email,
        ]
    )
    if result.returncode == 0:
        return
    if result.returncode == _EXIT_ITEM_NOT_FOUND:
        raise MailKeychainEntryNotFoundError(
            f"No Keychain entry for service={service!r}, account={email!r}."
        )
    _raise_security_failure(result, service, email, "delete-generic-password")


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

    Env-var fallback (#248): for uvx / headless / CI contexts where the
    Keychain isn't usable, an env var named per ``_env_var_name`` is checked
    first; a present, non-empty value is returned without shelling out to
    ``security``. This is less private than Keychain (env vars show up in
    ``ps`` / ``launchctl env`` / crash dumps) — see the README.
    """
    env_name = _env_var_name(mail_app_account)
    if env_name:
        env_pw = os.environ.get(env_name)
        if env_pw and env_pw.strip():
            logger.debug(
                "Using env-var IMAP password (%s) for account %r",
                env_name,
                mail_app_account,
            )
            # Strip surrounding whitespace — .env files / Docker / `export`
            # commonly append a trailing newline, and sending it as part of
            # the password fails LOGIN. Mirrors the Keychain path's rstrip.
            # (#349)
            return env_pw.strip()
    # Read-through fallback (#337): prefer the new prefix, retry the legacy one
    # only on a NotFound miss. AccessDenied / generic errors propagate from the
    # first attempt — they're explicit macOS signals, not "wrong prefix".
    try:
        return _find_password(SERVICE_NAME_PREFIX + mail_app_account, email)
    except MailKeychainEntryNotFoundError:
        return _find_password(
            _LEGACY_SERVICE_NAME_PREFIX + mail_app_account, email
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
    # Writes go to the new prefix only (#337); no legacy fallback on writes.
    service = SERVICE_NAME_PREFIX + mail_app_account
    result = _run_security(
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
        ]
    )
    if result.returncode == 0:
        return
    _raise_security_failure(result, service, email, "add-generic-password")


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
    # Delete-through fallback (#337): try the new prefix, then the legacy one
    # on a NotFound miss, so a pre-rename entry can still be removed.
    try:
        _delete_password(SERVICE_NAME_PREFIX + mail_app_account, email)
    except MailKeychainEntryNotFoundError:
        _delete_password(_LEGACY_SERVICE_NAME_PREFIX + mail_app_account, email)
