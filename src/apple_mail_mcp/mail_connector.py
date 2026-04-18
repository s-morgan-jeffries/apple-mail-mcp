"""
AppleScript-based connector for Apple Mail.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any, cast

from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from .utils import escape_applescript_string, parse_applescript_json, sanitize_input

logger = logging.getLogger(__name__)


def _wrap_as_json_script(body: str) -> str:
    """Wrap a tell-block body with ASObjC imports and an NSJSONSerialization return.

    The `body` must:
      - Contain a `tell application "Mail" ... end tell` block.
      - Assign the final result to an AppleScript variable named `resultData`
        inside that tell block.
      - Handle failures EITHER by letting AppleScript errors propagate via
        stderr (preserves _run_applescript's typed exception mapping, e.g.,
        MailAccountNotFoundError) OR by catching them in a try block and
        returning "ERROR: <message>" (surfaces as MailAppleScriptError on
        the Python side). Use the stderr path when the caller relies on
        typed exceptions; use the "ERROR:" path otherwise.

    The wrapper:
      - Prepends `use framework "Foundation"` and `use scripting additions`.
      - After the tell block, serializes `resultData` via NSJSONSerialization
        and returns the resulting NSString as text.

    Args:
        body: AppleScript tell-block source setting `resultData`.

    Returns:
        Full AppleScript source ready for osascript.
    """
    return (
        'use framework "Foundation"\n'
        "use scripting additions\n"
        "\n"
        f"{body}\n"
        "\n"
        "set jsonData to (current application's NSJSONSerialization's "
        "dataWithJSONObject:resultData options:0 |error|:(missing value))\n"
        "return (current application's NSString's alloc()'s "
        "initWithData:jsonData encoding:4) as text\n"
    )


class AppleMailConnector:
    """Interface to Apple Mail via AppleScript."""

    def __init__(self, timeout: int = 60) -> None:
        """
        Initialize the Mail connector.

        Args:
            timeout: Timeout in seconds for AppleScript operations
        """
        self.timeout = timeout

    def _run_applescript(self, script: str) -> str:
        """
        Execute AppleScript and return output.

        Args:
            script: AppleScript code to execute

        Returns:
            Script output as string

        Raises:
            MailAppleScriptError: If script execution fails
            MailAccountNotFoundError: If account not found
            MailMailboxNotFoundError: If mailbox not found
            MailMessageNotFoundError: If message not found
        """
        try:
            logger.debug(f"Executing AppleScript: {script[:200]}...")

            result = subprocess.run(
                ["/usr/bin/osascript", "-"],
                input=script,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                logger.error(f"AppleScript error: {error_msg}")

                # Parse error and raise appropriate exception
                if "Can't get account" in error_msg:
                    raise MailAccountNotFoundError(error_msg)
                elif "Can't get mailbox" in error_msg:
                    raise MailMailboxNotFoundError(error_msg)
                elif "Can't get message" in error_msg:
                    raise MailMessageNotFoundError(error_msg)
                else:
                    raise MailAppleScriptError(error_msg)

            output = result.stdout.strip()
            logger.debug(f"AppleScript output: {output[:200]}...")
            return output

        except subprocess.TimeoutExpired as e:
            raise MailAppleScriptError(f"Script execution timeout after {self.timeout}s") from e
        except Exception as e:
            if isinstance(e, (MailAccountNotFoundError, MailMailboxNotFoundError,
                            MailMessageNotFoundError, MailAppleScriptError)):
                raise
            raise MailAppleScriptError(f"Unexpected error: {str(e)}") from e

    def list_accounts(self) -> list[dict[str, Any]]:
        """List all mail accounts.

        Returns:
            List of account dicts with keys:
              - name: account display name
              - email_addresses: list of associated email addresses
        """
        tell_body = """
        tell application "Mail"
            set resultData to {}
            repeat with acc in accounts
                set accEmails to email addresses of acc
                if accEmails is missing value then set accEmails to {}
                set accRecord to {|name|:(name of acc), email_addresses:accEmails}
                set end of resultData to accRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def list_mailboxes(self, account: str) -> list[dict[str, Any]]:
        """List all mailboxes for an account.

        Args:
            account: Account name.

        Returns:
            List of dicts with keys: name, unread_count.

        Raises:
            MailAccountNotFoundError: If account doesn't exist.
        """
        account_safe = escape_applescript_string(sanitize_input(account))

        tell_body = f'''
        tell application "Mail"
            set accountRef to account "{account_safe}"
            set resultData to {{}}

            repeat with mb in mailboxes of accountRef
                set mbUnread to unread count of mb
                if mbUnread is missing value then set mbUnread to 0
                set mbRecord to {{|name|:(name of mb), unread_count:mbUnread}}
                set end of resultData to mbRecord
            end repeat
        end tell
        '''

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def search_messages(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search for messages matching criteria.

        Args:
            account: Account name
            mailbox: Mailbox name
            sender_contains: Filter by sender
            subject_contains: Filter by subject
            read_status: Filter by read status (True=read, False=unread)
            limit: Maximum results

        Returns:
            List of message dictionaries

        Raises:
            MailAccountNotFoundError: If account doesn't exist
            MailMailboxNotFoundError: If mailbox doesn't exist
        """
        account_safe = escape_applescript_string(sanitize_input(account))
        mailbox_safe = escape_applescript_string(sanitize_input(mailbox))

        # Build whose clause
        conditions = []
        if sender_contains:
            sender_safe = escape_applescript_string(sanitize_input(sender_contains))
            conditions.append(f'sender contains "{sender_safe}"')

        if subject_contains:
            subject_safe = escape_applescript_string(sanitize_input(subject_contains))
            conditions.append(f'subject contains "{subject_safe}"')

        if read_status is not None:
            status = "true" if read_status else "false"
            conditions.append(f"read status is {status}")

        whose_clause = " and ".join(conditions) if conditions else "true"
        limit_clause = f"items 1 thru {limit} of" if limit else ""

        tell_body = f'''
        tell application "Mail"
            set accountRef to account "{account_safe}"
            set mailboxRef to mailbox "{mailbox_safe}" of accountRef
            set matchedMessages to {limit_clause} (messages of mailboxRef whose {whose_clause})

            set resultData to {{}}
            repeat with msg in matchedMessages
                set msgRecord to {{id:(id of msg as text), subject:(subject of msg), sender:(sender of msg), date_received:(date received of msg as text), read_status:(read status of msg)}}
                set end of resultData to msgRecord
            end repeat
        end tell
        '''

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def get_message(self, message_id: str, include_content: bool = True) -> dict[str, Any]:
        """
        Get full message details.

        Args:
            message_id: Message ID
            include_content: Include message body

        Returns:
            Message dictionary

        Raises:
            MailMessageNotFoundError: If message doesn't exist
        """
        message_id_safe = escape_applescript_string(sanitize_input(message_id))

        # Note: Direct message ID lookup is tricky in AppleScript
        # We need to search through mailboxes
        # For now, we'll use a simplified approach

        content_clause = (
            'set msgContent to content of msg'
            if include_content
            else 'set msgContent to ""'
        )

        tell_body = f'''
        tell application "Mail"
            set resultData to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is {message_id_safe}
                        {content_clause}

                        set resultData to {{id:(id of msg as text), subject:(subject of msg), sender:(sender of msg), date_received:(date received of msg as text), read_status:(read status of msg), flagged:(flagged status of msg), content:msgContent}}
                        exit repeat
                    end try
                end repeat
                if resultData is not missing value then exit repeat
            end repeat

            if resultData is missing value then
                error "Can't get message: not found"
            end if
        end tell
        '''

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(dict[str, Any], parse_applescript_json(result))

    def send_email(
        self,
        subject: str,
        body: str,
        to: list[str],
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> bool:
        """
        Send an email.

        Args:
            subject: Email subject
            body: Email body
            to: List of To recipients
            cc: List of CC recipients
            bcc: List of BCC recipients

        Returns:
            True if sent successfully

        Raises:
            MailAppleScriptError: If send fails
        """
        subject_safe = escape_applescript_string(sanitize_input(subject))
        body_safe = escape_applescript_string(sanitize_input(body))

        # Build recipient lists
        to_list = ", ".join(f'"{escape_applescript_string(addr)}"' for addr in to)
        cc_list = ", ".join(f'"{escape_applescript_string(addr)}"' for addr in (cc or []))
        bcc_list = ", ".join(f'"{escape_applescript_string(addr)}"' for addr in (bcc or []))

        script = f"""
        tell application "Mail"
            set theMessage to make new outgoing message with properties {{subject:"{subject_safe}", content:"{body_safe}", visible:false}}

            tell theMessage
                -- Add To recipients
                repeat with addr in {{{to_list}}}
                    make new to recipient with properties {{address:addr}}
                end repeat

                -- Add CC recipients
                repeat with addr in {{{cc_list}}}
                    make new cc recipient with properties {{address:addr}}
                end repeat

                -- Add BCC recipients
                repeat with addr in {{{bcc_list}}}
                    make new bcc recipient with properties {{address:addr}}
                end repeat

                send
            end tell

            return "sent"
        end tell
        """

        result = self._run_applescript(script)
        return result == "sent"

    def mark_as_read(self, message_ids: list[str], read: bool = True) -> int:
        """
        Mark messages as read or unread.

        Args:
            message_ids: List of message IDs
            read: True for read, False for unread

        Returns:
            Number of messages updated

        Raises:
            MailAppleScriptError: If operation fails
        """
        if not message_ids:
            return 0

        status = "true" if read else "false"

        # Build list of IDs
        id_list = ", ".join(message_ids)

        script = f"""
        tell application "Mail"
            set idList to {{{id_list}}}
            set updateCount to 0

            repeat with msgId in idList
                repeat with acc in accounts
                    repeat with mb in mailboxes of acc
                        try
                            set msg to first message of mb whose id is msgId
                            set read status of msg to {status}
                            set updateCount to updateCount + 1
                        end try
                    end repeat
                end repeat
            end repeat

            return updateCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def send_email_with_attachments(
        self,
        subject: str,
        body: str,
        to: list[str],
        attachments: list[Path],
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        max_attachment_size: int = 25 * 1024 * 1024,
    ) -> bool:
        """
        Send an email with file attachments.

        Args:
            subject: Email subject
            body: Email body
            to: List of To recipients
            attachments: List of file paths to attach
            cc: List of CC recipients
            bcc: List of BCC recipients
            max_attachment_size: Maximum size per attachment in bytes

        Returns:
            True if sent successfully

        Raises:
            FileNotFoundError: If attachment file doesn't exist
            ValueError: If attachment exceeds size limit
            MailAppleScriptError: If send fails
        """
        from .security import validate_attachment_size, validate_attachment_type

        # Validate all attachments exist and are within size limit
        for attachment_path in attachments:
            if not attachment_path.exists():
                raise FileNotFoundError(f"Attachment not found: {attachment_path}")

            if not attachment_path.is_file():
                raise ValueError(f"Attachment is not a file: {attachment_path}")

            file_size = attachment_path.stat().st_size
            if not validate_attachment_size(file_size, max_attachment_size):
                raise ValueError(
                    f"Attachment {attachment_path.name} exceeds size limit "
                    f"({file_size} bytes > {max_attachment_size} bytes)"
                )

            if not validate_attachment_type(attachment_path.name):
                raise ValueError(
                    f"Attachment type not allowed: {attachment_path.name}"
                )

        subject_safe = escape_applescript_string(sanitize_input(subject))
        body_safe = escape_applescript_string(sanitize_input(body))

        # Build recipient lists
        to_list = ", ".join(f'"{escape_applescript_string(addr)}"' for addr in to)
        cc_list = ", ".join(f'"{escape_applescript_string(addr)}"' for addr in (cc or []))
        bcc_list = ", ".join(f'"{escape_applescript_string(addr)}"' for addr in (bcc or []))

        # Build attachment list (convert to POSIX file references)
        attachment_list = ", ".join(
            f'POSIX file "{escape_applescript_string(str(path.absolute()))}"'
            for path in attachments
        )

        script = f"""
        tell application "Mail"
            set theMessage to make new outgoing message with properties {{subject:"{subject_safe}", content:"{body_safe}", visible:false}}

            tell theMessage
                -- Add To recipients
                repeat with addr in {{{to_list}}}
                    make new to recipient with properties {{address:addr}}
                end repeat

                -- Add CC recipients
                repeat with addr in {{{cc_list}}}
                    make new cc recipient with properties {{address:addr}}
                end repeat

                -- Add BCC recipients
                repeat with addr in {{{bcc_list}}}
                    make new bcc recipient with properties {{address:addr}}
                end repeat

                -- Add attachments
                repeat with filePath in {{{attachment_list}}}
                    make new attachment with properties {{file name:filePath}} at after last paragraph
                end repeat

                send
            end tell

            return "sent"
        end tell
        """

        result = self._run_applescript(script)
        return result == "sent"

    def get_attachments(self, message_id: str) -> list[dict[str, Any]]:
        """
        Get list of attachments from a message.

        Args:
            message_id: Message ID

        Returns:
            List of attachment dictionaries with name, mime_type, size, downloaded

        Raises:
            MailMessageNotFoundError: If message doesn't exist
        """
        message_id_safe = escape_applescript_string(sanitize_input(message_id))

        tell_body = f'''
        tell application "Mail"
            set resultData to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is {message_id_safe}
                        set attList to mail attachments of msg

                        set resultData to {{}}
                        repeat with att in attList
                            set attRecord to {{|name|:(name of att), mime_type:(MIME type of att), size:(file size of att), downloaded:(downloaded of att)}}
                            set end of resultData to attRecord
                        end repeat
                        exit repeat
                    end try
                end repeat
                if resultData is not missing value then exit repeat
            end repeat

            if resultData is missing value then
                error "Can't get message: not found"
            end if
        end tell
        '''

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def save_attachments(
        self,
        message_id: str,
        save_directory: Path,
        attachment_indices: list[int] | None = None,
    ) -> int:
        """
        Save attachments from a message to a directory.

        Args:
            message_id: Message ID
            save_directory: Directory to save attachments to
            attachment_indices: Indices of attachments to save (None = all)

        Returns:
            Number of attachments saved

        Raises:
            FileNotFoundError: If save directory doesn't exist
            ValueError: If path validation fails
            MailMessageNotFoundError: If message doesn't exist
        """
        # Validate save directory
        if not save_directory.exists():
            raise FileNotFoundError(f"Save directory does not exist: {save_directory}")

        if not save_directory.is_dir():
            raise ValueError(f"Save path is not a directory: {save_directory}")

        # Prevent path traversal
        try:
            save_directory = save_directory.resolve()
            # Check for suspicious paths
            if ".." in str(save_directory):
                raise ValueError("Path traversal detected")
        except (RuntimeError, OSError) as e:
            raise ValueError(f"Invalid save directory: {e}") from e

        message_id_safe = escape_applescript_string(sanitize_input(message_id))
        dir_safe = escape_applescript_string(str(save_directory))

        # Build index filter if specified
        if attachment_indices is not None:
            # Convert to 1-based indexing for AppleScript
            indices_str = ", ".join(str(i + 1) for i in attachment_indices)
            index_filter = f"items {{{indices_str}}} of"
        else:
            index_filter = ""

        script = f"""
        tell application "Mail"
            -- Search all accounts for message
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is {message_id_safe}
                        set attList to {index_filter} mail attachments of msg
                        set saveCount to 0

                        repeat with att in attList
                            try
                                set attName to name of att
                                save att in ("{dir_safe}/" & attName)
                                set saveCount to saveCount + 1
                            end try
                        end repeat

                        return saveCount
                    end try
                end repeat
            end repeat

            error "Message not found"
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def move_messages(
        self,
        message_ids: list[str],
        destination_mailbox: str,
        account: str,
        gmail_mode: bool = False,
    ) -> int:
        """
        Move messages to a different mailbox.

        Args:
            message_ids: List of message IDs to move
            destination_mailbox: Name of destination mailbox
            account: Account name
            gmail_mode: Use Gmail-specific handling (copy + delete)

        Returns:
            Number of messages moved

        Raises:
            MailAccountNotFoundError: If account doesn't exist
            MailMailboxNotFoundError: If destination mailbox doesn't exist
        """
        if not message_ids:
            return 0

        from .utils import sanitize_input

        account_safe = escape_applescript_string(sanitize_input(account))
        mailbox_safe = escape_applescript_string(sanitize_input(destination_mailbox))
        id_list = ", ".join(message_ids)

        if gmail_mode:
            # Gmail requires copy + delete approach to properly handle labels
            script = f"""
            tell application "Mail"
                set accountRef to account "{account_safe}"
                set destMailbox to mailbox "{mailbox_safe}" of accountRef
                set idList to {{{id_list}}}
                set moveCount to 0

                repeat with msgId in idList
                    repeat with acc in accounts
                        repeat with mb in mailboxes of acc
                            try
                                set msg to first message of mb whose id is msgId
                                duplicate msg to destMailbox
                                delete msg
                                set moveCount to moveCount + 1
                            end try
                        end repeat
                    end repeat
                end repeat

                return moveCount
            end tell
            """
        else:
            # Standard IMAP move
            script = f"""
            tell application "Mail"
                set accountRef to account "{account_safe}"
                set destMailbox to mailbox "{mailbox_safe}" of accountRef
                set idList to {{{id_list}}}
                set moveCount to 0

                repeat with msgId in idList
                    repeat with acc in accounts
                        repeat with mb in mailboxes of acc
                            try
                                set msg to first message of mb whose id is msgId
                                set mailbox of msg to destMailbox
                                set moveCount to moveCount + 1
                            end try
                        end repeat
                    end repeat
                end repeat

                return moveCount
            end tell
            """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def flag_message(
        self,
        message_ids: list[str],
        flag_color: str,
    ) -> int:
        """
        Set flag color on messages.

        Args:
            message_ids: List of message IDs to flag
            flag_color: Flag color (none, orange, red, yellow, blue, green, purple, gray)

        Returns:
            Number of messages flagged

        Raises:
            ValueError: If flag color is invalid
        """
        if not message_ids:
            return 0

        from .utils import get_flag_index, validate_flag_color

        if not validate_flag_color(flag_color):
            raise ValueError(f"Invalid flag color: {flag_color}")

        flag_index = get_flag_index(flag_color)
        flagged_status = "true" if flag_color != "none" else "false"
        id_list = ", ".join(message_ids)

        script = f"""
        tell application "Mail"
            set idList to {{{id_list}}}
            set flagCount to 0

            repeat with msgId in idList
                repeat with acc in accounts
                    repeat with mb in mailboxes of acc
                        try
                            set msg to first message of mb whose id is msgId
                            set flag index of msg to {flag_index}
                            set flagged status of msg to {flagged_status}
                            set flagCount to flagCount + 1
                        end try
                    end repeat
                end repeat
            end repeat

            return flagCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def create_mailbox(
        self,
        account: str,
        name: str,
        parent_mailbox: str | None = None,
    ) -> bool:
        """
        Create a new mailbox/folder.

        Args:
            account: Account name
            name: Name for new mailbox
            parent_mailbox: Parent mailbox for nested creation (optional)

        Returns:
            True if created successfully

        Raises:
            ValueError: If name is invalid
            MailAccountNotFoundError: If account doesn't exist
            MailAppleScriptError: If mailbox already exists
        """
        from .utils import sanitize_mailbox_name

        # Validate and sanitize name
        sanitized_name = sanitize_mailbox_name(name)
        if not sanitized_name:
            raise ValueError(f"Invalid mailbox name: {name}")

        account_safe = escape_applescript_string(sanitize_input(account))
        name_safe = escape_applescript_string(sanitized_name)

        if parent_mailbox:
            parent_safe = escape_applescript_string(sanitize_input(parent_mailbox))
            script = f"""
            tell application "Mail"
                set accountRef to account "{account_safe}"
                set parentMailbox to mailbox "{parent_safe}" of accountRef
                make new mailbox at parentMailbox with properties {{name:"{name_safe}"}}
                return "success"
            end tell
            """
        else:
            script = f"""
            tell application "Mail"
                set accountRef to account "{account_safe}"
                make new mailbox at accountRef with properties {{name:"{name_safe}"}}
                return "success"
            end tell
            """

        result = self._run_applescript(script)
        return result == "success"

    def delete_messages(
        self,
        message_ids: list[str],
        permanent: bool = False,
        skip_bulk_check: bool = True,
    ) -> int:
        """
        Delete messages (move to trash or permanent delete).

        Args:
            message_ids: List of message IDs to delete
            permanent: If True, permanently delete (bypass trash)
            skip_bulk_check: If False, enforce bulk operation limits

        Returns:
            Number of messages deleted

        Raises:
            ValueError: If bulk check fails
        """
        if not message_ids:
            return 0

        # Safety check for bulk operations
        if not skip_bulk_check and len(message_ids) > 100:
            raise ValueError(
                f"Too many messages for bulk delete ({len(message_ids)}). "
                "Maximum is 100 without skip_bulk_check=True"
            )

        id_list = ", ".join(message_ids)

        if permanent:
            # Permanent delete (not recommended, requires extra caution)
            script = f"""
            tell application "Mail"
                set idList to {{{id_list}}}
                set deleteCount to 0

                repeat with msgId in idList
                    repeat with acc in accounts
                        repeat with mb in mailboxes of acc
                            try
                                set msg to first message of mb whose id is msgId
                                delete msg
                                set deleteCount to deleteCount + 1
                            end try
                        end repeat
                    end repeat
                end repeat

                return deleteCount
            end tell
            """
        else:
            # Move to trash (standard delete)
            script = f"""
            tell application "Mail"
                set idList to {{{id_list}}}
                set deleteCount to 0

                repeat with msgId in idList
                    repeat with acc in accounts
                        repeat with mb in mailboxes of acc
                            try
                                set msg to first message of mb whose id is msgId
                                delete msg
                                set deleteCount to deleteCount + 1
                            end try
                        end repeat
                    end repeat
                end repeat

                return deleteCount
            end tell
            """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def reply_to_message(
        self,
        message_id: str,
        body: str,
        reply_all: bool = False,
        quote_original: bool = True,
    ) -> str:
        """
        Reply to a message.

        Args:
            message_id: ID of message to reply to
            body: Reply body text
            reply_all: If True, reply to all recipients; if False, reply only to sender
            quote_original: If True, include original message quoted

        Returns:
            Message ID of the reply

        Raises:
            MailMessageNotFoundError: If message doesn't exist
        """
        from .utils import sanitize_input

        body_safe = escape_applescript_string(sanitize_input(body))
        reply_type = "reply to all" if reply_all else "reply"

        # Apple Mail's reply command automatically handles quoting if opened in editor
        # We'll create a reply and set its content
        script = f"""
        tell application "Mail"
            set idList to {{"{message_id}"}}

            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set origMsg to first message of mb whose id is "{message_id}"

                        -- Create reply message
                        set replyMsg to {reply_type} origMsg

                        -- Set body content
                        set content of replyMsg to "{body_safe}"

                        -- Get the message ID
                        set replyId to id of replyMsg

                        -- Send the message
                        send replyMsg

                        return replyId
                    end try
                end repeat
            end repeat

            error "Message not found"
        end tell
        """

        result = self._run_applescript(script)
        return result

    def forward_message(
        self,
        message_id: str,
        to: list[str],
        body: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        include_attachments: bool = True,
    ) -> str:
        """
        Forward a message to recipients.

        Args:
            message_id: ID of message to forward
            to: List of recipient email addresses
            body: Optional body text to add before forwarded content
            cc: Optional CC recipients
            bcc: Optional BCC recipients
            include_attachments: If True, include original attachments

        Returns:
            Message ID of the forwarded message

        Raises:
            ValueError: If no recipients or invalid emails
            MailMessageNotFoundError: If message doesn't exist
        """
        from .utils import format_applescript_list, sanitize_input, validate_email

        if not to:
            raise ValueError("At least one recipient required")

        # Validate all email addresses
        for email in to:
            if not validate_email(email):
                raise ValueError(f"Invalid email address: {email}")

        if cc:
            for email in cc:
                if not validate_email(email):
                    raise ValueError(f"Invalid CC email address: {email}")

        if bcc:
            for email in bcc:
                if not validate_email(email):
                    raise ValueError(f"Invalid BCC email address: {email}")

        body_safe = escape_applescript_string(sanitize_input(body))
        to_list = format_applescript_list(to)
        cc_list = format_applescript_list(cc) if cc else '""'
        bcc_list = format_applescript_list(bcc) if bcc else '""'

        script = f"""
        tell application "Mail"
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set origMsg to first message of mb whose id is "{message_id}"

                        -- Create forward message
                        set fwdMsg to forward origMsg

                        -- Add body text before forwarded content
                        if "{body_safe}" is not "" then
                            set origContent to content of fwdMsg
                            set content of fwdMsg to "{body_safe}" & return & return & origContent
                        end if

                        -- Set recipients
                        set toRecipients to {to_list}
                        repeat with recipientAddr in toRecipients
                            make new to recipient at end of to recipients of fwdMsg with properties {{address:recipientAddr}}
                        end repeat

                        -- Set CC if provided
                        if {cc_list} is not "" then
                            set ccRecipients to {cc_list}
                            repeat with recipientAddr in ccRecipients
                                make new cc recipient at end of cc recipients of fwdMsg with properties {{address:recipientAddr}}
                            end repeat
                        end if

                        -- Set BCC if provided
                        if {bcc_list} is not "" then
                            set bccRecipients to {bcc_list}
                            repeat with recipientAddr in bccRecipients
                                make new bcc recipient at end of bcc recipients of fwdMsg with properties {{address:recipientAddr}}
                            end repeat
                        end if

                        -- Get the message ID
                        set fwdId to id of fwdMsg

                        -- Send the message
                        send fwdMsg

                        return fwdId
                    end try
                end repeat
            end repeat

            error "Message not found"
        end tell
        """

        result = self._run_applescript(script)
        return result
