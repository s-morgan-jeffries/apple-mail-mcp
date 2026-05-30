# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false
#
# imapclient ships without a py.typed marker, so Pyright/Pylance can't verify
# argument types against its public API. Mypy is configured to ignore missing
# imports for the imapclient package via [[tool.mypy.overrides]] in
# pyproject.toml; Pyright respects file-level pragmas instead. The three
# suppressed categories cover the false positives that arise when calling
# search() / fetch() with list-shaped criteria and reading Envelope/BodyData
# fields. Suppression is scoped to this file so unrelated type bugs elsewhere
# in the codebase still surface.
"""IMAPClient wrapper for read operations.

Stateless, per-call connection lifecycle. This module is deliberately
unaware of Mail.app, Keychain, and the MCP server. It takes fully-
resolved credentials and talks IMAP. Callers (tests here; the
delegation layer in #40 later) are responsible for correlating
Mail.app account name → (host, port, email) and fetching the
password via ``keychain.get_imap_password``.

See ``docs/plans/2026-04-23-imap-connector-design.md``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from typing import Any, cast

from imapclient import DRAFT, IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError
from imapclient.response_types import Envelope

from .exceptions import (
    MailImapMoveUnsupportedError,
    MailImapTrashNotFoundError,
    MailMessageNotFoundError,
)

logger = logging.getLogger(__name__)

# Strict ISO 8601 YYYY-MM-DD. Duplicated from mail_connector to break an
# otherwise-circular import: mail_connector.search_messages delegates to
# this module, so mail_connector has to import from imap_connector, and a
# reverse dependency would deadlock. The regex is trivial; duplication is
# preferable to reshuffling the module layout.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CONNECT_TIMEOUT_S: float = 3.0
"""Per invariant 4 in imap-auth-options-decision.md: ≤3s so offline
fallback happens inside the graceful-degradation window without
waiting for TCP's default timeout."""

POOL_IDLE_TIMEOUT_S: float = 270.0
"""Default pool idle threshold. iCloud and most providers drop IMAP
sessions after ~30 min idle. 270s = 4.5 min keeps us comfortably under
that while still amortizing connect cost across realistic interactive
bursts (a series of search → get_message → get_attachments calls within
a couple minutes of each other reuses one connection)."""

_FLAG_SEEN = b"\\Seen"
_FLAG_FLAGGED = b"\\Flagged"


# ---------------------------------------------------------------------------
# Connection pool (issue #75)
# ---------------------------------------------------------------------------

@dataclass
class _PooledClient:
    """Tracks one cached IMAPClient: the connection, a lock that
    serializes its use across threads, and a monotonic timestamp for
    idle-timeout decisions."""

    client: IMAPClient
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)


# Exceptions that should invalidate a pooled connection. LoginError /
# IMAPClientError are direct protocol failures; OSError catches mid-
# session network drops (broken pipe, conn reset). MailMessageNotFoundError
# is NOT here — it's a clean "no match" answer, not a connection failure.
_POOL_INVALIDATE_EXCS: tuple[type[BaseException], ...] = (
    LoginError,
    IMAPClientError,
    OSError,
)


class ImapConnectionPool:
    """Reuses IMAPClient sessions across calls keyed by (host, email).

    Per the issue #75 design: opt-in (per-call lifecycle remains the
    default in :class:`ImapConnector` until a pool is explicitly
    passed). Lazy-reconnect on error and on idle timeout — if the
    cached client is stale, the next ``session()`` drops it and opens
    a fresh one transparently.

    Thread-safety:
    - The cache dict is guarded by ``_cache_lock``.
    - Each cached client has its own per-connection lock that
      ``session()`` holds for the duration of the yielded block.
      Concurrent calls to the same (host, email) serialize; concurrent
      calls to different keys run in parallel.
    """

    def __init__(self, *, idle_timeout_s: float = POOL_IDLE_TIMEOUT_S) -> None:
        self._cache: dict[tuple[str, str], _PooledClient] = {}
        self._cache_lock = threading.Lock()
        self._idle_timeout_s = idle_timeout_s

    @contextmanager
    def session(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        connect_timeout: float,
    ) -> Iterator[IMAPClient]:
        """Yield a logged-in IMAPClient for use; manage its lifecycle.

        On clean exit: bump ``last_used`` and keep the entry cached.

        On failure of one of ``_POOL_INVALIDATE_EXCS``: drop the entry
        (attempt logout, swallow logout errors) so the next call gets
        a fresh connection. The original exception still propagates;
        the orchestrator's existing fallback logic catches it.
        """
        key = (host, email)

        with self._cache_lock:
            entry = self._cache.get(key)
            stale = (
                entry is not None
                and time.monotonic() - entry.last_used > self._idle_timeout_s
            )
            if entry is None or stale:
                if entry is not None:
                    # Stale — try to be polite about closing it.
                    self._cache.pop(key, None)
                    try:
                        entry.client.logout()
                    except Exception:  # noqa: BLE001 — best effort
                        pass
                client = IMAPClient(
                    host, port=port, ssl=True, timeout=connect_timeout
                )
                client.login(email, password)
                entry = _PooledClient(client=client)
                self._cache[key] = entry

        # Acquire the per-connection lock OUTSIDE the cache lock — we
        # don't want a slow operation on one connection blocking cache
        # reads for unrelated keys.
        with entry.lock:
            try:
                yield entry.client
            except _POOL_INVALIDATE_EXCS:
                # Connection-level failure: drop and propagate.
                with self._cache_lock:
                    # Only drop if it's still the same entry — another
                    # thread may have already reconnected.
                    if self._cache.get(key) is entry:
                        self._cache.pop(key, None)
                try:
                    entry.client.logout()
                except Exception:  # noqa: BLE001 — best effort
                    pass
                raise
            else:
                entry.last_used = time.monotonic()

    def close(self) -> None:
        """Log out all cached clients. Safe to call multiple times.

        Acquires each entry's per-connection lock before issuing
        ``logout()`` so an in-flight ``session()``-holder finishes its
        operation before we drop the connection underneath it (#171).
        Latent today because FastMCP is single-threaded, but the
        pool's per-entry locks are designed for future thread-safety
        and the atexit hook (#127) fires ``close()`` at interpreter
        shutdown when daemon threads may still be alive.
        """
        with self._cache_lock:
            entries = list(self._cache.values())
            self._cache.clear()
        for entry in entries:
            # Wait for any in-flight session() block to finish before
            # logging out — mirrors session()'s invalidation path.
            with entry.lock:
                try:
                    entry.client.logout()
                except Exception:  # noqa: BLE001 — best effort
                    pass

_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _iso_to_imap_date(iso: str, field: str) -> str:
    if not _ISO_DATE_RE.match(iso):
        raise ValueError(
            f"{field} must be ISO 8601 YYYY-MM-DD, got: {iso!r}"
        )
    d = _date.fromisoformat(iso)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _iso_to_imap_before(iso: str, field: str) -> str:
    """Upper-bound helper: IMAP BEFORE is exclusive; pass date + 1 day."""
    if not _ISO_DATE_RE.match(iso):
        raise ValueError(
            f"{field} must be ISO 8601 YYYY-MM-DD, got: {iso!r}"
        )
    d = _date.fromisoformat(iso) + _timedelta(days=1)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _reject_control_chars(value: str, field: str) -> None:
    """Raise ValueError if ``value`` contains a C0 control character or DEL.

    IMAP command arguments and RFC 3501 quoted strings must not contain
    CR/LF. imapclient quotes string args but does NOT promote CR/LF-bearing
    values to literals (its 8-bit detector only fires on bytes > 127), so a
    raw CRLF embedded in a value is sent inline on the authenticated control
    channel and splits the command — letting following bytes be parsed as a
    new tagged command (IMAP command injection, CWE-93). Applied to every
    free-text value that becomes an IMAP command argument (SEARCH terms,
    Message-IDs, mailbox names) before it can reach the wire.
    """
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                f"{field} contains a disallowed control character "
                f"(U+{ord(ch):04X}); control characters are not permitted "
                f"in IMAP command arguments"
            )


