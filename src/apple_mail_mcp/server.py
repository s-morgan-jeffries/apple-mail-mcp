"""
FastMCP server for Apple Mail integration.
"""

import argparse
import logging
from typing import Any, cast

from fastmcp import Context, FastMCP
from fastmcp.server.elicitation import AcceptedElicitation

from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailRuleNotFoundError,
    MailTemplateError,
    MailTemplateInvalidFormatError,
    MailTemplateInvalidNameError,
    MailTemplateMissingVariableError,
    MailTemplateNotFoundError,
    MailUnsupportedRuleActionError,
)
from .imap_connector import ImapConnectionPool
from .mail_connector import AppleMailConnector
from .security import (
    check_rate_limit,
    check_test_mode_safety,
    operation_logger,
    validate_bulk_operation,
    validate_send_operation,
)
from .templates import Template, TemplateStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastMCP server
mcp = FastMCP("apple-mail")

# Initialize mail connector. Pool is opt-in via APPLE_MAIL_MCP_IMAP_POOL=1
# (default off, per #75 acceptance criteria — keep per-call lifecycle the
# default until benchmarks prove the speedup is worth the lifecycle
# complexity, then a follow-up can flip the default).
def _build_imap_pool() -> ImapConnectionPool | None:
    """Build an ImapConnectionPool when the opt-in env var is set.

    Pooling stays opt-in per #75's acceptance criteria: per-call lifecycle
    is the default until benchmarks prove the speedup is worth the
    lifecycle complexity, then a follow-up can flip the default."""
    import os
    flag = os.getenv("APPLE_MAIL_MCP_IMAP_POOL", "0").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        logger.info("IMAP connection pool enabled (APPLE_MAIL_MCP_IMAP_POOL)")
        return ImapConnectionPool()
    return None


mail = AppleMailConnector(imap_pool=_build_imap_pool())


def _build_send_summary(
    subject: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    body: str,
) -> str:
    """Build a human-readable confirmation summary for send operations."""
    lines = [f"To: {', '.join(to)}"]
    if cc:
        lines.append(f"CC: {', '.join(cc)}")
    if bcc:
        lines.append(f"BCC: {', '.join(bcc)}")
    lines.append(f"Subject: {subject}")
    preview = body[:200] + "..." if len(body) > 200 else body
    lines.append(f"\n{preview}")
    return "Send this email?\n\n" + "\n".join(lines)


def _build_forward_summary(
    message_id: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    body: str,
) -> str:
    """Build a human-readable confirmation summary for forward operations."""
    lines = [f"Forward message {message_id}", f"To: {', '.join(to)}"]
    if cc:
        lines.append(f"CC: {', '.join(cc)}")
    if bcc:
        lines.append(f"BCC: {', '.join(bcc)}")
    if body:
        preview = body[:200] + "..." if len(body) > 200 else body
        lines.append(f"\n{preview}")
    return "Forward this message?\n\n" + "\n".join(lines)


