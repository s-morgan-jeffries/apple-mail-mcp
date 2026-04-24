"""Exception class hierarchy tests."""

import pytest

from apple_mail_mcp.exceptions import (
    MailError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)


class TestKeychainExceptions:
    def test_keychain_error_is_mail_error(self):
        assert issubclass(MailKeychainError, MailError)

    def test_entry_not_found_is_keychain_error(self):
        assert issubclass(MailKeychainEntryNotFoundError, MailKeychainError)

    def test_access_denied_is_keychain_error(self):
        assert issubclass(MailKeychainAccessDeniedError, MailKeychainError)

    def test_entry_not_found_can_be_raised_and_caught(self):
        with pytest.raises(MailKeychainEntryNotFoundError):
            raise MailKeychainEntryNotFoundError("not found")

    def test_access_denied_can_be_caught_as_keychain_error(self):
        with pytest.raises(MailKeychainError):
            raise MailKeychainAccessDeniedError("denied")
