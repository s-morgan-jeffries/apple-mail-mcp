"""Persisted per-account IMAP login overrides (#341).

`_resolve_imap_config` derives the IMAP LOGIN username from Mail.app's
account properties. For a few account shapes that derivation is wrong and
can't be corrected from the properties alone — notably an iCloud account
whose Apple ID is a third-party email (e.g. ``@gmail.com``) with no
``@icloud.com`` alias in Mail.app's ``email addresses`` (#299's apple-alias
rule has nothing to pick from). The login then fails with
AUTHENTICATIONFAILED against ``*.mail.me.com``.

This module persists an explicit ``account -> login email`` override the
user supplies via ``setup-imap --email``, so runtime resolution honors the
same login that setup verified. The override value is a non-secret email
address (the password stays in the Keychain), so a small JSON file under
``~/.apple_mail_mcp/`` is the right home — matching the templates/ and
drafts/ stores.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _overrides_path() -> Path:
    """Path to the overrides file, honoring ``APPLE_MAIL_MCP_HOME``.

    Resolved at call time so env-var overrides and test-time monkeypatching
    are honored (same convention as templates/drafts ``default_root``).
    """
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = (
        Path(home_override)
        if home_override
        else Path.home() / ".apple_mail_mcp"
    )
    return base / "imap_login_overrides.json"


def _load() -> dict[str, str]:
    """Load the override map. A missing, unreadable, or corrupt file yields
    an empty map — overrides must never raise into the IMAP resolve path."""
    path = _overrides_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only str->str entries; ignore anything malformed.
    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def get_login_override(account: str) -> str | None:
    """Return the persisted IMAP login email for ``account``, or ``None``.

    Empty/whitespace-only stored values are treated as absent.
    """
    value = _load().get(account)
    if value and value.strip():
        return value.strip()
    return None


def set_login_override(account: str, email: str) -> None:
    """Persist ``account -> email`` (the IMAP LOGIN username). Creates the
    home directory and file if needed; merges with any existing entries."""
    overrides = _load()
    overrides[account] = email.strip()
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(overrides, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def delete_login_override(account: str) -> None:
    """Remove ``account``'s override if present. No-op when absent or when
    the file doesn't exist."""
    overrides = _load()
    if account not in overrides:
        return
    del overrides[account]
    path = _overrides_path()
    if overrides:
        path.write_text(
            json.dumps(overrides, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        # Last entry removed — drop the file so an empty store leaves no
        # stray artifact.
        path.unlink(missing_ok=True)
