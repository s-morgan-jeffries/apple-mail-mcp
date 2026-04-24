"""Integration tests for search_messages IMAP delegation against real iCloud.

Guarded by ``MAIL_TEST_MODE=true``. The positive test requires a Keychain
entry keyed to the Apple ID email (what Mail.app returns for `user name`),
not an @icloud.com alias::

    security add-generic-password \\
        -s "apple-mail-mcp.imap.iCloud" \\
        -a "<apple-id-email>" \\
        -w "<APP_PASSWORD>" \\
        -T "" -U

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=iCloud \\
        uv run pytest tests/integration/test_imap_delegation.py -v
"""

from __future__ import annotations

import logging
import os
import subprocess

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

ICLOUD_ACCOUNT_NAME = "iCloud"


def _test_mode_enabled() -> bool:
    return os.getenv("MAIL_TEST_MODE") == "true"


def _keychain_entry_exists(account_name: str, email: str) -> bool:
    service = f"apple-mail-mcp.imap.{account_name}"
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", email],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector()


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestIMAPDelegation:
    def test_search_messages_uses_imap_when_keychain_entry_present(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Positive path: real iCloud, Keychain entry present, search goes via IMAP.

        Skipped if the user hasn't set up the Apple-ID-keyed Keychain entry
        — this test requires an entry keyed to Mail.app's 'user name'
        property (the Apple ID), not an alias.
        """
        host, port, email = connector._resolve_imap_config(ICLOUD_ACCOUNT_NAME)
        if not _keychain_entry_exists(ICLOUD_ACCOUNT_NAME, email):
            pytest.skip(
                f"No Keychain entry under "
                f"apple-mail-mcp.imap.{ICLOUD_ACCOUNT_NAME} for {email}. "
                f"See the test file's module docstring for setup."
            )

        # If IMAP succeeds, _imap_failures stays empty. If anything falls back,
        # the set will contain iCloud. We assert the IMAP path executed.
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.search_messages(
                account=ICLOUD_ACCOUNT_NAME, limit=5
            )

        assert isinstance(result, list)
        # Search may be empty (iCloud inbox often is for this user), but the
        # IMAP path must have been used — which means the failures set is empty
        # AND we did not emit any WARNING about falling back.
        assert ICLOUD_ACCOUNT_NAME not in connector._imap_failures
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"Expected IMAP path to succeed silently, but got warnings: "
            f"{[r.getMessage() for r in warnings]}"
        )

        # Any messages returned must have the standard keys. (iCloud may
        # legitimately return [] — that's a successful IMAP search.)
        expected_keys = {
            "id", "subject", "sender", "date_received",
            "read_status", "flagged",
        }
        for msg in result:
            assert set(msg.keys()) == expected_keys

    def test_search_messages_falls_back_when_imap_host_unroutable(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Negative path: IMAP connect times out, AppleScript path runs.

        Monkey-patches _resolve_imap_config to return an unroutable host
        (10.255.255.1:993 — TEST-NET-1-adjacent; guaranteed to not route).
        Also stubs get_imap_password so the Keychain lookup doesn't short-
        circuit the test with a benign MailKeychainEntryNotFoundError —
        we want the 3s IMAP connect timeout to fire, OSError to propagate,
        and the first-failure WARNING path to execute.
        """
        def fake_config(_account: str) -> tuple[str, int, str]:
            return ("10.255.255.1", 993, "fake@example.com")

        monkeypatch.setattr(connector, "_resolve_imap_config", fake_config)
        monkeypatch.setattr(
            "apple_mail_mcp.mail_connector.get_imap_password",
            lambda _account, _email: "fake-password",
        )

        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.search_messages(
                account=ICLOUD_ACCOUNT_NAME, limit=5
            )

        # AppleScript path succeeded despite IMAP failure. iCloud may return
        # an empty inbox; the key assertion is that we got a list back at all
        # (i.e. search_messages didn't raise).
        assert isinstance(result, list)

        # The failures set must contain iCloud.
        assert ICLOUD_ACCOUNT_NAME in connector._imap_failures

        # First failure should log at WARNING.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly one WARNING, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}"
        )
        msg = warnings[0].getMessage()
        assert ICLOUD_ACCOUNT_NAME in msg

    def test_get_thread_uses_imap_when_keychain_entry_present(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Positive path: real iCloud, IMAP path resolves a thread.

        Discovers a real message ID at runtime (via Mail.app — iCloud's
        INBOX is empty for this user, so we look in Sent Messages where
        we know there are some). Skips if no messages anywhere.
        """
        host, port, email = connector._resolve_imap_config(ICLOUD_ACCOUNT_NAME)
        if not _keychain_entry_exists(ICLOUD_ACCOUNT_NAME, email):
            pytest.skip(
                f"No Keychain entry under "
                f"apple-mail-mcp.imap.{ICLOUD_ACCOUNT_NAME} for {email}."
            )

        # Find any iCloud message to use as anchor.
        anchor_finder = subprocess.run(
            [
                "/usr/bin/osascript", "-e",
                'tell application "Mail" to return id of '
                '(first message of mailbox "Sent Messages" of account "iCloud")',
            ],
            capture_output=True, text=True, check=False,
        )
        if anchor_finder.returncode != 0:
            pytest.skip(
                f"No anchor message available in iCloud Sent Messages: "
                f"{anchor_finder.stderr.strip()}"
            )
        anchor_id = anchor_finder.stdout.strip()
        assert anchor_id, "Anchor finder returned empty stdout"

        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.get_thread(message_id=anchor_id)

        assert isinstance(result, list)
        # IMAP path must have succeeded silently — no fallback WARNING for
        # this account.
        assert ICLOUD_ACCOUNT_NAME not in connector._imap_failures
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"Expected IMAP path to succeed silently, but got warnings: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_get_thread_falls_back_when_imap_host_unroutable(
        self,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Negative path for get_thread: IMAP fails, AppleScript path runs.

        Same monkey-patch trick as the search_messages negative test —
        force the IMAP connect to fail by pointing at an unroutable host,
        and stub the Keychain lookup so we don't short-circuit on the
        benign not-found path.
        """
        # Find an anchor first (anchor resolution stays AppleScript).
        anchor_finder = subprocess.run(
            [
                "/usr/bin/osascript", "-e",
                'tell application "Mail" to return id of '
                '(first message of mailbox "Sent Messages" of account "iCloud")',
            ],
            capture_output=True, text=True, check=False,
        )
        if anchor_finder.returncode != 0:
            pytest.skip(f"No anchor message available: {anchor_finder.stderr.strip()}")
        anchor_id = anchor_finder.stdout.strip()

        def fake_config(_account: str) -> tuple[str, int, str]:
            return ("10.255.255.1", 993, "fake@example.com")

        monkeypatch.setattr(connector, "_resolve_imap_config", fake_config)
        monkeypatch.setattr(
            "apple_mail_mcp.mail_connector.get_imap_password",
            lambda _account, _email: "fake-password",
        )

        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp"):
            result = connector.get_thread(message_id=anchor_id)

        # AppleScript fallback ran and returned the thread (at least the
        # anchor itself).
        assert isinstance(result, list)
        assert len(result) >= 1
        assert ICLOUD_ACCOUNT_NAME in connector._imap_failures
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert ICLOUD_ACCOUNT_NAME in warnings[0].getMessage()
