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
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any, cast

from imapclient.exceptions import IMAPClientError, LoginError

from .drafts import _validate_draft_id
from .exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftNotFoundError,
    MailImapMoveUnsupportedError,
    MailImapRequiredError,
    MailImapTrashNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotEmptyError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
    MailRuleNotFoundError,
    MailUnsupportedGmailSystemLabelError,
    MailUnsupportedRuleActionError,
)
from .imap_connector import ImapConnectionPool, ImapConnector
from .keychain import get_imap_password
from .utils import (
    applescript_account_clause,
    escape_applescript_string,
    get_flag_index,
    parse_applescript_json,
    sanitize_filename,
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
    MailImapMoveUnsupportedError,
    MailImapTrashNotFoundError,
)

logger = logging.getLogger(__name__)

# Strict ISO 8601 YYYY-MM-DD — search_messages's date_from/date_to filters
# reject anything else to prevent AppleScript injection via the date clause.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now() -> _datetime:
    """Indirection for monkeypatching in tests (#230). Returns tz-aware
    local time so comparison with IMAP envelope dates (also tz-aware) is
    well-defined."""
    return _datetime.now().astimezone()


def _construct_as_date_var(var: str, year: int, month: int, day: int) -> str:
    """Emit AppleScript that constructs an AS date object at midnight (local
    time) on the given (year, month, day).

    Why this exists: AppleScript's `date "YYYY-MM-DD"` literal does NOT
    parse ISO dates — `date "2026-05-28"` evaluates to year-12196, silently
    breaking any filter that depends on it. The property-setter pattern
    below is locale-independent and gives exactly midnight on the target
    date.

    The leading `set day of var to 1` is a defense against current-date
    quirks: if `current date` is e.g. 2026-01-31 and we immediately
    `set month of var to 2`, AppleScript would try to roll into Feb 31 and
    misbehave. Resetting day to 1 first, then year/month/day in that
    order, avoids all such edge cases. (#242)
    """
    indent = "\n            "
    return indent.join([
        f"set {var} to current date",
        f"set day of {var} to 1",
        f"set year of {var} to {year}",
        f"set month of {var} to {month}",
        f"set day of {var} to {day}",
        f"set time of {var} to 0",
    ])


def _build_date_filter_clauses(
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[str]]:
    """Build `(preamble, in_loop_clauses)` for the `date_from`/`date_to`
    AppleScript filters.

    The preamble constructs AS date objects via property setters and
    assigns them to `dateFromVar` / `dateToExclVar`. The loop clauses
    reference those variables (cheap comparison per message, no per-iter
    re-parsing of an ISO string).

    Both filters compose by intersection (any matching clause skips the
    message). Returns `("", [])` when neither bound is set.

    Note: the `received_within_hours` filter is NOT handled here — it's a
    short-circuit that lives outside the per-message filter block. See
    `_build_received_within_hours_short_circuit`.
    """
    preamble_parts: list[str] = []
    clauses: list[str] = []

    if date_from is not None:
        if not _ISO_DATE_RE.match(date_from):
            raise ValueError(
                f"date_from must be ISO 8601 YYYY-MM-DD, got: {date_from!r}"
            )
        d = _date.fromisoformat(date_from)
        preamble_parts.append(
            _construct_as_date_var("dateFromVar", d.year, d.month, d.day)
        )
        clauses.append(
            "if (date received of msg) < dateFromVar then set includeThis to false"
        )

    if date_to is not None:
        if not _ISO_DATE_RE.match(date_to):
            raise ValueError(
                f"date_to must be ISO 8601 YYYY-MM-DD, got: {date_to!r}"
            )
        # Upper bound is exclusive of the day AFTER date_to, so the full
        # day of date_to is included.
        excl = _date.fromisoformat(date_to) + _timedelta(days=1)
        preamble_parts.append(
            _construct_as_date_var(
                "dateToExclVar", excl.year, excl.month, excl.day
            )
        )
        clauses.append(
            "if (date received of msg) >= dateToExclVar then set includeThis to false"
        )

    preamble = "\n            ".join(preamble_parts) if preamble_parts else ""
    return preamble, clauses


def _build_received_within_hours_short_circuit(
    received_within_hours: int | None,
) -> tuple[str, str]:
    """Build the AS preamble + exit-clause for `received_within_hours`.

    Returns ``(preamble, exit_clause)``:

    - ``preamble`` is spliced ABOVE the search loop. It hoists the cutoff
      out of the per-iteration cost: ``set cutoffDate to (current date) -
      (N * hours)`` (computed once, not per message).
    - ``exit_clause`` is spliced INTO the loop body, before the per-message
      filter block. It uses ``exit repeat`` rather than the standard
      ``set includeThis to false`` skip. That short-circuit is **only sound
      under newest-first iteration** (#242): once a message has
      `date received < cutoff`, every subsequent message in the loop is
      also older than the cutoff, so we can bail out of the entire scan.
      With the previous oldest-first iteration this `exit repeat` would
      have terminated immediately on the first (oldest) message and
      returned nothing.

    Both strings are empty when ``received_within_hours`` is None — splicing
    them in is a no-op at script generation.
    """
    if received_within_hours is None:
        return "", ""
    if not isinstance(received_within_hours, int) or received_within_hours <= 0:
        raise ValueError(
            f"received_within_hours must be > 0, got: {received_within_hours!r}"
        )
    preamble = (
        f"set cutoffDate to (current date) - ({received_within_hours} * hours)"
    )
    exit_clause = (
        "if (date received of msg) < cutoffDate then exit repeat"
    )
    return preamble, exit_clause


def _compute_attachment_save_targets(
    attachment_names: list[str],
    save_directory: Path,
    attachment_indices: list[int] | None,
) -> list[tuple[int, Path]]:
    """Map selected attachments to sanitized, contained target paths.

    Returns ``(one_based_attachment_index, target_path)`` pairs in selection
    order. The attachment name originates from the email and is fully
    attacker-controlled (``name of att``), so it must never be concatenated
    into a filesystem path unsanitized — a ``../`` or absolute name would let
    the write escape ``save_directory`` (path traversal → arbitrary file
    write). Each name is reduced to a safe basename via
    :func:`sanitize_filename`; colliding basenames are de-duplicated with the
    attachment index so two attachments can't silently overwrite each other.
    Any target that would still escape ``save_directory`` after resolution is
    dropped defensively.

    ``save_directory`` must already be resolved by the caller.
    """
    if attachment_indices is not None:
        wanted = [
            (i, attachment_names[i])
            for i in attachment_indices
            if 0 <= i < len(attachment_names)
        ]
    else:
        wanted = list(enumerate(attachment_names))

    targets: list[tuple[int, Path]] = []
    used: set[str] = set()
    for idx, name in wanted:
        safe = sanitize_filename(name)
        if safe in used:
            safe = f"{idx}_{safe}"
        used.add(safe)
        target = (save_directory / safe).resolve()
        # sanitize_filename already strips separators and "..", so this can
        # only fail if that contract is ever broken — keep it as a hard gate.
        if target == save_directory or not target.is_relative_to(save_directory):
            continue
        targets.append((idx + 1, target))  # AppleScript indexing is 1-based
    return targets


