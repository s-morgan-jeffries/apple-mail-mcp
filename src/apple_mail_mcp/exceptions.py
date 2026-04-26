"""
Custom exceptions for Apple Mail MCP operations.
"""


class MailError(Exception):
    """Base exception for Mail operations."""

    pass


class MailAccountNotFoundError(MailError):
    """Account does not exist."""

    pass


class MailMailboxNotFoundError(MailError):
    """Mailbox does not exist."""

    pass


class MailMessageNotFoundError(MailError):
    """Message does not exist."""

    pass


class MailAppleScriptError(MailError):
    """AppleScript execution failed."""

    pass


class MailPermissionError(MailError):
    """Permission denied for operation."""

    pass


class MailOperationCancelledError(MailError):
    """User cancelled the operation."""

    pass


class MailSafetyError(MailError):
    """Safety check failed in test mode (wrong account or non-reserved recipient)."""

    pass


class MailKeychainError(MailError):
    """Keychain operation failed."""

    pass


class MailKeychainEntryNotFoundError(MailKeychainError):
    """Requested Keychain entry does not exist.

    Expected and benign: signals the user has not opted in to IMAP
    for this account. Delegation layer (future work) treats this as
    a silent fall-back-to-AppleScript signal.
    """

    pass


class MailKeychainAccessDeniedError(MailKeychainError):
    """Keychain refused access (ACL denied or user denied prompt).

    Worth surfacing to the user on first failure per the graceful-
    degradation invariants in imap-auth-options-decision.md.
    """

    pass


class MailRuleNotFoundError(MailError):
    """Rule index is out of range — no such rule exists in Mail.app."""

    pass


class MailUnsupportedRuleActionError(MailError):
    """update_rule was called on a rule whose existing actions include
    one that's not modeled in our schema (e.g. run-AppleScript,
    redirect, reply, play sound). Read access via list_rules is
    unaffected; only mutating an existing rule with these actions is
    refused.
    """

    pass


class MailTemplateError(MailError):
    """Base class for email-template errors."""

    pass


class MailTemplateNotFoundError(MailTemplateError):
    """No template exists with the requested name."""

    pass


class MailTemplateInvalidNameError(MailTemplateError):
    """Template name fails validation (path traversal, invalid chars,
    too long, or empty). Names must match ^[a-zA-Z0-9_-]{1,64}$."""

    pass


class MailTemplateInvalidFormatError(MailTemplateError):
    """A file in the templates directory could not be parsed as a
    template (malformed header, unreadable, or empty body)."""

    pass


class MailTemplateMissingVariableError(MailTemplateError):
    """render_template encountered a {placeholder} with no matching
    auto-fill or user-supplied variable. The exception message names
    the missing placeholder(s)."""

    pass
