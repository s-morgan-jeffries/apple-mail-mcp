"""
AppleScript-based connector for Apple Mail.
"""

import logging
import re
import subprocess
import time
import warnings
from collections.abc import Callable
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any, cast

from imapclient.exceptions import IMAPClientError, LoginError

from .drafts import _validate_draft_id
from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailRuleNotFoundError,
    MailUnsupportedRuleActionError,
)
from .imap_connector import ImapConnectionPool, ImapConnector
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

# Threshold (seconds) above which the AppleScript search path emits an
# INFO-level recommendation to enable IMAP delegation. Calibrated against
# the post-#32 baseline: a 50-message search on a 200+ msg mailbox runs
# in well under a second; sustained >5s suggests the user is hitting the
# AppleScript fallback against a mailbox where IMAP would help.
_SLOW_SEARCH_THRESHOLD_SEC = 5.0


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


def _bulk_repeat_block(
    *,
    account: str | None,
    source_mailbox: str | None,
    actions: list[str],
    counter_var: str,
) -> str:
    """Emit the AppleScript repeat block for a bulk-mutation operation.

    When `account` and `source_mailbox` are both provided, emits a narrow
    O(N) loop scoped to a single mailbox. When both are None, falls back
    to the legacy O(N × accounts × mailboxes) cross-scan for backwards
    compatibility. Any partial-pair raises ValueError — a mailbox name
    without an account is ambiguous (the same name can exist across
    multiple accounts).

    Args:
        account: Account name or UUID, or None.
        source_mailbox: Source mailbox name, or None.
        actions: One or more AppleScript statements to run inside the
            loop AFTER `set msg to first message ... whose id is msgId`.
            The counter increment is appended automatically.
        counter_var: Name of the AppleScript counter variable (e.g.
            "updateCount", "moveCount") that gets incremented per success.

    Returns:
        AppleScript fragment ready to interpolate into a `tell application
        "Mail"` block.

    Raises:
        ValueError: If exactly one of `account`/`source_mailbox` is given.
    """
    if (account is None) != (source_mailbox is None):
        missing = "source_mailbox" if account is not None else "account"
        raise ValueError(
            f"account and source_mailbox must be provided together; "
            f"missing {missing}"
        )

    if account is not None and source_mailbox is not None:
        # Narrow path: single mailbox, single loop. O(N).
        action_indent = " " * 20
        action_lines = "\n".join(action_indent + a for a in actions)
        account_clause = applescript_account_clause(account)
        mb_safe = escape_applescript_string(sanitize_input(source_mailbox))
        return (
            f'            set sourceMb to mailbox "{mb_safe}" of {account_clause}\n'
            f"            repeat with msgId in idList\n"
            f"                try\n"
            f"                    set msg to first message of sourceMb whose id is msgId\n"
            f"{action_lines}\n"
            f"                    set {counter_var} to {counter_var} + 1\n"
            f"                end try\n"
            f"            end repeat"
        )

    # Cross-scan path (legacy / backwards compat). O(N × M × K).
    action_indent = " " * 28
    action_lines = "\n".join(action_indent + a for a in actions)
    return (
        f"            repeat with msgId in idList\n"
        f"                repeat with acc in accounts\n"
        f"                    repeat with mb in mailboxes of acc\n"
        f"                        try\n"
        f"                            set msg to first message of mb whose id is msgId\n"
        f"{action_lines}\n"
        f"                            set {counter_var} to {counter_var} + 1\n"
        f"                        end try\n"
        f"                    end repeat\n"
        f"                end repeat\n"
        f"            end repeat"
    )