def _bracket_message_id(message_id: str) -> str:
    """Validate a Message-ID and return it in RFC 5322 bracketed form.

    Rejects control characters (see :func:`_reject_control_chars`), then
    wraps the id in angle brackets unless it already is. Single chokepoint
    for every ``SEARCH HEADER "Message-ID" "<id>"`` construction so no call
    site can build a bracketed id without the CRLF-injection guard.
    """
    _reject_control_chars(message_id, "message_id")
    if message_id.startswith("<") and message_id.endswith(">"):
        return message_id
    return f"<{message_id}>"


def _build_search_criteria(
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None = None,
    date_to: str | None = None,
    body_contains: str | None = None,
    text_contains: str | None = None,
) -> list[Any]:
    """Translate ImapConnector.search_messages parameters to IMAP SEARCH criteria.

    Returns ``["ALL"]`` if no filters are supplied — IMAP SEARCH requires at
    least one criterion.
    """
    criteria: list[Any] = []
    if sender_contains:
        _reject_control_chars(sender_contains, "sender_contains")
        criteria.extend(["FROM", sender_contains])
    if subject_contains:
        _reject_control_chars(subject_contains, "subject_contains")
        criteria.extend(["SUBJECT", subject_contains])
    if read_status is True:
        criteria.append("SEEN")
    elif read_status is False:
        criteria.append("UNSEEN")
    if is_flagged is True:
        criteria.append("FLAGGED")
    elif is_flagged is False:
        criteria.append("UNFLAGGED")
    if date_from is not None:
        criteria.extend(["SINCE", _iso_to_imap_date(date_from, "date_from")])
    if date_to is not None:
        criteria.extend(["BEFORE", _iso_to_imap_before(date_to, "date_to")])
    if body_contains:
        _reject_control_chars(body_contains, "body_contains")
        criteria.extend(["BODY", body_contains])
    if text_contains:
        _reject_control_chars(text_contains, "text_contains")
        criteria.extend(["TEXT", text_contains])
    return criteria or ["ALL"]


def _decode(b: bytes | bytearray | str | None) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    # bytes or bytearray — both have .decode().
    return b.decode("utf-8", errors="replace")


def _strip_brackets(s: str) -> str:
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1]
    return s


def _flatten_thread_clusters(tree: Any) -> Iterator[set[int]]:
    """Yield each top-level cluster of a THREAD result as a flat set of UIDs.

    IMAPClient's ``client.thread()`` returns nested tuples per RFC 5256 —
    each top-level element is one thread, with possibly-nested replies
    representing the reply tree. For dispatch (#123 Tier 2) we don't care
    about the tree shape; we just need to know which UIDs belong to which
    cluster so we can intersect with the anchor's relevant_uids.

    Edge cases:
    - The result may be ``None`` if the server returned an empty THREAD
      response — we yield nothing.
    - Each leaf may be an int or a string-coerced int depending on the
      IMAP server / IMAPClient version; we coerce to int.
    """
    if not tree:
        return
    for top in tree:
        out: set[int] = set()
        _flatten_one(top, out)
        if out:
            yield out


def _flatten_one(node: Any, out: set[int]) -> None:
    """Recursive walk of a THREAD nested-tuple node, adding UIDs to ``out``."""
    if isinstance(node, (tuple, list)):
        for child in node:
            _flatten_one(child, out)
    else:
        try:
            out.add(int(node))
        except (TypeError, ValueError):
            pass


def _format_sender(envelope: Envelope) -> str:
    from_ = envelope.from_ or ()
    if not from_:
        return ""
    first = from_[0]
    name = _decode(first.name)
    mailbox = _decode(first.mailbox)
    host = _decode(first.host)
    email = f"{mailbox}@{host}" if mailbox and host else mailbox or ""
    return f"{name} <{email}>" if name else email


def _bodystructure_extract_attachments(
    structure: Any,
) -> list[dict[str, Any]]:
    """Walk a BODYSTRUCTURE tuple and emit attachment metadata.

    A part counts as an attachment if any of:
    - Its disposition is ``attachment``.
    - Its disposition is ``inline`` AND it has a ``filename`` param.
    - It's a ``message/rfc822`` part (forwarded email — conventionally
      an attachment per RFC 2046 §5.2.1, and the case Mail.app's
      AppleScript surface sometimes silently drops).

    Returns dicts with keys: ``name``, ``mime_type``, ``size``,
    ``downloaded``. ``downloaded`` is always False on the IMAP path —
    BODYSTRUCTURE returns metadata only, not body bytes, and Mail.app's
    local cache state isn't observable from the protocol. Callers that
    need the bytes invoke ``save_attachments`` (which fetches on demand).
    """
    out: list[dict[str, Any]] = []

    def _filename_from_params(params: Any, key_name: bytes) -> str | None:
        """Pull a value out of a flat (k, v, k, v, ...) param tuple."""
        if not isinstance(params, tuple):
            return None
        for i in range(0, len(params) - 1, 2):
            k = params[i]
            if isinstance(k, bytes) and k.lower() == key_name:
                v = params[i + 1]
                if isinstance(v, (bytes, bytearray)):
                    return _decode(v)
        return None

    def _walk(s: Any) -> None:
        if not isinstance(s, tuple) or not s:
            return

        # Multipart: IMAPClient groups the child parts in a LIST at
        # position 0 — ``([child1, child2, ...], subtype, params, ...)``.
        # (Real iCloud/Gmail BODYSTRUCTUREs take this shape; missing it
        # silently dropped attachments on every multipart message.)
        if isinstance(s[0], list):
            for child in s[0]:
                _walk(child)
            return
        # Defensive: some inputs nest children as direct tuple elements.
        if isinstance(s[0], tuple):
            for child in s:
                if isinstance(child, tuple):
                    _walk(child)
            return

        # Leaf. Spec positions: type, subtype, params, id, desc, encoding,
        # size, [type-specific extras...], [disposition]. The disposition
        # tuple, if present, sits at the trailing end — but its exact index
        # varies (text has 'lines', message/rfc822 has envelope+body+lines,
        # etc.). Scan trailing elements for a tuple whose head is one of
        # the disposition keywords.
        leaf = s
        type_ = leaf[0] if isinstance(leaf[0], bytes) else b""
        subtype = (
            leaf[1] if len(leaf) > 1 and isinstance(leaf[1], bytes) else b""
        )
        ct_params = leaf[2] if len(leaf) > 2 else ()
        size_field = leaf[6] if len(leaf) > 6 else 0

        disp_kind: bytes | None = None
        disp_filename: str | None = None
        for elem in leaf:
            if (
                isinstance(elem, tuple)
                and elem
                and isinstance(elem[0], bytes)
                and elem[0].lower() in (b"attachment", b"inline")
            ):
                disp_kind = elem[0].lower()
                disp_params = elem[1] if len(elem) > 1 else ()
                disp_filename = _filename_from_params(disp_params, b"filename")
                break

        is_rfc822 = (
            type_.lower() == b"message" and subtype.lower() == b"rfc822"
        )
        is_attachment = (
            disp_kind == b"attachment"
            or (disp_kind == b"inline" and disp_filename is not None)
            or is_rfc822
        )
        if not is_attachment:
            return

        # Filename precedence: disposition's filename → content-type's
        # `name` param (legacy mailers) → empty string.
        name = disp_filename or _filename_from_params(ct_params, b"name") or ""
        mime_type = f"{_decode(type_)}/{_decode(subtype)}"

        out.append({
            "name": name,
            "mime_type": mime_type,
            "size": int(size_field) if isinstance(size_field, int) else 0,
            "downloaded": False,
        })

    _walk(structure)
    return out


