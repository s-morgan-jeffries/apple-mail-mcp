"""Shared fixtures for unit tests."""

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector
from apple_mail_mcp.security import rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state between tests to prevent cross-contamination."""
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _no_applescript_keychain_fallback(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the unit suite off real AppleScript.

    On a Keychain miss, ``_get_imap_password_with_fallback`` calls
    ``_alternative_account_identifier`` → ``list_accounts()`` → real
    ``osascript`` (the #243 name↔UUID fallback). The ~14 keychain-miss
    unit tests mock ``get_imap_password``/``_resolve_imap_config`` but not
    that fallback, so each fired a live osascript call — ~0.85s locally but
    ~30s in CI (Mail.app unresponsive), ballooning the suite to ~5 min
    (#298). Stub it to ``None`` so the fallback re-raises the original
    ``MailKeychainEntryNotFoundError`` (what those tests already assert)
    without touching AppleScript.

    Tests that exercise the real fallback (``TestKeychainDualFormLookup``)
    mock ``list_accounts`` on the instance and opt out via the
    ``real_account_fallback`` marker.
    """
    if request.node.get_closest_marker("real_account_fallback"):
        return
    monkeypatch.setattr(
        AppleMailConnector,
        "_alternative_account_identifier",
        lambda self, account: None,
    )
