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

from typing import Any

from imapclient import IMAPClient
from imapclient.response_types import Envelope

CONNECT_TIMEOUT_S: float = 3.0
"""Per invariant 4 in imap-auth-options-decision.md: ≤3s so offline
fallback happens inside the graceful-degradation window without
waiting for TCP's default timeout."""

_FLAG_SEEN = b"\\Seen"
_FLAG_FLAGGED = b"\\Flagged"


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
        client = IMAPClient(
            self._host, port=self._port, ssl=True, timeout=self._connect_timeout
        )
        try:
            client.login(self._email, self._password)
            client.select_folder(mailbox, readonly=True)

            criteria: list[Any] = ["ALL"]  # Filter building in later tasks.
            uids = client.search(criteria)
            if limit is not None:
                uids = uids[-limit:]

            if not uids:
                return []

            fetch_keys = [b"ENVELOPE", b"FLAGS"]
            fetched = client.fetch(uids, fetch_keys)
            return [
                _envelope_to_dict(
                    fetched[uid][b"ENVELOPE"], tuple(fetched[uid][b"FLAGS"])
                )
                for uid in uids
            ]
        finally:
            client.logout()