def _disposition_marks_attachment(elem: Any) -> bool:
    """True if a BODYSTRUCTURE element is a disposition tuple that marks an
    attachment — ``attachment``, or ``inline`` carrying a ``filename`` param.

    Disposition tuples look like ``(b"attachment", (b"filename", b"x.pdf"))``;
    the params are a flat ``(key, value, key, value, ...)`` tuple.
    """
    if not (isinstance(elem, tuple) and elem and isinstance(elem[0], bytes)):
        return False
    disp = elem[0].lower()
    if disp == b"attachment":
        return True
    if disp != b"inline":
        return False
    params = elem[1] if len(elem) > 1 else ()
    if not isinstance(params, tuple):
        return False
    return any(
        isinstance(params[i], bytes) and params[i].lower() == b"filename"
        for i in range(0, len(params) - 1, 2)
    )


def _leaf_has_attachment(structure: tuple[Any, ...]) -> bool:
    """True if a leaf BODYSTRUCTURE part is an attachment.

    A ``message/rfc822`` (forwarded email) part counts, matching
    ``_bodystructure_extract_attachments`` — otherwise the has_attachment
    search filter and the attachment list disagree (a forwarded-only message
    would be filtered out yet report an attachment). Otherwise, any element
    that is an attachment/inline-filename disposition tuple qualifies.
    """
    type_ = structure[0] if isinstance(structure[0], bytes) else b""
    subtype = (
        structure[1]
        if len(structure) > 1 and isinstance(structure[1], bytes)
        else b""
    )
    if type_.lower() == b"message" and subtype.lower() == b"rfc822":
        return True
    return any(_disposition_marks_attachment(elem) for elem in structure)


def _bodystructure_has_attachment(structure: Any) -> bool:
    """Walk an IMAPClient-parsed BODYSTRUCTURE tree and detect attachments.

    IMAPClient represents multipart as a tuple ``(part_tuple, ..., subtype)``
    where each ``part_tuple`` is either another multipart (starts with a
    tuple) or a leaf (starts with bytes like ``b"text"``, ``b"application"``).

    A message "has an attachment" if any leaf carries a disposition of
    ``attachment`` or ``inline`` with a ``filename`` parameter (see
    ``_leaf_has_attachment``).
    """
    if not isinstance(structure, tuple) or not structure:
        return False

    # Multipart — children grouped in a list at position 0 (IMAPClient).
    if isinstance(structure[0], list):
        return any(
            _bodystructure_has_attachment(child) for child in structure[0]
        )
    # Defensive: children nested as direct tuple elements.
    if isinstance(structure[0], tuple):
        return any(
            isinstance(child, tuple) and _bodystructure_has_attachment(child)
            for child in structure
        )

    return _leaf_has_attachment(structure)


def _envelope_to_dict(
    envelope: Envelope, flags: tuple[bytes, ...]
) -> dict[str, Any]:
    date = envelope.date
    if isinstance(date, _datetime):
        date_str = date.isoformat()
    else:
        date_str = _decode(date)
    # On the IMAP path, `id` and `rfc_message_id` are intentionally the
    # same value — both are the RFC 5322 Message-ID (bracketless). The
    # dual-emit (#148) lets cross-path consumers (e.g., callers feeding
    # this row to an AppleScript-only tool) always have an RFC id
    # available; on the AppleScript path the two diverge (`id` is the
    # Mail.app internal numeric id, `rfc_message_id` is the RFC form).
    rfc_id = _strip_brackets(_decode(envelope.message_id))
    return {
        "id": rfc_id,
        "rfc_message_id": rfc_id,
        "subject": _decode(envelope.subject),
        "sender": _format_sender(envelope),
        "date_received": date_str,
        "read_status": _FLAG_SEEN in flags,
        "flagged": _FLAG_FLAGGED in flags,
    }


