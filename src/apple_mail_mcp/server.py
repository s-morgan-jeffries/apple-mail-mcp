"""
FastMCP server for Apple Mail integration.
"""

import argparse
import atexit
import logging
import tempfile
from pathlib import Path
from typing import Any, cast

from fastmcp import Context, FastMCP
from fastmcp.server.elicitation import AcceptedElicitation

from .drafts import DraftStateStore, SeedRecord
from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftError,
    MailDraftInvalidIdError,
    MailDraftNotFoundError,
    MailImapRequiredError,
    MailMailboxNotEmptyError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailRuleNotFoundError,
    MailTemplateError,
    MailTemplateInvalidFormatError,
    MailTemplateInvalidNameError,
    MailTemplateMissingVariableError,
    MailTemplateNotFoundError,
    MailUnsupportedGmailSystemLabelError,
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


def _register_pool_atexit(pool: ImapConnectionPool | None) -> None:
    """Register ``pool.close()`` as an atexit hook so cached IMAP sessions
    get a clean LOGOUT on process exit instead of an abnormal disconnect
    (#127). No-op when ``pool`` is ``None`` (the default — pool is opt-in
    via ``APPLE_MAIL_MCP_IMAP_POOL=1``)."""
    if pool is not None:
        atexit.register(pool.close)


_imap_pool = _build_imap_pool()
_register_pool_atexit(_imap_pool)
mail = AppleMailConnector(imap_pool=_imap_pool)


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

    Patch semantics: only fields you provide are changed. ``conditions`` and
    ``actions``, when provided, REPLACE their respective structures wholesale
    (not merged).

    Conditional confirmation: prompts the user via MCP elicitation only when
    the patch touches ``conditions``, ``actions``, or ``match_logic`` —
    those replacements are irrecoverable. Patches limited to ``enabled``
    and/or ``name`` (trivially reversible) skip the prompt. The
    enable/disable path replaces the removed ``set_rule_enabled`` tool: call
    ``update_rule(rule_index, enabled=True|False)``.

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

        needs_confirmation = (
            conditions is not None
            or actions is not None
            or match_logic is not None
        )
        if needs_confirmation:
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


_SELECTED_SENTINEL = "SELECTED"


def _resolve_id_list_to_messages(
    ids: list[str],
    include_content: bool,
    account: str | None,
    mailbox: str | None,
    headers_only: bool = False,
    include_attachments: bool = False,
) -> list[dict[str, Any]]:
    """Resolve a mixed list of ids and ``SELECTED`` tokens to message dicts.

    ``SELECTED`` tokens expand inline to Mail.app's current UI selection
    (zero-or-more messages). Real ids are looked up via
    ``mail.get_message()``. Missing ids drop out silently
    (partial-results convention). The connector ``get_selected_messages``
    is called at most once even if ``SELECTED`` appears multiple times.

    Used by both ``search_messages.source`` (metadata mode,
    ``include_content=False``) and ``get_messages.message_ids`` (bodies
    mode, ``include_content=True``). The ``include_attachments`` flag
    threads through to both connector methods.
    """
    selected_resolved: list[dict[str, Any]] | None = None
    out: list[dict[str, Any]] = []
    for id_or_token in ids:
        if id_or_token == _SELECTED_SENTINEL:
            if selected_resolved is None:
                selected_resolved = mail.get_selected_messages(
                    include_content=include_content,
                    include_attachments=include_attachments,
                )
            out.extend(selected_resolved)
        else:
            try:
                msg = mail.get_message(
                    id_or_token,
                    include_content=include_content,
                    headers_only=headers_only,
                    account=account,
                    mailbox=mailbox,
                    include_attachments=include_attachments,
                )
                out.append(msg)
            except MailMessageNotFoundError:
                # Partial-results: missing ids drop out silently.
                continue
    return out