class AppleMailConnector:
    """Interface to Apple Mail via AppleScript."""

    _IMAP_BREAKER_TTL_S: float = 30.0
    """How long to skip IMAP for an account after a fallback-triggering
    failure. 30s is long enough to skip a tight burst of calls (typical
    agent workloads do many calls in succession), short enough that a
    refreshed Keychain entry or recovered network is picked up within a
    minute. Class constant — no public knob; tune by subclassing if
    really needed. See issue #118."""

    def __init__(
        self,
        timeout: int = 60,
        *,
        imap_pool: ImapConnectionPool | None = None,
    ) -> None:
        """
        Initialize the Mail connector.

        Args:
            timeout: Timeout in seconds for AppleScript operations.
            imap_pool: Optional ImapConnectionPool. When provided, every
                IMAP-delegated call (search_messages, get_message,
                get_attachments, get_thread) reuses cached connections
                across calls, amortizing the ~400 ms TCP+TLS+LOGIN
                overhead per call. Default None (per-call lifecycle —
                the v0.5.0 behavior). See issue #75.
        """
        self.timeout = timeout
        self._imap_pool = imap_pool
        # Accounts for which we've already logged a WARNING about IMAP failure.
        # Subsequent failures for the same account are demoted to DEBUG per
        # invariant 5 in docs/research/imap-auth-options-decision.md.
        self._imap_failures: set[str] = set()
        # Issue #118: per-account circuit breaker state. Maps account name
        # to the monotonic deadline before which IMAP is skipped entirely
        # (the orchestrator goes straight to the AppleScript path without
        # paying the connect/login round trip).
        self._imap_failure_until: dict[str, float] = {}

    def _imap_breaker_open(self, account: str) -> bool:
        """True if a recent IMAP failure on this account is still cooling
        down. Callers consult this *before* attempting IMAP — when True,
        skip IMAP entirely for this call (issue #118)."""
        deadline = self._imap_failure_until.get(account)
        return deadline is not None and time.monotonic() < deadline

    def _imap_clear_breaker(self, account: str) -> None:
        """Reset the cooldown for an account. Called after every
        successful IMAP call so a transient blip doesn't leave the
        breaker open longer than necessary."""
        self._imap_failure_until.pop(account, None)

    def _log_imap_fallback(self, account: str, exc: Exception) -> None:
        """Log an IMAP fallback event AND open the circuit breaker.

        MailKeychainEntryNotFoundError is a benign opt-out signal — always
        DEBUG, never tracked, never opens the breaker (the user explicitly
        chose not to configure IMAP for this account; cooling down would
        do nothing but cost a deadline lookup on every subsequent call).

        For any other failure: the first per-account occurrence logs
        WARNING; subsequent occurrences log DEBUG. LoginError gets a
        specialized message that names the exact `setup-imap` command —
        a stale/revoked Keychain password is the most common cause and
        the AppleScript fallback would otherwise hide the breakage from
        the user indefinitely (issue #118). For all non-benign failures,
        the breaker opens for ``_IMAP_BREAKER_TTL_S`` seconds.
        """
        if isinstance(exc, MailKeychainEntryNotFoundError):
            logger.debug(
                "IMAP not configured for %s (no Keychain entry); using AppleScript",
                account,
            )
            return

        # Non-benign failure: open the breaker.
        self._imap_failure_until[account] = (
            time.monotonic() + self._IMAP_BREAKER_TTL_S
        )

        if account not in self._imap_failures:
            self._imap_failures.add(account)
            if isinstance(exc, LoginError):
                logger.warning(
                    "IMAP login rejected for %r — likely an expired or "
                    "revoked app password. To refresh: "
                    "`apple-mail-mcp setup-imap --account %s`. The "
                    "AppleScript fallback is being used in the meantime; "
                    "results will be correct but slower.",
                    account, account,
                )
            else:
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

    def _resolve_account_to_sender(self, account: str) -> str:
        """Resolve an account name or UUID to its primary email address.

        Used by the send-path methods (#155) to wire up the AppleScript
        ``sender`` property on outgoing messages. Accepts either form
        matching the existing ``account`` convention on ``list_mailboxes``,
        ``search_messages``, etc.

        Raises:
            MailAccountNotFoundError: No account matches the given name/UUID.
            ValueError: Account exists but has no email addresses configured.
        """
        for acc in self.list_accounts():
            if acc.get("id") == account or acc.get("name") == account:
                emails = acc.get("email_addresses") or []
                if not emails:
                    raise ValueError(
                        f"Account {account!r} has no email addresses "
                        f"configured."
                    )
                return cast(str, emails[0])
        raise MailAccountNotFoundError(
            f"Account {account!r} not found in Mail.app configured accounts."
        )

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

    def update_rule(
        self,
        rule_index: int,
        name: str | None = None,
        enabled: bool | None = None,
        conditions: list[dict[str, Any]] | None = None,
        actions: dict[str, Any] | None = None,
        match_logic: str | None = None,
    ) -> None:
        """Update an existing Mail.app rule (patch-style for top-level fields,
        full replacement for conditions/actions when provided).

        Calls ``_check_supported_actions`` first; refuses to update any rule
        whose existing action set includes something outside our schema
        (run-AppleScript, redirect, reply text, etc.) — we cannot safely
        partial-update because the unsupported actions would be silently
        dropped or misrepresented.

        Args:
            rule_index: 1-based positional index from ``list_rules``.
            name: New name (only set if not None).
            enabled: New enabled state (only set if not None).
            conditions: If provided, REPLACES all existing conditions wholesale.
            actions: If provided, REPLACES all action flags wholesale —
                unprovided actions are reset to off.
            match_logic: 'all' | 'any', only set if not None.

        Raises:
            ValueError: If any provided input fails schema validation.
            MailRuleNotFoundError: If rule_index is out of range.
            MailUnsupportedRuleActionError: If the rule currently has an
                action outside the supported schema.
        """
        if rule_index < 1:
            raise MailRuleNotFoundError(
                f"rule_index must be 1-based and positive, got {rule_index}"
            )
        if match_logic is not None and match_logic not in ("all", "any"):
            raise ValueError(
                f"match_logic must be 'all' or 'any', got {match_logic!r}"
            )
        if conditions is not None:
            # Mail.app on macOS Tahoe (16.0 / macOS 26) has a recursion bug
            # in -[MFMessageRule(Applescript) removeFromCriteriaAtIndex:].
            # ANY AppleScript path that removes a rule condition (delete by
            # index, delete every, or assigning a new list to `rule
            # conditions`) hits the same broken accessor and crashes Mail.
            # Verified with a one-line minimal repro:
            #     tell application "Mail" to delete rule condition 1 of rule "X"
            # Until Apple fixes this, replacing conditions in place is not
            # implementable; users must delete and recreate the rule.
            raise MailUnsupportedRuleActionError(
                "Replacing rule conditions is not supported: Mail.app on "
                "macOS Tahoe has a recursion bug in its AppleScript handler "
                "for rule-condition deletion (-[MFMessageRule(Applescript) "
                "removeFromCriteriaAtIndex:]) that crashes Mail. To change "
                "conditions, delete the rule and recreate it with create_rule."
            )
        if actions is not None:
            self._validate_rule_actions(actions)
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("name, if provided, must be a non-empty string")

        # Refuse to patch rules whose existing actions we don't fully model.
        self._check_supported_actions(rule_index)

        # Renaming a rule invalidates the local AppleScript variable
        # bound to it (Mail.app tries to resolve the variable by the old
        # name on subsequent property accesses, which now fails). Defer
        # any rename to the very end so all other property changes
        # operate on a stable reference.
        body_parts: list[str] = [
            f"set newRule to rule {rule_index}",
        ]

        if match_logic is not None:
            body_parts.append(
                f"set all conditions must be met of newRule to "
                f"{'true' if match_logic == 'all' else 'false'}"
            )
        if actions is not None:
            # Reset all supported action flags first; then apply provided ones.
            # `set forward message ... to ""` raises -10000 when the value is
            # already empty (Tahoe quirk), so gate the reset on a length check.
            body_parts.extend([
                "set should move message of newRule to false",
                "set should copy message of newRule to false",
                "set mark read of newRule to false",
                "set mark flagged of newRule to false",
                "set mark flag index of newRule to -1",
                "set delete message of newRule to false",
                'if forward message of newRule is not "" then '
                'set forward message of newRule to ""',
            ])
            body_parts.extend(self._build_action_lines(actions))
        # `enabled` must come AFTER the action-reset block: setting enabled
        # before resets causes the reset to silently revert it (Tahoe quirk).
        if enabled is not None:
            body_parts.append(
                f"set enabled of newRule to "
                f"{'true' if enabled else 'false'}"
            )
        # Rename last — see comment above.
        if name is not None:
            name_safe = escape_applescript_string(sanitize_input(name))
            body_parts.append(f'set name of newRule to "{name_safe}"')

        if len(body_parts) == 1:
            # Only the rule lookup, no actual updates — caller passed nothing.
            return
        script = (
            'tell application "Mail"\n'
            + "\n".join(body_parts)
            + "\nend tell"
        )
        self._run_applescript(script)

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
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
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
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
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
            include_attachments=include_attachments,
            body_contains=body_contains,
            text_contains=text_contains,
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
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for messages matching criteria.

        Tries the IMAP path first (fast, server-side SEARCH). Falls back to
        AppleScript on any IMAP failure per the graceful-degradation invariants
        in docs/research/imap-auth-options-decision.md — so a user with no
        Keychain entry, a revoked password, or a dropped network still gets
        working search via AppleScript.

        When ``include_attachments=True``, every row includes an
        ``attachments`` field with the same shape as ``mail.get_attachments``
        rows (``name``, ``mime_type``, ``size``, ``downloaded``). On the IMAP
        path this is essentially free (BODYSTRUCTURE bundles into the same
        FETCH); on the AppleScript fallback, per-row attachment enumeration
        can be expensive on cold caches — see the ``include_attachments``
        notes in TOOLS.md and #142.

        ``body_contains`` and ``text_contains`` filter by message content
        (RFC 3501 ``BODY`` / ``TEXT`` semantics on IMAP; ``content of msg``
        on AppleScript). On AppleScript these can be very slow — measured
        148s for 100 cold-cache messages on a 47k-message INBOX. When the
        call commits to AppleScript and a body/text filter is set,
        ``on_warning`` (if provided) is invoked with a human-readable string
        describing the cost. See #145 / #146.
        """
        body_search = bool(body_contains or text_contains)

        if not self._imap_breaker_open(account):
            try:
                result = self._imap_search(
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
                    include_attachments,
                    body_contains,
                    text_contains,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        # We're committed to the AppleScript path. Warn proactively if a
        # body/text search is set — that's the multi-order-of-magnitude
        # slow case (#146).
        if on_warning is not None and body_search:
            on_warning(
                f"AppleScript body search can take minutes on large "
                f"mailboxes (measured 148s for 100 cold-cache messages on "
                f"a 47k-message Gmail INBOX). Run "
                f"`apple-mail-mcp setup-imap --account {account!r}` for "
                f"sub-second IMAP body search."
            )

        start = time.perf_counter()
        try:
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
                include_attachments,
                body_contains,
                text_contains,
            )
        finally:
            elapsed = time.perf_counter() - start
            if elapsed > _SLOW_SEARCH_THRESHOLD_SEC:
                logger.info(
                    "AppleScript search took %.1fs on account=%r mailbox=%r. "
                    "For large mailboxes, enabling IMAP delegation is "
                    "substantially faster — see the 'Optional: faster "
                    "search via IMAP' section in the project README.",
                    elapsed, account, mailbox,
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
        include_attachments: bool = False,
        body_contains: str | None = None,
        text_contains: str | None = None,
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

        # Build per-message AppleScript IF filters instead of a `whose` clause.
        #
        # Empirical finding (probes against an 8443-message Sent folder on
        # MobileMe): `messages of mb whose <filter>` triggers Mail.app's
        # internal predicate evaluator across the entire mailbox before any
        # iteration starts — measured >120s timeout for permissive filters
        # and effectively unbounded for selective ones. Manual indexed
        # iteration with the same filter as an IF body completes in ~1s for
        # the same query.
        #
        # The pattern: iterate `messages of mailboxRef` in REVERSE order
        # (newest first, matching typical user intent for mail), apply
        # filter expressions per-message, short-circuit when matchCount
        # reaches limit. Cost is bounded by `min(filter_misses + limit, N)`
        # times per-message-property-fetch — typically dominated by the
        # first few hundred recent messages, which Mail caches locally.
        filter_checks: list[str] = []

        if sender_contains:
            sender_safe = escape_applescript_string(sanitize_input(sender_contains))
            filter_checks.append(
                f'if (sender of msg) does not contain "{sender_safe}" '
                f'then set includeThis to false'
            )

        if subject_contains:
            subject_safe = escape_applescript_string(sanitize_input(subject_contains))
            filter_checks.append(
                f'if (subject of msg) does not contain "{subject_safe}" '
                f'then set includeThis to false'
            )

        if read_status is not None:
            target = "true" if read_status else "false"
            filter_checks.append(
                f'if (read status of msg) is not {target} '
                f'then set includeThis to false'
            )

        if is_flagged is not None:
            target = "true" if is_flagged else "false"
            filter_checks.append(
                f'if (flagged status of msg) is not {target} '
                f'then set includeThis to false'
            )

        if date_from is not None:
            if not _ISO_DATE_RE.match(date_from):
                raise ValueError(
                    f"date_from must be ISO 8601 YYYY-MM-DD, got: {date_from!r}"
                )
            filter_checks.append(
                f'if (date received of msg) < (date "{date_from}") '
                f'then set includeThis to false'
            )

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
            filter_checks.append(
                f'if (date received of msg) >= (date "{next_day}") '
                f'then set includeThis to false'
            )

        if has_attachment is True:
            filter_checks.append(
                "if (count of mail attachments of msg) = 0 "
                "then set includeThis to false"
            )
        elif has_attachment is False:
            filter_checks.append(
                "if (count of mail attachments of msg) > 0 "
                "then set includeThis to false"
            )

        # Body / text filters (#145). AppleScript `contains` is
        # case-insensitive by default, matching IMAP `SEARCH BODY`/`TEXT`
        # semantics. Reading `content of msg` is expensive — see #146 for
        # the proactive warning surfaced before this script runs.
        if body_contains:
            body_safe = escape_applescript_string(sanitize_input(body_contains))
            filter_checks.append(
                f'if (content of msg) does not contain "{body_safe}" '
                f'then set includeThis to false'
            )

        if text_contains:
            # `text_contains` is the IMAP `TEXT` predicate — substring match
            # against headers + body. AppleScript can't easily address all
            # headers in a per-msg property; we approximate with content +
            # subject + sender (the practical cases). Recipients omitted —
            # callers who need recipient matching should use `sender_contains`
            # or future params. Documented in TOOLS.md.
            text_safe = escape_applescript_string(sanitize_input(text_contains))
            filter_checks.append(
                f'if not ((content of msg) contains "{text_safe}" or '
                f'(subject of msg) contains "{text_safe}" or '
                f'(sender of msg) contains "{text_safe}") '
                f'then set includeThis to false'
            )

        # Render filter checks each on their own line, indented for the loop.
        filter_block = "\n                ".join(filter_checks) if filter_checks else ""

        # Per-match limit short-circuits the loop. With no limit, we collect
        # everything (newest-first) — same observable behavior as before
        # except for ordering (was oldest-first).
        effective_limit = str(limit) if limit else "999999999"

        if include_attachments:
            attachments_clause = '''
                    set attList to {}
                    repeat with att in mail attachments of msg
                        set attRecord to {|name|:(name of att), |mime_type|:(MIME type of att), |size|:(file size of att), |downloaded|:(downloaded of att)}
                        set end of attList to attRecord
                    end repeat'''
            attachments_field = ", |attachments|:attList"
        else:
            attachments_clause = ""
            attachments_field = ""

        tell_body = f'''
        tell application "Mail"
            set accountRef to {account_clause}
            set mailboxRef to mailbox "{mailbox_safe}" of accountRef
            set msgs to messages of mailboxRef
            set total to count of msgs

            set resultData to {{}}
            set matchCount to 0
            repeat with i from total to 1 by -1
                if matchCount >= {effective_limit} then exit repeat
                set msg to item i of msgs
                set includeThis to true
                {filter_block}
                if includeThis then{attachments_clause}
                    set msgRecord to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg){attachments_field}}}
                    set end of resultData to msgRecord
                    set matchCount to matchCount + 1
                end if
            end repeat
        end tell
        '''

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def get_message(
        self,
        message_id: str,
        include_content: bool = True,
        *,
        headers_only: bool = False,
        account: str | None = None,
        mailbox: str | None = None,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """
        Get full message details.

        Tries the IMAP path first when both ``account`` and ``mailbox``
        are supplied AND the account has a Keychain entry. Falls back to
        AppleScript on any IMAP failure per the graceful-degradation
        invariants in docs/research/imap-auth-options-decision.md, and
        also when no account/mailbox hint is given.

        Note on identifier semantics: the IMAP path matches against the
        RFC 5322 ``Message-ID`` header (the same form ``search_messages``
        returns when delegated through IMAP). The AppleScript path
        matches Mail.app's internal numeric message id. Callers that
        obtained ``message_id`` from a `search_messages` call should
        forward the same ``account`` + ``mailbox`` to keep the paths
        consistent.

        Args:
            message_id: Message ID. RFC 5322 form for the IMAP path,
                Mail.app internal id for the AppleScript path.
            include_content: When False, ``content`` is the empty string.
            headers_only: IMAP-only optimization — fetches ``BODY[HEADER]``
                instead of the body. Silently ignored on the AppleScript
                fallback path (AppleScript always returns body content
                when ``include_content`` is True).
            account: Mail.app account name. Optional; required (with
                ``mailbox``) to enable the IMAP fast path.
            mailbox: Folder to look in for the IMAP path. Optional.

        Returns:
            Message dictionary with keys: id, subject, sender,
            date_received, read_status, flagged, content.

        Raises:
            MailMessageNotFoundError: Message not found via either path.
        """
        if (
            account is not None
            and mailbox is not None
            and not self._imap_breaker_open(account)
        ):
            try:
                result = self._imap_get_message(
                    account=account,
                    mailbox=mailbox,
                    message_id=message_id,
                    include_content=include_content,
                    headers_only=headers_only,
                    include_attachments=include_attachments,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        return self._get_message_applescript(
            message_id, include_content, include_attachments
        )

    def _imap_get_message(
        self,
        *,
        account: str,
        mailbox: str,
        message_id: str,
        include_content: bool,
        headers_only: bool,
        include_attachments: bool,
    ) -> dict[str, Any]:
        """Run get_message through the IMAP path. Mirrors _imap_search.

        Propagates all fallback-triggering exceptions unchanged — the
        caller (get_message) catches and falls back.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.get_message(
            message_id,
            mailbox=mailbox,
            include_content=include_content,
            headers_only=headers_only,
            include_attachments=include_attachments,
        )

    def _get_message_applescript(
        self,
        message_id: str,
        include_content: bool,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """AppleScript fallback for get_message — iterates account × mailbox.

        Slow on accounts with many mailboxes (see issue #72). Callers
        with a known account+mailbox should provide them to take the
        IMAP path instead.
        """
        message_id_safe = escape_applescript_string(sanitize_input(message_id))

        content_clause = (
            'set msgContent to content of msg'
            if include_content
            else 'set msgContent to ""'
        )

        if include_attachments:
            attachments_clause = '''
                        set attList to {}
                        repeat with att in mail attachments of msg
                            set attRecord to {|name|:(name of att), |mime_type|:(MIME type of att), |size|:(file size of att), |downloaded|:(downloaded of att)}
                            set end of attList to attRecord
                        end repeat
'''
            attachments_field = ", |attachments|:attList"
        else:
            attachments_clause = ""
            attachments_field = ""

        tell_body = f'''
        tell application "Mail"
            set resultData to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is "{message_id_safe}"
                        {content_clause}
{attachments_clause}
                        set resultData to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent{attachments_field}}}
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

    def auto_template_vars(self, message_id: str | None) -> dict[str, str]:
        """Build the auto-fill variable dict for render_template.

        With ``message_id``, calls :meth:`get_message` (without content)
        and extracts ``recipient_name``, ``recipient_email``, and
        ``original_subject`` from the original sender. Always includes
        ``today`` (ISO date). User-supplied vars are layered on top of
        this dict at the call site, so user values win on conflict.
        """
        from email.utils import parseaddr

        out: dict[str, str] = {"today": _date.today().isoformat()}
        if message_id is None:
            return out
        msg = self.get_message(message_id, include_content=False)
        sender_field = str(msg.get("sender") or "")
        display_name, email_addr = parseaddr(sender_field)
        out["recipient_email"] = email_addr or sender_field
        out["recipient_name"] = display_name or out["recipient_email"]
        out["original_subject"] = str(msg.get("subject") or "")
        return out

    def mark_as_read(
        self,
        message_ids: list[str],
        read: bool = True,
        *,
        account: str | None = None,
        source_mailbox: str | None = None,
    ) -> int:
        """
        Mark messages as read or unread.

        Args:
            message_ids: List of message IDs
            read: True for read, False for unread
            account: Optional account name (or UUID) the messages live in.
                Must be provided together with `source_mailbox`. When both
                are given, the AppleScript narrows the scan to that single
                mailbox — O(N) instead of the default cross-scan O(N × M × K).
            source_mailbox: Optional source mailbox name; see `account`.

        Returns:
            Number of messages updated

        Raises:
            ValueError: If exactly one of `account`/`source_mailbox` is given.
            MailAppleScriptError: If operation fails
        """
        if not message_ids:
            return 0

        repeat_block = _bulk_repeat_block(
            account=account,
            source_mailbox=source_mailbox,
            actions=[f"set read status of msg to {'true' if read else 'false'}"],
            counter_var="updateCount",
        )

        # Build list of IDs (sanitize and escape each)
        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        script = f"""
        tell application "Mail"
            set idList to {{{id_list}}}
            set updateCount to 0

{repeat_block}

            return updateCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def get_attachments(
        self,
        message_id: str,
        *,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get list of attachments from a message.

        Tries the IMAP path first when both ``account`` and ``mailbox``
        are supplied AND the account has a Keychain entry — one
        BODYSTRUCTURE FETCH instead of an account×mailbox AppleScript
        scan plus per-attachment property reads. Falls back to AppleScript
        on any IMAP failure per the graceful-degradation invariants in
        docs/research/imap-auth-options-decision.md.

        The IMAP path also surfaces attachment cases Mail.app's
        AppleScript layer drops silently — forwarded message/rfc822
        parts, multipart/related inline images with filenames, and
        attachments with Unicode filenames (issue #73).

        Note on identifier semantics: same as ``get_message`` — the IMAP
        path matches against the RFC 5322 ``Message-ID`` header (the
        form ``search_messages`` returns when delegated through IMAP).
        The AppleScript path matches Mail.app's internal numeric id.
        Forward the same ``account`` + ``mailbox`` you used for
        ``search_messages`` to keep the paths consistent.

        Note on ``downloaded``: on the IMAP path, ``downloaded`` is
        always ``False`` — BODYSTRUCTURE returns metadata only, and
        Mail.app's local-cache state isn't observable from the IMAP
        protocol. On the AppleScript path it reflects Mail.app's cache.
        Treat ``False`` as "may need a network fetch on save".

        Args:
            message_id: Message ID (RFC 5322 form for IMAP path,
                Mail.app internal id for AppleScript path).
            account: Mail.app account name. Optional; required (with
                ``mailbox``) to enable the IMAP fast path.
            mailbox: Folder to look in for the IMAP path. Optional.

        Returns:
            List of attachment dicts with keys ``name`` (str),
            ``mime_type`` (str), ``size`` (int), ``downloaded`` (bool).

        Raises:
            MailMessageNotFoundError: Message not found via either path.
        """
        if (
            account is not None
            and mailbox is not None
            and not self._imap_breaker_open(account)
        ):
            try:
                result = self._imap_get_attachments(
                    account=account,
                    mailbox=mailbox,
                    message_id=message_id,
                )
                self._imap_clear_breaker(account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(account, exc)
                # fall through to AppleScript

        return self._get_attachments_applescript(message_id)

    def _imap_get_attachments(
        self,
        *,
        account: str,
        mailbox: str,
        message_id: str,
    ) -> list[dict[str, Any]]:
        """Run get_attachments through the IMAP path. Mirrors _imap_search
        and _imap_get_message.

        Propagates all fallback-triggering exceptions unchanged — the
        caller (get_attachments) catches and falls back.
        """
        host, port, email = self._resolve_imap_config(account)
        password = get_imap_password(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.get_attachments(message_id, mailbox=mailbox)

    def _get_attachments_applescript(
        self, message_id: str
    ) -> list[dict[str, Any]]:
        """AppleScript fallback for get_attachments — iterates account ×
        mailbox to locate the message, then enumerates attachments via
        Mail.app's model layer. Slow on accounts with many mailboxes;
        also subject to known silent-failure cases (see issue #73).
        Callers with a known account+mailbox should provide them to take
        the IMAP path instead.
        """
        message_id_safe = escape_applescript_string(sanitize_input(message_id))

        tell_body = f'''
        tell application "Mail"
            set resultData to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is "{message_id_safe}"
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
        anchor_account = cast(str, anchor["account"])
        if not self._imap_breaker_open(anchor_account):
            try:
                result = self._imap_get_thread(anchor)
                self._imap_clear_breaker(anchor_account)
                return result
            except _IMAP_FALLBACK_EXCS as exc:
                self._log_imap_fallback(anchor_account, exc)
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
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
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
                        set msg to first message of mb whose id is "{message_id_safe}"
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
                        set msg to first message of mb whose id is "{message_id_safe}"
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
        *,
        source_mailbox: str | None = None,
    ) -> int:
        """
        Move messages to a different mailbox.

        Args:
            message_ids: List of message IDs to move
            destination_mailbox: Name of destination mailbox
            account: Account name (or UUID) hosting the destination mailbox
            gmail_mode: Use Gmail-specific handling (copy + delete)
            source_mailbox: Optional source mailbox name to narrow the
                AppleScript scan to one mailbox (O(N) instead of O(N × M × K)).
                When provided, source is assumed to be in the same `account`
                as the destination — the common case. To move across
                accounts, omit `source_mailbox` to fall back to the
                cross-scan path.

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
        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        if gmail_mode:
            actions = ["duplicate msg to destMailbox", "delete msg"]
        else:
            actions = ["set mailbox of msg to destMailbox"]

        # `account` is required by this method (it's where the destination
        # lives), so source_mailbox is the only "optional" half here. Pass
        # account through only when the caller explicitly wants the narrow
        # source-scan path.
        repeat_block = _bulk_repeat_block(
            account=account if source_mailbox is not None else None,
            source_mailbox=source_mailbox,
            actions=actions,
            counter_var="moveCount",
        )

        script = f"""
        tell application "Mail"
            set accountRef to {account_clause}
            set destMailbox to mailbox "{mailbox_safe}" of accountRef
            set idList to {{{id_list}}}
            set moveCount to 0

