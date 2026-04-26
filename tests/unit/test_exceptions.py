"""Exception class hierarchy tests."""

import pytest

from apple_mail_mcp.exceptions import (
    MailError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
    MailRuleNotFoundError,
    MailTemplateError,
    MailTemplateInvalidFormatError,
    MailTemplateInvalidNameError,
    MailTemplateMissingVariableError,
    MailTemplateNotFoundError,
    MailUnsupportedRuleActionError,
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


class TestRuleExceptions:
    def test_rule_not_found_is_mail_error(self):
        assert issubclass(MailRuleNotFoundError, MailError)

    def test_unsupported_action_is_mail_error(self):
        assert issubclass(MailUnsupportedRuleActionError, MailError)

    def test_rule_not_found_can_be_raised_and_caught(self):
        with pytest.raises(MailRuleNotFoundError):
            raise MailRuleNotFoundError("rule index 99 out of range")

    def test_unsupported_action_can_be_raised_and_caught(self):
        with pytest.raises(MailUnsupportedRuleActionError):
            raise MailUnsupportedRuleActionError("rule uses run-AppleScript")


class TestTemplateExceptions:
    def test_template_error_is_mail_error(self):
        assert issubclass(MailTemplateError, MailError)

    def test_not_found_is_template_error(self):
        assert issubclass(MailTemplateNotFoundError, MailTemplateError)

    def test_invalid_name_is_template_error(self):
        assert issubclass(MailTemplateInvalidNameError, MailTemplateError)

    def test_invalid_format_is_template_error(self):
        assert issubclass(MailTemplateInvalidFormatError, MailTemplateError)

    def test_missing_variable_is_template_error(self):
        assert issubclass(MailTemplateMissingVariableError, MailTemplateError)

    def test_not_found_can_be_caught_as_template_error(self):
        with pytest.raises(MailTemplateError):
            raise MailTemplateNotFoundError("no template named foo")

    def test_missing_variable_can_be_raised_and_caught(self):
        with pytest.raises(MailTemplateMissingVariableError):
            raise MailTemplateMissingVariableError("missing: due_date")
