"""
CLI interface for Apple Mail MCP functions.

This module provides command-line access to all Apple Mail MCP operations.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from .mail_connector import AppleMailConnector

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def output_result(result: dict, format_type: str = "json") -> None:
    """Output result in the specified format."""
    if format_type == "json":
        click.echo(json.dumps(result, indent=2, default=str))
    elif format_type == "pretty":
        for key, value in result.items():
            if isinstance(value, list):
                click.echo(f"{key}:")
                for item in value:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            click.echo(f"  {k}: {v}")
                        click.echo("  ---")
                    else:
                        click.echo(f"  - {item}")
            else:
                click.echo(f"{key}: {value}")


def handle_error(error: Exception, error_type: str = "unknown") -> dict:
    """Create a standardized error response."""
    return {
        "success": False,
        "error": str(error),
        "error_type": error_type,
    }


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "pretty"]),
    default="json",
    help="Output format (default: json)",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, output_format: str) -> None:
    """Apple Mail CLI - Command line interface for Apple Mail operations.

    This tool provides direct access to Apple Mail functionality through
    the command line, mirroring all MCP server capabilities.
    """
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["format"] = output_format
    ctx.obj["mail"] = AppleMailConnector()

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


@cli.command("list-accounts")
@click.pass_context
def list_accounts(ctx: click.Context) -> None:
    """List all configured mail accounts.

    Example:
        apple-mail list-accounts
    """
    mail = ctx.obj["mail"]
    try:
        accounts = mail.list_accounts()
        result = {
            "success": True,
            "accounts": accounts,
            "count": len(accounts),
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("list-mailboxes")
@click.argument("account")
@click.pass_context
def list_mailboxes(ctx: click.Context, account: str) -> None:
    """List all mailboxes for an account.

    ACCOUNT: Account name (e.g., "Gmail", "iCloud")

    Example:
        apple-mail list-mailboxes Gmail
    """
    mail = ctx.obj["mail"]
    try:
        mailboxes = mail.list_mailboxes(account)
        result = {
            "success": True,
            "account": account,
            "mailboxes": mailboxes,
        }
    except MailAccountNotFoundError as e:
        result = {
            "success": False,
            "error": f"Account '{account}' not found",
            "error_type": "account_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("search")
@click.argument("account")
@click.option("-m", "--mailbox", default="INBOX", help="Mailbox name (default: INBOX)")
@click.option("-s", "--sender", help="Filter by sender email/domain")
@click.option("-j", "--subject", help="Filter by subject keywords")
@click.option(
    "-r",
    "--read-status",
    type=click.Choice(["read", "unread", "any"]),
    default="any",
    help="Filter by read status",
)
@click.option("-l", "--limit", type=int, default=50, help="Maximum results (default: 50)")
@click.pass_context
def search_messages(
    ctx: click.Context,
    account: str,
    mailbox: str,
    sender: Optional[str],
    subject: Optional[str],
    read_status: str,
    limit: int,
) -> None:
    """Search for messages matching criteria.

    ACCOUNT: Account name (e.g., "Gmail", "iCloud")

    Examples:
        apple-mail search Gmail --sender "john@example.com"
        apple-mail search Gmail -m INBOX --read-status unread --limit 10
        apple-mail search iCloud --subject "Meeting"
    """
    mail = ctx.obj["mail"]

    read_bool: Optional[bool] = None
    if read_status == "read":
        read_bool = True
    elif read_status == "unread":
        read_bool = False

    try:
        messages = mail.search_messages(
            account=account,
            mailbox=mailbox,
            sender_contains=sender,
            subject_contains=subject,
            read_status=read_bool,
            limit=limit,
        )
        result = {
            "success": True,
            "account": account,
            "mailbox": mailbox,
            "messages": messages,
            "count": len(messages),
        }
    except (MailAccountNotFoundError, MailMailboxNotFoundError) as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("unread")
@click.argument("account")
@click.option("-m", "--mailbox", default="INBOX", help="Mailbox name (default: INBOX)")
@click.option("-l", "--limit", type=int, default=20, help="Maximum results (default: 20)")
@click.pass_context
def get_unread(ctx: click.Context, account: str, mailbox: str, limit: int) -> None:
    """Get the most recent unread messages from a mailbox.

    ACCOUNT: Account name (e.g., "Gmail", "iCloud")

    This is a convenience command equivalent to:
        apple-mail search ACCOUNT -m MAILBOX --read-status unread --limit LIMIT

    Examples:
        apple-mail unread Gmail
        apple-mail unread iCloud -m "All Mail" --limit 50
    """
    mail = ctx.obj["mail"]
    try:
        messages = mail.search_messages(
            account=account,
            mailbox=mailbox,
            read_status=False,
            limit=limit,
        )
        result = {
            "success": True,
            "account": account,
            "mailbox": mailbox,
            "messages": messages,
            "count": len(messages),
        }
    except (MailAccountNotFoundError, MailMailboxNotFoundError) as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("get")
@click.argument("message_id")
@click.option("--no-content", is_flag=True, help="Exclude message body")
@click.pass_context
def get_message(ctx: click.Context, message_id: str, no_content: bool) -> None:
    """Get full details of a specific message.

    MESSAGE_ID: Message ID from search results

    Example:
        apple-mail get 12345
        apple-mail get 12345 --no-content
    """
    mail = ctx.obj["mail"]
    try:
        message = mail.get_message(message_id, include_content=not no_content)
        result = {
            "success": True,
            "message": message,
        }
    except MailMessageNotFoundError:
        result = {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("send")
@click.option("-s", "--subject", required=True, help="Email subject")
@click.option("-b", "--body", required=True, help="Email body")
@click.option("-t", "--to", "recipients", required=True, multiple=True, help="Recipient email(s)")
@click.option("-c", "--cc", multiple=True, help="CC recipient(s)")
@click.option("--bcc", multiple=True, help="BCC recipient(s)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def send_email(
    ctx: click.Context,
    subject: str,
    body: str,
    recipients: tuple,
    cc: tuple,
    bcc: tuple,
    yes: bool,
) -> None:
    """Send an email via Apple Mail.

    Examples:
        apple-mail send -s "Hello" -b "Hi there!" -t alice@example.com
        apple-mail send -s "Meeting" -b "Let's meet" -t bob@example.com -c carol@example.com
    """
    mail = ctx.obj["mail"]
    to_list = list(recipients)
    cc_list = list(cc) if cc else None
    bcc_list = list(bcc) if bcc else None

    # Show confirmation unless --yes flag is set
    if not yes:
        click.echo("Email Details:")
        click.echo(f"  Subject: {subject}")
        click.echo(f"  To: {', '.join(to_list)}")
        if cc_list:
            click.echo(f"  CC: {', '.join(cc_list)}")
        if bcc_list:
            click.echo(f"  BCC: {', '.join(bcc_list)}")
        click.echo(f"  Body: {body[:100]}{'...' if len(body) > 100 else ''}")
        click.echo()

        if not click.confirm("Send this email?"):
            result = {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }
            output_result(result, ctx.obj["format"])
            sys.exit(1)

    try:
        mail.send_email(
            subject=subject,
            body=body,
            to=to_list,
            cc=cc_list,
            bcc=bcc_list,
        )
        result = {
            "success": True,
            "message": "Email sent successfully",
            "details": {
                "subject": subject,
                "recipients": len(to_list) + len(cc_list or []) + len(bcc_list or []),
            },
        }
    except MailAppleScriptError as e:
        result = {
            "success": False,
            "error": f"Failed to send email: {str(e)}",
            "error_type": "send_error",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("mark-read")
@click.argument("message_ids", nargs=-1, required=True)
@click.option("--unread", is_flag=True, help="Mark as unread instead")
@click.pass_context
def mark_as_read(ctx: click.Context, message_ids: tuple, unread: bool) -> None:
    """Mark messages as read or unread.

    MESSAGE_IDS: One or more message IDs

    Examples:
        apple-mail mark-read 12345 12346
        apple-mail mark-read 12345 --unread
    """
    mail = ctx.obj["mail"]
    ids = list(message_ids)

    if len(ids) > 100:
        result = {
            "success": False,
            "error": f"Cannot process {len(ids)} messages at once (max: 100)",
            "error_type": "validation_error",
        }
        output_result(result, ctx.obj["format"])
        sys.exit(1)

    try:
        count = mail.mark_as_read(ids, read=not unread)
        result = {
            "success": True,
            "updated": count,
            "requested": len(ids),
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("send-with-attachments")
@click.option("-s", "--subject", required=True, help="Email subject")
@click.option("-b", "--body", required=True, help="Email body")
@click.option("-t", "--to", "recipients", required=True, multiple=True, help="Recipient email(s)")
@click.option("-a", "--attachment", "attachments", required=True, multiple=True, help="File path(s) to attach")
@click.option("-c", "--cc", multiple=True, help="CC recipient(s)")
@click.option("--bcc", multiple=True, help="BCC recipient(s)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def send_email_with_attachments(
    ctx: click.Context,
    subject: str,
    body: str,
    recipients: tuple,
    attachments: tuple,
    cc: tuple,
    bcc: tuple,
    yes: bool,
) -> None:
    """Send an email with file attachments.

    Examples:
        apple-mail send-with-attachments -s "Report" -b "See attached" -t bob@example.com -a report.pdf
        apple-mail send-with-attachments -s "Files" -b "Here are the files" -t alice@example.com -a file1.pdf -a file2.docx
    """
    mail = ctx.obj["mail"]
    to_list = list(recipients)
    attachment_paths = [Path(p) for p in attachments]
    cc_list = list(cc) if cc else None
    bcc_list = list(bcc) if bcc else None

    # Validate attachments exist
    missing_files = [str(p) for p in attachment_paths if not p.exists()]
    if missing_files:
        result = {
            "success": False,
            "error": f"Attachment files not found: {', '.join(missing_files)}",
            "error_type": "file_not_found",
        }
        output_result(result, ctx.obj["format"])
        sys.exit(1)

    # Show confirmation unless --yes flag is set
    if not yes:
        click.echo("Email Details:")
        click.echo(f"  Subject: {subject}")
        click.echo(f"  To: {', '.join(to_list)}")
        if cc_list:
            click.echo(f"  CC: {', '.join(cc_list)}")
        if bcc_list:
            click.echo(f"  BCC: {', '.join(bcc_list)}")
        click.echo(f"  Attachments: {', '.join(p.name for p in attachment_paths)}")
        click.echo(f"  Body: {body[:100]}{'...' if len(body) > 100 else ''}")
        click.echo()

        if not click.confirm("Send this email?"):
            result = {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }
            output_result(result, ctx.obj["format"])
            sys.exit(1)

    try:
        mail.send_email_with_attachments(
            subject=subject,
            body=body,
            to=to_list,
            attachments=attachment_paths,
            cc=cc_list,
            bcc=bcc_list,
        )
        result = {
            "success": True,
            "message": f"Email sent with {len(attachments)} attachment(s)",
            "details": {
                "subject": subject,
                "recipients": len(to_list) + len(cc_list or []) + len(bcc_list or []),
                "attachments": len(attachments),
            },
        }
    except (FileNotFoundError, ValueError) as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailAppleScriptError as e:
        result = {
            "success": False,
            "error": f"Failed to send email: {str(e)}",
            "error_type": "send_error",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("get-attachments")
@click.argument("message_id")
@click.pass_context
def get_attachments(ctx: click.Context, message_id: str) -> None:
    """Get list of attachments from a message.

    MESSAGE_ID: Message ID from search results

    Example:
        apple-mail get-attachments 12345
    """
    mail = ctx.obj["mail"]
    try:
        attachments = mail.get_attachments(message_id)
        result = {
            "success": True,
            "attachments": attachments,
            "count": len(attachments),
        }
    except MailMessageNotFoundError:
        result = {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("save-attachments")
@click.argument("message_id")
@click.argument("save_directory")
@click.option("-i", "--index", "indices", multiple=True, type=int, help="Specific attachment indices (0-based)")
@click.pass_context
def save_attachments(
    ctx: click.Context,
    message_id: str,
    save_directory: str,
    indices: tuple,
) -> None:
    """Save attachments from a message to a directory.

    MESSAGE_ID: Message ID from search results
    SAVE_DIRECTORY: Directory path to save attachments to

    Examples:
        apple-mail save-attachments 12345 ~/Downloads
        apple-mail save-attachments 12345 ~/Downloads -i 0 -i 2
    """
    mail = ctx.obj["mail"]
    save_path = Path(save_directory)

    if not save_path.exists():
        result = {
            "success": False,
            "error": f"Directory does not exist: {save_directory}",
            "error_type": "directory_not_found",
        }
        output_result(result, ctx.obj["format"])
        sys.exit(1)

    if not save_path.is_dir():
        result = {
            "success": False,
            "error": f"Path is not a directory: {save_directory}",
            "error_type": "invalid_directory",
        }
        output_result(result, ctx.obj["format"])
        sys.exit(1)

    attachment_indices = list(indices) if indices else None

    try:
        count = mail.save_attachments(
            message_id=message_id,
            save_directory=save_path,
            attachment_indices=attachment_indices,
        )
        result = {
            "success": True,
            "saved": count,
            "directory": save_directory,
        }
    except (FileNotFoundError, ValueError) as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError:
        result = {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("move")
@click.argument("message_ids", nargs=-1, required=True)
@click.option("-d", "--destination", required=True, help="Destination mailbox name")
@click.option("-a", "--account", required=True, help="Account name")
@click.option("--gmail", is_flag=True, help="Use Gmail-specific move handling")
@click.pass_context
def move_messages(
    ctx: click.Context,
    message_ids: tuple,
    destination: str,
    account: str,
    gmail: bool,
) -> None:
    """Move messages to a different mailbox/folder.

    MESSAGE_IDS: One or more message IDs

    Examples:
        apple-mail move 12345 12346 -d Archive -a Gmail
        apple-mail move 12345 -d "Projects/Client Work" -a iCloud
    """
    mail = ctx.obj["mail"]
    ids = list(message_ids)

    try:
        count = mail.move_messages(
            message_ids=ids,
            destination_mailbox=destination,
            account=account,
            gmail_mode=gmail,
        )
        result = {
            "success": True,
            "count": count,
            "destination": destination,
            "account": account,
        }
    except MailMailboxNotFoundError:
        result = {
            "success": False,
            "error": f"Mailbox '{destination}' not found in account '{account}'",
            "error_type": "mailbox_not_found",
        }
    except MailAccountNotFoundError:
        result = {
            "success": False,
            "error": f"Account '{account}' not found",
            "error_type": "account_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("flag")
@click.argument("message_ids", nargs=-1, required=True)
@click.option(
    "-c",
    "--color",
    required=True,
    type=click.Choice(["none", "orange", "red", "yellow", "blue", "green", "purple", "gray"]),
    help="Flag color",
)
@click.pass_context
def flag_message(ctx: click.Context, message_ids: tuple, color: str) -> None:
    """Set flag color on messages.

    MESSAGE_IDS: One or more message IDs

    Examples:
        apple-mail flag 12345 -c red
        apple-mail flag 12345 12346 -c none
    """
    mail = ctx.obj["mail"]
    ids = list(message_ids)

    try:
        count = mail.flag_message(message_ids=ids, flag_color=color)
        result = {
            "success": True,
            "count": count,
            "flag_color": color,
        }
    except ValueError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("create-mailbox")
@click.argument("account")
@click.argument("name")
@click.option("-p", "--parent", help="Parent mailbox for nesting")
@click.pass_context
def create_mailbox(ctx: click.Context, account: str, name: str, parent: Optional[str]) -> None:
    """Create a new mailbox/folder.

    ACCOUNT: Account name
    NAME: Name of the new mailbox

    Examples:
        apple-mail create-mailbox Gmail "Client Work"
        apple-mail create-mailbox iCloud "Work Projects" -p Projects
    """
    mail = ctx.obj["mail"]

    if not name or not name.strip():
        result = {
            "success": False,
            "error": "Mailbox name cannot be empty",
            "error_type": "validation_error",
        }
        output_result(result, ctx.obj["format"])
        sys.exit(1)

    try:
        success = mail.create_mailbox(account=account, name=name, parent_mailbox=parent)
        result = {
            "success": success,
            "account": account,
            "mailbox": name,
            "parent": parent,
        }
    except ValueError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailAccountNotFoundError:
        result = {
            "success": False,
            "error": f"Account '{account}' not found",
            "error_type": "account_not_found",
        }
    except MailAppleScriptError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "applescript_error",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("delete")
@click.argument("message_ids", nargs=-1, required=True)
@click.option("--permanent", is_flag=True, help="Permanently delete (bypass trash)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def delete_messages(
    ctx: click.Context,
    message_ids: tuple,
    permanent: bool,
    yes: bool,
) -> None:
    """Delete messages (move to trash or permanently delete).

    MESSAGE_IDS: One or more message IDs

    Examples:
        apple-mail delete 12345
        apple-mail delete 12345 12346 --permanent -y
    """
    mail = ctx.obj["mail"]
    ids = list(message_ids)

    if len(ids) > 100:
        result = {
            "success": False,
            "error": f"Cannot delete {len(ids)} messages at once (max: 100)",
            "error_type": "validation_error",
        }
        output_result(result, ctx.obj["format"])
        sys.exit(1)

    # Confirm permanent deletion
    if permanent and not yes:
        click.echo(f"WARNING: This will permanently delete {len(ids)} message(s)!")
        click.echo("This action cannot be undone.")
        if not click.confirm("Are you sure?"):
            result = {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }
            output_result(result, ctx.obj["format"])
            sys.exit(1)

    try:
        count = mail.delete_messages(
            message_ids=ids,
            permanent=permanent,
            skip_bulk_check=False,
        )
        result = {
            "success": True,
            "count": count,
            "permanent": permanent,
        }
    except ValueError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("reply")
@click.argument("message_id")
@click.option("-b", "--body", required=True, help="Reply body text")
@click.option("--all", "reply_all", is_flag=True, help="Reply to all recipients")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def reply_to_message(
    ctx: click.Context,
    message_id: str,
    body: str,
    reply_all: bool,
    yes: bool,
) -> None:
    """Reply to a message.

    MESSAGE_ID: ID of the message to reply to

    Examples:
        apple-mail reply 12345 -b "Thanks for your email!"
        apple-mail reply 12345 -b "Great point, team!" --all
    """
    mail = ctx.obj["mail"]

    if not yes:
        click.echo("Reply Details:")
        click.echo(f"  Original Message ID: {message_id}")
        click.echo(f"  Reply All: {reply_all}")
        click.echo(f"  Body: {body[:100]}{'...' if len(body) > 100 else ''}")
        click.echo()

        if not click.confirm("Send this reply?"):
            result = {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }
            output_result(result, ctx.obj["format"])
            sys.exit(1)

    try:
        reply_id = mail.reply_to_message(
            message_id=message_id,
            body=body,
            reply_all=reply_all,
        )
        result = {
            "success": True,
            "reply_id": reply_id,
            "original_message_id": message_id,
            "reply_all": reply_all,
        }
    except MailMessageNotFoundError:
        result = {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


@cli.command("forward")
@click.argument("message_id")
@click.option("-t", "--to", "recipients", required=True, multiple=True, help="Recipient email(s)")
@click.option("-b", "--body", default="", help="Body text to add before forwarded content")
@click.option("-c", "--cc", multiple=True, help="CC recipient(s)")
@click.option("--bcc", multiple=True, help="BCC recipient(s)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def forward_message(
    ctx: click.Context,
    message_id: str,
    recipients: tuple,
    body: str,
    cc: tuple,
    bcc: tuple,
    yes: bool,
) -> None:
    """Forward a message to recipients.

    MESSAGE_ID: ID of the message to forward

    Examples:
        apple-mail forward 12345 -t colleague@example.com
        apple-mail forward 12345 -t bob@example.com -b "FYI - see below"
    """
    mail = ctx.obj["mail"]
    to_list = list(recipients)
    cc_list = list(cc) if cc else None
    bcc_list = list(bcc) if bcc else None

    if not yes:
        click.echo("Forward Details:")
        click.echo(f"  Original Message ID: {message_id}")
        click.echo(f"  To: {', '.join(to_list)}")
        if cc_list:
            click.echo(f"  CC: {', '.join(cc_list)}")
        if bcc_list:
            click.echo(f"  BCC: {', '.join(bcc_list)}")
        if body:
            click.echo(f"  Body: {body[:100]}{'...' if len(body) > 100 else ''}")
        click.echo()

        if not click.confirm("Send this forward?"):
            result = {
                "success": False,
                "error": "User cancelled operation",
                "error_type": "cancelled",
            }
            output_result(result, ctx.obj["format"])
            sys.exit(1)

    try:
        forward_id = mail.forward_message(
            message_id=message_id,
            to=to_list,
            body=body,
            cc=cc_list,
            bcc=bcc_list,
        )
        result = {
            "success": True,
            "forward_id": forward_id,
            "original_message_id": message_id,
            "recipients": to_list,
            "cc": cc_list,
            "bcc": bcc_list,
        }
    except ValueError as e:
        result = {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
        }
    except MailMessageNotFoundError:
        result = {
            "success": False,
            "error": f"Message '{message_id}' not found",
            "error_type": "message_not_found",
        }
    except Exception as e:
        result = handle_error(e)

    output_result(result, ctx.obj["format"])
    sys.exit(0 if result.get("success") else 1)


def main() -> None:
    """Entry point for the CLI."""
    cli(obj={})


if __name__ == "__main__":
    main()

