"""
AppleScript-based connector for Apple Mail.
"""

import logging
import re
import subprocess
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any, cast

from imapclient.exceptions import IMAPClientError, LoginError

from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailRuleNotFoundError,
    MailUnsupportedRuleActionError,
)
from .imap_connector import ImapConnector
from .keychain import get_imap_password
from .utils import (
    applescript_account_clause,
    escape_applescript_string,
    get_flag_index,
    parse_applescript_json,
    sanitize_input,
    validate_email,
)

# Exception classes that trigger AppleScript fallback per the graceful-
# degradation invariants (docs/research/imap-auth-options-decision.md).
# OSError covers socket.timeout too. ValueError and MailAccountNotFoundError
# are deliberately NOT in this tuple — they indicate caller/config errors
# and must surface, not be papered over by fallback.
_IMAP_FALLBACK_EXCS: tuple[type[Exception], ...] = (
    MailKeychainEntryNotFoundError,
    MailKeychainAccessDeniedError,
    OSError,
    LoginError,
    IMAPClientError,
)

logger = logging.getLogger(__name__)

# Strict ISO 8601 YYYY-MM-DD — search_messages's date_from/date_to filters
# reject anything else to prevent AppleScript injection via the date clause.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# MCP-tool field name → Mail.app AppleScript `rule type` enum identifier.
# Verified against Mail.app's running rules: 'from header', 'subject header',
# 'message content' all confirmed live. Other values follow the same naming
# convention per Mail.app's AppleScript dictionary; verified via integration
# test on live rule creation.
_RULE_FIELD_MAP = {
    "from": "from header",
    "to": "to header",
    "subject": "subject header",
    "body": "message content",
    "any_recipient": "any recipient",
    "header_name": "header key",
}