def _compute_draft_extract_targets(
    attachment_names: list[str], dest_dir: Path
) -> list[Path]:
    """Sanitized, contained per-attachment target paths under ``dest_dir/<i>/``.

    Each attachment name is attacker-influenced (it can originate from a
    forwarded message's MIME filename), so it is reduced to a safe basename
    via :func:`sanitize_filename` before being joined under its index
    subdirectory. The subdir-per-index scheme isolates basename collisions;
    sanitization stops a ``..``/absolute name from escaping ``dest_dir`` once
    the path is resolved (``Path.resolve`` collapses ``..``). Returns one
    resolved path per name, in order.
    """
    dest_resolved = dest_dir.resolve()
    targets: list[Path] = []
    for i, name in enumerate(attachment_names):
        target = (dest_dir / str(i) / sanitize_filename(name)).resolve()
        if not target.is_relative_to(dest_resolved):
            # Unreachable while sanitize_filename strips separators and "..";
            # kept as a hard gate against a future regression in that contract.
            raise ValueError(f"draft attachment {name!r} escapes dest_dir")
        targets.append(target)
    return targets


def _filter_imap_results_to_cutoff(
    messages: list[dict[str, Any]], cutoff_dt: _datetime
) -> list[dict[str, Any]]:
    """Trim IMAP search results to messages received at-or-after `cutoff_dt`.

    The IMAP path's day-granular SINCE under-filters when the caller wants
    hour precision (e.g., received_within_hours=6 just before midnight
    includes everything since midnight on the previous day). This pass
    enforces hour precision in Python using the ISO 8601 ``date_received``
    that the IMAP connector emits.

    Messages whose ``date_received`` is missing or unparseable are kept
    defensively — better to over-return than to silently drop rows. The
    AS path doesn't need this helper because its embedded
    ``(current date) - (N * hours)`` clause is already hour-precise.
    """
    if cutoff_dt.tzinfo is None:
        cutoff_dt = cutoff_dt.astimezone()
    kept: list[dict[str, Any]] = []
    for m in messages:
        raw = m.get("date_received")
        if not raw:
            kept.append(m)
            continue
        try:
            parsed = _datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            kept.append(m)
            continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        if parsed >= cutoff_dt:
            kept.append(m)
    return kept

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


# Shared AppleScript handlers for mailbox lookup (#247).
#
# Mail.app's `mailboxes of account` returns a FLAT list of every mailbox
# belonging to the account — each one has a leaf `name` (no slash separators)
# and a `container` reference (a mailbox, a container class, or
# `missing value` at the top). Direct reference (`mailbox "X" of acctRef`)
# only resolves canonical mailboxes like INBOX and Mail.app system names;
# Gmail custom labels, nested folders, and most user-created hierarchies
# return error -1728. The reliable resolution pattern is:
#
#   1. Iterate `mailboxes of acctRef` once.
#   2. For each candidate, build its full slash-separated path via the
#      container chain (`buildMailboxPath`).
#   3. Match either by leaf `name` (single-component input) or by computed
#      full path (slash-bearing input).
#
# The class check on `container` accepts both `mailbox` and `container`
# (Gmail's [Gmail] folder is `container` class, not `mailbox`) and stops
# at the account boundary so paths don't include the account name.
#
# Empirically verified against a real Gmail account: this handler resolves
# Gmail's `Important` label (30k messages, previously unreachable via
# direct reference) and the full path `[Gmail]/Important` to the same
# mailbox.
_MAILBOX_RESOLVER_HANDLERS = '''using terms from application "Mail"
    on buildMailboxPath(mb)
        set parts to {name of mb}
        set current to mb
        repeat
            try
                set parentRef to container of current
                if parentRef is missing value then exit repeat
                set parentClass to class of parentRef
                if parentClass is not mailbox and parentClass is not container then exit repeat
                set parts to {(name of parentRef)} & parts
                set current to parentRef
            on error
                exit repeat
            end try
        end repeat
        set tid to AppleScript's text item delimiters
        set AppleScript's text item delimiters to "/"
        set fullPath to parts as text
        set AppleScript's text item delimiters to tid
        return fullPath
    end buildMailboxPath

    on resolveMailbox(acctRef, targetPath)
        if targetPath is "" then error "mailbox path is empty"
        set hasSlash to (targetPath contains "/")
        repeat with mb in (mailboxes of acctRef)
            if hasSlash then
                if my buildMailboxPath(mb) is targetPath then return mb
            else
                if (name of mb) is targetPath then return mb
            end if
        end repeat
        error "mailbox not found: " & targetPath
    end resolveMailbox

    on collectMailboxesWithPaths(acctRef)
        set results to {}
        repeat with mb in (mailboxes of acctRef)
            set mbName to name of mb
            set mbPath to my buildMailboxPath(mb)
            set mbUnread to unread count of mb
            if mbUnread is missing value then set mbUnread to 0
            set rec to {|name|:mbName, |path|:mbPath, |unread_count|:mbUnread}
            set end of results to rec
        end repeat
        return results
    end collectMailboxesWithPaths
end using terms from
'''


def _wrap_with_timeout(body: str, *, timeout: int) -> str:
    """Wrap an AppleScript tell-block in `with timeout … end timeout`.

    Threads the connector's configured timeout into the script so Mail's
    default 60s AppleEvent timeout does not fire before the subprocess-level
    kill timer — see issues #227 (JSON paths) and #233 (the non-JSON mutation
    paths). Without this, server-bound operations on slow accounts (Exchange/
    EWS) can trip `AppleEvent timed out (-1712)` and the user's
    ``AppleMailConnector(timeout=N)`` knob silently doesn't apply.

    The `body` must NOT contain top-level `use` statements or handler
    definitions — AppleScript forbids both inside a `with timeout` block.
    Keep handlers (e.g. ``_MAILBOX_RESOLVER_HANDLERS``) OUTSIDE this wrapper,
    prepended to the wrapped result by the caller.

    Args:
        body: AppleScript source (typically a `tell application "Mail"` block).
        timeout: AppleEvent timeout in seconds. Callers pass ``self.timeout``.

    Returns:
        The body wrapped in a `with timeout` block.
    """
    return f"with timeout of {timeout} seconds\n{body}\nend timeout\n"


