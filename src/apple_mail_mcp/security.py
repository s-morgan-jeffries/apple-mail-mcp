"""
Security utilities for Apple Mail MCP.
"""

import logging
from datetime import datetime
from typing import Any

from .utils import validate_email

logger = logging.getLogger(__name__)


class OperationLogger:
    """Log operations for audit trail."""

    def __init__(self) -> None:
        self.operations: list[dict[str, Any]] = []

    def log_operation(
        self, operation: str, parameters: dict[str, Any], result: str = "success"
    ) -> None:
        """
        Log an operation with timestamp.

        Args:
            operation: Operation name
            parameters: Operation parameters
            result: Result status (success/failure/cancelled)
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "parameters": parameters,
            "result": result,
        }
        self.operations.append(entry)
        logger.info(f"Operation logged: {operation} - {result}")

    def get_recent_operations(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get recent operations.

        Args:
            limit: Maximum number of operations to return

        Returns:
            List of recent operations
        """
        return self.operations[-limit:]


# Global operation logger instance
operation_logger = OperationLogger()


def require_confirmation(operation: str, details: dict[str, Any]) -> bool:
    """
    Request user confirmation for sensitive operations.

    Args:
        operation: Operation name
        details: Operation details to show user

    Returns:
        True if confirmed, False otherwise

    Raises:
        MailOperationCancelledError: If user cancels
    """
    # In a real implementation, this would:
    # 1. Show a confirmation dialog
    # 2. Display operation details
    # 3. Wait for user approval
    #
    # For MCP, this is typically handled by the client (Claude Desktop)
    # by including operation details in the response and requiring
    # explicit user action.
    #
    # For now, we'll log the request and return True for testing.
    # Production should implement proper confirmation flow.

    logger.warning(f"Confirmation requested for: {operation}")
    logger.warning(f"Details: {details}")

    # TODO: Implement proper confirmation mechanism
    # This could be:
    # - Return special MCP response that requires user confirmation
    # - Show system dialog (osascript -e 'display dialog ...')
    # - Use callback mechanism

    return True


def validate_send_operation(
    to: list[str], cc: list[str] | None = None, bcc: list[str] | None = None
) -> tuple[bool, str]:
    """
    Validate email sending operation.

    Args:
        to: List of To recipients
        cc: List of CC recipients
        bcc: List of BCC recipients

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Check for recipients
    if not to:
        return False, "At least one 'to' recipient is required"

    # Validate all email addresses
    all_recipients = to + (cc or []) + (bcc or [])
    invalid_emails = [email for email in all_recipients if not validate_email(email)]

    if invalid_emails:
        return False, f"Invalid email addresses: {', '.join(invalid_emails)}"

    # Check for reasonable limits (prevent spam)
    max_recipients = 100
    if len(all_recipients) > max_recipients:
        return False, f"Too many recipients (max: {max_recipients})"

    return True, ""


def validate_bulk_operation(item_count: int, max_items: int = 100) -> tuple[bool, str]:
    """
    Validate bulk operation limits.

    Args:
        item_count: Number of items in operation
        max_items: Maximum allowed items

    Returns:
        Tuple of (is_valid, error_message)
    """
    if item_count == 0:
        return False, "No items specified for operation"

    if item_count > max_items:
        return False, f"Too many items ({item_count}), maximum is {max_items}"

    return True, ""


def rate_limit_check(operation: str, window_seconds: int = 60, max_operations: int = 10) -> bool:
    """
    Check if operation should be rate limited.

    Args:
        operation: Operation name
        window_seconds: Time window in seconds
        max_operations: Maximum operations in window

    Returns:
        True if allowed, False if rate limited
    """
    # TODO: Implement actual rate limiting with timing
    # For now, just log and return True

    recent_ops = [
        op for op in operation_logger.operations if op["operation"] == operation
    ]

    if len(recent_ops) > max_operations:
        logger.warning(f"Rate limit check for {operation}: {len(recent_ops)} recent operations")

    return True


def validate_attachment_type(filename: str, allow_executables: bool = False) -> bool:
    """
    Validate attachment file type for security.

    Args:
        filename: Name of the attachment file
        allow_executables: Whether to allow executable files (default: False)

    Returns:
        True if file type is allowed, False otherwise

    Example:
        >>> validate_attachment_type("document.pdf")
        True
        >>> validate_attachment_type("malware.exe")
        False
    """
    # Dangerous executable extensions (block by default)
    dangerous_extensions = {
        '.exe', '.bat', '.cmd', '.com', '.scr', '.pif',
        '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh',
        '.msi', '.msp', '.scf', '.lnk', '.inf', '.reg',
        '.ps1', '.psm1', '.app', '.deb', '.rpm', '.sh',
        '.bash', '.csh', '.ksh', '.zsh', '.command'
    }

    filename_lower = filename.lower()

    # Check for dangerous extensions
    for ext in dangerous_extensions:
        if filename_lower.endswith(ext):
            return allow_executables

    # All other types are allowed
    return True


def validate_attachment_size(size_bytes: int, max_size: int = 25 * 1024 * 1024) -> bool:
    """
    Validate attachment file size.

    Args:
        size_bytes: Size of file in bytes
        max_size: Maximum allowed size in bytes (default: 25MB)

    Returns:
        True if within limit, False otherwise

    Example:
        >>> validate_attachment_size(1024 * 1024)  # 1MB
        True
        >>> validate_attachment_size(30 * 1024 * 1024)  # 30MB
        False
    """
    return size_bytes <= max_size