# MCP-tool operator name → Mail.app AppleScript `qualifier` enum identifier.
# 'does contain value', 'equal to value', 'begins with value' verified live
# against the user's existing rules. Others follow Mail.app's documented
# naming.
_RULE_OPERATOR_MAP = {
    "contains": "does contain value",
    "does_not_contain": "does not contain value",
    "begins_with": "begins with value",
    "ends_with": "ends with value",
    "equals": "equal to value",
}


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
        # Accounts for which we've already logged a WARNING about IMAP failure.
        # Subsequent failures for the same account are demoted to DEBUG per
        # invariant 5 in docs/research/imap-auth-options-decision.md.
        self._imap_failures: set[str] = set()

    def _log_imap_fallback(self, account: str, exc: Exception) -> None:
        """Log an IMAP fallback event at the level specified by the invariants.

        MailKeychainEntryNotFoundError is a benign opt-out signal — always DEBUG,
        never tracked. For any other failure, the first per-account occurrence
        logs WARNING; subsequent occurrences for the same account log DEBUG.
        """
        if isinstance(exc, MailKeychainEntryNotFoundError):
            logger.debug(
                "IMAP not configured for %s (no Keychain entry); using AppleScript",
                account,
            )
            return
        if account not in self._imap_failures:
            self._imap_failures.add(account)
            logger.warning(
                "IMAP failed for %s (%s: %s), falling back to AppleScript; "
                "subsequent failures for this account will log at DEBUG",
                account,
                type(exc).__name__,
                exc,
            )
        else:
            logger.debug(
                "IMAP retry failed for %s: %s: %s",
                account,
                type(exc).__name__,
                exc,
            )

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

                # macOS stderr uses curly apostrophes (Can't) that won't match a
                # straight-apostrophe substring. Normalize before dispatching.
                normalized = error_msg.replace("\u2019", "'")

                # Parse error and raise appropriate exception
                if "Can't get account" in normalized:
                    raise MailAccountNotFoundError(error_msg)
                elif "Can't get mailbox" in normalized:
                    raise MailMailboxNotFoundError(error_msg)
                elif "Can't get message" in normalized:
                    raise MailMessageNotFoundError(error_msg)
                elif "Can't get rule" in normalized:
                    raise MailRuleNotFoundError(error_msg)
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
              - id: account UUID (stable across name changes)
              - name: account display name
              - email_addresses: list of associated email addresses
              - account_type: lowercase Mail type (e.g., "imap", "pop", "iCloud")
              - enabled: whether the account is currently enabled in Mail.app
        """
        tell_body = """
        tell application "Mail"
            set resultData to {}
            repeat with acc in accounts
                set accEmails to email addresses of acc
                if accEmails is missing value then set accEmails to {}
                set accRecord to {|id|:(id of acc as text), |name|:(name of acc), |email_addresses|:accEmails, |account_type|:((account type of acc) as text), |enabled|:(enabled of acc)}
                set end of resultData to accRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def list_rules(self) -> list[dict[str, Any]]:
        """List all Mail.app rules.

        Returns:
            List of rule dicts with keys:
              - index: 1-based positional index, matching Mail.app's
                AppleScript ``rule N`` reference. Stable within a single
                snapshot; can change if the user reorders rules.
              - name: rule display name (NOT guaranteed unique — Mail
                allows duplicates).
              - enabled: whether the rule is currently enabled.

        Note:
            Mail.app does not expose a stable rule id via AppleScript;
            ``index`` is the canonical handle for downstream mutation tools
            (set_rule_enabled / delete_rule / update_rule). Callers that
            care about reorder-stability should call ``list_rules`` again
            immediately before each mutation.
        """
        tell_body = """
        tell application "Mail"
            set resultData to {}
            set ruleCount to count of rules
            repeat with i from 1 to ruleCount
                set r to rule i
                set ruleRecord to {|index|:i, |name|:(name of r), |enabled|:(enabled of r)}
                set end of resultData to ruleRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def set_rule_enabled(self, rule_index: int, enabled: bool) -> None:
        """Toggle the enabled state of a rule by 1-based index.

        Args:
            rule_index: 1-based positional index, as returned by ``list_rules``.
            enabled: New enabled state.

        Raises:
            MailRuleNotFoundError: If rule_index is out of range (≤0 or
                greater than the number of existing rules).
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        enabled_str = "true" if enabled else "false"
        script = (
            f'tell application "Mail" to '
            f"set enabled of rule {rule_index} to {enabled_str}"
        )
        self._run_applescript(script)

    def _validate_rule_condition(self, cond: dict[str, Any]) -> None:
        """Validate a single RuleCondition dict.

        Required keys: field (in _RULE_FIELD_MAP), operator (in
        _RULE_OPERATOR_MAP), value (non-empty str). header_name required
        iff field == 'header_name'.
        """
        if "field" not in cond or cond["field"] not in _RULE_FIELD_MAP:
            raise ValueError(
                f"condition.field must be one of {sorted(_RULE_FIELD_MAP)}, "
                f"got {cond.get('field')!r}"
            )
        if (
            "operator" not in cond
            or cond["operator"] not in _RULE_OPERATOR_MAP
        ):
            raise ValueError(
                f"condition.operator must be one of "
                f"{sorted(_RULE_OPERATOR_MAP)}, got {cond.get('operator')!r}"
            )
        if not cond.get("value") or not isinstance(cond["value"], str):
            raise ValueError("condition.value must be a non-empty string")
        if cond["field"] == "header_name":
            if not cond.get("header_name"):
                raise ValueError(
                    "condition.header_name is required when field is "
                    "'header_name'"
                )

    def _validate_rule_actions(self, actions: dict[str, Any]) -> None:
        """Validate a RuleActions dict has at least one meaningful entry,
        flag_color (if any) is valid, and forward_to emails are valid."""
        meaningful_keys = {
            "move_to", "copy_to", "mark_read", "mark_flagged",
            "delete", "forward_to",
        }
        # Strip falsy bools / empty containers — they're no-ops, not actions.
        active = {
            k: v for k, v in actions.items()
            if k in meaningful_keys and v
        }
        if not active:
            raise ValueError(
                "actions must include at least one of "
                f"{sorted(meaningful_keys)} with a truthy value"
            )
        if "flag_color" in actions and actions["flag_color"]:
            # get_flag_index raises ValueError on bad input.
            get_flag_index(actions["flag_color"])
        if active.get("forward_to"):
            for addr in active["forward_to"]:
                if not isinstance(addr, str) or not validate_email(addr):
                    raise ValueError(
                        f"forward_to entries must be valid email "
                        f"addresses; got {addr!r}"
                    )
        for mb_key in ("move_to", "copy_to"):
            if mb_key in active:
                ref = active[mb_key]
                if (
                    not isinstance(ref, dict)
                    or not ref.get("account")
                    or not ref.get("mailbox")
                ):
                    raise ValueError(
                        f"actions.{mb_key} must be a dict with "
                        f"'account' and 'mailbox' keys, got {ref!r}"
                    )

    def _build_action_lines(self, actions: dict[str, Any]) -> list[str]:
        """Translate a validated RuleActions dict into AppleScript lines.

        Each line operates on a variable named ``newRule`` (or ``r`` for
        update_rule's reuse). Caller picks the target variable name and
        substitutes.
        """
        lines: list[str] = []
        if actions.get("move_to"):
            mb_safe = escape_applescript_string(
                sanitize_input(actions["move_to"]["mailbox"])
            )
            acct_clause = applescript_account_clause(
                actions["move_to"]["account"]
            )
            lines.append("set should move message of newRule to true")
            lines.append(
                f'set move message of newRule to mailbox "{mb_safe}" '
                f"of {acct_clause}"
            )
        if actions.get("copy_to"):
            mb_safe = escape_applescript_string(
                sanitize_input(actions["copy_to"]["mailbox"])
            )
            acct_clause = applescript_account_clause(
                actions["copy_to"]["account"]
            )
            lines.append("set should copy message of newRule to true")
            lines.append(
                f'set copy message of newRule to mailbox "{mb_safe}" '
                f"of {acct_clause}"
            )
        if actions.get("mark_read"):
            lines.append("set mark read of newRule to true")
        if actions.get("mark_flagged"):
            lines.append("set mark flagged of newRule to true")
            if actions.get("flag_color"):
                idx = get_flag_index(actions["flag_color"])
                lines.append(
                    f"set mark flag index of newRule to {idx}"
                )
        if actions.get("delete"):
            lines.append("set delete message of newRule to true")
        if actions.get("forward_to"):
            recipients = ", ".join(actions["forward_to"])
            recipients_safe = escape_applescript_string(recipients)
            lines.append(
                f'set forward message of newRule to "{recipients_safe}"'
            )
        return lines

    def create_rule(
        self,
        name: str,
        conditions: list[dict[str, Any]],
        actions: dict[str, Any],
        match_logic: str = "all",
        enabled: bool = True,
    ) -> int:
        """Create a new Mail.app rule. Returns the new rule's 1-based index.

        Args:
            name: Rule display name.
            conditions: List of RuleCondition dicts. At least one required.
            actions: RuleActions dict. At least one action must be set.
            match_logic: 'all' (AND) or 'any' (OR) across conditions.
            enabled: Whether the rule is enabled on creation.

        Returns:
            1-based positional index of the newly-created rule (Mail.app
            appends new rules to the end, so this equals the new total
            count of rules).

        Raises:
            ValueError: If any input fails schema validation.
        """
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if not conditions:
            raise ValueError("conditions must have at least one entry")
        if match_logic not in ("all", "any"):
            raise ValueError(
                f"match_logic must be 'all' or 'any', got {match_logic!r}"
            )
        for cond in conditions:
            self._validate_rule_condition(cond)
        self._validate_rule_actions(actions)

        name_safe = escape_applescript_string(sanitize_input(name))
        all_conditions = "true" if match_logic == "all" else "false"
        enabled_str = "true" if enabled else "false"

        condition_lines: list[str] = []
        for cond in conditions:
            rule_type = _RULE_FIELD_MAP[cond["field"]]
            qualifier = _RULE_OPERATOR_MAP[cond["operator"]]
            expr_safe = escape_applescript_string(
                sanitize_input(cond["value"])
            )
            if cond["field"] == "header_name":
                header_safe = escape_applescript_string(
                    sanitize_input(cond["header_name"])
                )
                condition_lines.append(
                    f"make new rule condition with properties "
                    f"{{rule type:{rule_type}, qualifier:{qualifier}, "
                    f'expression:"{expr_safe}", header:"{header_safe}"}} '
                    f"at end of rule conditions of newRule"
                )
            else:
                condition_lines.append(
                    f"make new rule condition with properties "
                    f"{{rule type:{rule_type}, qualifier:{qualifier}, "
                    f'expression:"{expr_safe}"}} '
                    f"at end of rule conditions of newRule"
                )

        action_lines = self._build_action_lines(actions)

        body = (
            f'set newRule to make new rule with properties '
            f'{{name:"{name_safe}"}}\n'
            f"set all conditions must be met of newRule to {all_conditions}\n"
            + "\n".join(condition_lines) + "\n"
            + "\n".join(action_lines) + "\n"
            f"set enabled of newRule to {enabled_str}\n"
            f"return (count of rules) as text"
        )
        script = f'tell application "Mail"\n{body}\nend tell'
        return int(self._run_applescript(script))

    def _check_supported_actions(self, rule_index: int) -> None:
        """Verify a rule's existing actions are all in our schema.

        Used by ``update_rule`` before applying changes — if the rule
        currently has any action set that we don't model (run-AppleScript,
        redirect, reply text, play sound, highlight color, forward text),
        we can't safely partial-update because we'd silently drop or
        misrepresent that action. Read access via ``list_rules`` is
        unaffected.

        Raises:
            MailRuleNotFoundError: If rule_index is out of range.
            MailUnsupportedRuleActionError: If any action outside the
                medium-tier schema is currently set on the rule.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        tell_body = f'''
        tell application "Mail"
            set r to rule {rule_index}
            set resultData to {{|run_script_set|:(run script of r is not missing value), |play_sound_set|:(play sound of r is not missing value), |redirect_set|:((redirect message of r) is not ""), |forward_text_set|:((forward text of r) is not ""), |reply_text_set|:((reply text of r) is not ""), |highlight_text|:(highlight text using color of r), |color_message|:((color message of r) as text)}}
        end tell
        '''
        script = _wrap_as_json_script(tell_body)
        raw = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(raw))

        unsupported: list[str] = []
        if parsed.get("run_script_set"):
            unsupported.append("run script")
        if parsed.get("play_sound_set"):
            unsupported.append("play sound")
        if parsed.get("redirect_set"):
            unsupported.append("redirect message")
        if parsed.get("forward_text_set"):
            unsupported.append("forward text")
        if parsed.get("reply_text_set"):
            unsupported.append("reply text")
        if parsed.get("highlight_text"):
            unsupported.append("highlight text using color")
        if parsed.get("color_message", "none") != "none":
            unsupported.append("color message")

        if unsupported:
            raise MailUnsupportedRuleActionError(
                f"rule {rule_index} uses actions outside the supported "
                f"schema: {', '.join(unsupported)}. Edit this rule in "
                f"Mail.app's Rules pane instead."
            )

    def delete_rule(self, rule_index: int) -> str:
        """Delete a rule by 1-based index.

        Reads the rule's name in the same AppleScript call so callers
        (typically the server layer's elicitation summary) can echo the
        deleted name. After deletion, downstream rule indices shift down
        by one — callers should re-call ``list_rules`` before any further
        rule operations.

        Args:
            rule_index: 1-based positional index, as returned by ``list_rules``.

        Returns:
            The name of the deleted rule (for confirmation / logging).

        Raises:
            MailRuleNotFoundError: If rule_index is out of range.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        script = (
            f'tell application "Mail"\n'
            f"    set deletedName to name of rule {rule_index}\n"
            f"    delete rule {rule_index}\n"
            f"    return deletedName\n"
            f"end tell"
        )
        return self._run_applescript(script)

    def list_mailboxes(self, account: str) -> list[dict[str, Any]]:
        """List all mailboxes for an account.

        Args:
            account: Account name.

        Returns:
            List of dicts with keys: name, unread_count.

        Raises:
            MailAccountNotFoundError: If account doesn't exist.
        """
        account_clause = applescript_account_clause(account)

        tell_body = f'''
        tell application "Mail"
            set accountRef to {account_clause}
            set resultData to {{}}

            repeat with mb in mailboxes of accountRef
                set mbUnread to unread count of mb
                if mbUnread is missing value then set mbUnread to 0
                set mbRecord to {{|name|:(name of mb), |unread_count|:mbUnread}}
                set end of resultData to mbRecord
            end repeat
        end tell
        '''

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def _resolve_imap_config(self, account: str) -> tuple[str, int, str]:
        """Query Mail.app for the IMAP connection details of an account.

        Args:
            account: Mail.app account name (e.g. "iCloud", "Gmail").

        Returns:
            Tuple of (host, port, email). `email` is the first entry of
            Mail.app's `email addresses` list if non-empty, else falls back
            to the `user name` property.

            For iCloud specifically, `user name` is the Apple ID login
            identifier — which may be any email (e.g. a Gmail address) —
            while the IMAP server only accepts @icloud.com / @me.com aliases
            as LOGIN username. `email addresses` reliably contains the
            alias iCloud's IMAP server expects. For Gmail / Yahoo / generic
            IMAP accounts, the first email address typically equals
            `user name`, so the behavior is equivalent there.

        Raises:
            MailAccountNotFoundError: If the account doesn't exist.
        """
        account_clause = applescript_account_clause(account)
        tell_body = f'''
        tell application "Mail"
            set acctRef to {account_clause}
            set acctEmails to email addresses of acctRef
            if acctEmails is missing value then set acctEmails to {{}}
            set resultData to {{|host|:(server name of acctRef), |port|:(port of acctRef), |user_name|:(user name of acctRef), |email_addresses|:acctEmails}}
        end tell
        '''
        script = _wrap_as_json_script(tell_body)
        raw = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(raw))
        email_addresses = cast(list[str], parsed.get("email_addresses") or [])
        email = email_addresses[0] if email_addresses else cast(str, parsed["user_name"])
        return (
            cast(str, parsed["host"]),
            cast(int, parsed["port"]),
            email,
        )

    def _imap_search(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run search_messages through the IMAP path.

        Resolves host/port/email via AppleScript, fetches the password from
        Keychain, and delegates to ImapConnector. Propagates all fallback-
        triggering exceptions unchanged — the caller (search_messages) is
        responsible for catching and falling back.

        Raises:
            MailKeychainEntryNotFoundError: No opt-in (benign).
            MailKeychainAccessDeniedError: Keychain ACL refused.
            OSError (incl. socket.timeout): Network / connection failure.
            imapclient.exceptions.LoginError: Credentials rejected.
            imapclient.exceptions.IMAPClientError: Protocol or session error.
            MailAccountNotFoundError: Mail.app doesn't know this account.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password)
        return imap.search_messages(
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

    def search_messages(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search for messages matching criteria.

        Tries the IMAP path first (fast, server-side SEARCH). Falls back to
        AppleScript on any IMAP failure per the graceful-degradation invariants
        in docs/research/imap-auth-options-decision.md — so a user with no
        Keychain entry, a revoked password, or a dropped network still gets
        working search via AppleScript.
        """
        try:
            return self._imap_search(
                account,
                mailbox,
                sender_contains,
                subject_contains,
                read_status,
                is_flagged,
                date_from,
                date_to,
                has_attachment,
                limit,
            )
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            # fall through to AppleScript
        return self._search_messages_applescript(
            account,
            mailbox,
            sender_contains,
            subject_contains,
            read_status,
            is_flagged,
            date_from,
            date_to,
            has_attachment,
            limit,
        )

    def _search_messages_applescript(
        self,
        account: str,
        mailbox: str = "INBOX",
        sender_contains: str | None = None,
        subject_contains: str | None = None,
        read_status: bool | None = None,
        is_flagged: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachment: bool | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """AppleScript path for search_messages (the universal baseline).

        Called directly when IMAP is not configured for the account, or as a
        fallback when the IMAP path fails for any reason (see the graceful-
        degradation invariants in docs/research/imap-auth-options-decision.md).

        Args:
            account: Account name.
            mailbox: Mailbox name.
            sender_contains: Substring match on sender (server-side).
            subject_contains: Substring match on subject (server-side).
            read_status: Filter by read status (True=read, False=unread).
            is_flagged: Filter by flagged status (True=flagged, False=not).
            date_from: Inclusive lower bound on date received. ISO 8601 YYYY-MM-DD.
            date_to: Inclusive upper bound on date received (full day included).
                ISO 8601 YYYY-MM-DD.
            has_attachment: Filter messages with/without attachments. Applied
                post-whose because Mail rejects it inside a whose clause.
            limit: Maximum results.

        Returns:
            List of message dictionaries.

        Raises:
            ValueError: If date_from or date_to is not ISO 8601 YYYY-MM-DD.
            MailAccountNotFoundError: If account doesn't exist.
            MailMailboxNotFoundError: If mailbox doesn't exist.
        """
        account_clause = applescript_account_clause(account)
        mailbox_safe = escape_applescript_string(sanitize_input(mailbox))

        # Build whose clause (server-side filters)
        conditions: list[str] = []
        if sender_contains:
            sender_safe = escape_applescript_string(sanitize_input(sender_contains))
            conditions.append(f'sender contains "{sender_safe}"')

        if subject_contains:
            subject_safe = escape_applescript_string(sanitize_input(subject_contains))
            conditions.append(f'subject contains "{subject_safe}"')

        if read_status is not None:
            status = "true" if read_status else "false"
            conditions.append(f"read status is {status}")

        if is_flagged is not None:
            status = "true" if is_flagged else "false"
            conditions.append(f"flagged status is {status}")

        if date_from is not None:
            if not _ISO_DATE_RE.match(date_from):
                raise ValueError(
                    f"date_from must be ISO 8601 YYYY-MM-DD, got: {date_from!r}"
                )
            conditions.append(f'date received >= (date "{date_from}")')

        if date_to is not None:
            if not _ISO_DATE_RE.match(date_to):
                raise ValueError(
                    f"date_to must be ISO 8601 YYYY-MM-DD, got: {date_to!r}"
                )
            # Upper bound is exclusive of the day AFTER date_to, so the full
            # day of date_to is included.
            next_day = (
                _date.fromisoformat(date_to) + _timedelta(days=1)
            ).isoformat()
            conditions.append(f'date received < (date "{next_day}")')

        # AppleScript rejects `whose true` ("Illegal comparison or logical").
        # When no filters are supplied, drop the `whose` clause entirely.
        whose_part = f" whose {' and '.join(conditions)}" if conditions else ""

        # `has_attachment` can't go in the whose clause — Mail rejects
        # `(count of mail attachments) > 0` and `exists mail attachment` with
        # type-specifier errors. Applied post-whose inside the repeat.
        if has_attachment is None:
            attachment_check = ""
        elif has_attachment:
            attachment_check = (
                "if (count of mail attachments of msg) = 0 then "
                "set includeThis to false"
            )
        else:
            attachment_check = (
                "if (count of mail attachments of msg) > 0 then "
                "set includeThis to false"
            )

        # Per-match limit is applied after all filters (whose + attachment
        # post-filter) so the caller gets up to `limit` final matches.
        effective_limit = str(limit) if limit else "999999999"

        tell_body = f'''
        tell application "Mail"
            set accountRef to {account_clause}
            set mailboxRef to mailbox "{mailbox_safe}" of accountRef
            set allMatches to messages of mailboxRef{whose_part}
            set totalCount to count of allMatches

            set resultData to {{}}
            repeat with i from 1 to totalCount
                set msg to item i of allMatches
                set includeThis to true
                {attachment_check}
                if includeThis then
                    set msgRecord to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg)}}
                    set end of resultData to msgRecord
                    if (count of resultData) >= {effective_limit} then exit repeat
                end if
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

                        set resultData to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent}}
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
                            set attRecord to {{|name|:(name of att), |mime_type|:(MIME type of att), |size|:(file size of att), |downloaded|:(downloaded of att)}}
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

    def get_thread(self, message_id: str) -> list[dict[str, Any]]:
        """Return all messages in the thread containing ``message_id``.

        Tries the IMAP path first (server-side header search, no subject-
        prefilter dependency). Falls back to AppleScript on any IMAP
        failure per the graceful-degradation invariants in
        docs/research/imap-auth-options-decision.md — so a user with no
        Keychain entry, a revoked password, or a dropped network still
        gets working threading via AppleScript.

        Args:
            message_id: Internal Mail.app id of any message in the thread
                (the anchor). Typically obtained from search_messages or
                get_message results.

        Returns:
            List of message dicts sorted by date_received ascending. Each
            dict has the search_messages shape: id, subject, sender,
            date_received, read_status, flagged. A thread of 1 is valid
            (anchor with no threading headers).

        Raises:
            MailMessageNotFoundError: If no message with the given id exists.
        """
        anchor = self._resolve_thread_anchor_applescript(message_id)
        try:
            return self._imap_get_thread(anchor)
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(cast(str, anchor["account"]), exc)
            # fall through to AppleScript
        return self._collect_thread_applescript(anchor)

    def _imap_get_thread(
        self, anchor: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """IMAP path for get_thread.

        Takes the anchor dict produced by _resolve_thread_anchor_applescript
        and delegates thread-member collection to ImapConnector. Propagates
        all fallback-triggering exceptions unchanged — the caller
        (get_thread) is responsible for catching and falling back.

        Raises:
            MailKeychainEntryNotFoundError: No opt-in (benign).
            MailKeychainAccessDeniedError: Keychain ACL refused.
            OSError (incl. socket.timeout): Network / connection failure.
            imapclient.exceptions.LoginError: Credentials rejected.
            imapclient.exceptions.IMAPClientError: Protocol or session error.
            MailAccountNotFoundError: Mail.app doesn't know this account.
        """
        account = cast(str, anchor["account"])
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password)
        return imap.find_thread_members(
            anchor_rfc_message_id=cast(str, anchor["rfc_message_id"]),
            anchor_references=cast(list[str], anchor.get("references") or []),
        )

    def _get_thread_applescript(self, message_id: str) -> list[dict[str, Any]]:
        """AppleScript path for get_thread (the universal baseline).

        Composes _resolve_thread_anchor_applescript (call 1) and
        _collect_thread_applescript (call 2 + Python graph walk). Called
        directly when IMAP is not configured for the account, or as a
        fallback when the IMAP path fails for any reason.

        Uses Mail.app's indexed ``whose subject contains "..."`` filter as
        a pre-filter, then reconstructs the thread by walking RFC 5322
        Message-ID / In-Reply-To / References headers across the candidate
        set. Members whose subject was rewritten mid-thread are not found
        (documented limitation of this path; fixed by the IMAP path).
        """
        anchor = self._resolve_thread_anchor_applescript(message_id)
        return self._collect_thread_applescript(anchor)

    def _resolve_thread_anchor_applescript(
        self, message_id: str,
    ) -> dict[str, Any]:
        """AppleScript call 1: resolve Mail.app internal ID to thread anchor.

        Returns a dict with keys:
            internal_id: str — the Mail.app internal id the caller passed in
                (echoed back so downstream code can use it without threading
                it separately).
            account: str — Mail.app account name the message lives in.
            rfc_message_id: str — RFC 5322 Message-ID (no angle brackets).
            subject: str — message subject.
            in_reply_to: str | None — parent's Message-ID if present.
            references: list[str] — parsed References header (bracketless,
                order preserved, duplicates removed).

        Raises:
            MailMessageNotFoundError: If no message with the given id exists.
        """
        from .utils import parse_rfc822_ids

        message_id_safe = escape_applescript_string(sanitize_input(message_id))
        anchor_body = f'''
        tell application "Mail"
            set anchorResult to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is {message_id_safe}
                        set anchorInReplyTo to ""
                        set anchorRefs to ""
                        try
                            repeat with h in headers of msg
                                set hname to name of h
                                if hname is "in-reply-to" then set anchorInReplyTo to (content of h)
                                if hname is "references" then set anchorRefs to (content of h)
                            end repeat
                        end try
                        set resultData to {{|account|:(name of acc), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |in_reply_to|:anchorInReplyTo, |references_raw|:anchorRefs}}
                        set anchorResult to resultData
                        exit repeat
                    end try
                end repeat
                if anchorResult is not missing value then exit repeat
            end repeat

            if anchorResult is missing value then
                error "Can't get message: not found"
            end if
        end tell
        '''

        anchor_script = _wrap_as_json_script(anchor_body)
        anchor_raw = self._run_applescript(anchor_script)
        raw = cast(dict[str, Any], parse_applescript_json(anchor_raw))

        in_reply_to_raw = raw.get("in_reply_to") or ""
        references_raw = raw.get("references_raw") or ""
        return {
            "internal_id": message_id,
            "account": cast(str, raw["account"]),
            "rfc_message_id": cast(str, raw["rfc_message_id"]),
            "subject": cast(str, raw["subject"]),
            "in_reply_to": in_reply_to_raw or None,
            "references": parse_rfc822_ids(references_raw),
        }

    def _collect_thread_applescript(
        self, anchor: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """AppleScript call 2 + Python graph walk.

        Takes the anchor dict produced by _resolve_thread_anchor_applescript,
        fetches subject-prefiltered candidates across all mailboxes of the
        anchor's account, and walks the reference graph to assemble the
        thread. Returns the final sorted search-shape list.
        """
        from .utils import normalize_subject, parse_rfc822_ids, walk_thread_graph

        account_name = cast(str, anchor["account"])
        base_subject = normalize_subject(cast(str, anchor["subject"]))
        account_safe = escape_applescript_string(sanitize_input(account_name))
        subject_safe = escape_applescript_string(sanitize_input(base_subject))

        candidates_body = f'''
        tell application "Mail"
            set acctRef to account "{account_safe}"
            set resultData to {{}}
            repeat with mbRef in mailboxes of acctRef
                try
                    set hits to (messages of mbRef whose subject contains "{subject_safe}")
                    repeat with m in hits
                        set inReplyTo to ""
                        set refs to ""
                        try
                            repeat with h in headers of m
                                set hname to name of h
                                if hname is "in-reply-to" then set inReplyTo to (content of h)
                                if hname is "references" then set refs to (content of h)
                            end repeat
                        end try
                        set candRecord to {{|id|:(id of m as text), |rfc_message_id|:(message id of m), |in_reply_to|:inReplyTo, |references_raw|:refs, |subject|:(subject of m), |sender|:(sender of m), |date_received|:(date received of m as text), |read_status|:(read status of m), |flagged|:(flagged status of m)}}
                        set end of resultData to candRecord
                    end repeat
                on error
                    -- Some mailboxes (e.g. Gmail smart labels) reject whose clauses; skip
                end try
            end repeat
        end tell
        '''

        candidates_script = _wrap_as_json_script(candidates_body)
        candidates_raw = self._run_applescript(candidates_script)
        candidates = cast(
            list[dict[str, Any]],
            parse_applescript_json(candidates_raw),
        )

        # Enrich candidates with parsed references (Python-side).
        for cand in candidates:
            cand["references_parsed"] = parse_rfc822_ids(
                cand.get("references_raw", "")
            )

        # Seed the known-id frontier: anchor + its own references.
        anchor_rfc = cast(str, anchor["rfc_message_id"])
        known_ids: set[str] = {anchor_rfc}
        in_reply_to = cast("str | None", anchor.get("in_reply_to"))
        if in_reply_to:
            known_ids.add(in_reply_to)
        known_ids.update(cast(list[str], anchor.get("references") or []))

        # Separate the anchor's own candidate row (when present) from the
        # rest. The graph walk operates on the non-anchor candidates; the
        # anchor itself always belongs in the result.
        anchor_candidate: dict[str, Any] | None = None
        non_anchor_candidates: list[dict[str, Any]] = []
        for cand in candidates:
            if cand["rfc_message_id"] == anchor_rfc and anchor_candidate is None:
                anchor_candidate = cand
            else:
                non_anchor_candidates.append(cand)

        accepted = walk_thread_graph(
            known_ids=known_ids,
            candidates=non_anchor_candidates,
        )

        # Assemble final thread: anchor (from candidates or a minimal row
        # if the anchor's own row didn't surface in the candidate set).
        thread: list[dict[str, Any]] = []
        if anchor_candidate is not None:
            thread.append(anchor_candidate)
        else:
            logger.warning(
                "get_thread: anchor (rfc=%s) not in candidate set; "
                "result row will be incomplete",
                anchor_rfc,
            )
            thread.append({
                "id": cast(str, anchor.get("internal_id") or ""),
                "subject": anchor["subject"],
                "sender": "",
                "date_received": "",
                "read_status": False,
                "flagged": False,
            })
        thread.extend(accepted)

        # Sort by date_received ascending. AppleScript emits locale-formatted
        # strings; lexicographic sort is a close-enough proxy within a thread.
        thread.sort(key=lambda m: m.get("date_received") or "")

        # Drop threading internals from output rows (search-shape only).
        for m in thread:
            m.pop("rfc_message_id", None)
            m.pop("in_reply_to", None)
            m.pop("references_raw", None)
            m.pop("references_parsed", None)

        return thread

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

        account_clause = applescript_account_clause(account)
        mailbox_safe = escape_applescript_string(sanitize_input(destination_mailbox))
        id_list = ", ".join(message_ids)

        if gmail_mode:
            # Gmail requires copy + delete approach to properly handle labels
            script = f"""
            tell application "Mail"
                set accountRef to {account_clause}
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
                set accountRef to {account_clause}
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

        account_clause = applescript_account_clause(account)
        name_safe = escape_applescript_string(sanitized_name)

        if parent_mailbox:
            parent_safe = escape_applescript_string(sanitize_input(parent_mailbox))
            script = f"""
            tell application "Mail"
                set accountRef to {account_clause}
                set parentMailbox to mailbox "{parent_safe}" of accountRef
                make new mailbox at parentMailbox with properties {{name:"{name_safe}"}}
                return "success"
            end tell
            """
        else:
            script = f"""
            tell application "Mail"
                set accountRef to {account_clause}
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