{repeat_block}

            return moveCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def flag_message(
        self,
        message_ids: list[str],
        flag_color: str,
        *,
        account: str | None = None,
        source_mailbox: str | None = None,
    ) -> int:
        """
        Set flag color on messages.

        Args:
            message_ids: List of message IDs to flag
            flag_color: Flag color (none, orange, red, yellow, blue, green, purple, gray)
            account: Optional account name (or UUID); see `source_mailbox`.
            source_mailbox: Optional source mailbox name. When provided
                together with `account`, the AppleScript narrows the scan
                to that single mailbox — O(N) instead of O(N × M × K).
                Either alone raises ValueError.

        Returns:
            Number of messages flagged

        Raises:
            ValueError: If flag color is invalid, or if exactly one of
                `account`/`source_mailbox` is given.
        """
        if not message_ids:
            return 0

        from .utils import get_flag_index, validate_flag_color

        if not validate_flag_color(flag_color):
            raise ValueError(f"Invalid flag color: {flag_color}")

        flag_index = get_flag_index(flag_color)
        flagged_status = "true" if flag_color != "none" else "false"
        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        repeat_block = _bulk_repeat_block(
            account=account,
            source_mailbox=source_mailbox,
            actions=[
                f"set flag index of msg to {flag_index}",
                f"set flagged status of msg to {flagged_status}",
            ],
            counter_var="flagCount",
        )

        script = f"""
        tell application "Mail"
            set idList to {{{id_list}}}
            set flagCount to 0

{repeat_block}

            return flagCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def update_message(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None = None,
        flagged: bool | None = None,
        flag_color: str | None = None,
        destination_mailbox: str | None = None,
        account: str | None = None,
        source_mailbox: str | None = None,
        gmail_mode: bool = False,
    ) -> int:
        """
        Patch one or more messages in a single AppleScript pass.

        Consolidates ``mark_as_read`` + ``flag_message`` + ``move_messages``
        (#135). Caller sets only the fields they want changed; tool applies
        all of them in a single AppleScript pass via ``_bulk_repeat_block``.

        Order of operations: read-state and flag changes are applied first
        (in the source mailbox), then the move. IMAP requires the message
        to exist in the source folder for STORE before MOVE.

        Args:
            message_ids: List of message ids to update.
            read_status: When set, mark as read (True) / unread (False).
            flagged: When set, set flag presence. ``True`` without
                ``flag_color`` defaults to orange. ``False`` clears the
                flag.
            flag_color: Color name (orange, red, yellow, blue, green,
                purple, gray, none). Implies ``flagged=True`` unless
                "none". Validated.
            destination_mailbox: When set, move messages to this mailbox.
                Requires ``account`` (the destination's account).
            account: Account hosting destination (required when
                ``destination_mailbox`` is set). Also used with
                ``source_mailbox`` for narrow-path optimization.
            source_mailbox: Optional source mailbox. With ``account``,
                narrows the AppleScript scan to one mailbox.
            gmail_mode: Use Gmail-specific copy+delete for the move.

        Returns:
            Number of messages updated.

        Raises:
            ValueError: If no fields set; if exactly one of
                account/source_mailbox is given without a destination
                requirement; if flag_color invalid; if account is missing
                when destination_mailbox is set.
            MailAccountNotFoundError: account doesn't exist.
            MailMailboxNotFoundError: destination mailbox doesn't exist.
        """
        if not message_ids:
            return 0

        # At least one mutation field must be set (server tier also
        # validates; defense-in-depth here).
        if (
            read_status is None
            and flagged is None
            and flag_color is None
            and destination_mailbox is None
        ):
            raise ValueError("update_message: specify at least one field to update")

        if destination_mailbox is not None and account is None:
            raise ValueError(
                "update_message: account is required when "
                "destination_mailbox is set"
            )

        from .utils import get_flag_index, validate_flag_color

        actions: list[str] = []

        if read_status is not None:
            target = "true" if read_status else "false"
            actions.append(f"set read status of msg to {target}")

        # Flag handling — order matters: explicit flag_color wins over
        # bare flagged=True; flagged=False clears regardless of color.
        if flagged is False:
            actions.append("set flag index of msg to -1")
            actions.append("set flagged status of msg to false")
        elif flag_color is not None:
            if not validate_flag_color(flag_color):
                raise ValueError(f"Invalid flag color: {flag_color}")
            flag_index = get_flag_index(flag_color)
            actions.append(f"set flag index of msg to {flag_index}")
            flagged_status = "true" if flag_color != "none" else "false"
            actions.append(f"set flagged status of msg to {flagged_status}")
        elif flagged is True:
            # No color → default orange (matches existing flag_message default).
            actions.append(f"set flag index of msg to {get_flag_index('orange')}")
            actions.append("set flagged status of msg to true")

        # Move (always last — IMAP STORE requires source folder).
        if destination_mailbox is not None:
            if gmail_mode:
                actions.append("duplicate msg to destMailbox")
                actions.append("delete msg")
            else:
                actions.append("set mailbox of msg to destMailbox")

        repeat_block = _bulk_repeat_block(
            account=account if source_mailbox is not None else None,
            source_mailbox=source_mailbox,
            actions=actions,
            counter_var="updateCount",
        )

        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        # Set up destMailbox at script level when moving; the actions list
        # references it inside the loop.
        dest_setup = ""
        if destination_mailbox is not None:
            account_clause = applescript_account_clause(cast(str, account))
            mb_safe = escape_applescript_string(sanitize_input(destination_mailbox))
            dest_setup = (
                f'set accountRef to {account_clause}\n'
                f'            set destMailbox to mailbox "{mb_safe}" of accountRef'
            )

        script = f"""
        tell application "Mail"
            {dest_setup}
            set idList to {{{id_list}}}
            set updateCount to 0

{repeat_block}

            return updateCount
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
        *,
        account: str | None = None,
        source_mailbox: str | None = None,
    ) -> int:
        """
        Delete messages (always moves to the account's Trash mailbox).

        Args:
            message_ids: List of message IDs to delete
            permanent: Reserved; currently a no-op. Mail.app's AppleScript
                dictionary exposes no path to permanent-delete that bypasses
                Trash — see issue #111. Passing True emits a
                DeprecationWarning so callers see the discrepancy clearly
                rather than silently relying on absent behavior.
            skip_bulk_check: If False, enforce bulk operation limits
            account: Optional account name (or UUID); see `source_mailbox`.
            source_mailbox: Optional source mailbox name. When provided
                together with `account`, the AppleScript narrows the scan
                to that single mailbox — O(N) instead of O(N × M × K).
                Either alone raises ValueError.

        Returns:
            Number of messages deleted (moved to Trash)

        Raises:
            ValueError: If bulk check fails, or if exactly one of
                `account`/`source_mailbox` is given.
        """
        if not message_ids:
            return 0

        # `permanent` was originally meant to bypass Trash, but empirical
        # probing of Mail.app's AppleScript surface (issue #111) found no
        # primitive that can permanently-delete:
        #   - `delete msg` always moves to the account's Trash
        #   - A second `delete` on a trashed message is a no-op
        #   - There is no `empty trash` command in the dictionary
        # Until / unless that changes, the parameter is reserved. Surface
        # the gap loudly so MCP clients don't quietly trust a ghost knob.
        if permanent:
            warnings.warn(
                "delete_messages(permanent=True) currently behaves "
                "identically to permanent=False; Mail.app's AppleScript "
                "dictionary does not expose a way to bypass Trash. "
                "Messages are moved to the account's Trash mailbox in "
                "both cases. See issue #111.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Safety check for bulk operations
        if not skip_bulk_check and len(message_ids) > 100:
            raise ValueError(
                f"Too many messages for bulk delete ({len(message_ids)}). "
                "Maximum is 100 without skip_bulk_check=True"
            )

        id_list = ", ".join(
            f'"{escape_applescript_string(sanitize_input(mid))}"'
            for mid in message_ids
        )

        repeat_block = _bulk_repeat_block(
            account=account,
            source_mailbox=source_mailbox,
            actions=["delete msg"],
            counter_var="deleteCount",
        )

        script = f"""
        tell application "Mail"
            set idList to {{{id_list}}}
            set deleteCount to 0

{repeat_block}

            return deleteCount
        end tell
        """

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def get_selected_messages(
        self,
        include_content: bool = True,
        include_attachments: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get messages currently selected in Apple Mail.

        Args:
            include_content: Include message body (default: True)
            include_attachments: Include per-message attachment metadata
                list (name, mime_type, size, downloaded). On AppleScript,
                this can be expensive on cold caches — see #142.

        Returns:
            List of message dicts (same structure as get_message). Empty list if
            no messages are selected.

        Raises:
            MailAppleScriptError: If AppleScript execution fails
        """
        # Single AppleScript pass: enumerate `selection`, build a list of records,
        # and let _wrap_as_json_script emit the NSJSONSerialization epilogue.
        # This is one osascript call regardless of how many messages are
        # selected — N round-trips would cost 100-300ms each.
        content_clause = (
            "set msgContent to content of msg"
            if include_content
            else 'set msgContent to ""'
        )

        if include_attachments:
            attachments_clause = '''
                set attList to {}
                repeat with att in mail attachments of msg
                    set attRecord to {|name|:(name of att), |mime_type|:(MIME type of att), |size|:(file size of att), |downloaded|:(downloaded of att)}
                    set end of attList to attRecord
                end repeat'''
            attachments_field = ", |attachments|:attList"
        else:
            attachments_clause = ""
            attachments_field = ""

        tell_body = f"""
        tell application "Mail"
            set resultData to {{}}
            set sel to selection
            repeat with msg in sel
                {content_clause}{attachments_clause}
                set msgRecord to {{|id|:(id of msg as text), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent{attachments_field}}}
                set end of resultData to msgRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def delete_draft(self, draft_id: str) -> bool:
        """Move a draft to Trash (lifecycle endpoint for cancellation).

        Mail.app's ``delete`` moves the message to the Deleted Messages
        mailbox. Recovery from Trash is technically possible but Mail.app
        no longer treats a trashed draft as editable, so this is
        effectively a one-way discard.

        Args:
            draft_id: Mail.app internal draft id (from ``create_draft``).

        Returns:
            True if a draft with that id was found and trashed.

        Raises:
            MailDraftInvalidIdError: ``draft_id`` failed validation.
            MailDraftNotFoundError: no draft with that id exists.
        """
        _validate_draft_id(draft_id)

        script = f"""
        tell application "Mail"
            set didDelete to false
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        if name of mb contains "Drafts" then
                            try
                                set m to first message of mb whose id is "{draft_id}"
                                delete m
                                set didDelete to true
                                exit repeat
                            end try
                        end if
                    end repeat
                end try
                if didDelete then exit repeat
            end repeat
            if didDelete then
                return "OK"
            else
                return "NOT_FOUND"
            end if
        end tell
        """

        result = self._run_applescript(script).strip()
        if result == "OK":
            return True
        raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")

    def find_message_by_message_id(
        self, rfc5322_message_id: str
    ) -> str | None:
        """Resolve an RFC 5322 Message-ID header to Mail's internal id.

        Used by ``update_draft`` to recover a reply seed from a saved
        draft's ``In-Reply-To`` header (which carries the original
        message's RFC 5322 Message-ID, including the angle brackets).

        Args:
            rfc5322_message_id: e.g. ``<calendar-abc123@google.com>``.
                Angle brackets are accepted; AppleScript's ``message id``
                property stores the value verbatim as it appears on the
                wire.

        Returns:
            Mail's internal numeric id (as a string) of the first
            matching message found, or None if no message with that
            Message-ID exists in any mailbox.
        """
        if not rfc5322_message_id:
            return None
        safe = escape_applescript_string(sanitize_input(rfc5322_message_id))

        script = f"""
        tell application "Mail"
            set foundId to ""
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        try
                            set m to first message of mb whose message id is "{safe}"
                            set foundId to (id of m as text)
                            exit repeat
                        end try
                    end repeat
                end try
                if foundId is not "" then exit repeat
            end repeat
            if foundId is "" then
                return "NOT_FOUND"
            else
                return foundId
            end if
        end tell
        """

        result = self._run_applescript(script).strip()
        if result == "NOT_FOUND" or not result:
            return None
        return result

    def get_draft_state(self, draft_id: str) -> dict[str, Any]:
        """Read recipients, subject, body, threading headers, and
        attachment names from a saved draft.

        Used by ``update_draft`` to merge the caller's overrides with
        the draft's current state before delete-and-recreate.

        Iterates Drafts mailboxes manually (rather than `whose id is`)
        because newly-created drafts can take a moment to be queryable
        via whose-clause; iteration is reliable and Drafts mailboxes
        are typically small.

        Returns:
            ``{
                "draft_id": "...",
                "to":  [...email...],
                "cc":  [...email...],
                "bcc": [...email...],
                "subject": "...",
                "body": "...",
                "in_reply_to": "<msg-id>" | "",
                "references": "<msg-id> ..." | "",
                "attachment_names": ["foo.pdf", ...],
            }``

        Raises:
            MailDraftInvalidIdError: ``draft_id`` failed validation.
            MailDraftNotFoundError: no draft with that id exists.
        """
        _validate_draft_id(draft_id)

        tell_body = f"""
        tell application "Mail"
            set targetId to "{draft_id}"
            set foundDraft to missing value
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        if name of mb contains "Drafts" then
                            repeat with d in messages of mb
                                if (id of d as text) is targetId then
                                    set foundDraft to d
                                    exit repeat
                                end if
                            end repeat
                        end if
                        if foundDraft is not missing value then exit repeat
                    end repeat
                end try
                if foundDraft is not missing value then exit repeat
            end repeat

            if foundDraft is missing value then
                set resultData to {{|found|:false}}
            else
                set toList to {{}}
                try
                    repeat with r in to recipients of foundDraft
                        set end of toList to (address of r)
                    end repeat
                end try
                set ccList to {{}}
                try
                    repeat with r in cc recipients of foundDraft
                        set end of ccList to (address of r)
                    end repeat
                end try
                set bccList to {{}}
                try
                    repeat with r in bcc recipients of foundDraft
                        set end of bccList to (address of r)
                    end repeat
                end try

                set inReplyTo to ""
                set refs to ""
                try
                    repeat with h in headers of foundDraft
                        set hname to (name of h)
                        if hname is "In-Reply-To" then set inReplyTo to (content of h)
                        if hname is "References" then set refs to (content of h)
                    end repeat
                end try

                set attNames to {{}}
                try
                    repeat with a in mail attachments of foundDraft
                        try
                            set end of attNames to (name of a)
                        end try
                    end repeat
                end try

                set draftSubject to ""
                try
                    set draftSubject to (subject of foundDraft)
                end try
                set draftBody to ""
                try
                    set draftBody to (content of foundDraft)
                end try

                set resultData to {{|found|:true, |draft_id|:targetId, |to|:toList, |cc|:ccList, |bcc|:bccList, |subject|:draftSubject, |body|:draftBody, |in_reply_to|:inReplyTo, |references|:refs, |attachment_names|:attNames}}
            end if
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        raw = self._run_applescript(script)
        data = parse_applescript_json(raw)
        if not isinstance(data, dict) or not data.get("found"):
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")
        # Drop the internal flag from the user-visible payload.
        data.pop("found", None)
        return cast(dict[str, Any], data)

    def create_draft(
        self,
        *,
        seed: str = "new",
        seed_id: str | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        body: str = "",
        attachment_paths: list[Path] | None = None,
        reply_all: bool = False,
        from_account: str | None = None,
        send_now: bool = False,
    ) -> dict[str, str]:
        """Create a draft (fresh, reply, or forward). Optionally send.

        Args:
            seed: ``"new"``, ``"reply"``, or ``"forward"``.
            seed_id: Mail's internal id of the message to reply/forward.
                Required when ``seed != "new"``.
            to/cc/bcc: Recipient lists. For reply/forward, ``None`` keeps
                Mail's auto-derived recipients; an empty list explicitly
                clears that group; a populated list replaces.
            subject: Subject. For ``seed="new"`` this is required by the
                caller. For reply/forward, ``None`` keeps Mail's
                auto-derived ``Re:``/``Fwd:`` prefix; non-None overrides.
            body: Body text. For reply/forward, prepended above the
                auto-quoted original (``body + "\\n\\n" + auto-content``).
            attachment_paths: List of file paths. Each must exist.
            reply_all: For ``seed="reply"`` only — use ``reply to all``.
            from_account: Mail.app account name or UUID; ``None`` uses
                Mail's default sender for the seed message.
            send_now: ``False`` saves as draft and returns
                ``{"draft_id": ...}``. ``True`` sends and returns
                ``{"draft_id": "", "sent_message_id": ""}`` (sent_message_id
                is empty on this version — recovering the just-sent message
                across IMAP sync is unreliable).

        Returns:
            ``{"draft_id": <persisted-id>, "sent_message_id": <id-or-empty>}``.

        Raises:
            ValueError: invalid seed or missing required fields.
            MailAccountNotFoundError: ``from_account`` doesn't match.
            MailMessageNotFoundError: ``seed_id`` not found in any mailbox.
            MailAppleScriptError: AppleScript failure.
        """
        if seed not in ("new", "reply", "forward"):
            raise ValueError(f"seed must be 'new', 'reply', or 'forward'; got {seed!r}")
        if seed in ("reply", "forward"):
            if not seed_id:
                raise ValueError(f"seed_id is required for seed={seed!r}")
        else:  # seed == "new"
            if not to:
                raise ValueError("'to' is required when seed='new'")
            if not subject:
                raise ValueError("'subject' is required when seed='new'")

        # Escape user inputs.
        body_safe = escape_applescript_string(sanitize_input(body))
        subject_safe = (
            escape_applescript_string(sanitize_input(subject))
            if subject is not None
            else None
        )
        seed_id_safe = (
            escape_applescript_string(sanitize_input(seed_id))
            if seed_id is not None
            else None
        )

        # Sender clause.
        sender_clause = ""
        if from_account is not None:
            sender_email = self._resolve_account_to_sender(from_account)
            sender_safe = escape_applescript_string(sender_email)
            sender_clause = f'set sender of theMessage to "{sender_safe}"'

        # Recipient blocks: AppleScript fragments that, when included,
        # clear and re-populate that recipient group on `theMessage`.
        def _recipient_block(kind: str, addrs: list[str] | None) -> str:
            if addrs is None:
                return ""  # keep auto-derived
            list_str = ", ".join(
                f'"{escape_applescript_string(a)}"' for a in addrs
            )
            return f"""
                delete (every {kind} recipient of theMessage)
                repeat with addr in {{{list_str}}}
                    make new {kind} recipient at end of {kind} recipients of theMessage with properties {{address:addr}}
                end repeat
            """

        to_block = _recipient_block("to", to)
        cc_block = _recipient_block("cc", cc)
        bcc_block = _recipient_block("bcc", bcc)

        # Attachment block.
        attachment_block = ""
        if attachment_paths:
            for p in attachment_paths:
                pp = Path(p)
                if not pp.is_file():
                    raise FileNotFoundError(f"attachment not found: {pp}")
            paths_safe = ", ".join(
                f'"{escape_applescript_string(str(Path(p).resolve()))}"'
                for p in attachment_paths
            )
            attachment_block = f"""
                repeat with apath in {{{paths_safe}}}
                    tell theMessage to make new attachment with properties {{file name:(POSIX file apath)}} at after last paragraph
                end repeat
            """

        # Subject override (reply/forward only — for new, subject is set
        # via `make new outgoing message ... properties`).
        subject_override = ""
        if seed != "new" and subject is not None:
            subject_override = f'set subject of theMessage to "{subject_safe}"'

        # Body handling differs by seed:
        #
        # - new: body baked into `make new outgoing message` properties.
        # - reply/forward: Mail.app's auto-quoted content is NOT readable
        #   from `content of d` until AFTER save (where the draft becomes
        #   immutable), so true prepending is impossible. Tradeoff:
        #     * non-empty body  -> override Mail's auto-content with user
        #       body (matches existing reply_to_message behavior; loses
        #       inline quote but preserves threading headers).
        #     * empty body      -> leave Mail's auto-content alone (the
        #       quoted-reply default the user gets in Mail.app).
        if seed == "new":
            body_block = ""
        elif body:
            body_block = f'set content of theMessage to "{body_safe}"'
        else:
            body_block = ""

        # Seed-specific creation block.
        if seed == "new":
            creation_block = (
                f'set theMessage to make new outgoing message with properties '
                f'{{subject:"{subject_safe}", content:"{body_safe}", visible:false}}'
            )
        elif seed == "reply":
            verb = "reply to all" if reply_all else "reply"
            # Look up seed across all accounts/mailboxes; reuse the existing
            # message-id lookup pattern.
            creation_block = f"""
                set origMsg to missing value
                repeat with acc in accounts
                    try
                        repeat with mb in mailboxes of acc
                            try
                                set origMsg to first message of mb whose id is "{seed_id_safe}"
                                exit repeat
                            end try
                        end repeat
                    end try
                    if origMsg is not missing value then exit repeat
                end repeat
                if origMsg is missing value then error "SEED_NOT_FOUND"
                set theMessage to {verb} origMsg opening window false
            """
        else:  # forward
            creation_block = f"""
                set origMsg to missing value
                repeat with acc in accounts
                    try
                        repeat with mb in mailboxes of acc
                            try
                                set origMsg to first message of mb whose id is "{seed_id_safe}"
                                exit repeat
                            end try
                        end repeat
                    end try
                    if origMsg is not missing value then exit repeat
                end repeat
                if origMsg is missing value then error "SEED_NOT_FOUND"
                set theMessage to forward origMsg opening window false
            """

        # Terminal block: save (with id-bridging diff) or send.
        if send_now:
            terminal_block = """
                tell theMessage to send
                return "SENT"
            """
        else:
            terminal_block = """
                save theMessage
                delay 0.5

                set newDraftId to ""
                repeat with acc in accounts
                    try
                        repeat with mb in mailboxes of acc
                            if name of mb contains "Drafts" then
                                repeat with d in messages of mb
                                    set candId to (id of d as text)
                                    if candId is not in beforeIds then
                                        set newDraftId to candId
                                        exit repeat
                                    end if
                                end repeat
                            end if
                            if newDraftId is not "" then exit repeat
                        end repeat
                    end try
                    if newDraftId is not "" then exit repeat
                end repeat
                return newDraftId
            """

        # Pre-save snapshot for id diffing (only when saving as draft).
        snapshot_block = ""
        if not send_now:
            snapshot_block = """
                set beforeIds to {}
                repeat with acc in accounts
                    try
                        repeat with mb in mailboxes of acc
                            if name of mb contains "Drafts" then
                                repeat with d in messages of mb
                                    copy (id of d as text) to end of beforeIds
                                end repeat
                            end if
                        end repeat
                    end try
                end repeat
            """

        script = f"""
        tell application "Mail"
            {snapshot_block}

            {creation_block}

            {sender_clause}
            {subject_override}
            {body_block}
            {to_block}
            {cc_block}
            {bcc_block}
            {attachment_block}

            {terminal_block}
        end tell
        """

        try:
            result = self._run_applescript(script).strip()
        except MailAppleScriptError as e:
            if "SEED_NOT_FOUND" in str(e):
                raise MailMessageNotFoundError(
                    f"no message with id {seed_id!r}"
                ) from e
            raise

        if send_now:
            return {"draft_id": "", "sent_message_id": ""}
        return {"draft_id": result, "sent_message_id": ""}

    def extract_draft_attachments(
        self,
        draft_id: str,
        attachment_names: list[str],
        dest_dir: Path,
    ) -> list[Path]:
        """Save each attachment of a draft to disk.

        Used by ``update_draft`` to preserve attachments through the
        delete-and-recreate cycle. Mail.app doesn't expose attachment
        file paths on saved drafts (`file of att` returns an opaque
        reference), so we extract via the ``save`` AppleScript command.

        Each attachment lands in its own ``<dest_dir>/<i>/`` subdirectory
        so filename collisions between attachments don't lose data.
        Original filenames are preserved.

        Args:
            draft_id: Draft to read attachments from.
            attachment_names: Filenames (index-aligned with the draft's
                ``mail attachments`` collection). Caller typically sources
                these from ``get_draft_state(draft_id)["attachment_names"]``.
            dest_dir: Existing directory under which subdirectories are
                created. Caller owns the lifecycle (e.g. tempdir cleanup).

        Returns:
            Paths of the extracted files, in the same order as
            ``attachment_names``. Length equals number of attachments
            actually written; missing entries indicate per-attachment
            extraction failures.

        Raises:
            MailDraftInvalidIdError: ``draft_id`` failed validation.
            MailDraftNotFoundError: no draft with that id exists.
            FileNotFoundError: ``dest_dir`` does not exist.
        """
        _validate_draft_id(draft_id)
        dest_dir = Path(dest_dir)
        if not dest_dir.is_dir():
            raise FileNotFoundError(f"dest_dir does not exist: {dest_dir}")
        if not attachment_names:
            return []

        # Pre-create per-attachment subdirectories on the Python side so
        # the AppleScript only has to do `save att in (POSIX file <path>)`.
        target_paths: list[Path] = []
        for i, name in enumerate(attachment_names):
            subdir = dest_dir / str(i)
            subdir.mkdir(parents=True, exist_ok=True)
            target_paths.append(subdir / name)

        targets_safe = ", ".join(
            f'"{escape_applescript_string(str(p.resolve()))}"'
            for p in target_paths
        )

        script = f"""
        tell application "Mail"
            set targetId to "{draft_id}"
            set foundDraft to missing value
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        if name of mb contains "Drafts" then
                            repeat with d in messages of mb
                                if (id of d as text) is targetId then
                                    set foundDraft to d
                                    exit repeat
                                end if
                            end repeat
                        end if
                        if foundDraft is not missing value then exit repeat
                    end repeat
                end try
                if foundDraft is not missing value then exit repeat
            end repeat
            if foundDraft is missing value then return "ERR_NOT_FOUND"

            set targetPaths to {{{targets_safe}}}
            set atts to mail attachments of foundDraft
            set saved to 0
            set total to count of atts
            if total > (count of targetPaths) then set total to (count of targetPaths)
            repeat with i from 1 to total
                set a to item i of atts
                set tp to item i of targetPaths
                try
                    save a in (POSIX file tp)
                    set saved to saved + 1
                end try
            end repeat
            return saved as text
        end tell
        """

        result = self._run_applescript(script).strip()
        if result == "ERR_NOT_FOUND":
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")

        # Return only the paths that actually got files written.
        return [p for p in target_paths if p.is_file()]
