"""
Security utilities for Apple Mail MCP.
"""

import logging
import os
import subprocess
import time
from collections import deque
from datetime import datetime
from functools import lru_cache
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


TIER_LIMITS: dict[str, tuple[int, float]] = {
    "cheap_reads": (60, 60.0),
    "expensive_ops": (20, 60.0),
    "sends": (3, 60.0),
}

OPERATION_TIERS: dict[str, str] = {
    "list_accounts": "cheap_reads",
    "list_rules": "cheap_reads",
    "list_mailboxes": "cheap_reads",
    "get_message": "cheap_reads",
    "get_attachments": "cheap_reads",
    "get_thread": "cheap_reads",
    "save_attachments": "cheap_reads",
    "search_messages": "expensive_ops",
    "mark_as_read": "expensive_ops",
    "move_messages": "expensive_ops",
    "flag_message": "expensive_ops",
    "create_mailbox": "expensive_ops",
    "delete_messages": "expensive_ops",
    "set_rule_enabled": "expensive_ops",
    "delete_rule": "expensive_ops",
    "create_rule": "expensive_ops",
    "update_rule": "expensive_ops",
    "reply_to_message": "expensive_ops",
    "send_email": "sends",
    "send_email_with_attachments": "sends",
    "forward_message": "sends",
    # Email templates (#30) — local file I/O only, never touches Mail.app.
    "list_templates": "cheap_reads",
    "get_template": "cheap_reads",
    "save_template": "cheap_reads",
    "delete_template": "cheap_reads",
    "render_template": "cheap_reads",
}


class RateLimiter:
    """Sliding-window rate limiter with per-tier tracking."""

    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {t: deque() for t in TIER_LIMITS}

    def check(self, tier: str) -> bool:
        """Return True if allowed, False if rate-limited."""
        now = time.monotonic()
        max_calls, window = TIER_LIMITS[tier]
        q = self._windows[tier]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= max_calls:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        """Clear all tier windows."""
        for q in self._windows.values():
            q.clear()


rate_limiter = RateLimiter()


def check_rate_limit(operation: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """
    Check rate limit for an operation. Returns None if allowed,
    or a structured error dict if rate-limited.
    """
    tier = OPERATION_TIERS[operation]
    if rate_limiter.check(tier):
        return None
    operation_logger.log_operation(operation, params, "rate_limited")
    max_calls, window = TIER_LIMITS[tier]
    return {
        "success": False,
        "error": f"Rate limit exceeded: {max_calls} calls per {int(window)}s for {tier} operations",
        "error_type": "rate_limited",
    }


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


# ---------------------------------------------------------------------------
# Test-mode safety system (MAIL_TEST_MODE)
# ---------------------------------------------------------------------------

RESERVED_TEST_DOMAINS = {"example.com", "example.net", "example.org"}
RESERVED_TEST_TLDS = {".example", ".test", ".invalid", ".localhost"}

ACCOUNT_GATED_OPERATIONS = {
    "list_mailboxes",
    "search_messages",
    "move_messages",
    "create_mailbox",
}

SEND_OPERATIONS = {
    "send_email",
    "send_email_with_attachments",
    "reply_to_message",
    "forward_message",
}

# Rule-mutation operations: in test mode, may only target rules whose
# names start with the test prefix below. Protects the user's real rules
# during integration testing.
RULE_GATED_OPERATIONS = {
    "set_rule_enabled",
    "create_rule",
    "update_rule",
    "delete_rule",
}

RULE_TEST_PREFIX = "[apple-mail-mcp-test]"


def _is_test_mode_enabled() -> bool:
    return os.environ.get("MAIL_TEST_MODE", "").lower() == "true"


def _get_test_account() -> str | None:
    return os.environ.get("MAIL_TEST_ACCOUNT")


@lru_cache(maxsize=4)
def _get_test_account_identifiers(test_account_name: str) -> frozenset[str]:
    """Return the set of identifiers (name + UUID) that match the test account.

    The test account is configured by name via MAIL_TEST_ACCOUNT, but per #61
    callers may pass either the name or the UUID to account-gated tools.
    Returns both so the safety gate accepts either form.

    Cached per process, keyed by the test-account name. Tests can clear the
    cache with ``_get_test_account_identifiers.cache_clear()``. If the UUID
    lookup fails (account doesn't exist, AppleScript permission denied),
    falls back to name-only matching with a warning — degraded mode that
    still enforces the test-account boundary by name.
    """
    identifiers: set[str] = {test_account_name}
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'tell application "Mail" to return id of account '
                f'"{test_account_name}"',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            "Test-mode safety gate: failed to resolve UUID for account %r "
            "(%s); falling back to name-only matching",
            test_account_name, exc,
        )
        return frozenset(identifiers)

    if result.returncode == 0:
        uuid = result.stdout.strip()
        if uuid:
            identifiers.add(uuid)
    else:
        logger.warning(
            "Test-mode safety gate: failed to resolve UUID for account %r "
            "(exit %d): %s; falling back to name-only matching",
            test_account_name, result.returncode, result.stderr.strip(),
        )
    return frozenset(identifiers)