def _apply_search_filters(
    messages: list[dict[str, Any]],
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None,
    date_to: str | None,
    has_attachment: bool | None,
    limit: int,
    body_contains: str | None = None,
    text_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Post-filter a list of message dicts in Python.

    Used by the ``source=[ids]`` dispatch path of ``search_messages``:
    after resolving the id list to message dicts (some via
    ``mail.get_selected_messages`` for the ``SELECTED`` sentinel, others
    via per-id ``mail.get_message``), apply the same predicates the
    IMAP/AppleScript search paths apply server-side, then truncate to
    ``limit``. The corpus is bounded by the caller's id list, so the
    cost is negligible.

    ``body_contains`` and ``text_contains`` (#145) match against the
    ``content`` field — the server tier forces ``include_content=True``
    on the per-id fetch when these filters are set, so ``content`` is
    populated. ``text_contains`` checks ``content + subject + sender``
    (the practical IMAP ``TEXT`` approximation; recipients omitted).
    """
    def matches(m: dict[str, Any]) -> bool:
        if sender_contains is not None and sender_contains.lower() not in str(
            m.get("sender", "")
        ).lower():
            return False
        if subject_contains is not None and subject_contains.lower() not in str(
            m.get("subject", "")
        ).lower():
            return False
        if read_status is not None and bool(m.get("read_status")) != read_status:
            return False
        if is_flagged is not None and bool(m.get("flagged")) != is_flagged:
            return False
        if date_from is not None and str(m.get("date_received", "")) < date_from:
            return False
        if date_to is not None and str(m.get("date_received", "")) > date_to:
            return False
        if has_attachment is not None and bool(
            m.get("has_attachment")
        ) != has_attachment:
            return False
        if body_contains is not None and body_contains.lower() not in str(
            m.get("content", "")
        ).lower():
            return False
        if text_contains is not None:
            needle = text_contains.lower()
            haystack = (
                str(m.get("content", "")).lower()
                + " "
                + str(m.get("subject", "")).lower()
                + " "
                + str(m.get("sender", "")).lower()
            )
            if needle not in haystack:
                return False
        return True

    return [m for m in messages if matches(m)][:limit]


@mcp.tool()
def search_messages(
    account: str | None = None,
    mailbox: str = "INBOX",
    sender_contains: str | None = None,
    subject_contains: str | None = None,
    read_status: bool | None = None,
    is_flagged: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_attachment: bool | None = None,
    limit: int = 50,
    source: list[str] | None = None,
    include_attachments: bool = False,
    body_contains: str | None = None,
    text_contains: str | None = None,
) -> dict[str, Any]:
    """
    Search for messages matching criteria. Returns metadata-only rows.

    Two corpus modes:

    - ``source=None`` (default): search the given account/mailbox using
      the IMAP/AppleScript SEARCH path. ``account`` is required.
    - ``source=[id1, id2, ...]``: scope the search to the specific
      messages identified by the given ids. ``account``/``mailbox`` are
      ignored; the connector resolves each id self-sufficiently. The
      resulting message dicts are post-filtered by the other criteria
      (``sender_contains``, ``read_status``, etc.) — full filter
      composition. The literal token ``"SELECTED"`` may appear in the
      list and is server-resolved at call time to Mail.app's current UI
      selection (zero-or-more messages). Mixed lists like
      ``["SELECTED", "12345"]`` are valid. Missing ids drop out silently
      (partial-results).

    For thread retrieval, call ``get_thread(message_id)`` to expand an
    anchor into thread member ids, then optionally pipe those ids into
    ``source=[ids]`` for filtered metadata browsing or into
    ``get_messages([ids])`` for full bodies.

    Args:
        account: Mail.app account display name (e.g., "Gmail", "iCloud") or
            UUID (from list_accounts). Required when ``source is None``;
            ignored when ``source`` is a list. Names are convenient but
            unstable across renames; UUIDs are stable.
        mailbox: Mailbox name (default: "INBOX"). Ignored when ``source``
            is a list.
        sender_contains: Filter by sender email/domain substring.
        subject_contains: Filter by subject keywords substring.
        read_status: Filter by read status (true=read, false=unread).
        is_flagged: Filter by flagged status (true=flagged, false=not flagged).
        date_from: Inclusive lower bound on date received. ISO 8601 YYYY-MM-DD.
        date_to: Inclusive upper bound on date received (full day included). ISO 8601 YYYY-MM-DD.
        has_attachment: Filter messages with (true) or without (false) attachments.
        limit: Maximum results to return (default: 50).
        source: Optional list of message ids (with optional ``"SELECTED"``
            sentinel) to restrict the search to. ``None`` (default)
            searches the account/mailbox normally.
        include_attachments: When True, each row includes an ``attachments``
            field listing per-attachment metadata (name, mime_type, size,
            downloaded). Default False — opt-in because the AppleScript
            fallback path can be slow on cold caches (#142). Free on the
            IMAP fast path. To fetch attachment metadata for a known list
            of ids cheaply, prefer ``get_messages([ids])`` (default-on
            attachments, bounded cardinality).
        body_contains: Substring match against message body content. IMAP
            uses ``BODY`` predicate (sub-second); AppleScript reads
            ``content of msg`` per candidate (very slow on large mailboxes
            — measured 148s for 100 cold-cache messages). When the call
            commits to AppleScript with this filter set, a ``warnings``
            field is included in the response. Case-insensitive on both
            paths.
        text_contains: Substring match against headers + body (RFC 3501
            ``TEXT`` semantics). On AppleScript, approximated as
            ``content + subject + sender`` (recipients and other headers
            not matched). Same perf characteristics as ``body_contains``.

    Returns:
        Dictionary containing matching messages. Each message row includes
        id, subject, sender, date_received, read_status, flagged. Rows
        are metadata-only — call ``get_messages([ids])`` for bodies.

    Example:
        >>> search_messages("Gmail", sender_contains="john@example.com", read_status=False, limit=10)
        {"success": True, "messages": [...], "count": 5}
        >>> search_messages(source=["SELECTED"])
        {"success": True, "messages": [...], "count": 2}
        >>> search_messages(source=["12345", "SELECTED"], read_status=False)
        {"success": True, "messages": [...], "count": 3}
    """
    try:
        warnings: list[str] = []

        if source is not None:
            # body/text filters need bodies on the resolved messages so the
            # post-filter can match content. Force include_content=True for
            # the per-id fetch when these filters are set.
            need_body = bool(body_contains or text_contains)
            resolved = _resolve_id_list_to_messages(
                source,
                include_content=need_body,
                account=account,
                mailbox=mailbox,
                include_attachments=include_attachments,
            )
            filtered = _apply_search_filters(
                resolved,
                sender_contains,
                subject_contains,
                read_status,
                is_flagged,
                date_from,
                date_to,
                has_attachment,
                limit,
                body_contains=body_contains,
                text_contains=text_contains,
            )
            operation_logger.log_operation(
                "search_messages",
                {
                    "source": source,
                    "filters": {
                        "sender": sender_contains,
                        "subject": subject_contains,
                        "read_status": read_status,
                        "is_flagged": is_flagged,
                        "date_from": date_from,
                        "date_to": date_to,
                        "has_attachment": has_attachment,
                        "body_contains": body_contains,
                        "text_contains": text_contains,
                    },
                },
                "success",
            )
            response: dict[str, Any] = {
                "success": True,
                "account": None,
                "mailbox": None,
                "messages": filtered,
                "count": len(filtered),
            }
            if warnings:
                response["warnings"] = warnings
            return response

        if account is None:
            return {
                "success": False,
                "error": "account is required when source is not provided",
                "error_type": "validation_error",
            }

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
            include_attachments=include_attachments,
            body_contains=body_contains,
            text_contains=text_contains,
            on_warning=warnings.append,
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
                    "body_contains": body_contains,
                    "text_contains": text_contains,
                },
            },
            "success"
        )

        response = {
            "success": True,
            "account": account,
            "mailbox": mailbox,
            "messages": messages,
            "count": len(messages),
        }
        if warnings:
            response["warnings"] = warnings
        return response

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
def get_messages(
    message_ids: list[str],
    include_content: bool = True,
    headers_only: bool = False,
    account: str | None = None,
    mailbox: str | None = None,
    include_attachments: bool = True,
) -> dict[str, Any]:
    """
    Get full details of one or more messages, with bodies.

    Returns a list of message dicts (possibly of length 0 or 1). Pair with
    ``search_messages`` (metadata-only) and ``get_thread`` (thread member
    ids) to fetch bodies for specific messages.

    Args:
        message_ids: List of message ids to fetch. May include the literal
            token ``"SELECTED"``, which the server resolves at call time
            to Mail.app's current UI selection (zero-or-more messages).
            Mixed lists like ``["SELECTED", "12345"]`` are valid. Empty
            list is a no-op (returns empty result, no error). Missing ids
            drop out silently (partial-results convention) — the response
            contains whatever was found.
        include_content: Include message bodies (default: True).
        headers_only: Skip body fetch on the IMAP path for explicit ids
            (default: False). Silently ignored on the AppleScript fallback.
        account: Mail.app account name. Together with ``mailbox``, activates
            the IMAP fast path for explicit ids: one round-trip lookup
            instead of an account×mailbox AppleScript scan (issue #72).
            Ignored for the ``"SELECTED"`` sentinel (selection is global).
        mailbox: Folder to look in for the IMAP fast path (e.g. "INBOX").
        include_attachments: Include per-attachment metadata (name,
            mime_type, size, downloaded) on each message (default: True).
            Bounded cost — id-list cardinality is typically 1-10. Free on
            the IMAP fast path; cheap-enough on the AppleScript fallback
            for typical id counts.

    Returns:
        Dictionary containing the list of messages and count.

    Example:
        >>> get_messages(["12345"], account="iCloud", mailbox="INBOX")
        {"success": True, "messages": [...], "count": 1}
        >>> get_messages(["SELECTED"])
        {"success": True, "messages": [...], "count": 2}
        >>> get_messages(["SELECTED", "12345"])
        {"success": True, "messages": [...], "count": 3}
    """
    try:
        rate_err = check_rate_limit(
            "get_messages", {"count": len(message_ids)}
        )
        if rate_err:
            return rate_err

        logger.info(f"Getting messages: {len(message_ids)} ids")

        messages = _resolve_id_list_to_messages(
            message_ids,
            include_content=include_content,
            account=account,
            mailbox=mailbox,
            headers_only=headers_only,
            include_attachments=include_attachments,
        )

        operation_logger.log_operation(
            "get_messages",
            {"count": len(message_ids)},
            "success"
        )

        return {
            "success": True,
            "messages": messages,
            "count": len(messages),
        }

    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def update_message(
    message_ids: list[str],
    read_status: bool | None = None,
    flagged: bool | None = None,
    flag_color: str | None = None,
    destination_mailbox: str | None = None,
    account: str | None = None,
    source_mailbox: str | None = None,
    gmail_mode: bool = False,
) -> dict[str, Any]:
    """
    Update one or more messages: change read state, flag, and/or move,
    in one atomic call (#135).

    Patch semantics — caller specifies only the fields to change. All
    specified mutations apply in a single AppleScript pass via the
    bulk-update helper. Replaces the previous `mark_as_read`,
    `move_messages`, and `flag_message` tools.

    Order of operations (matters for IMAP): read-state and flag changes
    apply first (in source mailbox), then the move. IMAP requires the
    message to exist in the source folder for STORE before MOVE.

    Args:
        message_ids: List of message IDs to update.
        read_status: True to mark as read, False to mark as unread,
            None to leave unchanged.
        flagged: True to flag (default red if no `flag_color` set),
            False to clear the flag, None to leave unchanged.
        flag_color: Color name (orange, red, yellow, blue, green,
            purple, gray, none). Implies `flagged=True` unless "none".
            Validated against the existing flag-color schema.
        destination_mailbox: Move messages here (requires `account`).
        account: Account name or UUID hosting the destination mailbox.
            Required when `destination_mailbox` is set; also used with
            `source_mailbox` for narrow-path optimization.
        source_mailbox: Source mailbox name. With `account`, narrows the
            AppleScript scan to one mailbox (O(N) instead of cross-scan).
        gmail_mode: Use Gmail-specific copy+delete instead of MOVE.

    Returns:
        Dictionary with `updated` (int count) and `requested` (input count).

    Example:
        >>> # Mark read + move to Archive in one call:
        >>> update_message(
        ...     ["12345"], read_status=True,
        ...     destination_mailbox="Archive", account="iCloud",
        ...     source_mailbox="INBOX",
        ... )
        {"success": True, "updated": 1, "requested": 1}

        >>> # Restore from Trash:
        >>> update_message(
        ...     ["12345"], destination_mailbox="INBOX",
        ...     account="iCloud", source_mailbox="Deleted Messages",
        ... )

        >>> # Set red flag:
        >>> update_message(["12345"], flag_color="red")
    """
    try:
        # Validate at least one field is set (AC #3 from #135).
        if (
            read_status is None
            and flagged is None
            and flag_color is None
            and destination_mailbox is None
        ):
            return {
                "success": False,
                "error": "specify at least one field to update",
                "error_type": "validation_error",
            }

        # Test-mode safety: when account is provided (moves, or narrow-path),
        # gate against MAIL_TEST_ACCOUNT.
        if account is not None:
            safety_err = check_test_mode_safety(
                "update_message", account=account
            )
            if safety_err:
                return safety_err

        rate_err = check_rate_limit("update_message", {"count": len(message_ids)})
        if rate_err:
            return rate_err

        # Validate bulk size
        is_valid, error_msg = validate_bulk_operation(len(message_ids), max_items=100)
        if not is_valid:
            logger.error(f"Validation failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "error_type": "validation_error",
            }

        logger.info(
            f"Updating {len(message_ids)} messages "
            f"(read={read_status}, flagged={flagged}, color={flag_color}, "
            f"dest={destination_mailbox})"
        )

        count = mail.update_message(
            message_ids,
            read_status=read_status,
            flagged=flagged,
            flag_color=flag_color,
            destination_mailbox=destination_mailbox,
            account=account,
            source_mailbox=source_mailbox,
            gmail_mode=gmail_mode,
        )

        operation_logger.log_operation(
            "update_message",
            {
                "count": len(message_ids),
                "read_status": read_status,
                "flagged": flagged,
                "flag_color": flag_color,
                "destination_mailbox": destination_mailbox,
            },
            "success",
        )

        return {
            "success": True,
            "updated": count,
            "requested": len(message_ids),
        }

    except MailAccountNotFoundError as e:
        logger.error(f"Account not found: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "account_not_found",
        }
    except MailMailboxNotFoundError as e:
        logger.error(f"Mailbox not found: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "not_found",
        }
    except ValueError as e:
        logger.error(f"Validation error in update_message: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except Exception as e:
        logger.error(f"Error updating messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def get_thread(message_id: str) -> dict[str, Any]:
    """
    Return all messages in the thread containing the given message.

    Looks up the anchor message by its id, then reconstructs the
    conversation via the connector's tiered IMAP threading dispatch
    (Tier 1 X-GM-THRID for Gmail, Tier 3 header-search BFS fallback)
    or the AppleScript path. Result rows are sorted by ``date_received``
    ascending.

    The returned ids can be piped into ``search_messages(source=[ids])``
    for filtered metadata or ``get_messages([ids])`` for full bodies.

    Known limitation: thread members whose subject was rewritten
    mid-conversation are missed on the AppleScript fallback path
    (subject prefilter tradeoff).

    Args:
        message_id: Internal id of any message in the thread
            (from ``search_messages`` or ``get_messages`` results).

    Returns:
        Dictionary with the thread list. Rows are metadata-only —
        id, subject, sender, date_received, read_status, flagged.

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
def update_mailbox(
    account: str,
    name: str,
    new_name: str | None = None,
    new_parent: str | None = None,
) -> dict[str, Any]:
    """Rename and/or re-parent (move) an existing mailbox.

    Two delivery paths:

    - **Rename only** (``new_name`` set, ``new_parent`` is ``None``):
      AppleScript. Fast, no IMAP credentials needed.
    - **Move** (``new_parent`` set; optionally combined with rename):
      IMAP RENAME. Requires IMAP credentials in Keychain (#73 opt-in
      flow) — returns ``error_type: "imap_required"`` when missing.

    At least one of ``new_name`` / ``new_parent`` must be provided.

    Refused (#164): operations targeting the bare ``[Gmail]`` parent or
    any ``[Gmail]/...`` child path return ``error_type:
    "unsupported_gmail_system_label"``. Applies to both the source
    ``name`` and the resulting destination (``new_parent`` join). Gmail's
    IMAP server doesn't support normal RENAME semantics for these paths;
    user-created Gmail labels (``Newsletters``, etc.) behave normally.

    Args:
        account: Mail.app account display name or UUID.
        name: Current mailbox name. Slash-separated for nested mailboxes
            (e.g. ``"Archive/2024"``).
        new_name: Replacement leaf name. ``None`` to keep the current
            leaf when moving. Path-traversal characters stripped via
            ``sanitize_mailbox_name``; an entirely-stripped value
            returns ``validation_error``.
        new_parent: Destination parent path. ``None`` keeps current
            parent (rename-only). ``""`` (empty string) moves to
            top-level. Non-empty string moves under that path.

    Returns:
        ``{success, account, name, new_name, new_parent}`` on success,
        or structured error response.
    """
    try:
        safety_err = check_test_mode_safety("update_mailbox", account=account)
        if safety_err:
            return safety_err

        if not name or not name.strip():
            return {
                "success": False,
                "error": "Mailbox name cannot be empty",
                "error_type": "validation_error",
            }
        if new_name is None and new_parent is None:
            return {
                "success": False,
                "error": "At least one of new_name or new_parent is required",
                "error_type": "validation_error",
            }
        if new_name is not None and not new_name.strip():
            return {
                "success": False,
                "error": "new_name cannot be empty (pass None to keep current leaf)",
                "error_type": "validation_error",
            }

        rate_err = check_rate_limit(
            "update_mailbox",
            {"account": account, "name": name, "new_name": new_name,
             "new_parent": new_parent},
        )
        if rate_err:
            return rate_err

        logger.info(
            f"Updating mailbox {name!r} in {account}: "
            f"new_name={new_name!r}, new_parent={new_parent!r}"
        )

        success = mail.update_mailbox(
            account=account, name=name,
            new_name=new_name, new_parent=new_parent,
        )
        operation_logger.log_operation(
            "update_mailbox",
            {"account": account, "name": name,
             "new_name": new_name, "new_parent": new_parent},
            "success" if success else "failure",
        )

        return {
            "success": success,
            "account": account,
            "name": name,
            "new_name": new_name,
            "new_parent": new_parent,
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailImapRequiredError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "imap_required",
        }
    except MailMailboxNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "mailbox_not_found",
        }
    except MailAccountNotFoundError:
        return {
            "success": False,
            "error": f"Account {account!r} not found",
            "error_type": "account_not_found",
        }
    except MailAppleScriptError as e:
        logger.error(f"AppleScript error in update_mailbox: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "applescript_error",
        }
    except MailUnsupportedGmailSystemLabelError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "unsupported_gmail_system_label",
        }
    except Exception as e:
        logger.exception(f"Unexpected error in update_mailbox: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
async def delete_mailbox(
    account: str,
    name: str,
    delete_messages: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a mailbox via IMAP.

    Mail.app's AppleScript dictionary doesn't expose a working delete
    primitive for mailboxes, so this operation goes through IMAP. Requires
    IMAP credentials in Keychain (#73 opt-in flow) — returns
    ``error_type: "imap_required"`` when missing.

    Always elicits user confirmation (destructive). By default refuses
    non-empty mailboxes to prevent accidental data loss; pass
    ``delete_messages=True`` to cascade.

    Refused (#164): targeting the bare ``[Gmail]`` parent or any
    ``[Gmail]/...`` child path returns ``error_type:
    "unsupported_gmail_system_label"``. Gmail's IMAP server doesn't
    support DELETE for these paths.

    Args:
        account: Mail.app account display name or UUID.
        name: Mailbox name. Slash-separated for nested mailboxes.
        delete_messages: When False (default), refuse if the mailbox
            contains messages. When True, cascade-delete the mailbox
            and its contents.

    Returns:
        ``{success, account, name, deleted_message_count}`` on success.
    """
    try:
        safety_err = check_test_mode_safety("delete_mailbox", account=account)
        if safety_err:
            return safety_err

        if not name or not name.strip():
            return {
                "success": False,
                "error": "Mailbox name cannot be empty",
                "error_type": "validation_error",
            }

        rate_err = check_rate_limit(
            "delete_mailbox",
            {"account": account, "name": name,
             "delete_messages": delete_messages},
        )
        if rate_err:
            return rate_err

        verb = "delete (cascading messages)" if delete_messages else "delete (refuse if non-empty)"
        summary = (
            f"{verb} mailbox?\n\n"
            f"Account: {account}\n"
            f"Mailbox: {name}\n\n"
            f"This is destructive. The mailbox will be removed from the IMAP server."
        )
        cancel_err = await _elicit_confirmation(
            ctx, summary, "delete_mailbox",
            {"account": account, "name": name,
             "delete_messages": delete_messages},
        )
        if cancel_err:
            return cancel_err

        count = mail.delete_mailbox(
            account=account, name=name, delete_messages=delete_messages
        )
        operation_logger.log_operation(
            "delete_mailbox",
            {"account": account, "name": name,
             "delete_messages": delete_messages,
             "deleted_message_count": count},
            "success",
        )
        return {
            "success": True,
            "account": account,
            "name": name,
            "deleted_message_count": count,
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailImapRequiredError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "imap_required",
        }
    except MailMailboxNotEmptyError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "mailbox_not_empty",
        }
    except MailMailboxNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "mailbox_not_found",
        }
    except MailAccountNotFoundError:
        return {
            "success": False,
            "error": f"Account {account!r} not found",
            "error_type": "account_not_found",
        }
    except MailUnsupportedGmailSystemLabelError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "unsupported_gmail_system_label",
        }
    except Exception as e:
        logger.exception(f"Unexpected error in delete_mailbox: {e}")
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
    text to ``create_draft`` or ``update_draft`` (with ``send_now=True``
    when ready to send).

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


def _get_draft_state_store() -> DraftStateStore:
    """Return the active DraftStateStore. Re-resolved per call so the
    APPLE_MAIL_MCP_HOME env var (and test-time monkeypatching) take
    effect at use time, not import time. Mirrors _get_template_store."""
    return DraftStateStore()


def _draft_error_response(e: MailDraftError) -> dict[str, Any]:
    """Map a draft exception to {success, error, error_type}."""
    if isinstance(e, MailDraftNotFoundError):
        et = "draft_not_found"
    elif isinstance(e, MailDraftInvalidIdError):
        et = "invalid_draft_id"
    else:
        et = "draft_error"
    return {"success": False, "error": str(e), "error_type": et}


def _draft_action_error(op: str, e: Exception) -> dict[str, Any] | None:
    """Map a catchable draft-action exception to a response dict.

    Returns None if the exception isn't one we model here (caller should
    fall through to a generic ``unknown`` mapping). Centralizing this
    keeps the per-tool exception handling small enough to stay under
    the cyclomatic-complexity threshold."""
    if isinstance(e, MailMessageNotFoundError):
        return {"success": False, "error": str(e), "error_type": "message_not_found"}
    if isinstance(e, MailAccountNotFoundError):
        return {"success": False, "error": str(e), "error_type": "account_not_found"}
    if isinstance(e, FileNotFoundError):
        return {"success": False, "error": str(e), "error_type": "file_not_found"}
    if isinstance(e, MailDraftError):
        return _draft_error_response(e)
    if isinstance(e, MailAppleScriptError):
        logger.error(f"AppleScript error in {op}: {e}")
        return {"success": False, "error": str(e), "error_type": "applescript_error"}
    return None


def _resolve_draft_seed(
    draft_id: str,
    state: dict[str, Any],
    store: DraftStateStore,
) -> tuple[str, str | None, bool]:
    """Determine the seed kind / id / reply_all for an update_draft call.

    Lookup order: persisted disk state first (fast); In-Reply-To header
    fallback for externally-created reply drafts (slow); fresh seed
    if neither yields anything.

    Returns ``(seed_kind, seed_id, reply_all)``.
    """
    seed_record = store.get_seed(draft_id)
    if seed_record:
        return seed_record.seed_kind, seed_record.seed_id, seed_record.reply_all

    in_reply_to = state.get("in_reply_to") or ""
    if in_reply_to:
        logger.warning(
            "update_draft falling back to In-Reply-To lookup for "
            "externally-created draft %s — this may take 30s+ on "
            "large mailboxes",
            draft_id,
        )
        resolved = mail.find_message_by_message_id(in_reply_to)
        if resolved:
            return "reply", resolved, False

    return "new", None, False


def _resolve_draft_attachments(
    draft_id: str,
    attachment_paths: list[str] | None,
    existing_names: list[str],
) -> tuple[list[Path] | None, "tempfile.TemporaryDirectory[str] | None"]:
    """Compute final attachment paths for an update_draft call.

    Semantics:
        - ``attachment_paths is None`` AND draft has attachments
            → extract existing to a temp dir; caller must clean it up.
        - ``attachment_paths is None`` AND no existing attachments → None.
        - ``attachment_paths == []`` → explicitly clear.
        - ``attachment_paths == [...]`` → replace with caller-supplied list.

    Returns ``(final_paths, tempdir_to_clean_up)``. Caller is responsible
    for the tempdir's lifecycle (typically via ``finally`` cleanup).
    """
    if attachment_paths is not None:
        if attachment_paths == []:
            return [], None
        return [Path(p) for p in attachment_paths], None

    if not existing_names:
        return None, None

    tempdir = tempfile.TemporaryDirectory(prefix="amm-update-attach-")
    extracted = mail.extract_draft_attachments(
        draft_id, existing_names, Path(tempdir.name)
    )
    return extracted, tempdir


def _build_draft_send_summary(
    seed_kind: str,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str | None,
    body: str,
) -> str:
    """Confirmation summary when create_draft / update_draft is sending."""
    verb = {"reply": "Send this reply?", "forward": "Forward this message?"}.get(
        seed_kind, "Send this email?"
    )
    lines: list[str] = []
    if to:
        lines.append(f"To: {', '.join(to)}")
    if cc:
        lines.append(f"CC: {', '.join(cc)}")
    if bcc:
        lines.append(f"BCC: {', '.join(bcc)}")
    if subject:
        lines.append(f"Subject: {subject}")
    if body:
        preview = body[:200] + "..." if len(body) > 200 else body
        lines.append(f"\n{preview}")
    return verb + "\n\n" + "\n".join(lines)


@mcp.tool()
async def create_draft(
    reply_to: str | None = None,
    forward_of: str | None = None,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    body: str = "",
    attachment_paths: list[str] | None = None,
    reply_all: bool = False,
    template_name: str | None = None,
    template_vars: dict[str, str] | None = None,
    from_account: str | None = None,
    send_now: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a draft (fresh, reply, or forward). Optionally send immediately.

    Mail.app's actual primitive is the draft — every outgoing message is
    a draft until sent. This tool lets callers create one, optionally
    seeded from an existing message (reply or forward), and either save
    it for later or send it now.

    Args:
        reply_to: Mail.app id of a message to reply to. Mutually exclusive
            with ``forward_of``. When set, ``to``/``cc`` recipients and
            ``subject`` are auto-derived from the original (override by
            passing them explicitly).
        forward_of: Mail.app id of a message to forward. Mutually exclusive
            with ``reply_to``. ``to`` is required (recipient of the forward).
        to/cc/bcc: Recipient lists. For reply/forward, ``None`` keeps the
            auto-derived recipients; ``[]`` explicitly clears that group;
            a populated list replaces.
        subject: Subject. Required when both seeds are None. For
            reply/forward, ``None`` keeps Mail's ``Re:``/``Fwd:`` prefix.
        body: Body text. For reply/forward, a non-empty body REPLACES
            Mail's auto-quoted content; an empty body leaves the
            auto-quote intact (matches Mail.app's default reply behavior).
        attachment_paths: List of file paths to attach.
        reply_all: For ``reply_to`` only — use ``reply to all``.
        template_name: Optional template to render for ``subject`` and
            ``body``. Caller-supplied ``subject``/``body`` override the
            rendered output. ``template_vars`` override auto-fills.
        template_vars: Variables to pass to the template renderer.
            Requires ``template_name``.
        from_account: Mail.app account name or UUID. ``None`` uses Mail's
            default.
        send_now: ``False`` (default) saves as draft. ``True`` sends
            immediately and elicits user confirmation.

    Returns:
        ``{"success": True, "draft_id": "<id>", "sent_message_id": ""}``
        when saved as draft. ``draft_id`` is empty when sent.
    """
    try:
        # ----------------------------------------------------------------
        # Param-shape validation
        # ----------------------------------------------------------------
        if reply_to and forward_of:
            return {
                "success": False,
                "error": "reply_to and forward_of are mutually exclusive",
                "error_type": "validation_error",
            }
        if template_vars and not template_name:
            return {
                "success": False,
                "error": "template_vars requires template_name",
                "error_type": "validation_error",
            }

        # Resolve seed.
        if reply_to:
            seed_kind = "reply"
            seed_id: str | None = reply_to
        elif forward_of:
            seed_kind = "forward"
            seed_id = forward_of
        else:
            seed_kind = "new"
            seed_id = None

        # ----------------------------------------------------------------
        # Template resolution
        # ----------------------------------------------------------------
        if template_name:
            try:
                template = _get_template_store().get(template_name)
                auto_vars = mail.auto_template_vars(seed_id)
                merged_vars: dict[str, str] = {**auto_vars, **(template_vars or {})}
                rendered = template.render(merged_vars)
                # User-supplied subject/body override the rendered output.
                if subject is None:
                    subject = rendered["subject"]
                if not body:
                    body = rendered["body"] or ""
            except MailTemplateError as e:
                return _template_error_response(e)

        # Fresh-seed required-field validation (after template rendering
        # so a template can supply subject/body).
        if seed_kind == "new":
            if not to:
                return {
                    "success": False,
                    "error": "'to' is required when not replying or forwarding",
                    "error_type": "validation_error",
                }
            if not subject:
                return {
                    "success": False,
                    "error": "'subject' is required when not replying or forwarding",
                    "error_type": "validation_error",
                }

        # ----------------------------------------------------------------
        # Send-only checks (drafts are local — no rate limit / safety)
        # ----------------------------------------------------------------
        if send_now:
            all_recipients = (to or []) + (cc or []) + (bcc or [])
            if all_recipients:
                safety_err = check_test_mode_safety(
                    "create_draft", recipients=all_recipients
                )
                if safety_err:
                    return safety_err
            rate_err = check_rate_limit(
                "create_draft", {"subject": subject, "to": to}
            )
            if rate_err:
                return rate_err
            if to is not None or cc is not None or bcc is not None:
                # Only validate recipient shape when caller supplied any —
                # for reply with no overrides, recipients come from Mail.
                is_valid, error_msg = validate_send_operation(
                    to or [], cc, bcc
                )
                if not is_valid:
                    return {
                        "success": False,
                        "error": error_msg,
                        "error_type": "validation_error",
                    }
            summary = _build_draft_send_summary(
                seed_kind, to, cc, bcc, subject, body
            )
            cancel_err = await _elicit_confirmation(
                ctx, summary, "create_draft",
                {"subject": subject, "to": to, "seed_kind": seed_kind},
            )
            if cancel_err:
                return cancel_err

        # ----------------------------------------------------------------
        # Connector call
        # ----------------------------------------------------------------
        attachment_path_objs = (
            [Path(p) for p in attachment_paths]
            if attachment_paths is not None
            else None
        )
        result = mail.create_draft(
            seed=seed_kind,
            seed_id=seed_id,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            attachment_paths=attachment_path_objs,
            reply_all=reply_all,
            from_account=from_account,
            send_now=send_now,
        )
        draft_id = result.get("draft_id", "")

        # Persist seed metadata for reply/forward drafts so update_draft
        # can rebuild without an O(N) header lookup.
        if not send_now and draft_id and seed_kind in ("reply", "forward") and seed_id:
            _get_draft_state_store().set_seed(
                draft_id,
                SeedRecord(
                    seed_kind=cast(Any, seed_kind),
                    seed_id=seed_id,
                    reply_all=reply_all,
                ),
            )

        operation_logger.log_operation(
            "create_draft",
            {
                "seed_kind": seed_kind,
                "seed_id": seed_id,
                "send_now": send_now,
                "draft_id": draft_id,
            },
            "success",
        )
        return {
            "success": True,
            "draft_id": draft_id,
            "sent_message_id": result.get("sent_message_id", ""),
            "details": {"seed_kind": seed_kind, "send_now": send_now},
        }

    except Exception as e:
        handled = _draft_action_error("create_draft", e)
        if handled is not None:
            return handled
        logger.exception(f"Unexpected error in create_draft: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}


@mcp.tool()
async def update_draft(
    draft_id: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    attachment_paths: list[str] | None = None,
    template_name: str | None = None,
    template_vars: dict[str, str] | None = None,
    from_account: str | None = None,
    send_now: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update an existing draft. Implemented as delete-and-recreate.

    **Returns a NEW draft_id** — Mail.app forbids mutating saved drafts,
    so update is implemented by reading the draft's current state,
    deleting it, and creating a new draft with the merged fields.
    Threading headers (for reply seeds) and forward anchor are preserved
    via persisted seed metadata.

    Field merge semantics: any non-None argument overrides the existing
    value. ``None`` keeps the existing value. ``attachment_paths=None``
    PRESERVES existing attachments (extracted via Mail's ``save``
    command); ``[]`` explicitly clears them; a list replaces.

    For drafts created externally (not via ``create_draft``), seed
    recovery falls back to scanning Mail.app for the In-Reply-To header
    — this can be slow on large mailboxes (~30s+ per call). Forward
    seeds without disk state are misclassified as fresh; pass an
    explicit body if so.

    Args:
        draft_id: Mail.app id of the existing draft.
        to/cc/bcc: Override recipient groups (None = keep, [] = clear,
            list = replace).
        subject: Override subject. None keeps existing.
        body: Override body. None keeps existing. Non-None replaces
            (including the empty string, which clears).
        attachment_paths: Override attachments. None preserves existing
            via temp-dir extraction; [] clears; list replaces.
        template_name / template_vars: Optional template render. User-
            supplied subject/body override the rendered output.
        from_account: Override sender.
        send_now: ``False`` (default) saves new draft. ``True`` sends
            after eliciting confirmation.

    Returns:
        ``{"success": True, "draft_id": "<NEW>", "sent_message_id": ""}``.
    """
    tempdir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if template_vars and not template_name:
            return {
                "success": False,
                "error": "template_vars requires template_name",
                "error_type": "validation_error",
            }

        # ----------------------------------------------------------------
        # Read existing state and resolve seed
        # ----------------------------------------------------------------
        try:
            state = mail.get_draft_state(draft_id)
        except MailDraftError as e:
            return _draft_error_response(e)

        store = _get_draft_state_store()
        seed_kind, seed_id, reply_all = _resolve_draft_seed(
            draft_id, state, store
        )

        # ----------------------------------------------------------------
        # Template resolution (overrides nothing the user explicitly set)
        # ----------------------------------------------------------------
        merged_subject = subject
        merged_body = body
        if template_name:
            try:
                template = _get_template_store().get(template_name)
                auto_vars = mail.auto_template_vars(seed_id)
                merged_vars: dict[str, str] = {
                    **auto_vars, **(template_vars or {})
                }
                rendered = template.render(merged_vars)
                if merged_subject is None:
                    merged_subject = rendered["subject"]
                if merged_body is None:
                    merged_body = rendered["body"] or ""
            except MailTemplateError as e:
                return _template_error_response(e)

        # ----------------------------------------------------------------
        # Field merge: caller's non-None > existing state
        # ----------------------------------------------------------------
        final_to = to if to is not None else state.get("to", [])
        final_cc = cc if cc is not None else state.get("cc", [])
        final_bcc = bcc if bcc is not None else state.get("bcc", [])
        final_subject = (
            merged_subject if merged_subject is not None else state.get("subject")
        )
        final_body = (
            merged_body if merged_body is not None else state.get("body", "")
        )

        # ----------------------------------------------------------------
        # Attachment handling: preserve via extraction when None, replace
        # otherwise. tempdir (if any) is cleaned up in the finally block.
        # ----------------------------------------------------------------
        final_attachments, tempdir = _resolve_draft_attachments(
            draft_id, attachment_paths, state.get("attachment_names", []) or []
        )

        # ----------------------------------------------------------------
        # Send-only checks
        # ----------------------------------------------------------------
        if send_now:
            all_recipients = (
                (final_to or []) + (final_cc or []) + (final_bcc or [])
            )
            if all_recipients:
                safety_err = check_test_mode_safety(
                    "update_draft", recipients=all_recipients
                )
                if safety_err:
                    return safety_err
            rate_err = check_rate_limit(
                "update_draft",
                {"draft_id": draft_id, "subject": final_subject},
            )
            if rate_err:
                return rate_err
            summary = _build_draft_send_summary(
                seed_kind, final_to, final_cc, final_bcc, final_subject,
                final_body or "",
            )
            cancel_err = await _elicit_confirmation(
                ctx, summary, "update_draft",
                {"draft_id": draft_id, "send_now": True},
            )
            if cancel_err:
                return cancel_err

        # ----------------------------------------------------------------
        # Delete + recreate. Clear stale state first so a connector failure
        # doesn't leave orphan entries.
        # ----------------------------------------------------------------
        try:
            mail.delete_draft(draft_id)
        except MailDraftNotFoundError:
            return _draft_error_response(
                MailDraftNotFoundError(f"no draft with id {draft_id!r}")
            )
        store.delete(draft_id)

        result = mail.create_draft(
            seed=seed_kind,
            seed_id=seed_id,
            to=final_to,
            cc=final_cc,
            bcc=final_bcc,
            subject=final_subject,
            body=final_body or "",
            attachment_paths=final_attachments,
            reply_all=reply_all,
            from_account=from_account,
            send_now=send_now,
        )
        new_draft_id = result.get("draft_id", "")

        # Re-persist seed state for reply/forward drafts under the NEW id.
        if (
            not send_now
            and new_draft_id
            and seed_kind in ("reply", "forward")
            and seed_id
        ):
            store.set_seed(
                new_draft_id,
                SeedRecord(
                    seed_kind=cast(Any, seed_kind),
                    seed_id=seed_id,
                    reply_all=reply_all,
                ),
            )

        operation_logger.log_operation(
            "update_draft",
            {
                "old_draft_id": draft_id,
                "new_draft_id": new_draft_id,
                "send_now": send_now,
            },
            "success",
        )
        return {
            "success": True,
            "draft_id": new_draft_id,
            "sent_message_id": result.get("sent_message_id", ""),
            "details": {"seed_kind": seed_kind, "send_now": send_now},
        }

    except Exception as e:
        handled = _draft_action_error("update_draft", e)
        if handled is not None:
            return handled
        logger.exception(f"Unexpected error in update_draft: {e}")
        return {"success": False, "error": str(e), "error_type": "unknown"}
    finally:
        if tempdir is not None:
            try:
                tempdir.cleanup()
            except Exception:
                pass


@mcp.tool()
def delete_draft(draft_id: str) -> dict[str, Any]:
    """Delete (move to Trash) an existing draft.

    Lifecycle endpoint for cancellation. Mail.app moves the message to
    the Deleted Messages mailbox; recovery is technically possible but
    Mail.app no longer treats trashed drafts as editable, so this is
    effectively a one-way discard. No elicitation (recoverable from
    Trash) and no rate limit (local operation).

    Args:
        draft_id: Mail.app id of the draft.

    Returns:
        ``{"success": True}`` on a clean delete; an error response if
        no draft with that id exists.
    """
    try:
        mail.delete_draft(draft_id)
        _get_draft_state_store().delete(draft_id)
        operation_logger.log_operation(
            "delete_draft", {"draft_id": draft_id}, "success"
        )
        return {"success": True, "draft_id": draft_id}
    except MailDraftError as e:
        return _draft_error_response(e)
    except MailAppleScriptError as e:
        logger.error(f"AppleScript error in delete_draft: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "applescript_error",
        }
    except Exception as e:
        logger.exception(f"Unexpected error in delete_draft: {e}")
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