async def _elicit_confirmation(
    ctx: Context | None, summary: str, operation: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Elicit user confirmation via MCP. Returns error dict if declined, None if approved."""
    if not ctx:
        return None
    try:
        result = await ctx.elicit(summary, None)
        if not isinstance(result, AcceptedElicitation):
            operation_logger.log_operation(operation, params, "cancelled")
            return {
                "success": False,
                "error": "User declined to send",
                "error_type": "cancelled",
            }
    except Exception:
        logger.warning("Elicitation not supported by client, proceeding without confirmation")
    return None


@mcp.tool()
def list_accounts() -> dict[str, Any]:
    """
    List all configured email accounts in Apple Mail.

    Returns each account's id (UUID), display name, email addresses,
    account type, and enabled state. Account ids are stable across name
    changes; prefer them over names for identifying accounts.

    Returns:
        Dictionary containing the accounts list.

    Example:
        >>> list_accounts()
        {"success": True, "accounts": [
            {"id": "B21B254B-...", "name": "Gmail", "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True}, ...
        ]}
    """
    try:
        rate_err = check_rate_limit("list_accounts", {})
        if rate_err:
            return rate_err

        logger.info("Listing accounts")

        accounts = mail.list_accounts()

        operation_logger.log_operation("list_accounts", {}, "success")

        return {
            "success": True,
            "accounts": accounts,
            "count": len(accounts),
        }

    except Exception as e:
        logger.error(f"Error listing accounts: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def list_rules() -> dict[str, Any]:
    """
    List all Mail.app rules (read-only).

    Returns each rule's display name and enabled state. Rule names are NOT
    guaranteed unique — Mail allows duplicates — and rules have no stable
    id via AppleScript. This tool is read-only; mutation (enable/disable,
    create, delete) is tracked as a separate enhancement.

    Returns:
        Dictionary containing the rules list.

    Example:
        >>> list_rules()
        {"success": True, "rules": [
            {"name": "Junk filter", "enabled": True},
            {"name": "News From Apple", "enabled": False}, ...
        ], "count": 2}
    """
    try:
        rate_err = check_rate_limit("list_rules", {})
        if rate_err:
            return rate_err

        logger.info("Listing rules")

        rules = mail.list_rules()

        operation_logger.log_operation("list_rules", {}, "success")

        return {
            "success": True,
            "rules": rules,
            "count": len(rules),
        }

    except Exception as e:
        logger.error(f"Error listing rules: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


def _resolve_rule_name(rule_index: int) -> str | None:
    """Look up a rule's name from its 1-based index via list_rules.

    Used by the rule mutation tools to feed the safety gate. Returns None
    if the rule doesn't exist (caller surfaces a typed error).
    """
    rules = mail.list_rules()
    for r in rules:
        if r.get("index") == rule_index:
            return cast(str, r.get("name", ""))
    return None


@mcp.tool()
def set_rule_enabled(rule_index: int, enabled: bool) -> dict[str, Any]:
    """
    Enable or disable a Mail.app rule by 1-based positional index.

    Trivially reversible — no confirmation prompt. The index is the one
    returned by ``list_rules``; if you've changed rule order in Mail.app
    since the last list_rules call, re-list before calling this.

    Args:
        rule_index: 1-based positional index from list_rules.
        enabled: True to enable, False to disable.

    Returns:
        Dictionary with success status and the rule's name.
    """
    try:
        rate_err = check_rate_limit(
            "set_rule_enabled", {"rule_index": rule_index}
        )
        if rate_err:
            return rate_err

        rule_name = _resolve_rule_name(rule_index)
        if rule_name is None:
            return {
                "success": False,
                "error": f"No rule at index {rule_index}",
                "error_type": "rule_not_found",
            }

        safety_err = check_test_mode_safety(
            "set_rule_enabled", rule_name=rule_name
        )
        if safety_err:
            return safety_err

        mail.set_rule_enabled(rule_index, enabled)
        operation_logger.log_operation(
            "set_rule_enabled",
            {"rule_index": rule_index, "enabled": enabled, "name": rule_name},
            "success",
        )
        return {
            "success": True,
            "rule_index": rule_index,
            "name": rule_name,
            "enabled": enabled,
        }

    except MailRuleNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "rule_not_found",
        }
    except Exception as e:
        logger.error(f"Error in set_rule_enabled: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
async def delete_rule(
    rule_index: int,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Delete a Mail.app rule by 1-based positional index.

    Destructive — requires user confirmation via MCP elicitation before
    running. Cannot be undone (Mail.app does not version rule history).

    Args:
        rule_index: 1-based positional index from list_rules.

    Returns:
        Dictionary with success status and the deleted rule's name.

    Note:
        After deletion, downstream rule indices shift down by one. Re-call
        list_rules before any further rule operations.
    """
    try:
        rate_err = check_rate_limit(
            "delete_rule", {"rule_index": rule_index}
        )
        if rate_err:
            return rate_err

        rule_name = _resolve_rule_name(rule_index)
        if rule_name is None:
            return {
                "success": False,
                "error": f"No rule at index {rule_index}",
                "error_type": "rule_not_found",
            }

        safety_err = check_test_mode_safety(
            "delete_rule", rule_name=rule_name
        )
        if safety_err:
            return safety_err

        summary = (
            f"Delete Mail.app rule '{rule_name}' (index {rule_index})? "
            f"This cannot be undone."
        )
        cancel_err = await _elicit_confirmation(
            ctx, summary, "delete_rule", {"rule_index": rule_index}
        )
        if cancel_err:
            return cancel_err

        deleted = mail.delete_rule(rule_index)
        operation_logger.log_operation(
            "delete_rule",
            {"rule_index": rule_index, "deleted_name": deleted},
            "success",
        )
        return {
            "success": True,
            "rule_index": rule_index,
            "deleted_name": deleted,
        }

    except MailRuleNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "rule_not_found",
        }
    except Exception as e:
        logger.error(f"Error in delete_rule: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def create_rule(
    name: str,
    conditions: list[dict[str, Any]],
    actions: dict[str, Any],
    match_logic: str = "all",
    enabled: bool = True,
) -> dict[str, Any]:
    """
    Create a new Mail.app rule.

    Additive — no confirmation prompt. Mail.app appends new rules to the
    end of the rule list, so the returned ``rule_index`` equals the new
    total rule count.

    Args:
        name: Rule display name. Need not be unique.
        conditions: List of condition dicts (at least one required). Each:
            - field: 'from' | 'to' | 'subject' | 'body' | 'any_recipient' |
                'header_name'
            - operator: 'contains' | 'does_not_contain' | 'begins_with' |
                'ends_with' | 'equals'
            - value: substring or value to match
            - header_name: required iff field == 'header_name'
        actions: Dict with at least one truthy entry from:
            - move_to: {"account": str, "mailbox": str}
            - copy_to: {"account": str, "mailbox": str}
            - mark_read: bool
            - mark_flagged: bool (with optional flag_color enum)
            - flag_color: 'none' | 'red' | 'orange' | 'yellow' | 'green' |
                'blue' | 'purple' | 'gray'
            - delete: bool
            - forward_to: list[str] of email addresses
        match_logic: 'all' (AND across conditions) or 'any' (OR). Default 'all'.
        enabled: Whether the rule is enabled on creation. Default True.

    Returns:
        Dictionary with success status, rule_index, and name.
    """
    try:
        rate_err = check_rate_limit("create_rule", {"name": name})
        if rate_err:
            return rate_err

        safety_err = check_test_mode_safety(
            "create_rule", rule_name=name
        )
        if safety_err:
            return safety_err

        new_index = mail.create_rule(
            name=name,
            conditions=conditions,
            actions=actions,
            match_logic=match_logic,
            enabled=enabled,
        )
        operation_logger.log_operation(
            "create_rule",
            {"name": name, "rule_index": new_index},
            "success",
        )
        return {
            "success": True,
            "rule_index": new_index,
            "name": name,
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except Exception as e:
        logger.error(f"Error in create_rule: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
async def update_rule(
    rule_index: int,
    name: str | None = None,
    enabled: bool | None = None,
    conditions: list[dict[str, Any]] | None = None,
    actions: dict[str, Any] | None = None,
    match_logic: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Update an existing Mail.app rule (patch semantics).

    Destructive — requires user confirmation via MCP elicitation before
    running, because the previous condition/action state is irrecoverable
    once replaced. Patch semantics: only fields you provide are changed.
    ``conditions`` and ``actions``, when provided, REPLACE their respective
    structures wholesale (not merged).

    Refuses to update any rule whose existing actions include something
    outside the supported schema (run-AppleScript, redirect, reply text,
    play sound, custom highlight color); raises
    MailUnsupportedRuleActionError. Edit such rules in Mail.app's UI.

    Args:
        rule_index: 1-based positional index from list_rules.
        name: New name (only set if not None).
        enabled: New enabled state (only set if not None).
        conditions: If provided, REPLACES all existing conditions.
        actions: If provided, REPLACES all action flags wholesale.
        match_logic: 'all' or 'any', only set if not None.

    Returns:
        Dictionary with success status.
    """
    try:
        rate_err = check_rate_limit(
            "update_rule", {"rule_index": rule_index}
        )
        if rate_err:
            return rate_err

        rule_name = _resolve_rule_name(rule_index)
        if rule_name is None:
            return {
                "success": False,
                "error": f"No rule at index {rule_index}",
                "error_type": "rule_not_found",
            }

        safety_err = check_test_mode_safety(
            "update_rule", rule_name=rule_name
        )
        if safety_err:
            return safety_err

        summary = (
            f"Update Mail.app rule '{rule_name}' (index {rule_index})? "
            f"Previous condition/action state cannot be recovered."
        )
        cancel_err = await _elicit_confirmation(
            ctx, summary, "update_rule", {"rule_index": rule_index}
        )
        if cancel_err:
            return cancel_err

        mail.update_rule(
            rule_index=rule_index,
            name=name,
            enabled=enabled,
            conditions=conditions,
            actions=actions,
            match_logic=match_logic,
        )
        operation_logger.log_operation(
            "update_rule",
            {"rule_index": rule_index, "previous_name": rule_name},
            "success",
        )
        return {
            "success": True,
            "rule_index": rule_index,
        }

    except MailRuleNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "rule_not_found",
        }
    except MailUnsupportedRuleActionError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "unsupported_rule_action",
        }
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except Exception as e:
        logger.error(f"Error in update_rule: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def list_mailboxes(account: str) -> dict[str, Any]:
    """
    List all mailboxes for an account.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts). Names are convenient but unstable
            across renames; UUIDs are stable.

    Returns:
        Dictionary containing mailboxes list

    Example:
        >>> list_mailboxes("Gmail")
        {"mailboxes": [{"name": "INBOX", "unread_count": 5}, ...]}
    """
    try:
        safety_err = check_test_mode_safety("list_mailboxes", account=account)
        if safety_err:
            return safety_err

        rate_err = check_rate_limit("list_mailboxes", {"account": account})
        if rate_err:
            return rate_err

        logger.info(f"Listing mailboxes for account: {account}")

        mailboxes = mail.list_mailboxes(account)

        operation_logger.log_operation(
            "list_mailboxes",
            {"account": account},
            "success"
        )

        return {
            "success": True,
            "account": account,
            "mailboxes": mailboxes,
        }

    except MailAccountNotFoundError as e:
        logger.error(f"Account not found: {e}")
        return {
            "success": False,
            "error": f"Account '{account}' not found",
            "error_type": "account_not_found",
        }
    except Exception as e:
        logger.error(f"Error listing mailboxes: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def search_messages(
    account: str,
    mailbox: str = "INBOX",
    sender_contains: str | None = None,
    subject_contains: str | None = None,
    read_status: bool | None = None,
    is_flagged: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_attachment: bool | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Search for messages matching criteria.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts). Names are convenient but unstable
            across renames; UUIDs are stable.
        mailbox: Mailbox name (default: "INBOX").
        sender_contains: Filter by sender email/domain substring.
        subject_contains: Filter by subject keywords substring.
        read_status: Filter by read status (true=read, false=unread).
        is_flagged: Filter by flagged status (true=flagged, false=not flagged).
        date_from: Inclusive lower bound on date received. ISO 8601 YYYY-MM-DD.
        date_to: Inclusive upper bound on date received (full day included). ISO 8601 YYYY-MM-DD.
        has_attachment: Filter messages with (true) or without (false) attachments.
        limit: Maximum results to return (default: 50).

    Returns:
        Dictionary containing matching messages. Each message row includes
        id, subject, sender, date_received, read_status, flagged.

    Example:
        >>> search_messages("Gmail", sender_contains="john@example.com", read_status=False, limit=10)
        {"success": True, "messages": [...], "count": 5}
    """
    try:
        safety_err = check_test_mode_safety("search_messages", account=account)
        if safety_err:
            return safety_err

        rate_err = check_rate_limit("search_messages", {"account": account, "mailbox": mailbox})
        if rate_err:
            return rate_err

        logger.info(
            f"Searching messages in {account}/{mailbox} with filters: "
            f"sender={sender_contains}, subject={subject_contains}, read={read_status}, "
            f"flagged={is_flagged}, date_from={date_from}, date_to={date_to}, "
            f"has_attachment={has_attachment}"
        )

        messages = mail.search_messages(
            account=account,
            mailbox=mailbox,
            sender_contains=sender_contains,
            subject_contains=subject_contains,
            read_status=read_status,
            is_flagged=is_flagged,
            date_from=date_from,
            date_to=date_to,
            has_attachment=has_attachment,
            limit=limit,
        )

        operation_logger.log_operation(
            "search_messages",
            {
                "account": account,
                "mailbox": mailbox,
                "filters": {
                    "sender": sender_contains,
                    "subject": subject_contains,
                    "read_status": read_status,
                    "is_flagged": is_flagged,
                    "date_from": date_from,
                    "date_to": date_to,
                    "has_attachment": has_attachment,
                },
            },
            "success"
        )

        return {
            "success": True,
            "account": account,
            "mailbox": mailbox,
            "messages": messages,
            "count": len(messages),
        }

    except (MailAccountNotFoundError, MailMailboxNotFoundError) as e:
        logger.error(f"Not found error: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "not_found",
        }
    except ValueError as e:
        logger.error(f"Validation error in search_messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except Exception as e:
        logger.error(f"Error searching messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def get_message(
    message_id: str,
    include_content: bool = True,
    headers_only: bool = False,
    account: str | None = None,
    mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Get full details of a specific message.

    Args:
        message_id: Message ID from search results.
        include_content: Include message body (default: True).
        headers_only: Skip body fetch on the IMAP path (default: False).
            Silently ignored when falling back to AppleScript.
        account: Mail.app account name. Together with `mailbox`, activates
            the IMAP fast path: one round-trip lookup instead of an
            account×mailbox AppleScript scan (issue #72). Without these,
            falls back to the slower AppleScript scan.
        mailbox: Folder to look in for the IMAP fast path (e.g. "INBOX").

    Returns:
        Dictionary containing message details.

    Example:
        >>> get_message("12345", account="iCloud", mailbox="INBOX")
        {"success": True, "message": {...}}
    """
    try:
        rate_err = check_rate_limit("get_message", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Getting message: {message_id}")

        message = mail.get_message(
            message_id,
            include_content=include_content,
            headers_only=headers_only,
            account=account,
            mailbox=mailbox,
        )

        operation_logger.log_operation(
            "get_message",
            {"message_id": message_id},
            "success"
        )

        return {
            "success": True,
            "message": message,
        }

    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error getting message: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def get_selected_messages(include_content: bool = True) -> dict[str, Any]:
    """
    Get messages currently selected in the Apple Mail application.

    Returns the full details of whichever message(s) the user has highlighted
    in Mail's message list at the time of the call.

    Args:
        include_content: Include message body text (default: true)

    Returns:
        Dictionary containing selected messages and count

    Example:
        >>> get_selected_messages()
        {"success": True, "messages": [...], "count": 1}
    """
    try:
        messages = mail.get_selected_messages(include_content=include_content)

        operation_logger.log_operation(
            "get_selected_messages",
            {"include_content": include_content},
            "success",
        )

        return {
            "success": True,
            "messages": messages,
            "count": len(messages),
        }

    except Exception as e:
        logger.error(f"Error getting selected messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
async def send_email(
    subject: str,
    body: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Send an email via Apple Mail.

    Requires user confirmation via MCP elicitation before sending.

    Args:
        subject: Email subject
        body: Email body (plain text)
        to: List of recipient email addresses
        cc: List of CC recipients (optional)
        bcc: List of BCC recipients (optional)

    Returns:
        Dictionary indicating success or failure

    Example:
        >>> send_email(
        ...     subject="Meeting Follow-up",
        ...     body="Thanks for the great meeting!",
        ...     to=["alice@example.com"],
        ...     cc=["bob@example.com"]
        ... )
        {"success": True, "message": "Email sent successfully"}
    """
    try:
        all_recipients = to + (cc or []) + (bcc or [])
        safety_err = check_test_mode_safety("send_email", recipients=all_recipients)
        if safety_err:
            return safety_err

        rate_err = check_rate_limit("send_email", {"subject": subject, "to": to})
        if rate_err:
            return rate_err

        # Validate operation
        is_valid, error_msg = validate_send_operation(to, cc, bcc)
        if not is_valid:
            logger.error(f"Validation failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": "validation_error",
            }

        # Elicit user confirmation
        summary = _build_send_summary(subject, to, cc, bcc, body)
        cancel_err = await _elicit_confirmation(
            ctx, summary, "send_email", {"subject": subject, "to": to}
        )
        if cancel_err:
            return cancel_err

        # Send the email
        mail.send_email(
            subject=subject,
            body=body,
            to=to,
            cc=cc,
            bcc=bcc,
        )

        operation_logger.log_operation(
            "send_email",
            {"subject": subject, "to": to, "cc": cc, "bcc": bcc},
            "success"
        )

        return {
            "success": True,
            "message": "Email sent successfully",
            "details": {
                "subject": subject,
                "recipients": len(to) + len(cc or []) + len(bcc or []),
            },
        }

    except MailAppleScriptError as e:
        logger.error(f"Error sending email: {e}")
        operation_logger.log_operation(
            "send_email",
            {"subject": subject},
            "failure"
        )
        return {
            "success": False,
            "error": f"Failed to send email: {str(e)}",
            "error_type": "send_error",
        }
    except Exception as e:
        logger.error(f"Unexpected error sending email: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def mark_as_read(
    message_ids: list[str],
    read: bool = True,
    account: str | None = None,
    source_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Mark messages as read or unread.

    Args:
        message_ids: List of message IDs to update
        read: True to mark as read, False to mark as unread (default: true)
        account: Optional account name (or UUID) the messages live in.
            Must be provided together with `source_mailbox`. When both
            are given, the operation is much faster — single mailbox
            scan instead of cross-account search.
        source_mailbox: Optional source mailbox name (e.g. "INBOX").
            See `account`.

    Returns:
        Dictionary indicating success and number of messages updated

    Example:
        >>> mark_as_read(["12345", "12346"], read=True)
        {"success": True, "updated": 2}
        >>> mark_as_read(["12345"], account="Gmail", source_mailbox="INBOX")
        {"success": True, "updated": 1}
    """
    try:
        rate_err = check_rate_limit("mark_as_read", {"count": len(message_ids)})
        if rate_err:
            return rate_err

        # Validate bulk operation
        is_valid, error_msg = validate_bulk_operation(len(message_ids), max_items=100)
        if not is_valid:
            logger.error(f"Validation failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": "validation_error",
            }

        logger.info(f"Marking {len(message_ids)} messages as {'read' if read else 'unread'}")

        count = mail.mark_as_read(
            message_ids,
            read=read,
            account=account,
            source_mailbox=source_mailbox,
        )

        operation_logger.log_operation(
            "mark_as_read",
            {"count": len(message_ids), "read": read},
            "success"
        )

        return {
            "success": True,
            "updated": count,
            "requested": len(message_ids),
        }

    except Exception as e:
        logger.error(f"Error marking messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
async def send_email_with_attachments(
    subject: str,
    body: str,
    to: list[str],
    attachments: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Send an email with file attachments via Apple Mail.

    Requires user confirmation via MCP elicitation before sending.

    Args:
        subject: Email subject
        body: Email body (plain text)
        to: List of recipient email addresses
        attachments: List of file paths to attach
        cc: List of CC recipients (optional)
        bcc: List of BCC recipients (optional)

    Returns:
        Dictionary indicating success or failure

    Example:
        >>> send_email_with_attachments(
        ...     subject="Report",
        ...     body="Please find the attached report.",
        ...     to=["colleague@example.com"],
        ...     attachments=["/Users/me/Documents/report.pdf"]
        ... )
        {"success": True, "message": "Email sent with 1 attachment(s)"}
    """
    from pathlib import Path

    try:
        all_recipients = to + (cc or []) + (bcc or [])
        safety_err = check_test_mode_safety("send_email_with_attachments", recipients=all_recipients)
        if safety_err:
            return safety_err

        rate_err = check_rate_limit("send_email_with_attachments", {"subject": subject, "to": to})
        if rate_err:
            return rate_err

        # Convert string paths to Path objects
        attachment_paths = [Path(p) for p in attachments]

        # Validate operation
        is_valid, error_msg = validate_send_operation(to, cc, bcc)
        if not is_valid:
            logger.error(f"Validation failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": "validation_error",
            }

        # Validate attachments exist
        missing_files = [str(p) for p in attachment_paths if not p.exists()]
        if missing_files:
            return {
                "success": False,
                "error": f"Attachment files not found: {', '.join(missing_files)}",
                "error_type": "file_not_found",
            }

        # Elicit user confirmation
        summary = _build_send_summary(subject, to, cc, bcc, body)
        cancel_err = await _elicit_confirmation(
            ctx, summary, "send_email_with_attachments", {"subject": subject, "to": to}
        )
        if cancel_err:
            return cancel_err

        # Send the email
        mail.send_email_with_attachments(
            subject=subject,
            body=body,
            to=to,
            attachments=attachment_paths,
            cc=cc,
            bcc=bcc,
        )

        operation_logger.log_operation(
            "send_email_with_attachments",
            {"subject": subject, "to": to, "attachments": len(attachments)},
            "success"
        )

        return {
            "success": True,
            "message": f"Email sent with {len(attachments)} attachment(s)",
            "details": {
                "subject": subject,
                "recipients": len(to) + len(cc or []) + len(bcc or []),
                "attachments": len(attachments),
            },
        }

    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Validation error: {e}")
        operation_logger.log_operation(
            "send_email_with_attachments",
            {"subject": subject},
            "failure"
        )
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailAppleScriptError as e:
        logger.error(f"Error sending email: {e}")
        operation_logger.log_operation(
            "send_email_with_attachments",
            {"subject": subject},
            "failure"
        )
        return {
            "success": False,
            "error": f"Failed to send email: {str(e)}",
            "error_type": "send_error",
        }
    except Exception as e:
        logger.error(f"Unexpected error sending email with attachments: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def get_attachments(
    message_id: str,
    account: str | None = None,
    mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Get list of attachments from a message.

    Args:
        message_id: Message ID from search results.
        account: Mail.app account name. Together with `mailbox`, activates
            the IMAP fast path: one BODYSTRUCTURE FETCH instead of an
            account×mailbox AppleScript scan plus per-attachment property
            reads (issue #73). Without these, falls back to the slower
            AppleScript scan.
        mailbox: Folder to look in for the IMAP fast path (e.g. "INBOX").

    Returns:
        Dictionary with list of attachments.

    Example:
        >>> get_attachments("CABCD@x", account="iCloud", mailbox="INBOX")
        {
            "success": True,
            "attachments": [
                {
                    "name": "report.pdf",
                    "mime_type": "application/pdf",
                    "size": 524288,
                    "downloaded": False  # always False on IMAP path
                }
            ],
            "count": 1
        }

    Note on `downloaded`:
        On the IMAP path (account+mailbox supplied), `downloaded` is
        always False — BODYSTRUCTURE returns metadata only and Mail.app's
        local cache state isn't observable via IMAP. On the AppleScript
        fallback path it reflects Mail.app's actual cache. Treat False
        as "may need a network fetch on save".
    """
    try:
        rate_err = check_rate_limit("get_attachments", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Getting attachments for message: {message_id}")

        attachments = mail.get_attachments(
            message_id, account=account, mailbox=mailbox,
        )

        operation_logger.log_operation(
            "get_attachments",
            {"message_id": message_id},
            "success"
        )

        return {
            "success": True,
            "attachments": attachments,
            "count": len(attachments),
        }

    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error getting attachments: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def get_thread(message_id: str) -> dict[str, Any]:
    """
    Return all messages in the thread containing the given message.

    Looks up the message by its internal id, then reconstructs the
    conversation by reading RFC 5322 threading headers (Message-ID,
    In-Reply-To, References) across messages in the same account.
    Results are sorted by date_received ascending.

    Known limitation: thread members whose subject was rewritten
    mid-conversation are missed (subject prefilter tradeoff).

    Args:
        message_id: Internal id of any message in the thread
            (from search_messages or get_message results).

    Returns:
        Dictionary with the thread list.

    Example:
        >>> get_thread("12345")
        {"success": True, "thread": [{...}, {...}], "count": 2}
    """
    try:
        rate_err = check_rate_limit("get_thread", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Getting thread for message: {message_id}")

        thread = mail.get_thread(message_id)

        operation_logger.log_operation(
            "get_thread", {"message_id": message_id}, "success"
        )

        return {
            "success": True,
            "thread": thread,
            "count": len(thread),
        }

    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error getting thread: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def save_attachments(
    message_id: str,
    save_directory: str,
    attachment_indices: list[int] | None = None,
) -> dict[str, Any]:
    """
    Save attachments from a message to a directory.

    Args:
        message_id: Message ID from search results
        save_directory: Directory path to save attachments to
        attachment_indices: Specific attachment indices to save (0-based), None for all

    Returns:
        Dictionary indicating success and number of attachments saved

    Example:
        >>> save_attachments("12345", "/Users/me/Downloads")
        {"success": True, "saved": 2, "directory": "/Users/me/Downloads"}

        >>> save_attachments("12345", "/Users/me/Downloads", [0, 2])
        {"success": True, "saved": 2, "directory": "/Users/me/Downloads"}
    """
    from pathlib import Path

    try:
        rate_err = check_rate_limit("save_attachments", {"message_id": message_id})
        if rate_err:
            return rate_err

        save_path = Path(save_directory)

        # Validate directory
        if not save_path.exists():
            return {
                "success": False,
                "error": f"Directory does not exist: {save_directory}",
                "error_type": "directory_not_found",
            }

        if not save_path.is_dir():
            return {
                "success": False,
                "error": f"Path is not a directory: {save_directory}",
                "error_type": "invalid_directory",
            }

        logger.info(
            f"Saving attachments from message {message_id} to {save_directory}"
        )

        count = mail.save_attachments(
            message_id=message_id,
            save_directory=save_path,
            attachment_indices=attachment_indices,
        )

        operation_logger.log_operation(
            "save_attachments",
            {
                "message_id": message_id,
                "directory": save_directory,
                "indices": attachment_indices,
            },
            "success"
        )

        return {
            "success": True,
            "saved": count,
            "directory": save_directory,
        }

    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Validation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error saving attachments: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def move_messages(
    message_ids: list[str],
    destination_mailbox: str,
    account: str,
    gmail_mode: bool = False,
    source_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Move messages to a different mailbox/folder.

    Args:
        message_ids: List of message IDs to move
        destination_mailbox: Name of destination mailbox (use "/" for nested: "Projects/Client Work")
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts) containing the messages. Names are
            convenient but unstable across renames; UUIDs are stable.
        gmail_mode: Use Gmail-specific move handling (copy + delete) for label-based systems
        source_mailbox: Optional source mailbox name. When provided, the
            operation is much faster — single mailbox scan instead of
            cross-account search. Source is assumed to be in the same
            `account` as the destination.

    Returns:
        Dictionary with success status and number of messages moved

    Example:
        move_messages(
            message_ids=["12345", "12346"],
            destination_mailbox="Archive",
            account="Gmail",
            source_mailbox="INBOX",
        )
    """
    try:
        safety_err = check_test_mode_safety("move_messages", account=account)
        if safety_err:
            return safety_err

        if not message_ids:
            return {
                "success": True,
                "count": 0,
                "message": "No messages to move",
            }

        rate_err = check_rate_limit("move_messages", {"count": len(message_ids)})
        if rate_err:
            return rate_err

        logger.info(
            f"Moving {len(message_ids)} message(s) to {destination_mailbox} in account {account}"
        )

        # Move the messages
        count = mail.move_messages(
            message_ids=message_ids,
            destination_mailbox=destination_mailbox,
            account=account,
            gmail_mode=gmail_mode,
            source_mailbox=source_mailbox,
        )

        return {
            "success": True,
            "count": count,
            "destination": destination_mailbox,
            "account": account,
        }

    except MailMailboxNotFoundError as e:
        logger.error(f"Mailbox not found: {e}")
        return {
            "success": False,
            "error": f"Mailbox '{destination_mailbox}' not found in account '{account}'",
            "error_type": "mailbox_not_found",
        }
    except MailAccountNotFoundError as e:
        logger.error(f"Account not found: {e}")
        return {
            "success": False,
            "error": f"Account '{account}' not found",
            "error_type": "account_not_found",
        }
    except Exception as e:
        logger.error(f"Error moving messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def flag_message(
    message_ids: list[str],
    flag_color: str,
    account: str | None = None,
    source_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Set flag color on messages.

    Args:
        message_ids: List of message IDs to flag
        flag_color: Flag color name (none, orange, red, yellow, blue, green, purple, gray)
        account: Optional account name (or UUID) the messages live in.
            Must be provided together with `source_mailbox`. When both
            are given, the operation is much faster.
        source_mailbox: Optional source mailbox name; see `account`.

    Returns:
        Dictionary with success status and number of messages flagged

    Example:
        flag_message(
            message_ids=["12345"],
            flag_color="red",
            account="Gmail",
            source_mailbox="INBOX",
        )
    """
    try:
        if not message_ids:
            return {
                "success": True,
                "count": 0,
                "message": "No messages to flag",
            }

        rate_err = check_rate_limit("flag_message", {"count": len(message_ids)})
        if rate_err:
            return rate_err

        logger.info(f"Flagging {len(message_ids)} message(s) with color {flag_color}")

        # Flag the messages
        count = mail.flag_message(
            message_ids=message_ids,
            flag_color=flag_color,
            account=account,
            source_mailbox=source_mailbox,
        )

        return {
            "success": True,
            "count": count,
            "flag_color": flag_color,
        }

    except ValueError as e:
        logger.error(f"Invalid flag color: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error flagging messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def create_mailbox(
    account: str,
    name: str,
    parent_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Create a new mailbox/folder.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts) to create the mailbox in. Names are
            convenient but unstable across renames; UUIDs are stable.
        name: Name of the new mailbox
        parent_mailbox: Optional parent mailbox for nesting (None = top-level)

    Returns:
        Dictionary with success status and mailbox details

    Example:
        create_mailbox(
            account="Gmail",
            name="Client Work",
            parent_mailbox="Projects"
        )
    """
    try:
        safety_err = check_test_mode_safety("create_mailbox", account=account)
        if safety_err:
            return safety_err

        if not name or not name.strip():
            return {
                "success": False,
                "error": "Mailbox name cannot be empty",
                "error_type": "validation_error",
            }

        rate_err = check_rate_limit("create_mailbox", {"account": account, "name": name})
        if rate_err:
            return rate_err

        logger.info(f"Creating mailbox '{name}' in account {account}")

        # Create the mailbox
        success = mail.create_mailbox(
            account=account,
            name=name,
            parent_mailbox=parent_mailbox,
        )

        return {
            "success": success,
            "account": account,
            "mailbox": name,
            "parent": parent_mailbox,
        }

    except ValueError as e:
        logger.error(f"Validation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailAccountNotFoundError as e:
        logger.error(f"Account not found: {e}")
        return {
            "success": False,
            "error": f"Account '{account}' not found",
            "error_type": "account_not_found",
        }
    except MailAppleScriptError as e:
        logger.error(f"AppleScript error: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "applescript_error",
        }
    except Exception as e:
        logger.error(f"Error creating mailbox: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def delete_messages(
    message_ids: list[str],
    permanent: bool = False,
    account: str | None = None,
    source_mailbox: str | None = None,
) -> dict[str, Any]:
    """
    Delete messages (always moves to the account's Trash mailbox).

    Args:
        message_ids: List of message IDs to delete
        permanent: Reserved; currently a no-op. Mail.app's AppleScript
            dictionary exposes no path to permanent-delete that bypasses
            Trash (issue #111). Passing True emits a DeprecationWarning;
            messages still go to Trash. Recoverable from the account's
            Trash mailbox until that mailbox is emptied.
        account: Optional account name (or UUID) the messages live in.
            Must be provided together with `source_mailbox`. When both
            are given, the operation is much faster.
        source_mailbox: Optional source mailbox name; see `account`.

    Returns:
        Dictionary with success status and number of messages deleted

    Example:
        delete_messages(
            message_ids=["12345"],
            account="Gmail",
            source_mailbox="INBOX",
        )

    Note:
        Bulk deletions are limited to 100 messages for safety.
        All deletes are recoverable from Trash; there is currently no
        AppleScript path to bypass it. See issue #111.
    """
    try:
        if not message_ids:
            return {
                "success": True,
                "count": 0,
                "message": "No messages to delete",
            }

        rate_err = check_rate_limit("delete_messages", {"count": len(message_ids)})
        if rate_err:
            return rate_err

        # Validate bulk operation limit
        if len(message_ids) > 100:
            return {
                "success": False,
                "error": f"Cannot delete {len(message_ids)} messages at once (max: 100)",
                "error_type": "validation_error",
            }

        logger.info(f"Deleting {len(message_ids)} message(s) to trash")

        # Delete the messages
        count = mail.delete_messages(
            message_ids=message_ids,
            permanent=permanent,
            skip_bulk_check=False,  # Enforce limit
            account=account,
            source_mailbox=source_mailbox,
        )

        return {
            "success": True,
            "count": count,
            "permanent": permanent,
        }

    except ValueError as e:
        logger.error(f"Validation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error deleting messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def reply_to_message(
    message_id: str,
    body: str,
    reply_all: bool = False,
) -> dict[str, Any]:
    """
    Reply to a message.

    Args:
        message_id: ID of the message to reply to
        body: Reply body text
        reply_all: If True, reply to all recipients; if False, reply only to sender (default: False)

    Returns:
        Dictionary with success status and reply message ID

    Example:
        reply_to_message(
            message_id="12345",
            body="Thanks for your email! I'll get back to you soon.",
            reply_all=False
        )
    """
    try:
        safety_err = check_test_mode_safety("reply_to_message")
        if safety_err:
            return safety_err

        rate_err = check_rate_limit("reply_to_message", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Creating reply to message {message_id}")

        # Reply to the message
        reply_id = mail.reply_to_message(
            message_id=message_id,
            body=body,
            reply_all=reply_all,
        )

        return {
            "success": True,
            "reply_id": reply_id,
            "original_message_id": message_id,
            "reply_all": reply_all,
        }

    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error replying to message: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
async def forward_message(
    message_id: str,
    to: list[str],
    body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Forward a message to recipients.

    Requires user confirmation via MCP elicitation before forwarding.

    Args:
        message_id: ID of the message to forward
        to: List of recipient email addresses
        body: Optional body text to add before forwarded content (default: "")
        cc: Optional CC recipients
        bcc: Optional BCC recipients

    Returns:
        Dictionary with success status and forwarded message ID

    Example:
        forward_message(
            message_id="12345",
            to=["colleague@example.com"],
            body="FYI - thought you'd find this interesting."
        )

    Note:
        Original message content and attachments are automatically included.
    """
    try:
        if not to:
            return {
                "success": False,
                "error": "At least one recipient required",
                "error_type": "validation_error",
            }

        all_recipients = to + (cc or []) + (bcc or [])
        safety_err = check_test_mode_safety("forward_message", recipients=all_recipients)
        if safety_err:
            return safety_err

        rate_err = check_rate_limit("forward_message", {"message_id": message_id, "to": to})
        if rate_err:
            return rate_err

        # Elicit user confirmation
        summary = _build_forward_summary(message_id, to, cc, bcc, body)
        cancel_err = await _elicit_confirmation(
            ctx, summary, "forward_message", {"message_id": message_id, "to": to}
        )
        if cancel_err:
            return cancel_err

        logger.info(f"Forwarding message {message_id} to {len(to)} recipient(s)")

        # Forward the message
        forward_id = mail.forward_message(
            message_id=message_id,
            to=to,
            body=body,
            cc=cc,
            bcc=bcc,
        )

        return {
            "success": True,
            "forward_id": forward_id,
            "original_message_id": message_id,
            "recipients": to,
            "cc": cc,
            "bcc": bcc,
        }

    except ValueError as e:
        logger.error(f"Validation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error forwarding message: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


# ---------------------------------------------------------------------
# Email templates (#30) — see docs/reference/TOOLS.md for the file format
# ---------------------------------------------------------------------

# Single shared store; root resolves at call-time via env override so tests
# and unusual setups can redirect.
def _get_template_store() -> TemplateStore:
    """Return the active TemplateStore. Re-resolved per call so the
    APPLE_MAIL_MCP_HOME env var (and test-time monkeypatching) take
    effect at use time, not import time."""
    return TemplateStore()


def _template_error_response(e: MailTemplateError) -> dict[str, Any]:
    """Map a template exception to the standard {success, error, error_type}
    response shape."""
    if isinstance(e, MailTemplateNotFoundError):
        et = "template_not_found"
    elif isinstance(e, MailTemplateInvalidNameError):
        et = "invalid_template_name"
    elif isinstance(e, MailTemplateInvalidFormatError):
        et = "invalid_template_format"
    elif isinstance(e, MailTemplateMissingVariableError):
        et = "missing_template_variable"
    else:
        et = "template_error"
    return {"success": False, "error": str(e), "error_type": et}


@mcp.tool()
def list_templates() -> dict[str, Any]:
    """List all stored email templates.

    Templates live as files at ~/.apple_mail_mcp/templates/<name>.md.
    Override the location with the APPLE_MAIL_MCP_HOME environment
    variable.

    Returns:
        Dictionary with each template's name and subject (or null if
        no subject header is set).
    """
    try:
        rate_err = check_rate_limit("list_templates", {})
        if rate_err:
            return rate_err
        templates = _get_template_store().list()
        operation_logger.log_operation("list_templates", {}, "success")
        return {
            "success": True,
            "templates": [
                {"name": t.name, "subject": t.subject} for t in templates
            ],
            "count": len(templates),
        }
    except Exception as e:
        logger.error(f"Error in list_templates: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}


@mcp.tool()
def get_template(name: str) -> dict[str, Any]:
    """Read a single template by name.

    Args:
        name: Template name (alphanumerics, underscore, hyphen; 1-64 chars).

    Returns:
        Dictionary with name, subject (may be null), body, and the sorted
        list of placeholder names found in subject + body.
    """
    try:
        rate_err = check_rate_limit("get_template", {"name": name})
        if rate_err:
            return rate_err
        t = _get_template_store().get(name)
        operation_logger.log_operation("get_template", {"name": name}, "success")
        return {
            "success": True,
            "name": t.name,
            "subject": t.subject,
            "body": t.body,
            "placeholders": t.placeholders(),
        }
    except MailTemplateError as e:
        return _template_error_response(e)
    except Exception as e:
        logger.error(f"Error in get_template: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}


@mcp.tool()
def save_template(
    name: str, body: str, subject: str | None = None
) -> dict[str, Any]:
    """Create or overwrite a template.

    Args:
        name: Template name (alphanumerics, underscore, hyphen; 1-64 chars).
        body: Template body text. May contain {placeholder} tokens.
        subject: Optional subject template. May also contain placeholders.

    Returns:
        Dictionary with the template name and a `created` flag (true for
        new templates, false when an existing template was overwritten).

    No confirmation prompt — additive (or self-overwrite, which is the
    explicit user intent for an idempotent save).
    """
    try:
        rate_err = check_rate_limit("save_template", {"name": name})
        if rate_err:
            return rate_err
        if not isinstance(body, str) or not body.strip():
            return {
                "success": False,
                "error": "body must be a non-empty string",
                "error_type": "validation_error",
            }
        # Normalize body to end with a newline so on-disk files stay tidy.
        normalized_body = body if body.endswith("\n") else body + "\n"
        template = Template(
            name=name, subject=subject, body=normalized_body
        )
        created = _get_template_store().save(template)
        operation_logger.log_operation(
            "save_template", {"name": name, "created": created}, "success"
        )
        return {"success": True, "name": name, "created": created}
    except MailTemplateError as e:
        return _template_error_response(e)
    except Exception as e:
        logger.error(f"Error in save_template: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}


@mcp.tool()
async def delete_template(
    name: str, ctx: Context | None = None
) -> dict[str, Any]:
    """Delete a template by name.

    Destructive — requires user confirmation via MCP elicitation before
    running.

    Args:
        name: Template name to delete.

    Returns:
        Dictionary with success status and the deleted template's name.
    """
    try:
        rate_err = check_rate_limit("delete_template", {"name": name})
        if rate_err:
            return rate_err
        # Verify it exists before asking the user — saves them a useless
        # confirmation prompt for a non-existent name.
        _get_template_store().get(name)

        summary = (
            f"Delete email template '{name}'? "
            f"This removes the file at ~/.apple_mail_mcp/templates/{name}.md."
        )
        cancel_err = await _elicit_confirmation(
            ctx, summary, "delete_template", {"name": name}
        )
        if cancel_err:
            return cancel_err

        _get_template_store().delete(name)
        operation_logger.log_operation(
            "delete_template", {"name": name}, "success"
        )
        return {"success": True, "name": name}
    except MailTemplateError as e:
        return _template_error_response(e)
    except Exception as e:
        logger.error(f"Error in delete_template: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}


@mcp.tool()
def render_template(
    name: str,
    message_id: str | None = None,
    vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render a template into ready-to-send subject and body text.

    No side effects — caller is responsible for passing the rendered
    text to reply_to_message, forward_message, or send_email.

    With ``message_id``, the original sender's display name and email,
    the original subject, and today's date are auto-populated as
    ``recipient_name``, ``recipient_email``, ``original_subject``, and
    ``today``. Without ``message_id``, only ``today`` is auto-filled.
    User-supplied ``vars`` always override auto-fills on conflict.

    Args:
        name: Template name to render.
        message_id: Optional source-message id for reply context.
        vars: Optional dict of variable overrides / additional values.

    Returns:
        Dictionary with the rendered subject (may be null), body, and
        the merged variable dict that was used.
    """
    try:
        rate_err = check_rate_limit("render_template", {"name": name})
        if rate_err:
            return rate_err
        template = _get_template_store().get(name)
        auto_vars = mail.auto_template_vars(message_id)
        merged: dict[str, str] = {**auto_vars, **(vars or {})}
        rendered = template.render(merged)
        operation_logger.log_operation(
            "render_template",
            {"name": name, "message_id": message_id},
            "success",
        )
        return {
            "success": True,
            "subject": rendered["subject"],
            "body": rendered["body"],
            "used_vars": merged,
        }
    except MailTemplateError as e:
        return _template_error_response(e)
    except MailMessageNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "message_not_found",
        }
    except Exception as e:
        logger.error(f"Error in render_template: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-mail-mcp",
        description=(
            "Apple Mail MCP server. With no subcommand, starts the MCP "
            "server (this is what Claude Desktop / mcp clients invoke)."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    setup_imap = sub.add_parser(
        "setup-imap",
        help=(
            "Configure the Keychain entry that enables the IMAP fast path "
            "for a Mail.app account."
        ),
    )
    setup_imap.add_argument(
        "--account",
        required=True,
        help="Mail.app account name (e.g. 'iCloud', 'Gmail').",
    )
    setup_imap.add_argument(
        "--email",
        default=None,
        help=(
            "Override the email address used as the Keychain key. "
            "Defaults to the first email in Mail.app's account configuration."
        ),
    )
    setup_imap.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "Remove the Keychain entry for this account (disables the IMAP "
            "fast path; AppleScript fallback continues to work)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Defaults to running the MCP server.

    With ``setup-imap`` (or any future subcommand), dispatches and exits
    with the subcommand's exit code. Returning an int from main() lets
    pytest-style tests assert exit codes without raising SystemExit.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "setup-imap":
        from .cli import run_setup_imap

        return run_setup_imap(
            account_name=args.account,
            cli_email=args.email,
            uninstall=args.uninstall,
        )

    logger.info("Starting Apple Mail MCP server")
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