def _is_reserved_test_domain(email: str) -> bool:
    """True if email's domain is an RFC 2606 reserved test domain."""
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    if domain in RESERVED_TEST_DOMAINS:
        return True
    for tld in RESERVED_TEST_TLDS:
        bare = tld.lstrip(".")
        if domain == bare or domain.endswith(tld):
            return True
    return False


def _safety_error(operation: str, message: str) -> dict[str, Any]:
    operation_logger.log_operation(
        operation, {"violation": message}, "safety_violation"
    )
    return {
        "success": False,
        "error": message,
        "error_type": "safety_violation",
    }


def check_test_mode_safety(
    operation: str,
    account: str | None = None,
    recipients: list[str] | None = None,
    rule_name: str | None = None,
) -> dict[str, Any] | None:
    """
    Enforce test-mode safety checks. Returns None if allowed (or no test mode),
    or a structured error dict on safety violation.

    In test mode (MAIL_TEST_MODE=true):
    - Account-gated operations must target MAIL_TEST_ACCOUNT.
    - Send operations must send only to RFC 2606 reserved domains.
    - reply_to_message is blocked (can't inspect original recipients here).
    - Rule-mutation operations must target rules whose names start with
      RULE_TEST_PREFIX (protects the user's real rules during integration
      testing).
    """
    if not _is_test_mode_enabled():
        return None

    # reply_to_message has no recipient visibility at this layer; block it
    if operation == "reply_to_message":
        return _safety_error(
            operation,
            "Test mode: reply_to_message is blocked because recipients cannot "
            "be verified without fetching the original message. Use forward_message "
            "with explicit recipients instead.",
        )

    # Account-gated operations: verify target account matches MAIL_TEST_ACCOUNT
    # by either name or UUID (per #61, account-gated tools accept both forms).
    if operation in ACCOUNT_GATED_OPERATIONS and account is not None:
        test_account = _get_test_account()
        if test_account is None:
            return _safety_error(
                operation,
                "MAIL_TEST_MODE is set but MAIL_TEST_ACCOUNT is not",
            )
        if account not in _get_test_account_identifiers(test_account):
            return _safety_error(
                operation,
                f"Test mode: account '{account}' does not match "
                f"MAIL_TEST_ACCOUNT='{test_account}'",
            )

    # Rule-mutation operations: verify the target rule's name starts with
    # the test prefix. The caller (server tool wrapper) is responsible for
    # resolving rule_index → rule_name before calling, since the safety gate
    # has no Mail.app access of its own.
    if operation in RULE_GATED_OPERATIONS and rule_name is not None:
        if not rule_name.startswith(RULE_TEST_PREFIX):
            return _safety_error(
                operation,
                f"Test mode: rule mutations are restricted to rules whose "
                f"name starts with {RULE_TEST_PREFIX!r}. Got rule_name="
                f"{rule_name!r}.",
            )

    # Send operations: verify every recipient is on a reserved test domain
    if operation in SEND_OPERATIONS and recipients is not None:
        bad = [r for r in recipients if not _is_reserved_test_domain(r)]
        if bad:
            return _safety_error(
                operation,
                f"Test mode: recipients must use RFC 2606 reserved domains "
                f"(example.com/.test/.invalid/etc.). Violations: {', '.join(bad)}",
            )

    return None
