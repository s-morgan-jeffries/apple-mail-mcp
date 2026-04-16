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
