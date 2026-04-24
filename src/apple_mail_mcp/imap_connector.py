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

import re
from datetime import date as _date
from datetime import timedelta as _timedelta
from typing import Any

from imapclient import IMAPClient
from imapclient.response_types import Envelope

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

_FLAG_SEEN = b"\\Seen"
_FLAG_FLAGGED = b"\\Flagged"

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


def _build_search_criteria(
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[Any]:
    """Translate ImapConnector.search_messages parameters to IMAP SEARCH criteria.

    Returns ``["ALL"]`` if no filters are supplied — IMAP SEARCH requires at
    least one criterion.
    """
    criteria: list[Any] = []
    if sender_contains:
        criteria.extend(["FROM", sender_contains])
    if subject_contains:
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
    return criteria or ["ALL"]


def _decode(b: bytes | str | None) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b


def _strip_brackets(s: str) -> str:
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1]
    return s


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


def _bodystructure_has_attachment(structure: Any) -> bool:
    """Walk an IMAPClient-parsed BODYSTRUCTURE tree and detect attachments.

    IMAPClient represents multipart as a tuple ``(part_tuple, ..., subtype)``
    where each ``part_tuple`` is either another multipart (starts with a
    tuple) or a leaf (starts with bytes like ``b"text"``, ``b"application"``).

    A message "has an attachment" if any leaf carries a disposition of
    ``attachment`` or ``inline`` with a ``filename`` parameter.
    """
    if not isinstance(structure, tuple) or not structure:
        return False

    # Multipart — first element is a nested tuple (sub-part).
    if isinstance(structure[0], tuple):
        for child in structure:
            if isinstance(child, tuple) and _bodystructure_has_attachment(child):
                return True
        return False

    # Leaf — scan for a disposition tuple whose first element is
    # b"attachment" or b"inline".
    for elem in structure:
        if (
            isinstance(elem, tuple)
            and elem
            and isinstance(elem[0], bytes)
        ):
            disp = elem[0].lower()
            if disp == b"attachment":
                return True
            if disp == b"inline":
                params = elem[1] if len(elem) > 1 else ()
                if isinstance(params, tuple):
                    # Params are a flat tuple (key, value, key, value, ...).
                    for i in range(0, len(params) - 1, 2):
                        key = params[i]
                        if isinstance(key, bytes) and key.lower() == b"filename":
                            return True
    return False


def _envelope_to_dict(
    envelope: Envelope, flags: tuple[bytes, ...]
) -> dict[str, Any]:
    date = envelope.date
    if hasattr(date, "isoformat"):
        date_str = date.isoformat()
    else:
        date_str = _decode(date)
    return {
        "id": _strip_brackets(_decode(envelope.message_id)),
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
    ) -> None:
        self._host = host
        self._port = port
        self._email = email
        self._password = password
        self._connect_timeout = connect_timeout

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
        )

        client = IMAPClient(
            self._host, port=self._port, ssl=True, timeout=self._connect_timeout
        )
        try:
            client.login(self._email, self._password)
            client.select_folder(mailbox, readonly=True)

            uids = client.search(criteria)
            if limit is not None:
                uids = uids[-limit:]

            if not uids:
                return []

            fetch_keys: list[bytes] = [b"ENVELOPE", b"FLAGS"]
            if has_attachment is not None:
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
                results.append(
                    _envelope_to_dict(entry[b"ENVELOPE"], tuple(entry[b"FLAGS"]))
                )
            return results
        finally:
            client.logout()
