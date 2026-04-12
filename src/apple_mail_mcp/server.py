"""
FastMCP server for Apple Mail integration.
"""

import logging
from typing import Any

from fastmcp import FastMCP

from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from .mail_connector import AppleMailConnector
from .security import (
    check_rate_limit,
    operation_logger,
    require_confirmation,
    validate_bulk_operation,
    validate_send_operation,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastMCP server
mcp = FastMCP("apple-mail")

# Initialize mail connector
mail = AppleMailConnector()


@mcp.tool()
def list_mailboxes(account: str) -> dict[str, Any]:
    """
    List all mailboxes for an account.

    Args:
        account: Account name (e.g., "Gmail", "iCloud")

    Returns:
        Dictionary containing mailboxes list

    Example:
        >>> list_mailboxes("Gmail")
        {"mailboxes": [{"name": "INBOX", "unread_count": 5}, ...]}
    """
    try:
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
    limit: int = 50,
) -> dict[str, Any]:
    """
    Search for messages matching criteria.

    Args:
        account: Account name (e.g., "Gmail", "iCloud")
        mailbox: Mailbox name (default: "INBOX")
        sender_contains: Filter by sender email/domain
        subject_contains: Filter by subject keywords
        read_status: Filter by read status (true=read, false=unread)
        limit: Maximum results to return (default: 50)

    Returns:
        Dictionary containing matching messages

    Example:
        >>> search_messages("Gmail", sender_contains="john@example.com", read_status=False, limit=10)
        {"success": True, "messages": [...], "count": 5}
    """
    try:
        rate_err = check_rate_limit("search_messages", {"account": account, "mailbox": mailbox})
        if rate_err:
            return rate_err

        logger.info(
            f"Searching messages in {account}/{mailbox} with filters: "
            f"sender={sender_contains}, subject={subject_contains}, read={read_status}"
        )

        messages = mail.search_messages(
            account=account,
            mailbox=mailbox,
            sender_contains=sender_contains,
            subject_contains=subject_contains,
            read_status=read_status,
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
    except Exception as e:
        logger.error(f"Error searching messages: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }


@mcp.tool()
def get_message(message_id: str, include_content: bool = True) -> dict[str, Any]:
    """
    Get full details of a specific message.

    Args:
        message_id: Message ID from search results
        include_content: Include message body (default: true)

    Returns:
        Dictionary containing message details

    Example:
        >>> get_message("12345")
        {"success": True, "message": {...}}
    """
    try:
        rate_err = check_rate_limit("get_message", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Getting message: {message_id}")

        message = mail.get_message(message_id, include_content=include_content)

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
def send_email(
    subject: str,
    body: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send an email via Apple Mail.

    IMPORTANT: This operation requires user confirmation before sending.

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

        # Require confirmation
        confirmation_details = {
            "subject": subject,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "body_preview": body[:100] + "..." if len(body) > 100 else body,
        }

        logger.info(f"Requesting confirmation to send email: {subject}")
        logger.info(f"Recipients: {to}, CC: {cc}, BCC: {bcc}")

        # In production, this should actually block and wait for user confirmation
        # For now, we'll proceed but log the confirmation requirement
        if not require_confirmation("send_email", confirmation_details):
            operation_logger.log_operation(
                "send_email",
                confirmation_details,
                "cancelled"
            )
            return {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }

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
def mark_as_read(message_ids: list[str], read: bool = True) -> dict[str, Any]:
    """
    Mark messages as read or unread.

    Args:
        message_ids: List of message IDs to update
        read: True to mark as read, False to mark as unread (default: true)

    Returns:
        Dictionary indicating success and number of messages updated

    Example:
        >>> mark_as_read(["12345", "12346"], read=True)
        {"success": True, "updated": 2}
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

        count = mail.mark_as_read(message_ids, read=read)

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
def send_email_with_attachments(
    subject: str,
    body: str,
    to: list[str],
    attachments: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send an email with file attachments via Apple Mail.

    IMPORTANT: This operation requires user confirmation before sending.

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

        # Require confirmation
        confirmation_details = {
            "subject": subject,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "attachments": [p.name for p in attachment_paths],
            "body_preview": body[:100] + "..." if len(body) > 100 else body,
        }

        logger.info(f"Requesting confirmation to send email with attachments: {subject}")
        logger.info(f"Recipients: {to}, Attachments: {len(attachments)}")

        if not require_confirmation("send_email_with_attachments", confirmation_details):
            operation_logger.log_operation(
                "send_email_with_attachments",
                confirmation_details,
                "cancelled"
            )
            return {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }

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
def get_attachments(message_id: str) -> dict[str, Any]:
    """
    Get list of attachments from a message.

    Args:
        message_id: Message ID from search results

    Returns:
        Dictionary with list of attachments

    Example:
        >>> get_attachments("12345")
        {
            "success": True,
            "attachments": [
                {
                    "name": "report.pdf",
                    "mime_type": "application/pdf",
                    "size": 524288,
                    "downloaded": True
                }
            ],
            "count": 1
        }
    """
    try:
        rate_err = check_rate_limit("get_attachments", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Getting attachments for message: {message_id}")

        attachments = mail.get_attachments(message_id)

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
) -> dict[str, Any]:
    """
    Move messages to a different mailbox/folder.

    Args:
        message_ids: List of message IDs to move
        destination_mailbox: Name of destination mailbox (use "/" for nested: "Projects/Client Work")
        account: Account name containing the messages
        gmail_mode: Use Gmail-specific move handling (copy + delete) for label-based systems

    Returns:
        Dictionary with success status and number of messages moved

    Example:
        move_messages(
            message_ids=["12345", "12346"],
            destination_mailbox="Archive",
            account="Gmail"
        )
    """
    try:
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
) -> dict[str, Any]:
    """
    Set flag color on messages.

    Args:
        message_ids: List of message IDs to flag
        flag_color: Flag color name (none, orange, red, yellow, blue, green, purple, gray)

    Returns:
        Dictionary with success status and number of messages flagged

    Example:
        flag_message(
            message_ids=["12345"],
            flag_color="red"
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
        account: Account name to create mailbox in
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
) -> dict[str, Any]:
    """
    Delete messages (move to trash or permanently delete).

    Args:
        message_ids: List of message IDs to delete
        permanent: If True, permanently delete; if False, move to Trash (default: False)

    Returns:
        Dictionary with success status and number of messages deleted

    Example:
        delete_messages(
            message_ids=["12345"],
            permanent=False  # Move to trash
        )

    Note:
        Bulk deletions are limited to 100 messages for safety.
        Permanent deletion cannot be undone - use with caution.
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

        delete_type = "permanently" if permanent else "to trash"
        logger.info(f"Deleting {len(message_ids)} message(s) {delete_type}")

        # Delete the messages
        count = mail.delete_messages(
            message_ids=message_ids,
            permanent=permanent,
            skip_bulk_check=False,  # Enforce limit
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
def forward_message(
    message_id: str,
    to: list[str],
    body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict[str, Any]:
    """
    Forward a message to recipients.

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

        rate_err = check_rate_limit("forward_message", {"message_id": message_id, "to": to})
        if rate_err:
            return rate_err

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


def main() -> None:
    """Run the MCP server."""
    logger.info("Starting Apple Mail MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