def _wrap_as_json_script(
    body: str, *, timeout: int, handlers: str = ""
) -> str:
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
      - Optionally inserts top-level `handlers` (e.g. ``_MAILBOX_RESOLVER_HANDLERS``)
        between the `use` statements and the `with timeout` block. Handlers
        must be defined at script top level (outside any `tell` block) so
        the body inside the tell can call them with ``my handlerName(...)``.
      - Wraps the body in `with timeout of {timeout} seconds ... end timeout`
        so Mail's default 60 s AppleEvent timeout does not fire before the
        connector's subprocess timeout — see issue #227. Without this,
        per-message property fetches on Exchange/EWS mailboxes (server-
        bound, not local) trip `AppleEvent timed out (-1712)` and leave the
        scripting bridge in `Connection is invalid (-609)` for ~30 s.
      - After the tell block, serializes `resultData` via NSJSONSerialization
        and returns the resulting NSString as text. The serializer runs in
        the ASObjC bridge (no AppleEvent) so it is intentionally OUTSIDE the
        timeout block — but `resultData` is still visible because AppleScript
        `with timeout` is a control construct, not a scope.

    Args:
        body: AppleScript tell-block source setting `resultData`.
        timeout: AppleEvent timeout in seconds for the wrapped tell block.
            Callers should pass ``self.timeout`` so the in-script timeout
            matches the subprocess-level kill timer.
        handlers: Optional AppleScript handler block to inject at script
            top level (before `with timeout`). Empty string = no handlers.

    Returns:
        Full AppleScript source ready for osascript.
    """
    handlers_block = f"{handlers}\n" if handlers else ""
    return (
        'use framework "Foundation"\n'
        "use scripting additions\n"
        "\n"
        f"{handlers_block}"
        f"{_wrap_with_timeout(body, timeout=timeout)}"
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
            f'            set sourceMb to my resolveMailbox({account_clause}, "{mb_safe}")\n'
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

        if isinstance(exc, MailImapMoveUnsupportedError):
            # Capability gap is permanent for that server; opening the
            # 30s breaker would skip IMAP for read paths that work fine.
            logger.debug(
                "IMAP server for %s lacks MOVE/UIDPLUS; using AppleScript "
                "for the move-only patch",
                account,
            )
            return

        if isinstance(exc, MailImapTrashNotFoundError):
            # Same reasoning as above — Trash discovery failing once
            # means it'll fail every time for this server, so opening
            # the breaker would only hurt unrelated read paths.
            logger.debug(
                "IMAP server for %s has no discoverable Trash folder; "
                "using AppleScript for delete_messages",
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
              - name: account preferences-sidebar label (e.g. "Gmail")
              - full_name: per-message display name used in outgoing
                "From" headers (e.g. "Alice Smith"), or None if no
                full name is configured for the account
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
                set accFullName to full name of acc
                if accFullName is missing value then set accFullName to ""
                set accRecord to {|id|:(id of acc as text), |name|:(name of acc), |full_name|:accFullName, |email_addresses|:accEmails, |account_type|:((account type of acc) as text), |enabled|:(enabled of acc)}
                set end of resultData to accRecord
            end repeat
        end tell
        """

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        result = self._run_applescript(script)
        accounts = cast(list[dict[str, Any]], parse_applescript_json(result))
        # Normalize empty-string full_name to None so callers don't have
        # to distinguish two "no display name" representations.
        for acc in accounts:
            if not (acc.get("full_name") or "").strip():
                acc["full_name"] = None
        return accounts

    def _resolve_account_to_sender(self, account: str) -> str:
        """Resolve an account name or UUID to a sender string for the
        AppleScript ``sender`` property.

        Returns ``"Display Name <email>"`` when the account has a
        ``full_name`` configured (#158), or bare ``email`` as a graceful
        fallback when no full name is set. The Display-Name form is what
        recipients see in their inbox's From column.

        Used by the draft lifecycle (``create_draft`` / ``update_draft``)
        per #155. Accepts either name or UUID, matching the convention on
        ``list_mailboxes``, ``search_messages``, etc.

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
                email = cast(str, emails[0])
                full_name = (acc.get("full_name") or "").strip()
                if full_name:
                    return f"{full_name} <{email}>"
                return email
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

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
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
        script = _wrap_with_timeout(
            f'tell application "Mail" to '
            f"set enabled of rule {rule_index} to {enabled_str}",
            timeout=self.timeout,
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
                f"set move message of newRule to "
                f'(my resolveMailbox({acct_clause}, "{mb_safe}"))'
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
                f"set copy message of newRule to "
                f'(my resolveMailbox({acct_clause}, "{mb_safe}"))'
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
        script = f"{_MAILBOX_RESOLVER_HANDLERS}" + _wrap_with_timeout(
            f'tell application "Mail"\n{body}\nend tell',
            timeout=self.timeout,
        )
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
        script = f"{_MAILBOX_RESOLVER_HANDLERS}" + _wrap_with_timeout(
            'tell application "Mail"\n'
            + "\n".join(body_parts)
            + "\nend tell",
            timeout=self.timeout,
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
        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
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
        script = _wrap_with_timeout(
            f'tell application "Mail"\n'
            f"    set deletedName to name of rule {rule_index}\n"
            f"    delete rule {rule_index}\n"
            f"    return deletedName\n"
            f"end tell",
            timeout=self.timeout,
        )
        return self._run_applescript(script)

    def list_mailboxes(self, account: str) -> list[dict[str, Any]]:
        """List all mailboxes for an account.

        Args:
            account: Account name (or UUID).

        Returns:
            List of dicts with keys ``name`` (leaf), ``path`` (full
            slash-separated path from account root — usable directly in
            ``search_messages.mailbox`` / ``move_messages.destination_mailbox``
            for nested addressing), and ``unread_count``.

            Mail.app exposes mailboxes as a flat list with leaf names; the
            ``path`` field is computed by walking each mailbox's container
            chain (see ``_MAILBOX_RESOLVER_HANDLERS``). For top-level
            mailboxes, ``name == path``. For nested mailboxes (Gmail labels
            under ``[Gmail]``, user-created folder hierarchies, etc.) the
            two differ — ``path`` is what ``resolveMailbox`` matches against
            for unambiguous addressing.

        Raises:
            MailAccountNotFoundError: If account doesn't exist.
        """
        account_clause = applescript_account_clause(account)

        tell_body = f'''
        tell application "Mail"
            set accountRef to {account_clause}
            set resultData to my collectMailboxesWithPaths(accountRef)
        end tell
        '''

        script = _wrap_as_json_script(
            tell_body,
            timeout=self.timeout,
            handlers=_MAILBOX_RESOLVER_HANDLERS,
        )
        result = self._run_applescript(script)
        return cast(list[dict[str, Any]], parse_applescript_json(result))

    def _get_imap_password_with_fallback(
        self, account: str, email: str
    ) -> str:
        """Look up the IMAP Keychain password, retrying with the alternative
        account-identifier form (name ↔ UUID) on a NotFound miss. (#243)

        Keychain entries are written by ``setup-imap`` keyed on whatever
        string the user typed — typically the Mail.app account NAME.
        Callers (including the MCP layer) may legitimately pass either the
        name or the UUID per the documented stability story on
        ``search_messages.account``. This wrapper bridges the gap: try the
        caller-supplied form first; on NotFound, resolve UUID↔name via
        ``list_accounts()`` and retry. Other Keychain errors (AccessDenied,
        generic Keychain errors) are NOT retried — they're explicit signals
        from macOS and falling back would mask them.
        """
        try:
            return get_imap_password(account, email)
        except MailKeychainEntryNotFoundError:
            alt = self._alternative_account_identifier(account)
            if alt is None:
                raise
            return get_imap_password(alt, email)

    def _alternative_account_identifier(self, account: str) -> str | None:
        """Given a Mail.app account name OR UUID, return the other form.

        Returns ``None`` if the input doesn't match any account or if the
        account list can't be retrieved. Used by
        ``_get_imap_password_with_fallback`` to bridge the
        name-vs-UUID Keychain key mismatch.
        """
        try:
            accounts = self.list_accounts()
        except Exception:
            return None
        for acc in accounts:
            name = acc.get("name")
            uid = acc.get("id")
            if name == account and uid:
                return cast(str, uid)
            if uid == account and name:
                return cast(str, name)
        return None

    def _resolve_imap_config(self, account: str) -> tuple[str, int, str]:
        """Query Mail.app for the IMAP connection details of an account.

        Args:
            account: Mail.app account name (e.g. "iCloud", "Gmail").

        Returns:
            Tuple of (host, port, email). `email` is Mail.app's `user name`
            property if non-empty, else falls back to the first entry of
            `email addresses`.

            `user name` is the credential Mail.app itself sends as the IMAP
            LOGIN — it's the source of truth. `email addresses` is the SMTP
            From list, which usually overlaps with `user name` for Gmail /
            Yahoo / @icloud.com-primary accounts but diverges for iCloud
            accounts whose Apple ID is on a custom domain (Apple's "Custom
            Email Domain" setup): there `email_addresses[0]` is an SMTP-
            only From alias that the IMAP server rejects with
            AUTHENTICATIONFAILED, while `user name` (the Apple ID itself)
            is what the server actually accepts. Preferring `user name`
            matches Mail.app's own behavior in every configuration we've
            seen. (#201)

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
        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        parsed = cast(dict[str, Any], parse_applescript_json(raw))
        email_addresses = cast(list[str], parsed.get("email_addresses") or [])
        user_name = cast(str, parsed.get("user_name") or "")
        email = user_name or (email_addresses[0] if email_addresses else "")
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
        password = self._get_imap_password_with_fallback(account, email)
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
        received_within_hours: int | None = None,
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

        # #230: received_within_hours is a hour-granular relative cutoff that
        # composes with date_from. Validate up front (so both IMAP and AS
        # paths see a consistent error), then desugar to date_from for the
        # IMAP path's day-granular SINCE pre-filter and post-filter results
        # to enforce hour precision. The AS path receives the raw param and
        # embeds an (current date) - (N * hours) clause that Mail.app
        # evaluates server-side — no post-filter needed there.
        cutoff_dt: _datetime | None = None
        if received_within_hours is not None:
            if not isinstance(received_within_hours, int) or received_within_hours <= 0:
                raise ValueError(
                    f"received_within_hours must be > 0, got: {received_within_hours!r}"
                )
            cutoff_dt = _now() - _timedelta(hours=received_within_hours)
            cutoff_date_iso = cutoff_dt.date().isoformat()
            if date_from is None or cutoff_date_iso > date_from:
                date_from = cutoff_date_iso

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
                if cutoff_dt is not None:
                    result = _filter_imap_results_to_cutoff(result, cutoff_dt)
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
                received_within_hours=received_within_hours,
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
        received_within_hours: int | None = None,
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

        date_preamble, date_filter_clauses = _build_date_filter_clauses(
            date_from, date_to
        )
        filter_checks.extend(date_filter_clauses)

        # `received_within_hours` is handled OUTSIDE the per-message filter
        # block (#242): a `set cutoffDate to ...` preamble is hoisted above
        # the loop (computed once, not per message), and an `if ... then
        # exit repeat` short-circuit is spliced into the loop body before
        # the filter block. This is sound only because the loop iterates
        # newest-first (see the `repeat with i from 1 to total` comment
        # below) — once a message has date < cutoff, every subsequent
        # iteration's message is also < cutoff, so we can bail out of the
        # entire scan.
        cutoff_preamble, cutoff_exit_clause = (
            _build_received_within_hours_short_circuit(received_within_hours)
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

        # Per-match limit short-circuits the loop once enough hits accrue.
        # Iteration is newest-first (#242): Mail.app exposes `item 1 of
        # msgs` as the newest message and `item total of msgs` as the
        # oldest, so the `repeat with i from 1 to total` form below visits
        # newest first and naturally bounds the scan to ~limit iterations
        # for limit-bounded queries.
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
            set mailboxRef to my resolveMailbox(accountRef, "{mailbox_safe}")
            set msgs to messages of mailboxRef
            set total to count of msgs
            {date_preamble}
            {cutoff_preamble}

            set resultData to {{}}
            set matchCount to 0
            repeat with i from 1 to total
                if matchCount >= {effective_limit} then exit repeat
                set msg to item i of msgs
                {cutoff_exit_clause}
                set includeThis to true
                {filter_block}
                if includeThis then{attachments_clause}
                    set msgRecord to {{|id|:(id of msg as text), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg){attachments_field}}}
                    set end of resultData to msgRecord
                    set matchCount to matchCount + 1
                end if
            end repeat
        end tell
        '''

        script = _wrap_as_json_script(
            tell_body,
            timeout=self.timeout,
            handlers=_MAILBOX_RESOLVER_HANDLERS,
        )
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
        password = self._get_imap_password_with_fallback(account, email)
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
                        set resultData to {{|id|:(id of msg as text), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |sender|:(sender of msg), |date_received|:(date received of msg as text), |read_status|:(read status of msg), |flagged|:(flagged status of msg), |content|:msgContent{attachments_field}}}
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

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
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

        script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
            f"""tell application "Mail"
            set idList to {{{id_list}}}
            set updateCount to 0

{repeat_block}

            return updateCount
        end tell""",
            timeout=self.timeout,
        )

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
        password = self._get_imap_password_with_fallback(account, email)
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

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
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
        password = self._get_imap_password_with_fallback(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.find_thread_members(
            anchor_rfc_message_id=cast(str, anchor["rfc_message_id"]),
            anchor_references=cast(list[str], anchor.get("references") or []),
        )

    def _imap_move_messages(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
        destination_mailbox: str,
    ) -> int:
        """IMAP path for the move-only branch of update_message (#149).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.move_messages. Propagates all fallback-triggering
        exceptions unchanged — the caller (_try_imap_move_only) catches
        and falls back via _IMAP_FALLBACK_EXCS.
        """
        host, port, email = self._resolve_imap_config(account)
        password = self._get_imap_password_with_fallback(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.move_messages(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
            destination_mailbox=destination_mailbox,
        )

    def _try_imap_move_only(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
        destination_mailbox: str,
    ) -> int | None:
        """Attempt the move-only IMAP fast path. Returns the move count
        on success, or None to signal the caller should fall through to
        the AppleScript pass.

        Caller must already have verified this is a move-only patch
        (read_status / flagged / flag_color all None) and that account +
        source_mailbox + destination_mailbox are all provided.
        """
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_move_messages(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
                destination_mailbox=destination_mailbox,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _imap_delete_messages(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
    ) -> int:
        """IMAP path for delete_messages (#150).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.delete_messages (which discovers the Trash folder
        via SPECIAL-USE \\Trash with conventional-name fallback, then
        does UID MOVE / UID COPY+STORE+EXPUNGE). Propagates all
        fallback-triggering exceptions unchanged — the caller
        (_try_imap_delete) catches and falls back via _IMAP_FALLBACK_EXCS.
        """
        host, port, email = self._resolve_imap_config(account)
        password = self._get_imap_password_with_fallback(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.delete_messages(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
        )

    def _try_imap_delete(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
    ) -> int | None:
        """Attempt the IMAP fast path for delete_messages. Returns the
        moved-to-Trash count on success, or None when the caller should
        fall through to the AppleScript pass.

        Caller must already have verified that account + source_mailbox
        are both provided (without source_mailbox, IMAP would have to
        SEARCH every mailbox per Message-ID).
        """
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_delete_messages(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _imap_set_read_status(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
        read: bool,
    ) -> int:
        """IMAP path for the read-status-only branch of update_message (#151).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.set_read_status (\\Seen STORE — base IMAP, no
        capability check). Propagates all fallback-triggering exceptions
        unchanged.
        """
        host, port, email = self._resolve_imap_config(account)
        password = self._get_imap_password_with_fallback(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.set_read_status(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
            read=read,
        )

    def _try_imap_read_only(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
        read: bool,
    ) -> int | None:
        """Attempt the IMAP fast path for the read-status-only branch.
        Returns updated count on success, or None to signal the caller
        should fall through to the AppleScript pass.

        Caller must already have verified this is a read-only patch
        and that account + source_mailbox are both provided.
        """
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_set_read_status(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
                read=read,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _maybe_imap_move_only(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Branch out to the IMAP fast path (#149) when this update_message
        call is a move-only patch with a source_mailbox hint. Returns the
        moved-count on success, or None when the caller should fall
        through to the AppleScript pass.

        Combined patches (move + read/flag) and patches without
        source_mailbox return None unconditionally — those stay on
        AppleScript pending #150 / #151 / #152.
        """
        move_only = (
            destination_mailbox is not None
            and read_status is None
            and flagged is None
            and flag_color is None
        )
        if not move_only or source_mailbox is None:
            return None
        return self._try_imap_move_only(
            message_ids,
            account=cast(str, account),
            source_mailbox=source_mailbox,
            destination_mailbox=cast(str, destination_mailbox),
        )

    def _maybe_imap_read_only(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Branch out to the IMAP fast path (#151) when this update_message
        call is a read-status-only patch with account + source_mailbox.
        Returns the updated count on success, or None when the caller
        should fall through to the AppleScript pass.

        Combined patches (read + move / read + flag) and patches without
        source_mailbox or account return None unconditionally — those
        stay on AppleScript pending #152.
        """
        read_only = (
            read_status is not None
            and destination_mailbox is None
            and flagged is None
            and flag_color is None
        )
        if not read_only or source_mailbox is None or account is None:
            return None
        return self._try_imap_read_only(
            message_ids,
            account=account,
            source_mailbox=source_mailbox,
            read=cast(bool, read_status),
        )

    def _imap_set_flagged_status(
        self,
        *,
        account: str,
        message_ids: list[str],
        source_mailbox: str,
        flagged: bool,
    ) -> int:
        """IMAP path for the flag-only branch of update_message (#152).

        Resolves config and Keychain credentials, then delegates to
        ImapConnector.set_flagged_status (\\Flagged STORE — base IMAP,
        no capability check). Propagates all fallback-triggering
        exceptions unchanged.
        """
        host, port, email = self._resolve_imap_config(account)
        password = self._get_imap_password_with_fallback(account, email)
        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        return imap.set_flagged_status(
            message_ids=message_ids,
            source_mailbox=source_mailbox,
            flagged=flagged,
        )

    def _try_imap_flag_only(
        self,
        message_ids: list[str],
        *,
        account: str,
        source_mailbox: str,
        flagged: bool,
    ) -> int | None:
        """Attempt IMAP fast path for flag-only patch. Returns count
        on success, or None to fall through to AppleScript."""
        if self._imap_breaker_open(account):
            return None
        try:
            result = self._imap_set_flagged_status(
                account=account,
                message_ids=message_ids,
                source_mailbox=source_mailbox,
                flagged=flagged,
            )
            self._imap_clear_breaker(account)
            return result
        except _IMAP_FALLBACK_EXCS as exc:
            self._log_imap_fallback(account, exc)
            return None

    def _maybe_imap_flag_only(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Branch out to the IMAP fast path (#152) when this
        update_message call is a flag-only patch (flagged set, no
        flag_color, no other fields) with account + source_mailbox.

        flag_color requires Mail.app's $MailFlagBit* keywords which
        IMAP can't set; combined patches need multiple actions in one
        AppleScript pass. Both fall through to AppleScript.
        """
        flag_only = (
            flagged is not None
            and flag_color is None
            and read_status is None
            and destination_mailbox is None
        )
        if not flag_only or source_mailbox is None or account is None:
            return None
        return self._try_imap_flag_only(
            message_ids,
            account=account,
            source_mailbox=source_mailbox,
            flagged=cast(bool, flagged),
        )

    @staticmethod
    def _build_flag_actions(
        flagged: bool | None,
        flag_color: str | None,
    ) -> list[str]:
        """Translate the (flagged, flag_color) patch into AppleScript
        action strings. Pulled out of update_message in #174 to keep
        that method below the CC ≤ 20 threshold.

        Order of precedence: ``flagged=False`` always clears regardless
        of color; ``flag_color`` set wins over bare ``flagged=True``;
        bare ``flagged=True`` defaults to red (#185 fix).
        """
        from .utils import get_flag_index, validate_flag_color

        if flagged is False:
            return [
                "set flag index of msg to -1",
                "set flagged status of msg to false",
            ]
        if flag_color is not None:
            if not validate_flag_color(flag_color):
                raise ValueError(f"Invalid flag color: {flag_color}")
            flag_index = get_flag_index(flag_color)
            flagged_status = "true" if flag_color != "none" else "false"
            return [
                f"set flag index of msg to {flag_index}",
                f"set flagged status of msg to {flagged_status}",
            ]
        if flagged is True:
            # No color → default red. flag index 0 (red) sets bare \\Flagged
            # on the IMAP server with no $MailFlagBit* keyword — same state
            # the #152 IMAP fast path produces, ensuring path-independent
            # rendering in Mail.app.
            return [
                f"set flag index of msg to {get_flag_index('red')}",
                "set flagged status of msg to true",
            ]
        return []

    def _try_imap_fast_paths(
        self,
        message_ids: list[str],
        *,
        read_status: bool | None,
        flagged: bool | None,
        flag_color: str | None,
        destination_mailbox: str | None,
        source_mailbox: str | None,
        account: str | None,
    ) -> int | None:
        """Try each per-mutation IMAP fast path in turn (#149/#151/#152).
        Returns the first non-None result, or None if no fast path
        applies (caller falls through to the AppleScript pass).

        The three fast paths' branch conditions are mutually exclusive
        (each requires a single-field patch in its specific field), so
        order doesn't matter functionally — but the historical order
        is preserved for grep-ability against the issue numbers.

        Pulled out of update_message in #174 to keep that function
        below the CC ≤ 20 threshold; #149/#151/#152 each added a
        _maybe_imap_* call + if-check, drifting it from 21 to 24.
        Net effect of this helper: 1 call + 1 if-check at the call
        site instead of 3 of each.
        """
        for fast_path in (
            self._maybe_imap_move_only,
            self._maybe_imap_read_only,
            self._maybe_imap_flag_only,
        ):
            result = fast_path(
                message_ids,
                read_status=read_status,
                flagged=flagged,
                flag_color=flag_color,
                destination_mailbox=destination_mailbox,
                source_mailbox=source_mailbox,
                account=account,
            )
            if result is not None:
                return result
        return None

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

        anchor_script = _wrap_as_json_script(anchor_body, timeout=self.timeout)
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

        candidates_script = _wrap_as_json_script(candidates_body, timeout=self.timeout)
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
                "rfc_message_id": cast(
                    "str | None", anchor.get("rfc_message_id")
                ),
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

        # Drop threading-internal scratch fields from output rows. Per
        # #148 we KEEP rfc_message_id alongside id (dual-emit), so
        # callers can hand it to the IMAP fast paths from #149/#150/
        # #151/#152 even when get_thread fell back to AppleScript.
        for m in thread:
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

        # Enumerate attachment names first so the (attacker-controlled)
        # filename never reaches a filesystem path unsanitized. The leaf
        # names are reduced to safe basenames and joined under the resolved
        # save_directory on the Python side; the AppleScript then saves each
        # selected attachment by index to a precomputed, contained POSIX
        # path. (Concatenating `name of att` into the path inside AppleScript
        # was a path-traversal → arbitrary-file-write vector.)
        attachments = self._get_attachments_applescript(message_id)
        targets = _compute_attachment_save_targets(
            [str(a.get("name", "")) for a in attachments],
            save_directory,
            attachment_indices,
        )
        if not targets:
            return 0

        message_id_safe = escape_applescript_string(sanitize_input(message_id))
        idx_list = ", ".join(str(idx) for idx, _ in targets)
        path_list = ", ".join(
            f'"{escape_applescript_string(str(path))}"' for _, path in targets
        )

        script = _wrap_with_timeout(
            f"""tell application "Mail"
            -- Search all accounts for message
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is "{message_id_safe}"
                        set theAtts to mail attachments of msg
                        set idxList to {{{idx_list}}}
                        set pathList to {{{path_list}}}
                        set saveCount to 0

                        repeat with k from 1 to count of idxList
                            set ai to item k of idxList
                            set tp to item k of pathList
                            try
                                save (item ai of theAtts) in (POSIX file tp)
                                set saveCount to saveCount + 1
                            end try
                        end repeat

                        return saveCount
                    end try
                end repeat
            end repeat

            error "Message not found"
        end tell""",
            timeout=self.timeout,
        )

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

        script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
            f"""tell application "Mail"
            set accountRef to {account_clause}
            set destMailbox to my resolveMailbox(accountRef, "{mailbox_safe}")
            set idList to {{{id_list}}}
            set moveCount to 0

{repeat_block}

            return moveCount
        end tell""",
            timeout=self.timeout,
        )

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

        script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
            f"""tell application "Mail"
            set idList to {{{id_list}}}
            set flagCount to 0

{repeat_block}

            return flagCount
        end tell""",
            timeout=self.timeout,
        )

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
                ``flag_color`` defaults to red (Mail.app's default flag
                color, matching the IMAP fast path's bare ``\\Flagged``
                rendering). ``False`` clears the flag.
            flag_color: Color name (orange, red, yellow, blue, green,
                purple, gray, none). Implies ``flagged=True`` unless
                "none". Validated.
            destination_mailbox: When set, move messages to this mailbox.
                Requires ``account`` (the destination's account).
            account: Account hosting destination (required when
                ``destination_mailbox`` is set). Also used with
                ``source_mailbox`` for narrow-path optimization.
            source_mailbox: Optional source mailbox. With ``account``,
                narrows the AppleScript scan to one mailbox. Required to
                unlock the IMAP fast path on move-only patches (#149) —
                without it, the move runs via AppleScript even when
                IMAP is configured.
            gmail_mode: Use Gmail-specific copy+delete for the move.

        IMAP fast path (#149): when the patch is move-only
        (``destination_mailbox`` is the only field set) and
        ``source_mailbox`` is provided, the move runs server-side via
        IMAP ``UID MOVE`` (RFC 6851), avoiding the AppleScript ``whose
        message id is`` linear scan that costs ~57s on a 47k-message
        mailbox. Combined patches (move + read/flag in one call) stay on
        AppleScript until siblings #150 / #151 / #152 land.

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

        imap_count = self._try_imap_fast_paths(
            message_ids,
            read_status=read_status,
            flagged=flagged,
            flag_color=flag_color,
            destination_mailbox=destination_mailbox,
            source_mailbox=source_mailbox,
            account=account,
        )
        if imap_count is not None:
            return imap_count

        actions: list[str] = []

        if read_status is not None:
            target = "true" if read_status else "false"
            actions.append(f"set read status of msg to {target}")

        actions.extend(self._build_flag_actions(flagged, flag_color))

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
                f'            set destMailbox to my resolveMailbox(accountRef, "{mb_safe}")'
            )

        script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
            f"""tell application "Mail"
            {dest_setup}
            set idList to {{{id_list}}}
            set updateCount to 0

{repeat_block}

            return updateCount
        end tell""",
            timeout=self.timeout,
        )

        result = self._run_applescript(script)
        return int(result) if result.isdigit() else 0

    def update_mailbox(
        self,
        account: str,
        name: str,
        new_name: str | None = None,
        new_parent: str | None = None,
    ) -> bool:
        """Rename and/or re-parent an existing mailbox.

        - **Rename only** (``new_name`` set, ``new_parent`` is ``None``):
          AppleScript's ``set name of mailbox X to "Y"``. Fast, no IMAP
          credentials needed.
        - **Move** (``new_parent`` set): IMAP RENAME with the destination
          path computed from ``new_parent`` + the leaf of ``name``.
          ``new_parent=""`` means move to top-level.
          Requires IMAP credentials in Keychain (#73 opt-in flow).

        At least one of ``new_name`` / ``new_parent`` must be provided.
        Combined ("move and rename") works in one IMAP RENAME.

        Args:
            account: Account name or UUID.
            name: Current mailbox name. Slash-separated for nested
                mailboxes (e.g. ``"Archive/2024"``).
            new_name: Replacement leaf name. ``None`` to keep the current
                leaf. Sanitized via ``sanitize_mailbox_name``.
            new_parent: Destination parent path. ``None`` means keep
                current parent (rename only). ``""`` (empty string) means
                move to top-level. Non-empty string means move under that
                path.

        Returns:
            True on success.

        Raises:
            ValueError: If neither ``new_name`` nor ``new_parent`` was
                provided, or ``new_name`` sanitizes to empty.
            MailUnsupportedGmailSystemLabelError: If the source ``name``
                or the resulting destination is a Gmail system label
                (``[Gmail]`` / ``[Gmail]/...``). Pre-flight refusal —
                no AppleScript or IMAP traffic. See #164.
            MailAccountNotFoundError: If account doesn't exist.
            MailMailboxNotFoundError: If the source mailbox doesn't exist.
            MailImapRequiredError: If a move was requested but no IMAP
                credentials are configured for ``account``.
            MailAppleScriptError: If a rename-only path otherwise fails.
            imapclient.exceptions.IMAPClientError: If a move otherwise
                fails on the IMAP server.
        """
        from .utils import is_gmail_system_label, sanitize_mailbox_name

        if new_name is None and new_parent is None:
            raise ValueError(
                "update_mailbox requires at least one of new_name or new_parent"
            )

        if is_gmail_system_label(name):
            raise MailUnsupportedGmailSystemLabelError(
                f"cannot update Gmail system label {name!r}; Gmail's IMAP "
                f"server does not support normal RENAME for these paths "
                f"(see #164)"
            )

        sanitized_new_name: str | None = None
        if new_name is not None:
            sanitized_new_name = sanitize_mailbox_name(new_name)
            if not sanitized_new_name:
                raise ValueError(f"Invalid new_name: {new_name}")

        # ------------------------------------------------------------------
        # Rename-only path (no parent change) -> AppleScript
        # ------------------------------------------------------------------
        if new_parent is None:
            assert sanitized_new_name is not None  # narrowed by the guard above
            account_clause = applescript_account_clause(account)
            name_safe = escape_applescript_string(sanitize_input(name))
            new_name_safe = escape_applescript_string(sanitized_new_name)

            script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
                f"""tell application "Mail"
                set accountRef to {account_clause}
                try
                    set mb to my resolveMailbox(accountRef, "{name_safe}")
                on error
                    error "MAILBOX_NOT_FOUND"
                end try
                set name of mb to "{new_name_safe}"
                return "success"
            end tell""",
                timeout=self.timeout,
            )

            try:
                result = self._run_applescript(script)
            except MailAppleScriptError as e:
                if "MAILBOX_NOT_FOUND" in str(e):
                    raise MailMailboxNotFoundError(
                        f"mailbox {name!r} not found in account {account!r}"
                    ) from e
                raise
            return result == "success"

        # ------------------------------------------------------------------
        # Move (with optional rename) -> IMAP RENAME
        # ------------------------------------------------------------------
        # Destination path = new_parent + "/" + (new_name or current leaf).
        leaf = sanitized_new_name if sanitized_new_name else name.rsplit("/", 1)[-1]
        if new_parent == "":
            destination = leaf
        else:
            destination = f"{new_parent}/{leaf}"

        if is_gmail_system_label(destination):
            raise MailUnsupportedGmailSystemLabelError(
                f"cannot move mailbox {name!r} to {destination!r}; the "
                f"destination would land in Gmail's system-label namespace "
                f"(see #164)"
            )

        try:
            host, port, email = self._resolve_imap_config(account)
            password = self._get_imap_password_with_fallback(account, email)
        except (
            MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError
        ) as e:
            raise MailImapRequiredError(
                f"moving a mailbox requires IMAP credentials for account "
                f"{account!r}; configure them via the Keychain opt-in flow"
            ) from e

        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        try:
            imap.rename_mailbox(name, destination)
        except IMAPClientError as e:
            # Map "mailbox doesn't exist" to the typed error; let other
            # IMAP errors propagate for the server layer to translate.
            msg = str(e).lower()
            if "no such" in msg or "doesn't exist" in msg or "doesn't exist" in msg:
                raise MailMailboxNotFoundError(
                    f"mailbox {name!r} not found in account {account!r}"
                ) from e
            raise
        return True

    def delete_mailbox(
        self,
        account: str,
        name: str,
        delete_messages: bool = False,
    ) -> int:
        """Delete a mailbox via IMAP DELETE.

        Mail.app's AppleScript ``delete`` command's handler refuses
        mailbox specifiers (verified by probe), so this operation is
        IMAP-only. Requires Keychain credentials per the #73 opt-in
        flow; raises ``MailImapRequiredError`` otherwise.

        Args:
            account: Account name or UUID.
            name: Mailbox name. Slash-separated for nested mailboxes.
            delete_messages: When False (default), refuse if the mailbox
                contains messages. When True, cascade-delete the mailbox
                and its contents.

        Returns:
            Number of messages that existed at delete time (0 for empty
            mailbox; positive when ``delete_messages=True`` cascaded).

        Raises:
            MailUnsupportedGmailSystemLabelError: If ``name`` is a Gmail
                system label (``[Gmail]`` / ``[Gmail]/...``). Pre-flight
                refusal — no IMAP traffic. See #164.
            MailAccountNotFoundError: If account doesn't exist.
            MailMailboxNotFoundError: If the mailbox doesn't exist on
                the IMAP server.
            MailMailboxNotEmptyError: If ``delete_messages=False`` and
                the mailbox is non-empty.
            MailImapRequiredError: If no Keychain credentials.
            imapclient.exceptions.IMAPClientError: Other server-side
                error.
        """
        from .utils import is_gmail_system_label

        if is_gmail_system_label(name):
            raise MailUnsupportedGmailSystemLabelError(
                f"cannot delete Gmail system label {name!r}; Gmail's IMAP "
                f"server does not support DELETE for these paths (see #164)"
            )

        try:
            host, port, email = self._resolve_imap_config(account)
            password = self._get_imap_password_with_fallback(account, email)
        except (
            MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError
        ) as e:
            raise MailImapRequiredError(
                f"deleting a mailbox requires IMAP credentials for account "
                f"{account!r}; configure them via the Keychain opt-in flow"
            ) from e

        imap = ImapConnector(host, port, email, password, pool=self._imap_pool)
        try:
            return imap.delete_mailbox(name, allow_non_empty=delete_messages)
        except ValueError as e:
            # ImapConnector raises ValueError on the non-empty refusal.
            raise MailMailboxNotEmptyError(str(e)) from e
        except IMAPClientError as e:
            msg = str(e).lower()
            if "no such" in msg or "doesn't exist" in msg or "nonexistent" in msg:
                raise MailMailboxNotFoundError(
                    f"mailbox {name!r} not found in account {account!r}"
                ) from e
            raise

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
            script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
                f"""tell application "Mail"
                set accountRef to {account_clause}
                set parentMailbox to my resolveMailbox(accountRef, "{parent_safe}")
                make new mailbox at parentMailbox with properties {{name:"{name_safe}"}}
                return "success"
            end tell""",
                timeout=self.timeout,
            )
        else:
            script = _wrap_with_timeout(
                f"""tell application "Mail"
                set accountRef to {account_clause}
                make new mailbox at accountRef with properties {{name:"{name_safe}"}}
                return "success"
            end tell""",
                timeout=self.timeout,
            )

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

        # IMAP fast path (#150). Requires account + source_mailbox —
        # without source_mailbox, IMAP would have to SEARCH every
        # mailbox per Message-ID, defeating the speed win. Falls
        # through to the AppleScript pass on any _IMAP_FALLBACK_EXCS
        # exception (incl. capability gaps and trash-not-found).
        if account is not None and source_mailbox is not None:
            imap_count = self._try_imap_delete(
                message_ids,
                account=account,
                source_mailbox=source_mailbox,
            )
            if imap_count is not None:
                return imap_count

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

        script = f"{_MAILBOX_RESOLVER_HANDLERS}\n" + _wrap_with_timeout(
            f"""tell application "Mail"
            set idList to {{{id_list}}}
            set deleteCount to 0

{repeat_block}

            return deleteCount
        end tell""",
            timeout=self.timeout,
        )

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

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
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

        script = _wrap_with_timeout(
            f"""tell application "Mail"
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
        end tell""",
            timeout=self.timeout,
        )

        result = self._run_applescript(script).strip()
        if result == "OK":
            return True
        raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")

    def find_message_by_message_id(
        self, rfc5322_message_id: str
    ) -> str | None:
        """Resolve an RFC 5322 Message-ID header to Mail's internal id.

        Used by ``update_draft`` to recover a reply seed from a saved
        draft's ``In-Reply-To`` header, and by ``create_draft`` to accept
        the bracketless RFC ids that read tools emit on the IMAP path
        (#148 / #205).

        Args:
            rfc5322_message_id: e.g. ``<calendar-abc123@google.com>`` or
                ``calendar-abc123@google.com``. Brackets are stripped
                from the input; the AppleScript ``whose`` clause then
                queries for both the bare and bracketed forms in one
                pass. Mail.app's ``message id`` property storage
                normalization is not uniform — IMAP-backed accounts
                (iCloud, Gmail) store the value bare, while other paths
                may store with angle brackets per RFC 5322. Querying
                both forms in a single clause is robust to either
                convention and matches in one round-trip.

        Returns:
            Mail's internal numeric id (as a string) of the first
            matching message found, or None if no message with that
            Message-ID exists in any mailbox.
        """
        if not rfc5322_message_id:
            return None
        bare = rfc5322_message_id
        if bare.startswith("<") and bare.endswith(">"):
            bare = bare[1:-1]
        bracketed = f"<{bare}>"
        safe_bare = escape_applescript_string(sanitize_input(bare))
        safe_bracketed = escape_applescript_string(sanitize_input(bracketed))

        script = _wrap_with_timeout(
            f"""tell application "Mail"
            set foundId to ""
            repeat with acc in accounts
                try
                    repeat with mb in mailboxes of acc
                        try
                            set m to first message of mb whose (message id is "{safe_bare}" or message id is "{safe_bracketed}")
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
        end tell""",
            timeout=self.timeout,
        )

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

        script = _wrap_as_json_script(tell_body, timeout=self.timeout)
        raw = self._run_applescript(script)
        data = parse_applescript_json(raw)
        if not isinstance(data, dict) or not data.get("found"):
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")
        # Drop the internal flag from the user-visible payload.
        data.pop("found", None)
        return cast(dict[str, Any], data)

    def _maybe_resolve_rfc_seed_id(
        self, seed: str, seed_id: str | None
    ) -> str | None:
        """Translate an RFC 5322 Message-ID seed into Mail's internal id.

        Read tools (#148) emit the bracketless RFC id as ``id`` on the
        IMAP path; passing that value to ``create_draft(reply_to=...)``
        used to fail because the AppleScript ``whose id is`` clause
        matches Mail's internal numeric id only. Mail's internal id is a
        stringified long integer with no ``@``, so ``'@' in seed_id`` is
        an unambiguous discriminator. (#205)

        Returns ``seed_id`` unchanged for the ``new`` seed, for empty
        seeds, or for non-RFC ids. Raises ``MailMessageNotFoundError``
        when an RFC id is passed but doesn't match any message.
        """
        if seed not in ("reply", "forward") or not seed_id or "@" not in seed_id:
            return seed_id
        resolved = self.find_message_by_message_id(seed_id)
        if resolved is None:
            raise MailMessageNotFoundError(
                f"no message with message-id {seed_id!r}"
            )
        return resolved

    @staticmethod
    def _validate_create_draft_args(
        seed: str,
        seed_id: str | None,
        to: list[str] | None,
        subject: str | None,
    ) -> None:
        """Validate the per-seed argument requirements of create_draft.
        Raises ValueError with a specific message on the first violation.
        (#193)
        """
        if seed not in ("new", "reply", "forward"):
            raise ValueError(
                f"seed must be 'new', 'reply', or 'forward'; got {seed!r}"
            )
        if seed in ("reply", "forward"):
            if not seed_id:
                raise ValueError(f"seed_id is required for seed={seed!r}")
        else:  # seed == "new"
            if not to:
                raise ValueError("'to' is required when seed='new'")
            if not subject:
                raise ValueError("'subject' is required when seed='new'")

    @staticmethod
    def _build_attachment_block(
        attachment_paths: list[Path] | None,
    ) -> str:
        """AppleScript fragment that attaches files to ``theMessage``.
        Returns ``""`` when ``attachment_paths`` is None or empty.
        Raises ``FileNotFoundError`` on the first non-existent path. (#193)
        """
        if not attachment_paths:
            return ""
        for p in attachment_paths:
            if not Path(p).is_file():
                raise FileNotFoundError(f"attachment not found: {p}")
        paths_safe = ", ".join(
            f'"{escape_applescript_string(str(Path(p).resolve()))}"'
            for p in attachment_paths
        )
        return f"""
            repeat with apath in {{{paths_safe}}}
                tell theMessage to make new attachment with properties {{file name:(POSIX file apath)}} at after last paragraph
            end repeat
        """

    @staticmethod
    def _build_creation_block(
        seed: str,
        seed_id_safe: str | None,
        reply_all: bool,
        subject_safe: str | None,
        body_safe: str,
    ) -> str:
        """Per-seed AppleScript fragment that produces ``theMessage``.

        The reply/forward branches share the cross-account ``whose id
        is "{seed_id_safe}"`` lookup pattern, differing only in the
        Mail.app verb (``reply`` / ``reply to all`` / ``forward``).
        ``seed_id_safe`` is expected to be Mail's internal id —
        callers route RFC 5322 ids through
        ``_maybe_resolve_rfc_seed_id`` first (#205). (#193)
        """
        if seed == "new":
            return (
                f'set theMessage to make new outgoing message with properties '
                f'{{subject:"{subject_safe}", content:"{body_safe}", visible:false}}'
            )
        if seed == "reply":
            verb = "reply to all" if reply_all else "reply"
        else:  # forward
            verb = "forward"
        return f"""
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
            seed_id: Identifier of the message to reply/forward. Accepts
                either Mail's internal numeric id OR an RFC 5322
                Message-ID (with or without angle brackets). The latter
                is what read tools (``search_messages`` / ``get_messages``)
                emit as ``id`` on the IMAP path (#148), so callers can
                forward those ids verbatim. Required when ``seed != "new"``.
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
        self._validate_create_draft_args(seed, seed_id, to, subject)

        # If the caller handed us an RFC 5322 Message-ID (the form read
        # tools emit on the IMAP path per #148), resolve to Mail's
        # internal id before the `whose id is` lookup below. (#205)
        seed_id = self._maybe_resolve_rfc_seed_id(seed, seed_id)

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

        # Sender clause. Apply the SECURITY_CHECKLIST two-step idiom
        # (sanitize_input then escape_applescript_string) even though the
        # resolver pulls from Mail.app's own account list — the convention
        # exists so we don't have to risk-assess each site individually,
        # and the Display-Name <email> form from #158 broadened what
        # characters can appear here. (#173)
        sender_clause = ""
        if from_account is not None:
            sender_email = self._resolve_account_to_sender(from_account)
            sender_safe = escape_applescript_string(sanitize_input(sender_email))
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

        attachment_block = self._build_attachment_block(attachment_paths)

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
        #       body (loses inline quote but preserves threading headers).
        #     * empty body      -> leave Mail's auto-content alone (the
        #       quoted-reply default the user gets in Mail.app).
        if seed == "new":
            body_block = ""
        elif body:
            body_block = f'set content of theMessage to "{body_safe}"'
        else:
            body_block = ""

        creation_block = self._build_creation_block(
            seed, seed_id_safe, reply_all, subject_safe, body_safe,
        )

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

        script = _wrap_with_timeout(
            f"""tell application "Mail"
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
        end tell""",
            timeout=self.timeout,
        )

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

        # Compute sanitized, containment-checked target paths on the Python
        # side (the attachment name is attacker-influenced — it can carry a
        # forwarded message's MIME filename), then pre-create each per-index
        # subdirectory so the AppleScript only does `save att in (POSIX file
        # <path>)`.
        target_paths = _compute_draft_extract_targets(attachment_names, dest_dir)
        for p in target_paths:
            p.parent.mkdir(parents=True, exist_ok=True)

        targets_safe = ", ".join(
            f'"{escape_applescript_string(str(p))}"' for p in target_paths
        )

        script = _wrap_with_timeout(
            f"""tell application "Mail"
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
        end tell""",
            timeout=self.timeout,
        )

        result = self._run_applescript(script).strip()
        if result == "ERR_NOT_FOUND":
            raise MailDraftNotFoundError(f"no draft with id {draft_id!r}")

        # Return only the paths that actually got files written.
        return [p for p in target_paths if p.is_file()]