class ImapConnector:
    def __init__(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        *,
        pool: ImapConnectionPool | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._email = email
        self._password = password
        self._connect_timeout = connect_timeout
        self._pool = pool

    @contextmanager
    def _session(self) -> Iterator[IMAPClient]:
        """Yield a logged-in IMAPClient. Routes through the pool when
        one was provided at construction; otherwise opens and closes a
        fresh connection per call (the v0.5.0 behavior).

        All four public methods of this connector (search_messages,
        get_message, get_attachments, find_thread_members) use this
        helper so the pool / no-pool decision lives in exactly one place.
        """
        if self._pool is not None:
            with self._pool.session(
                self._host, self._port, self._email,
                self._password, self._connect_timeout,
            ) as client:
                yield client
            return

        client = IMAPClient(
            self._host, port=self._port, ssl=True, timeout=self._connect_timeout
        )
        try:
            client.login(self._email, self._password)
            yield client
        finally:
            client.logout()

    def search_messages(
        self,
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
        # Validate and translate filters before opening a connection so that
        # invalid input fails fast without the TCP-connect + LOGIN round trip.
        criteria = _build_search_criteria(
            sender_contains,
            subject_contains,
            read_status,
            is_flagged,
            date_from,
            date_to,
            body_contains,
            text_contains,
        )
        _reject_control_chars(mailbox, "mailbox")

        with self._session() as client:
            client.select_folder(mailbox, readonly=True)

            uids = client.search(criteria)
            if limit is not None:
                uids = uids[-limit:]

            if not uids:
                return []

            fetch_keys: list[bytes] = [b"ENVELOPE", b"FLAGS"]
            # BODYSTRUCTURE bundles into the same FETCH (no extra round-trip)
            # whether we need it for has_attachment filtering or for the
            # include_attachments output field — only fetch once.
            need_bodystructure = (
                has_attachment is not None or include_attachments
            )
            if need_bodystructure:
                fetch_keys.append(b"BODYSTRUCTURE")
            fetched = client.fetch(uids, fetch_keys)

            results: list[dict[str, Any]] = []
            for uid in uids:
                entry = fetched[uid]
                if has_attachment is not None:
                    has = _bodystructure_has_attachment(
                        entry.get(b"BODYSTRUCTURE")
                    )
                    if has_attachment is True and not has:
                        continue
                    if has_attachment is False and has:
                        continue
                row = _envelope_to_dict(
                    entry[b"ENVELOPE"], tuple(entry[b"FLAGS"])
                )
                if include_attachments:
                    row["attachments"] = _bodystructure_extract_attachments(
                        entry.get(b"BODYSTRUCTURE")
                    )
                results.append(row)
            return results

    def get_message(
        self,
        message_id: str,
        mailbox: str = "INBOX",
        *,
        include_content: bool = True,
        headers_only: bool = False,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """Look up a single message by RFC 5322 Message-ID and return its
        envelope + flags, optionally with body content.

        ``message_id`` is matched against the ``Message-ID`` header via
        ``SEARCH HEADER "Message-ID" "<id>"``. The angle brackets are
        added if missing — RFC 5322 stores the ID in bracketed form and
        IMAP servers compare against the literal header value.

        Args:
            message_id: RFC 5322 Message-ID, with or without surrounding
                ``<>``. The bracketless form is what
                ``search_messages`` returns; either works as input.
            mailbox: Folder to look in. IMAP requires a SELECT before
                FETCH; cross-folder lookup is not in scope here (callers
                without a folder hint stay on the AppleScript path in
                the orchestrator above).
            include_content: When False, ``content`` is the empty string
                (matches the AppleScript path's behavior with the same
                flag).
            headers_only: When True, fetches ``BODY[HEADER]`` instead of
                ``BODY[TEXT]`` — useful for preview-style callers who
                don't want the body. ``content`` is always returned as
                the empty string in this mode (the headers themselves
                are reflected via the envelope dict; we don't expose the
                raw RFC 822 header block).

        Returns:
            Dict with the same keys as the AppleScript ``get_message``
            output: ``id``, ``subject``, ``sender``, ``date_received``,
            ``read_status``, ``flagged``, ``content``.

        Raises:
            MailMessageNotFoundError: No message in ``mailbox`` matches
                the Message-ID. (The orchestrator's caller may then fall
                back to AppleScript.)
            IMAPClientError: Login / SELECT / SEARCH / FETCH failed.
        """
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")

        fetch_keys: list[bytes] = [b"ENVELOPE", b"FLAGS"]
        want_body = include_content and not headers_only
        if want_body:
            fetch_keys.append(b"BODY[TEXT]")
        elif headers_only:
            # We don't currently use the raw header block for anything in
            # the response (envelope already gives us subject/sender/date),
            # but requesting BODY[HEADER] is the spec-correct way to ask
            # the server for headers without paying for the body. Some
            # servers send less data this way; some don't care.
            fetch_keys.append(b"BODY[HEADER]")
        if include_attachments:
            fetch_keys.append(b"BODYSTRUCTURE")

        with self._session() as client:
            client.select_folder(mailbox, readonly=True)

            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )

            fetched = client.fetch(uids[:1], fetch_keys)
            entry = next(iter(fetched.values()))

            result = _envelope_to_dict(
                entry[b"ENVELOPE"], tuple(entry[b"FLAGS"])
            )
            if want_body:
                body_bytes = entry.get(b"BODY[TEXT]") or b""
                result["content"] = _decode(body_bytes)
            else:
                result["content"] = ""
            if include_attachments:
                result["attachments"] = _bodystructure_extract_attachments(
                    entry.get(b"BODYSTRUCTURE")
                )
            return result

    def get_attachments(
        self,
        message_id: str,
        mailbox: str = "INBOX",
    ) -> list[dict[str, Any]]:
        """Return a list of attachment metadata dicts for a message.

        Looks the message up by RFC 5322 Message-ID via
        ``SEARCH HEADER "Message-ID" "<id>"``, then issues a single
        ``FETCH ... BODYSTRUCTURE`` and walks the MIME tree to extract
        attachment parts. One round-trip total for metadata, vs the
        AppleScript path's account×mailbox scan plus per-attachment
        property fetches.

        The IMAP path also surfaces attachment cases Mail.app's
        AppleScript layer drops silently — see issue #73 for the
        catalog (forwarded message/rfc822 parts, multipart/related
        inline images, Unicode filenames).

        Args:
            message_id: RFC 5322 Message-ID, with or without surrounding
                ``<>``. The bracketless form is what
                ``search_messages`` returns; either works as input.
            mailbox: Folder to look in. IMAP requires SELECT before
                FETCH; cross-folder discovery isn't in scope here
                (callers without a folder hint stay on the AppleScript
                path in the orchestrator above).

        Returns:
            List of dicts with keys ``name`` (str), ``mime_type`` (str),
            ``size`` (int), ``downloaded`` (bool, always False on this
            path). Empty list when the message has no attachments.

        Raises:
            MailMessageNotFoundError: No message in ``mailbox`` matches
                the Message-ID.
            IMAPClientError: Login / SELECT / SEARCH / FETCH failed.
        """
        bracketed = _bracket_message_id(message_id)
        _reject_control_chars(mailbox, "mailbox")

        with self._session() as client:
            client.select_folder(mailbox, readonly=True)

            uids = client.search(["HEADER", "Message-ID", bracketed])
            if not uids:
                raise MailMessageNotFoundError(
                    f"Message-ID {message_id!r} not found in mailbox "
                    f"{mailbox!r} on {self._host}."
                )

            fetched = client.fetch(uids[:1], [b"BODYSTRUCTURE"])
            entry = next(iter(fetched.values()))
            return _bodystructure_extract_attachments(
                entry.get(b"BODYSTRUCTURE")
            )

    def find_thread_members(
        self,
        anchor_rfc_message_id: str,
        anchor_references: list[str],
    ) -> list[dict[str, Any]]:
        """Return all messages in the anchor's thread across the account.

        Tiered, capability-detected dispatch:

        - **Tier 1 (Gmail X-GM-THRID via All Mail)** (#122) — when
          ``X-GM-EXT-1`` is advertised AND ``[Gmail]/All Mail`` is
          visible (``\\All`` SPECIAL-USE flag), look the conversation
          up by Gmail's 64-bit thread ID. ~5 round-trips,
          mailbox-count-independent.

        - **Tier 1.5 (Gmail X-GM-THRID per-mailbox)** (#125) — when
          ``X-GM-EXT-1`` is advertised but All Mail is hidden over IMAP
          (per-folder-size opt-out in Gmail Settings — common). Find
          the anchor's UID + X-GM-THRID in INBOX or Sent, then search
          every selectable folder by X-GM-THRID. ~2+M*2 round-trips
          (~186 on a 92-folder account vs ~1100 for BFS).

        - **Tier 2 (RFC 5256 THREAD)** (#123) — when ``THREAD=REFERENCES``
          or ``THREAD=REFS`` is advertised (Fastmail, some Dovecot;
          NOT Gmail typically, NOT iCloud). Per mailbox: a single
          THREAD command returns nested UID tuples representing the
          full thread structure; walk to find clusters containing
          relevant UIDs. ~M*2-4 round-trips depending on anchor
          locality.

        - **Tier 3 (header-search BFS)** — universal fallback. Iterates
          every mailbox; searches Message-ID / In-Reply-To /
          References for every known thread ID. Used on iCloud and
          any provider not covered above. Also used when a higher tier
          advertises but rejects mid-flight (server lied about its
          capabilities).

        A single ``_session()`` covers all tiers, so fall-through
        doesn't pay a second connect+login. With the pool (#75),
        all tiers share the cached connection.

        Args:
            anchor_rfc_message_id: RFC 5322 Message-ID of the thread anchor,
                bracketless (as returned by _strip_brackets).
            anchor_references: List of Message-IDs from the anchor's
                References header, bracketless, order preserved. Used by
                Tier 2 + Tier 3; Tier 1 / 1.5 don't need them
                (X-GM-THRID is the authoritative grouping).

        Returns:
            List of message dicts in the same shape as search_messages
            (``id``, ``subject``, ``sender``, ``date_received``,
            ``read_status``, ``flagged``), deduped by Message-ID, sorted
            chronologically ascending.
        """
        _reject_control_chars(anchor_rfc_message_id, "anchor_rfc_message_id")
        with self._session() as client:
            # Tier 1: Gmail X-GM-THRID via [Gmail]/All Mail
            if self._has_capability(client, b"X-GM-EXT-1"):
                tier1 = self._thread_via_xgm_thrid(client, anchor_rfc_message_id)
                if tier1 is not None:
                    return tier1

                # Tier 1.5: Gmail X-GM-THRID per-mailbox (when All Mail hidden)
                tier_1_5 = self._thread_via_xgm_per_mailbox(
                    client, anchor_rfc_message_id
                )
                if tier_1_5 is not None:
                    return tier_1_5

            # Tier 2: RFC 5256 THREAD (Fastmail, Dovecot)
            if (
                self._has_capability(client, b"THREAD=REFERENCES")
                or self._has_capability(client, b"THREAD=REFS")
            ):
                tier2 = self._thread_via_imap_thread(
                    client, anchor_rfc_message_id, anchor_references
                )
                if tier2 is not None:
                    return tier2

            # Tier 3: header-search BFS — universal fallback.
            return self._find_thread_members_bfs(
                client, anchor_rfc_message_id, anchor_references,
            )

    def delete_mailbox(self, name: str, *, allow_non_empty: bool = False) -> int:
        """Delete an IMAP mailbox via the IMAP DELETE command.

        Pre-flight: SELECT readonly to read the EXISTS count. Refuses if
        the mailbox has messages and ``allow_non_empty=False`` — surface
        as ``MailMailboxNotEmptyError`` from the caller.

        Args:
            name: Mailbox name (slash-separated for nested).
            allow_non_empty: When True, skips the message-count refusal
                and lets IMAP DELETE remove the mailbox along with its
                contents.

        Returns:
            The message count that was on the mailbox at the moment of
            delete (0 if empty, N if cascading).

        Raises:
            ValueError: If the mailbox is non-empty and the caller did
                not opt in to ``allow_non_empty``.
            imapclient.exceptions.IMAPClientError: Server-side error
                (mailbox doesn't exist, permission denied, etc.).
        """
        _reject_control_chars(name, "name")
        with self._session() as client:
            try:
                info = client.select_folder(name, readonly=True)
            except IMAPClientError:
                # Surface "mailbox doesn't exist" via the original IMAP error.
                raise
            count = int(info.get(b"EXISTS", 0))
            if count > 0 and not allow_non_empty:
                # CLOSE the readonly select before raising — leaves the
                # connection clean for any subsequent reuse via the pool.
                try:
                    client.close_folder()
                except IMAPClientError:
                    pass
                raise ValueError(
                    f"mailbox {name!r} is not empty ({count} messages); "
                    f"pass allow_non_empty=True to cascade-delete"
                )
            # IMAP DELETE requires the mailbox NOT to be selected on most
            # servers; CLOSE first.
            try:
                client.close_folder()
            except IMAPClientError:
                pass
            client.delete_folder(name)
            return count

    def rename_mailbox(self, old_name: str, new_name: str) -> None:
        """Rename a mailbox via the IMAP RENAME command.

        IMAP RENAME also serves as the move primitive — destination path
        with a different parent (e.g. ``"OldParent/Child"`` →
        ``"NewParent/Child"``) re-parents the mailbox in one operation.

        Args:
            old_name: Current mailbox name (slash-separated for nested).
            new_name: Destination name. Slash-separated; intermediate
                parents must exist (server behavior varies — most refuse
                to auto-create missing parents).

        Raises:
            imapclient.exceptions.IMAPClientError: Server-side error
                (source missing, destination exists, parent missing,
                permission denied, etc.).
        """
        _reject_control_chars(old_name, "old_name")
        _reject_control_chars(new_name, "new_name")
        with self._session() as client:
            client.rename_folder(old_name, new_name)

    def move_messages(
        self,
        message_ids: list[str],
        source_mailbox: str,
        destination_mailbox: str,
    ) -> int:
        """Move messages between mailboxes via IMAP. Issue #149.

        Resolves each RFC 5322 Message-ID to a UID via
        ``SEARCH HEADER "Message-ID" "<id>"`` against ``source_mailbox``,
        then issues one server-side move per session:

        - When ``MOVE`` (RFC 6851) is advertised: ``UID MOVE <uids> <dest>``
          — atomic, single round-trip after resolution.
        - Else when ``UIDPLUS`` (RFC 4315) is advertised: ``UID COPY`` +
          ``UID STORE +FLAGS (\\Deleted)`` + ``UID EXPUNGE``. The expunge
          is scoped to the just-deleted UIDs only — safe even if other
          ``\\Deleted``-flagged messages exist in the mailbox.
        - Else: raises :class:`MailImapMoveUnsupportedError`. A non-UIDPLUS
          unscoped EXPUNGE would remove all flagged messages in the
          mailbox, not just the ones we just moved.

        Args:
            message_ids: RFC 5322 Message-IDs, with or without surrounding
                ``<>``. Bracketless is what ``search_messages`` returns;
                either form works.
            source_mailbox: Mailbox the messages currently live in.
            destination_mailbox: Target mailbox.

        Returns:
            Count of resolved + moved messages. Message-IDs that don't
            resolve to a UID are silently skipped, matching the
            AppleScript path's best-effort partial-success behavior.

        Raises:
            MailImapMoveUnsupportedError: Server advertises neither MOVE
                nor UIDPLUS.
            IMAPClientError: SELECT / SEARCH / MOVE / COPY / STORE /
                EXPUNGE failed at the protocol level.
        """
        bracketed_ids = [_bracket_message_id(mid) for mid in message_ids]
        _reject_control_chars(source_mailbox, "source_mailbox")
        _reject_control_chars(destination_mailbox, "destination_mailbox")

        with self._session() as client:
            client.select_folder(source_mailbox, readonly=False)

            has_move = self._has_capability(client, b"MOVE")
            has_uidplus = self._has_capability(client, b"UIDPLUS")
            if not has_move and not has_uidplus:
                raise MailImapMoveUnsupportedError(
                    f"IMAP server at {self._host} advertises neither MOVE "
                    f"(RFC 6851) nor UIDPLUS (RFC 4315); cannot perform a "
                    f"safe scoped move"
                )

            uids: list[int] = []
            for bracketed in bracketed_ids:
                found = client.search(["HEADER", "Message-ID", bracketed])
                uids.extend(found)
            if not uids:
                return 0

            if has_move:
                client.move(uids, destination_mailbox)
            else:
                client.copy(uids, destination_mailbox)
                client.add_flags(uids, [b"\\Deleted"], silent=True)
                client.uid_expunge(uids)
            return len(uids)

    # Conventional Trash folder names to fall back on when the IMAP
    # server doesn't advertise \\Trash via SPECIAL-USE (RFC 6154).
    # Order is informative — first match wins per `client.list_folders`.
    _CONVENTIONAL_TRASH_NAMES: tuple[str, ...] = (
        "Trash",
        "[Gmail]/Trash",
        "Deleted Messages",
        "Deleted Items",
    )

    # Conventional Drafts folder names to fall back on when the server
    # doesn't advertise \\Drafts via SPECIAL-USE (RFC 6154). Issue #245.
    _CONVENTIONAL_DRAFTS_NAMES: tuple[str, ...] = (
        "Drafts",
        "[Gmail]/Drafts",
        "INBOX.Drafts",
    )

    def append_draft(self, raw_message: bytes) -> str:
        """APPEND a pre-built RFC822 message to the account's Drafts
        folder with the ``\\Draft`` flag, and return the folder used.

        Bypasses Mail.app's AppleScript ``content`` setter, which wraps
        every body in an ``Apple-Mail-URLShareWrapper``
        ``<blockquote type="cite">`` that renders as a quote on iOS
        (Mail.app bug FB11734014, issue #245). The caller builds
        ``raw_message`` via :func:`apple_mail_mcp.draft_builder.build_draft_mime`.
        """
        with self._session() as client:
            folder = self._find_drafts_folder(
                client
            ) or self._find_drafts_by_convention(client)
            if folder is None:
                raise MailMessageNotFoundError(
                    f"No Drafts folder found on {self._host} "
                    f"(no \\Drafts SPECIAL-USE flag and none of "
                    f"{list(self._CONVENTIONAL_DRAFTS_NAMES)} present)."
                )
            client.append(folder, raw_message, flags=[DRAFT])
            return folder

    @staticmethod
    def _find_drafts_folder(client: IMAPClient) -> str | None:
        """Return the Drafts folder name via the ``\\Drafts`` SPECIAL-USE
        flag (RFC 6154), or None if the server doesn't advertise it."""
        for flags, _delim, name in client.list_folders():
            if b"\\Drafts" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    def _find_drafts_by_convention(self, client: IMAPClient) -> str | None:
        """Fall back to conventional Drafts names for servers that don't
        advertise SPECIAL-USE ``\\Drafts``. First match wins in
        :attr:`_CONVENTIONAL_DRAFTS_NAMES` order."""
        present: set[str] = set()
        for _flags, _delim, name in client.list_folders():
            if isinstance(name, (bytes, bytearray)):
                present.add(name.decode("utf-8", errors="replace"))
            else:
                present.add(name)
        for candidate in self._CONVENTIONAL_DRAFTS_NAMES:
            if candidate in present:
                return candidate
        return None

    def delete_messages(
        self,
        message_ids: list[str],
        source_mailbox: str,
    ) -> int:
        """Move messages to the account's Trash folder via IMAP. Issue #150.

        Mirrors :meth:`move_messages`'s capability dispatch but resolves
        the destination by discovering the server's Trash folder:

        - Prefer RFC 6154 SPECIAL-USE ``\\Trash``.
        - Fall back to a hard-coded list of conventional names
          (``Trash``, ``[Gmail]/Trash``, ``Deleted Messages``,
          ``Deleted Items``) by scanning the folder list.
        - Raise :class:`MailImapTrashNotFoundError` when neither finds
          anything; the orchestrator falls back to AppleScript.

        Same MOVE / UIDPLUS dispatch and partial-success semantics as
        :meth:`move_messages` (Message-IDs that don't resolve to a UID
        are silently skipped).

        Args:
            message_ids: RFC 5322 Message-IDs, with or without surrounding
                ``<>``.
            source_mailbox: Mailbox the messages currently live in.

        Returns:
            Count of resolved + moved messages.

        Raises:
            MailImapMoveUnsupportedError: Server advertises neither MOVE
                nor UIDPLUS.
            MailImapTrashNotFoundError: No Trash folder discoverable via
                SPECIAL-USE or conventional names.
            IMAPClientError: Protocol-level failure.
        """
        bracketed_ids = [_bracket_message_id(mid) for mid in message_ids]
        _reject_control_chars(source_mailbox, "source_mailbox")

        with self._session() as client:
            # #199 / #198: do all LIST traffic in AUTHENTICATED state.
            # Some servers (Exchange Online, older Dovecot) issue an
            # implicit CLOSE when LIST runs while a mailbox is SELECTED,
            # causing the subsequent SEARCH to fail with "No mailbox
            # selected". Resolve capabilities + Trash before SELECT so
            # all SELECT-state work happens in one uninterrupted window.
            has_move = self._has_capability(client, b"MOVE")
            has_uidplus = self._has_capability(client, b"UIDPLUS")
            if not has_move and not has_uidplus:
                raise MailImapMoveUnsupportedError(
                    f"IMAP server at {self._host} advertises neither MOVE "
                    f"(RFC 6851) nor UIDPLUS (RFC 4315); cannot perform a "
                    f"safe scoped move to Trash"
                )

            trash = self._find_trash_folder(client)
            if trash is None:
                trash = self._find_trash_by_convention(client)
            if trash is None:
                raise MailImapTrashNotFoundError(
                    f"IMAP server at {self._host} has no \\Trash "
                    f"SPECIAL-USE folder and none of "
                    f"{list(self._CONVENTIONAL_TRASH_NAMES)} were "
                    f"present in the folder listing"
                )

            client.select_folder(source_mailbox, readonly=False)

            uids: list[int] = []
            for bracketed in bracketed_ids:
                found = client.search(["HEADER", "Message-ID", bracketed])
                uids.extend(found)
            if not uids:
                return 0

            if has_move:
                client.move(uids, trash)
            else:
                client.copy(uids, trash)
                client.add_flags(uids, [b"\\Deleted"], silent=True)
                client.uid_expunge(uids)
            return len(uids)

    def set_read_status(
        self,
        message_ids: list[str],
        source_mailbox: str,
        read: bool,
    ) -> int:
        """Mark messages as read (read=True) or unread (read=False) via
        IMAP UID STORE on the \\Seen flag. Issue #151.

        \\Seen is RFC 3501 base IMAP — no capability negotiation
        needed, works against every server. Idempotent at the server
        level (re-setting an already-set flag is a no-op), matching
        the AppleScript path's behavior.

        Args:
            message_ids: RFC 5322 Message-IDs, with or without
                surrounding angle brackets.
            source_mailbox: Mailbox the messages live in.
            read: True to mark read (+\\Seen), False to mark unread
                (-\\Seen).

        Returns:
            Count of resolved + updated messages. Message-IDs that
            don't resolve to a UID are silently skipped (matches
            AppleScript).

        Raises:
            IMAPClientError: SELECT / SEARCH / STORE failed at the
                protocol level.
        """
        bracketed_ids = [_bracket_message_id(mid) for mid in message_ids]
        _reject_control_chars(source_mailbox, "source_mailbox")

        with self._session() as client:
            client.select_folder(source_mailbox, readonly=False)

            uids: list[int] = []
            for bracketed in bracketed_ids:
                found = client.search(["HEADER", "Message-ID", bracketed])
                uids.extend(found)
            if not uids:
                return 0

            if read:
                client.add_flags(uids, [b"\\Seen"], silent=True)
            else:
                client.remove_flags(uids, [b"\\Seen"], silent=True)
            return len(uids)

    def set_flagged_status(
        self,
        message_ids: list[str],
        source_mailbox: str,
        flagged: bool,
    ) -> int:
        """Set or clear the IMAP \\Flagged flag via UID STORE. Issue #152.

        Like \\Seen in #151, \\Flagged is RFC 3501 base IMAP — no
        capability negotiation needed, works against every server.
        Idempotent at the server level (re-setting an already-set flag
        is a no-op), matching the AppleScript path's behavior.

        Note that Mail.app's flag-color attributes (\\$MailFlagBit*
        user keywords) are Mail.app-specific and not in scope here.
        Callers who want a specific color must use the AppleScript
        path via ``update_message(flag_color=...)``. The bare
        \\Flagged set by this method renders as the default (red) flag
        in Mail.app — identical to today's ``update_message(flagged=True)``
        AppleScript path which sets ``flag index = 0`` and produces
        the same bare \\Flagged on the server (verified empirically
        against Gmail/Mail.app, 2026-05-12).

        Args:
            message_ids: RFC 5322 Message-IDs, with or without
                surrounding angle brackets.
            source_mailbox: Mailbox the messages live in.
            flagged: True to set \\Flagged, False to clear it.

        Returns:
            Count of resolved + updated messages. Message-IDs that
            don't resolve to a UID are silently skipped (matches
            AppleScript).

        Raises:
            IMAPClientError: SELECT / SEARCH / STORE failed at the
                protocol level.
        """
        bracketed_ids = [_bracket_message_id(mid) for mid in message_ids]
        _reject_control_chars(source_mailbox, "source_mailbox")

        with self._session() as client:
            client.select_folder(source_mailbox, readonly=False)

            uids: list[int] = []
            for bracketed in bracketed_ids:
                found = client.search(["HEADER", "Message-ID", bracketed])
                uids.extend(found)
            if not uids:
                return 0

            if flagged:
                client.add_flags(uids, [b"\\Flagged"], silent=True)
            else:
                client.remove_flags(uids, [b"\\Flagged"], silent=True)
            return len(uids)

    @staticmethod
    def _has_capability(client: IMAPClient, name: bytes) -> bool:
        """True if ``name`` (e.g. ``b"X-GM-EXT-1"``) is in the post-login
        capability list. IMAPClient caches CAPABILITY across the session;
        this is a local lookup, no round trip."""
        return name in client.capabilities()

    @staticmethod
    def _find_all_mail_folder(client: IMAPClient) -> str | None:
        """Return the Gmail All Mail folder name via the ``\\All``
        SPECIAL-USE flag, or None if not found.

        Hardcoding ``[Gmail]/All Mail`` would break on localized Gmail
        accounts (e.g. ``[Google Mail]/Tutta la posta``); the SPECIAL-USE
        flag is the standard way to find it regardless of label."""
        for flags, _delim, name in client.list_folders():
            if b"\\All" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    @staticmethod
    def _find_sent_folder(client: IMAPClient) -> str | None:
        """Return the Sent folder name via the ``\\Sent`` SPECIAL-USE flag,
        or None if not found. Used by Tier 1.5 (#125) as the second
        anchor-lookup target after INBOX — covers the common case of a
        thread anchored at a sent message."""
        for flags, _delim, name in client.list_folders():
            if b"\\Sent" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    @staticmethod
    def _find_trash_folder(client: IMAPClient) -> str | None:
        """Return the Trash folder name via the ``\\Trash`` SPECIAL-USE
        flag (RFC 6154), or None if the server doesn't advertise it.
        Used by ``delete_messages`` (#150); falls back to
        :meth:`_find_trash_by_convention` when this returns None."""
        for flags, _delim, name in client.list_folders():
            if b"\\Trash" in flags:
                if isinstance(name, (bytes, bytearray)):
                    return name.decode("utf-8", errors="replace")
                return cast(str, name)
        return None

    def _find_trash_by_convention(
        self, client: IMAPClient
    ) -> str | None:
        """Fall back to a hard-coded list of conventional Trash names
        for servers that don't advertise SPECIAL-USE ``\\Trash`` (#150).
        Scans the folder listing once and returns the first conventional
        name present, in :attr:`_CONVENTIONAL_TRASH_NAMES` order."""
        present: set[str] = set()
        for _flags, _delim, name in client.list_folders():
            if isinstance(name, (bytes, bytearray)):
                present.add(name.decode("utf-8", errors="replace"))
            else:
                present.add(name)
        for candidate in self._CONVENTIONAL_TRASH_NAMES:
            if candidate in present:
                return candidate
        return None

    @staticmethod
    def _merge_envelope_fetch_into(
        fetched: dict[int, dict[bytes, Any]],
        collected: dict[str, dict[str, Any]],
    ) -> None:
        """Merge a ``client.fetch([ENVELOPE, FLAGS])`` response into the
        cross-mailbox ``collected`` dict keyed by stripped
        rfc_message_id. Skips entries with no envelope or no
        message_id; first occurrence per id wins (dedup across
        mailboxes).

        Shared between Tier 1.5 (``_thread_via_xgm_per_mailbox``) and
        Tier 2 (``_thread_via_imap_thread``) — both run the identical
        per-mailbox loop after FETCH. (#194 / #195)
        """
        for fetch_entry in fetched.values():
            envelope = fetch_entry.get(b"ENVELOPE")
            if envelope is None:
                continue
            raw_msgid = getattr(envelope, "message_id", None)
            if not raw_msgid:
                continue
            clean_msgid = _strip_brackets(_decode(raw_msgid))
            if clean_msgid in collected:
                continue
            fetch_flags = tuple(fetch_entry.get(b"FLAGS", ()) or ())
            collected[clean_msgid] = _envelope_to_dict(
                envelope, fetch_flags
            )

    def _thread_via_xgm_thrid(
        self,
        client: IMAPClient,
        anchor_rfc_message_id: str,
    ) -> list[dict[str, Any]] | None:
        """Tier 1 (Gmail X-GM-THRID) implementation. Returns thread member
        dicts on success, OR ``None`` when the strategy can't run (no
        ``\\All`` folder, anchor not in All Mail, X-GM-THRID query
        rejected mid-flight). On None, the caller falls through to Tier 3.

        Returning None (rather than raising) keeps the dispatcher's
        control flow clean — exceptions are reserved for failures the
        orchestrator's ``_IMAP_FALLBACK_EXCS`` should observe."""
        all_mail = self._find_all_mail_folder(client)
        if all_mail is None:
            return None

        client.select_folder(all_mail, readonly=True)

        bracketed = _bracket_message_id(anchor_rfc_message_id)
        try:
            anchor_uids = client.search(["HEADER", "Message-ID", bracketed])
        except IMAPClientError:
            return None
        if not anchor_uids:
            return None

        try:
            thrid_fetch = client.fetch(anchor_uids[:1], [b"X-GM-THRID"])
        except IMAPClientError:
            return None
        thrid_entry: dict[bytes, Any] = next(iter(thrid_fetch.values()), {})
        thrid = thrid_entry.get(b"X-GM-THRID")
        if thrid is None:
            return None

        try:
            thread_uids = client.search(["X-GM-THRID", str(thrid)])
        except IMAPClientError:
            return None
        if not thread_uids:
            return None

        fetched = client.fetch(thread_uids, [b"ENVELOPE", b"FLAGS"])

        collected: dict[str, dict[str, Any]] = {}
        for fetch_entry in fetched.values():
            envelope = fetch_entry.get(b"ENVELOPE")
            if envelope is None:
                continue
            raw_msgid = getattr(envelope, "message_id", None)
            if not raw_msgid:
                continue
            clean_msgid = _strip_brackets(_decode(raw_msgid))
            if clean_msgid in collected:
                continue
            flags = tuple(fetch_entry.get(b"FLAGS", ()) or ())
            collected[clean_msgid] = _envelope_to_dict(envelope, flags)

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )

    def _thread_via_xgm_per_mailbox(
        self,
        client: IMAPClient,
        anchor_rfc_message_id: str,
    ) -> list[dict[str, Any]] | None:
        """Tier 1.5 (Gmail X-GM-THRID per-mailbox, #125).

        Used when ``X-GM-EXT-1`` is advertised but ``[Gmail]/All Mail`` is
        hidden over IMAP (Gmail Settings → Forwarding and POP/IMAP →
        Folder size limits). The per-mailbox X-GM-THRID search covers
        the same conversation-grouping the All Mail path would, just with
        ~M*2 round-trips instead of ~5.

        Returns ``None`` (not raise) when the strategy can't find the
        anchor or the X-GM-THRID query gets rejected — dispatcher then
        falls through to Tier 2 / Tier 3.
        """
        # Step 1: locate the anchor's UID. Try INBOX first, then \\Sent.
        bracketed = _bracket_message_id(anchor_rfc_message_id)
        anchor_uid: int | None = None
        for folder_name in self._anchor_lookup_folders(client):
            try:
                client.select_folder(folder_name, readonly=True)
            except IMAPClientError:
                continue
            try:
                uids = client.search(["HEADER", "Message-ID", bracketed])
            except IMAPClientError:
                continue
            if uids:
                anchor_uid = uids[0]
                break

        if anchor_uid is None:
            # Anchor not in INBOX or Sent → fall through to Tier 2 / Tier 3.
            return None

        # Step 2: get the anchor's X-GM-THRID.
        try:
            thrid_fetch = client.fetch([anchor_uid], [b"X-GM-THRID"])
        except IMAPClientError:
            return None
        thrid_entry: dict[bytes, Any] = next(iter(thrid_fetch.values()), {})
        thrid = thrid_entry.get(b"X-GM-THRID")
        if thrid is None:
            return None

        # Step 3: iterate every selectable folder, collect cluster UIDs by
        # X-GM-THRID, FETCH ENVELOPE+FLAGS for matches.
        collected: dict[str, dict[str, Any]] = {}
        thrid_str = str(thrid)
        for flags, _delim, raw_name in client.list_folders():
            if b"\\Noselect" in flags:
                continue
            if isinstance(raw_name, (bytes, bytearray)):
                folder_name = raw_name.decode("utf-8", errors="replace")
            else:
                folder_name = cast(str, raw_name)
            try:
                client.select_folder(folder_name, readonly=True)
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 1.5: skipping mailbox %s (SELECT rejected): %s",
                    folder_name, exc,
                )
                continue
            try:
                uids = client.search(["X-GM-THRID", thrid_str])
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 1.5: skipping mailbox %s (X-GM-THRID search "
                    "rejected): %s",
                    folder_name, exc,
                )
                continue
            if not uids:
                continue
            try:
                fetched = client.fetch(uids, [b"ENVELOPE", b"FLAGS"])
            except IMAPClientError:
                continue
            self._merge_envelope_fetch_into(fetched, collected)

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )

    def _anchor_lookup_folders(
        self, client: IMAPClient
    ) -> Iterator[str]:
        """Yield folder names to try in order when looking up the
        anchor's UID for Tier 1.5: INBOX first, then ``\\Sent`` (if
        distinct). Covers ~95% of cases per #125."""
        yield "INBOX"
        sent = self._find_sent_folder(client)
        if sent and sent != "INBOX":
            yield sent

    def _thread_via_imap_thread(
        self,
        client: IMAPClient,
        anchor_rfc_message_id: str,
        anchor_references: list[str],
    ) -> list[dict[str, Any]] | None:
        """Tier 2 (RFC 5256 THREAD, #123).

        Used when the server advertises ``THREAD=REFERENCES`` or
        ``THREAD=REFS`` (Fastmail, some Dovecot deployments). Per
        mailbox: SELECT, narrow-search for any UID containing the
        anchor's Message-ID or referencing it, then THREAD on the
        full mailbox to collect the cluster(s) those UIDs belong to,
        then FETCH ENVELOPE+FLAGS.

        Returns ``None`` (not raise) when THREAD gets rejected
        mid-flight — dispatcher then falls through to Tier 3.
        """
        bracketed = _bracket_message_id(anchor_rfc_message_id)
        # Pick the algorithm name the server actually advertises.
        if self._has_capability(client, b"THREAD=REFERENCES"):
            algo = "REFERENCES"
        elif self._has_capability(client, b"THREAD=REFS"):
            algo = "REFS"
        else:
            return None

        collected: dict[str, dict[str, Any]] = {}
        for flags, _delim, raw_name in client.list_folders():
            if b"\\Noselect" in flags:
                continue
            if isinstance(raw_name, (bytes, bytearray)):
                folder_name = raw_name.decode("utf-8", errors="replace")
            else:
                folder_name = cast(str, raw_name)
            try:
                client.select_folder(folder_name, readonly=True)
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 2: skipping mailbox %s (SELECT rejected): %s",
                    folder_name, exc,
                )
                continue
            # Narrow-search: anchor UID + sibling-replies in this mailbox.
            try:
                anchor_uids = client.search(
                    ["HEADER", "Message-ID", bracketed]
                )
                ref_uids = client.search(
                    ["HEADER", "References", bracketed]
                )
            except IMAPClientError:
                continue
            relevant_uids = set(anchor_uids) | set(ref_uids)
            if not relevant_uids:
                continue
            # Run THREAD; walk tree for clusters intersecting relevant_uids.
            try:
                tree = client.thread(
                    algorithm=algo, criteria="ALL", charset="UTF-8"
                )
            except IMAPClientError as exc:
                logger.debug(
                    "Tier 2: THREAD rejected on mailbox %s: %s. "
                    "Falling through to Tier 3.",
                    folder_name, exc,
                )
                # Mid-flight rejection — fall through to Tier 3 BFS,
                # which produces a *complete* thread by re-walking via
                # header search.
                #
                # Why THREAD-failure aborts and SELECT/search/fetch-
                # failures just `continue` (#172):
                #
                # - SELECT/search/fetch failures are bounded to one
                #   mailbox. Skipping that mailbox loses a few known-
                #   relevant UIDs but doesn't cast doubt on data
                #   already collected from earlier mailboxes.
                # - THREAD failure indicates capability-level
                #   unreliability: the server advertised THREAD but
                #   can't actually perform it on at least some
                #   mailboxes. That means THREAD output from earlier
                #   mailboxes may also have been silently wrong
                #   (truncated trees, missing references). Falling
                #   through to Tier 3 BFS re-validates the whole
                #   thread.
                #
                # We intentionally discard any partial `collected`
                # from earlier mailboxes here.
                return None
            cluster_uids: set[int] = set()
            for cluster in _flatten_thread_clusters(tree):
                if cluster & relevant_uids:
                    cluster_uids |= cluster
            if not cluster_uids:
                continue
            try:
                fetched = client.fetch(
                    sorted(cluster_uids), [b"ENVELOPE", b"FLAGS"]
                )
            except IMAPClientError:
                continue
            self._merge_envelope_fetch_into(fetched, collected)

        if not collected:
            # No mailbox in the account had any matching message — Tier 2
            # didn't help. Fall through.
            return None

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )

    @staticmethod
    def _find_thread_members_bfs(
        client: IMAPClient,
        anchor_rfc_message_id: str,
        anchor_references: list[str],
    ) -> list[dict[str, Any]]:
        """Tier 3: per-mailbox header-search BFS. Universal fallback —
        works against any IMAP server that supports basic SEARCH HEADER
        (RFC 3501).

        For each mailbox, searches the Message-ID / In-Reply-To /
        References headers for every known thread ID (the anchor plus
        its known ancestors). A single pass suffices because well-formed
        replies copy the entire References chain of their parent — so
        searching on the anchor's Message-ID against the References
        header captures all descendants regardless of tree depth.

        Cost: M × N × 3 SEARCHes + M SELECTs + up to M FETCHes, where
        M is mailbox count and N is the size of the known-IDs set.
        Slow on accounts with many mailboxes; #122 (X-GM-THRID) is the
        Gmail-specific optimization, #123 (RFC 5256 THREAD) the more
        general one."""
        known_ids: set[str] = {anchor_rfc_message_id} | set(anchor_references)
        mailboxes = client.list_folders()
        collected: dict[str, dict[str, Any]] = {}

        for _flags, _delimiter, raw_name in mailboxes:
            # imapclient returns names as str when its decoder succeeds,
            # bytes/bytearray on failure. Coerce to str either way.
            if isinstance(raw_name, (bytes, bytearray)):
                mailbox_name = raw_name.decode("utf-8", errors="replace")
            else:
                mailbox_name = raw_name

            try:
                client.select_folder(mailbox_name, readonly=True)
            except IMAPClientError as exc:
                # Some mailboxes (e.g. Gmail smart labels) reject SELECT.
                # Matches the AppleScript path's precedent of skipping
                # them silently.
                logger.debug(
                    "find_thread_members: skipping mailbox %s: %s",
                    mailbox_name, exc,
                )
                continue

            # Search for each known id across three header types. IMAP
            # returns UIDs whose specified header contains the given
            # substring; each search is server-side and indexed.
            uids_found: set[int] = set()
            for id_ in known_ids:
                id_quoted = _bracket_message_id(id_)
                for header in ("Message-ID", "In-Reply-To", "References"):
                    try:
                        uids = client.search(["HEADER", header, id_quoted])
                    except IMAPClientError as exc:
                        logger.debug(
                            "find_thread_members: search failed in %s for "
                            "%s=%s: %s",
                            mailbox_name, header, id_quoted, exc,
                        )
                        continue
                    uids_found.update(uids)

            if not uids_found:
                continue

            fetched = client.fetch(
                list(uids_found), [b"ENVELOPE", b"FLAGS"]
            )
            for fetch_entry in fetched.values():
                envelope = fetch_entry.get(b"ENVELOPE")
                if envelope is None:
                    continue
                raw_msgid = getattr(envelope, "message_id", None)
                if not raw_msgid:
                    continue
                clean_msgid = _strip_brackets(_decode(raw_msgid))
                if clean_msgid in collected:
                    continue
                flags = tuple(fetch_entry.get(b"FLAGS", ()) or ())
                collected[clean_msgid] = _envelope_to_dict(envelope, flags)

        return sorted(
            collected.values(),
            key=lambda m: m.get("date_received") or "",
        )
