"""Integration tests for ImapConnector against real iCloud.

Guarded by ``MAIL_TEST_MODE=true``. Requires a Keychain entry:

    security add-generic-password \\
        -s "apple-mail-mcp.imap.iCloud" \\
        -a "s.morgan.jeffries@icloud.com" \\
        -w "<APP_PASSWORD>" -T "" -U

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=iCloud \\
        uv run pytest tests/integration/test_imap_connector.py -v
"""

from __future__ import annotations

import os

import pytest

from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_mcp.imap_connector import ImapConnector
from apple_mail_mcp.keychain import get_imap_password

ICLOUD_HOST = "imap.mail.me.com"
ICLOUD_PORT = 993
ICLOUD_ACCOUNT_NAME = "iCloud"
ICLOUD_EMAIL = "s.morgan.jeffries@icloud.com"


def _test_mode_enabled() -> bool:
    return os.getenv("MAIL_TEST_MODE") == "true"


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestEndToEndICloud:
    def test_end_to_end_search_returns_list(self):
        password = get_imap_password(ICLOUD_ACCOUNT_NAME, ICLOUD_EMAIL)
        connector = ImapConnector(
            ICLOUD_HOST, ICLOUD_PORT, ICLOUD_EMAIL, password
        )
        result = connector.search_messages(limit=5)
        assert isinstance(result, list)
        # May be empty (per PR #70 spike finding — merged-away Apple ID's
        # residual mailbox). Any non-empty result must have the standard
        # keys matching mail_connector.search_messages output shape.
        expected_keys = {
            "id",
            "subject",
            "sender",
            "date_received",
            "read_status",
            "flagged",
        }
        for msg in result:
            assert set(msg.keys()) == expected_keys
            assert isinstance(msg["read_status"], bool)
            assert isinstance(msg["flagged"], bool)

    def test_keychain_entry_missing_raises_entry_not_found(self):
        with pytest.raises(MailKeychainEntryNotFoundError):
            get_imap_password("DoesNotExistAccount", "nobody@example.com")
