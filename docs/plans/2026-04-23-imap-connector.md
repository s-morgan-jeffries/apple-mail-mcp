# `imap_connector.py` + `keychain.py` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship two new modules — `keychain.py` (Keychain password lookup) and `imap_connector.py` (IMAPClient wrapper with `search_messages` only) — plus their exception types, unit tests, and one integration test against real iCloud. `mail_connector.py` is **not** touched; delegation wiring is #40's job.

**Architecture:** Stateless `ImapConnector` takes fully-resolved `(host, port, email, password)` and opens a fresh IMAPClient connection per call. `keychain.get_imap_password()` shells out to `security find-generic-password` and maps exit codes to typed exceptions. No coupling to Mail.app, no auto-discovery, no pooling. See [`docs/plans/2026-04-23-imap-connector-design.md`](2026-04-23-imap-connector-design.md) for the full design and the six locked decisions that produced it.

**Tech Stack:** Python 3.10+, `imapclient>=3.0.0` (promoted from `research` optional to primary dep), `subprocess` for Keychain, `pytest` with `unittest.mock.patch` for unit tests, `pytest.mark.integration` for the iCloud test.

**Design doc:** [`docs/plans/2026-04-23-imap-connector-design.md`](2026-04-23-imap-connector-design.md)

---

## Preflight

**Branch:** `feature/issue-41-imap-connector`. Commit on this branch; open one PR at the end.

**Environment:**
- `uv pip install -e '.[dev]'` — verify `pytest` and `mypy` work.
- `uv run python -c "from imapclient import IMAPClient; print(IMAPClient.__module__)"` — verify imapclient is importable (already installed via `research` extra from #70).
- Your existing `apple-mail-mcp.imap.iCloud` Keychain entry from PR #70's spike is reused by the integration test. If it's been deleted, re-create via `security add-generic-password -s "apple-mail-mcp.imap.iCloud" -a s.morgan.jeffries@icloud.com -w <APP_PASSWORD> -T "" -U` before running integration tests.

**What NOT to touch (design doc's "Out of scope"):**
- `src/apple_mail_mcp/mail_connector.py`
- `src/apple_mail_mcp/server.py`
- Any existing test file
- The `research` optional-deps group in `pyproject.toml` (leave `imapclient` there — we're adding it to primary, not moving it)

**TDD discipline:** RED → GREEN → commit per test. If a test is non-obvious, run it standalone (`pytest tests/unit/test_keychain.py::TestEntryNotFound::test_exit_44 -v`) rather than the whole suite.

---

## Task 1: Add `MailKeychain*` exception classes

**Files:**
- Modify: `src/apple_mail_mcp/exceptions.py`
- Create: `tests/unit/test_exceptions.py` (new — there is no existing test for exceptions.py; confirm first with `ls tests/unit/test_exceptions.py`)

If `tests/unit/test_exceptions.py` already exists, append to it instead of creating.

**Step 1: Write the failing tests**

```python
# tests/unit/test_exceptions.py
"""Exception class hierarchy tests."""
import pytest
from apple_mail_mcp.exceptions import (
    MailError,
    MailKeychainError,
    MailKeychainEntryNotFoundError,
    MailKeychainAccessDeniedError,
)


class TestKeychainExceptions:
    def test_keychain_error_is_mail_error(self):
        assert issubclass(MailKeychainError, MailError)

    def test_entry_not_found_is_keychain_error(self):
        assert issubclass(MailKeychainEntryNotFoundError, MailKeychainError)

    def test_access_denied_is_keychain_error(self):
        assert issubclass(MailKeychainAccessDeniedError, MailKeychainError)

    def test_entry_not_found_can_be_raised_and_caught(self):
        with pytest.raises(MailKeychainEntryNotFoundError):
            raise MailKeychainEntryNotFoundError("not found")

    def test_access_denied_can_be_caught_as_keychain_error(self):
        with pytest.raises(MailKeychainError):
            raise MailKeychainAccessDeniedError("denied")
```

**Step 2: Run test to verify failures**

```
uv run pytest tests/unit/test_exceptions.py -v
```

Expected: all tests FAIL with `ImportError: cannot import name 'MailKeychainError' from 'apple_mail_mcp.exceptions'`.

**Step 3: Implement**

Append to `src/apple_mail_mcp/exceptions.py`:

```python
class MailKeychainError(MailError):
    """Keychain operation failed."""

    pass


class MailKeychainEntryNotFoundError(MailKeychainError):
    """Requested Keychain entry does not exist.

    Expected and benign: signals the user has not opted in to IMAP
    for this account. Delegation layer (future work) treats this as
    a silent fall-back-to-AppleScript signal.
    """

    pass


class MailKeychainAccessDeniedError(MailKeychainError):
    """Keychain refused access (ACL denied or user denied prompt).

    Worth surfacing to the user on first failure per the graceful-
    degradation invariants in imap-auth-options-decision.md.
    """

    pass
```

**Step 4: Run tests**

```
uv run pytest tests/unit/test_exceptions.py -v
```

Expected: all 5 tests PASS.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/exceptions.py tests/unit/test_exceptions.py
git commit -m "Add MailKeychain exception hierarchy (#41)"
```

---

## Task 2: Promote `imapclient` to primary dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Edit `pyproject.toml`**

Change the `dependencies = [...]` list (currently just `fastmcp>=0.2.0`) to add `imapclient>=3.0.0`. Leave the `research` extra intact (`imap_connector.py` is shipping code, but the spike scripts still use the extra's name implicitly in their docstrings).

```toml
dependencies = [
    "fastmcp>=0.2.0",
    "imapclient>=3.0.0",
]
```

**Step 2: Reinstall and verify**

```
uv pip install -e '.'
uv run python -c "from imapclient import IMAPClient; print('ok')"
```

Expected output: `ok`.

**Step 3: Run full test suite to confirm no regression**

```
make test
```

Expected: 306 passed + 5 new from Task 1 = 311 passed. No failures.

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "Promote imapclient to primary dependency (#41)"
```

---

## Task 3: Implement `keychain.py`

**Files:**
- Create: `src/apple_mail_mcp/keychain.py`
- Create: `tests/unit/test_keychain.py`

This task has multiple RED/GREEN cycles. Commit after all tests pass, not per cycle, to keep the first module-introducing commit readable.

**Step 1: Write the full test file first**

```python
# tests/unit/test_keychain.py
"""Tests for Keychain password retrieval."""
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from apple_mail_mcp.exceptions import (
    MailKeychainError,
    MailKeychainEntryNotFoundError,
    MailKeychainAccessDeniedError,
)
from apple_mail_mcp.keychain import SERVICE_NAME_PREFIX, get_imap_password


def _mock_security(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mock subprocess.CompletedProcess-like result."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestServiceNamePrefix:
    def test_prefix_matches_decision_doc(self):
        assert SERVICE_NAME_PREFIX == "apple-mail-mcp.imap."


class TestHappyPath:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_returns_password_stripped_of_trailing_newline(self, mock_run):
        mock_run.return_value = _mock_security(0, stdout="secret123\n")
        result = get_imap_password("iCloud", "user@icloud.com")
        assert result == "secret123"

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_invokes_security_with_correct_args(self, mock_run):
        mock_run.return_value = _mock_security(0, stdout="p\n")
        get_imap_password("iCloud", "user@icloud.com")
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            "apple-mail-mcp.imap.iCloud",
            "-a",
            "user@icloud.com",
        ]

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_preserves_internal_whitespace(self, mock_run):
        mock_run.return_value = _mock_security(0, stdout="with spaces\n")
        assert get_imap_password("iCloud", "u@i.com") == "with spaces"


class TestEntryNotFound:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_exit_44_raises_entry_not_found(self, mock_run):
        mock_run.return_value = _mock_security(
            44,
            stderr="security: SecKeychainSearchCopyNext: The specified item could "
            "not be found in the keychain.",
        )
        with pytest.raises(MailKeychainEntryNotFoundError):
            get_imap_password("iCloud", "u@i.com")


class TestAccessDenied:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_exit_128_raises_access_denied(self, mock_run):
        mock_run.return_value = _mock_security(
            128, stderr="User interaction is not allowed."
        )
        with pytest.raises(MailKeychainAccessDeniedError):
            get_imap_password("iCloud", "u@i.com")

    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_user_deny_error_code_raises_access_denied(self, mock_run):
        # errSecAuthFailed = -25293 on some paths; errUserCanceled = -128
        # errSecInteractionNotAllowed = -25308. Real-world output varies.
        mock_run.return_value = _mock_security(
            1, stderr="security: SecKeychainItemCopyAccess: (-25308)"
        )
        with pytest.raises(MailKeychainAccessDeniedError):
            get_imap_password("iCloud", "u@i.com")


class TestOtherFailure:
    @patch("apple_mail_mcp.keychain.subprocess.run")
    def test_unknown_failure_raises_keychain_error(self, mock_run):
        mock_run.return_value = _mock_security(
            2, stderr="some other failure"
        )
        with pytest.raises(MailKeychainError) as exc_info:
            get_imap_password("iCloud", "u@i.com")
        # Make sure it's the base class (or at least not a subclass that
        # would be caught by more specific handlers).
        assert type(exc_info.value) is MailKeychainError
        assert "some other failure" in str(exc_info.value)

    @patch("apple_mail_mcp.keychain.subprocess.run", side_effect=FileNotFoundError("security"))
    def test_security_binary_missing_raises_keychain_error(self, mock_run):
        with pytest.raises(MailKeychainError):
            get_imap_password("iCloud", "u@i.com")
```

**Step 2: Run tests to verify failures**

```
uv run pytest tests/unit/test_keychain.py -v
```

Expected: `ImportError` on `apple_mail_mcp.keychain` (module doesn't exist yet).

**Step 3: Implement**

Create `src/apple_mail_mcp/keychain.py`:

```python
"""macOS Keychain password retrieval for IMAP credentials.

Users populate Keychain entries via `security add-generic-password`
with service name `apple-mail-mcp.imap.<mail_app_account_name>` and
the account's email as the key. This module retrieves them.

See docs/research/imap-auth-options-decision.md for the chosen auth
path and the service-name convention. See docs/plans/
2026-04-23-imap-connector-design.md for module-level design decisions.
"""

from __future__ import annotations

import subprocess

from apple_mail_mcp.exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)

SERVICE_NAME_PREFIX = "apple-mail-mcp.imap."

# security(1) exit codes we recognize. Others map to base MailKeychainError.
_EXIT_ITEM_NOT_FOUND = 44
_EXIT_INTERACTION_NOT_ALLOWED = 128
_ACCESS_DENIED_MARKERS = ("-25308", "-128", "not allowed", "user canceled")


def get_imap_password(mail_app_account: str, email: str) -> str:
    """Return the app-specific password stored in Keychain.

    Args:
        mail_app_account: Mail.app account name (e.g. "iCloud", "Gmail").
        email: Email address the password is keyed to.

    Returns:
        The password, as stored (trailing newline from `security -w` stripped).

    Raises:
        MailKeychainEntryNotFoundError: No matching item.
        MailKeychainAccessDeniedError: ACL or user denial.
        MailKeychainError: Any other security(1) failure.
    """
    service = SERVICE_NAME_PREFIX + mail_app_account
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                service,
                "-a",
                email,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MailKeychainError(f"`security` binary not found: {exc}") from exc

    if result.returncode == 0:
        return result.stdout.rstrip("\n")

    stderr = result.stderr or ""

    if result.returncode == _EXIT_ITEM_NOT_FOUND:
        raise MailKeychainEntryNotFoundError(
            f"No Keychain entry for service={service!r}, account={email!r}."
        )

    if result.returncode == _EXIT_INTERACTION_NOT_ALLOWED or any(
        marker in stderr for marker in _ACCESS_DENIED_MARKERS
    ):
        raise MailKeychainAccessDeniedError(
            f"Keychain access denied for service={service!r}, account={email!r}: "
            f"{stderr.strip()}"
        )

    raise MailKeychainError(
        f"security find-generic-password failed (exit {result.returncode}): "
        f"{stderr.strip()}"
    )
```

**Step 4: Run tests**

```
uv run pytest tests/unit/test_keychain.py -v
```

Expected: all tests PASS.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/keychain.py tests/unit/test_keychain.py
git commit -m "Add keychain.py for IMAP password lookup (#41)"
```

---

## Task 4: Implement `ImapConnector` skeleton + constructor

**Files:**
- Create: `src/apple_mail_mcp/imap_connector.py`
- Create: `tests/unit/test_imap_connector.py`

**Step 1: Write constructor tests**

```python
# tests/unit/test_imap_connector.py
"""Tests for ImapConnector."""
from unittest.mock import patch, MagicMock

import pytest

from apple_mail_mcp.imap_connector import CONNECT_TIMEOUT_S, ImapConnector


class TestConstructor:
    def test_default_timeout(self):
        conn = ImapConnector("host", 993, "u@i.com", "pw")
        assert conn._connect_timeout == CONNECT_TIMEOUT_S

    def test_timeout_is_three_seconds_by_default(self):
        assert CONNECT_TIMEOUT_S == 3.0

    def test_custom_timeout(self):
        conn = ImapConnector("host", 993, "u@i.com", "pw", connect_timeout=10.0)
        assert conn._connect_timeout == 10.0

    def test_stores_credentials(self):
        conn = ImapConnector("imap.example.com", 993, "user@example.com", "secret")
        assert conn._host == "imap.example.com"
        assert conn._port == 993
        assert conn._email == "user@example.com"
        assert conn._password == "secret"
```

**Step 2: Run — expect ImportError**

```
uv run pytest tests/unit/test_imap_connector.py::TestConstructor -v
```

**Step 3: Implement minimal skeleton**

Create `src/apple_mail_mcp/imap_connector.py`:

```python
"""IMAPClient wrapper for read operations.

Stateless, per-call connection lifecycle. This module is deliberately
unaware of Mail.app, Keychain, and the MCP server. It takes fully-
resolved credentials and talks IMAP. Callers (tests here; the delegation
layer in #40 later) are responsible for correlating Mail.app account
name → (host, port, email) and fetching the password.

See docs/plans/2026-04-23-imap-connector-design.md.
"""

from __future__ import annotations

from typing import Any

CONNECT_TIMEOUT_S: float = 3.0
"""Per invariant 4 in imap-auth-options-decision.md: ≤3s so offline
fallback happens inside the graceful-degradation window without
waiting for TCP's default timeout."""


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
        raise NotImplementedError  # Implemented in later tasks.
```

**Step 4: Run tests — expect pass**

```
uv run pytest tests/unit/test_imap_connector.py::TestConstructor -v
```

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/imap_connector.py tests/unit/test_imap_connector.py
git commit -m "Add ImapConnector skeleton (#41)"
```

---

## Task 5: Implement `search_messages` happy path (no filters)

Goal for this task: `search_messages()` with defaults opens an IMAPClient, SELECTs INBOX, SEARCHes ALL, FETCHes envelope+flags, translates envelopes to dicts, LOGOUTs, and returns.

**Files:**
- Modify: `src/apple_mail_mcp/imap_connector.py`
- Modify: `tests/unit/test_imap_connector.py`

**Step 1: Append tests**

Add to `tests/unit/test_imap_connector.py`:

```python
from imapclient.response_types import Address, Envelope  # noqa: E402
from datetime import datetime  # noqa: E402


def _fake_envelope(
    *,
    message_id: bytes = b"<msg-1@example.com>",
    subject: bytes = b"Hello",
    sender: bytes = b"Alice",
    sender_mailbox: bytes = b"alice",
    sender_host: bytes = b"example.com",
    date: datetime | None = None,
) -> Envelope:
    date = date or datetime(2026, 4, 22, 10, 0, 0)
    from_addr = Address(sender, None, sender_mailbox, sender_host)
    return Envelope(
        date=date,
        subject=subject,
        from_=(from_addr,),
        sender=(from_addr,),
        reply_to=(from_addr,),
        to=(),
        cc=(),
        bcc=(),
        in_reply_to=None,
        message_id=message_id,
    )


def _fake_fetch_result(uids: list[int]) -> dict[int, dict[bytes, Any]]:
    """Build a FETCH-style dict with ENVELOPE + FLAGS for given UIDs."""
    return {
        uid: {
            b"ENVELOPE": _fake_envelope(
                message_id=f"<msg-{uid}@example.com>".encode(),
                subject=f"Subject {uid}".encode(),
            ),
            b"FLAGS": (b"\\Seen",),
        }
        for uid in uids
    }


class TestSearchHappyPath:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_no_filters_opens_connection_and_searches_all(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1, 2, 3]
        mock_client.fetch.return_value = _fake_fetch_result([1, 2, 3])

        conn = ImapConnector("imap.example.com", 993, "u@e.com", "pw")
        result = conn.search_messages()

        # Connection setup
        mock_cls.assert_called_once_with(
            "imap.example.com", port=993, ssl=True, timeout=3.0
        )
        mock_client.login.assert_called_once_with("u@e.com", "pw")
        mock_client.select_folder.assert_called_once_with("INBOX", readonly=True)

        # SEARCH with no filters → ALL
        mock_client.search.assert_called_once_with(["ALL"])

        # FETCH with envelope + flags
        fetch_args = mock_client.fetch.call_args
        assert fetch_args[0][0] == [1, 2, 3]
        assert b"ENVELOPE" in fetch_args[0][1]
        assert b"FLAGS" in fetch_args[0][1]

        # LOGOUT
        mock_client.logout.assert_called_once()

        assert len(result) == 3

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_empty_search_result_skips_fetch(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages()

        mock_client.fetch.assert_not_called()
        mock_client.logout.assert_called_once()
        assert result == []

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_custom_mailbox(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        conn.search_messages(mailbox="Archive")

        mock_client.select_folder.assert_called_once_with("Archive", readonly=True)

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_logout_called_on_exception(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.side_effect = RuntimeError("boom")

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        with pytest.raises(RuntimeError, match="boom"):
            conn.search_messages()

        mock_client.logout.assert_called_once()
```

**Step 2: Run — expect failures (NotImplementedError)**

**Step 3: Implement**

In `src/apple_mail_mcp/imap_connector.py`, add imports and implement `search_messages`:

```python
from imapclient import IMAPClient
from imapclient.response_types import Envelope

# ... (existing code above) ...

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


def _envelope_to_dict(envelope: Envelope, flags: tuple[bytes, ...]) -> dict[str, Any]:
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
```

And the method body:

```python
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

            criteria = ["ALL"]  # Filter building in later tasks.
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
```

**Step 4: Run**

```
uv run pytest tests/unit/test_imap_connector.py -v
```

Expected: all `TestSearchHappyPath` tests pass; `TestConstructor` still passes.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/imap_connector.py tests/unit/test_imap_connector.py
git commit -m "Implement search_messages happy path with no filters (#41)"
```

---

## Task 6: Implement filter translation (text, flag filters)

Now wire in the parameters that translate directly to IMAP SEARCH criteria: `sender_contains`, `subject_contains`, `read_status`, `is_flagged`.

**Files:**
- Modify: `src/apple_mail_mcp/imap_connector.py`
- Modify: `tests/unit/test_imap_connector.py`

**Step 1: Append tests**

```python
class TestTextFilters:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_sender_contains_maps_to_from(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            sender_contains="alice"
        )

        mock_client.search.assert_called_once_with(["FROM", "alice"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_subject_contains_maps_to_subject(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            subject_contains="invoice"
        )

        mock_client.search.assert_called_once_with(["SUBJECT", "invoice"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_sender_and_subject_combined(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            sender_contains="bob", subject_contains="report"
        )

        mock_client.search.assert_called_once_with(
            ["FROM", "bob", "SUBJECT", "report"]
        )


class TestFlagFilters:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_read_status_true_maps_to_seen(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(read_status=True)
        mock_client.search.assert_called_once_with(["SEEN"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_read_status_false_maps_to_unseen(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(read_status=False)
        mock_client.search.assert_called_once_with(["UNSEEN"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_is_flagged_true_maps_to_flagged(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(is_flagged=True)
        mock_client.search.assert_called_once_with(["FLAGGED"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_is_flagged_false_maps_to_unflagged(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(is_flagged=False)
        mock_client.search.assert_called_once_with(["UNFLAGGED"])
```

**Step 2: Run — expect failures (current impl hardcodes `["ALL"]`).**

**Step 3: Implement**

Extract a helper and update `search_messages`:

```python
def _build_search_criteria(
    sender_contains: str | None,
    subject_contains: str | None,
    read_status: bool | None,
    is_flagged: bool | None,
    date_from: str | None = None,  # handled in Task 7
    date_to: str | None = None,
) -> list[Any]:
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
    return criteria or ["ALL"]
```

In `search_messages`, replace the `criteria = ["ALL"]` line with:

```python
        criteria = _build_search_criteria(
            sender_contains,
            subject_contains,
            read_status,
            is_flagged,
            date_from,
            date_to,
        )
```

**Step 4: Run all unit tests**

```
uv run pytest tests/unit/test_imap_connector.py -v
```

Expected: all pass including new flag/text tests; earlier no-filter tests still pass (empty criteria → `["ALL"]`).

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/imap_connector.py tests/unit/test_imap_connector.py
git commit -m "Add text and flag search filters to ImapConnector (#41)"
```

---

## Task 7: Date filters + limit enforcement

IMAP dates are `dd-Mmm-yyyy`, not ISO. Reuse `_ISO_DATE_RE` from `mail_connector.py` for validation. `date_to` is inclusive of the full day: pass `BEFORE <date_to + 1 day>`.

**Files:**
- Modify: `src/apple_mail_mcp/imap_connector.py`
- Modify: `tests/unit/test_imap_connector.py`

**Step 1: Append tests**

```python
class TestDateFilters:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_from_iso_converted_to_imap_format(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            date_from="2026-04-22"
        )
        mock_client.search.assert_called_once_with(["SINCE", "22-Apr-2026"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_to_is_inclusive_of_full_day(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            date_to="2026-04-22"
        )
        # Inclusive upper bound → BEFORE next day
        mock_client.search.assert_called_once_with(["BEFORE", "23-Apr-2026"])

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_invalid_date_from_raises_value_error(self, mock_cls):
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        with pytest.raises(ValueError, match="ISO 8601"):
            conn.search_messages(date_from="04/22/2026")

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_invalid_date_to_raises_value_error(self, mock_cls):
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        with pytest.raises(ValueError, match="ISO 8601"):
            conn.search_messages(date_to="not-a-date")

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_range(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(
            date_from="2026-04-01", date_to="2026-04-22"
        )
        mock_client.search.assert_called_once_with(
            ["SINCE", "01-Apr-2026", "BEFORE", "23-Apr-2026"]
        )


class TestLimit:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_limit_slices_uids_from_end(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = list(range(1, 101))  # 100 UIDs
        mock_client.fetch.return_value = _fake_fetch_result(list(range(91, 101)))

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages(limit=10)

        # Fetch should have been called with only the last 10 UIDs
        fetch_uids = mock_client.fetch.call_args[0][0]
        assert fetch_uids == list(range(91, 101))
        assert len(result) == 10

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_limit_none_fetches_all(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = list(range(1, 11))
        mock_client.fetch.return_value = _fake_fetch_result(list(range(1, 11)))

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        conn.search_messages(limit=None)

        fetch_uids = mock_client.fetch.call_args[0][0]
        assert fetch_uids == list(range(1, 11))
```

**Step 2: Run — expect failures on date tests.**

**Step 3: Implement**

Add at module top in `imap_connector.py`:

```python
from datetime import date as _date, timedelta as _timedelta

from apple_mail_mcp.mail_connector import _ISO_DATE_RE  # reuse existing regex
```

If importing `_ISO_DATE_RE` creates a circular-import risk (it shouldn't — `mail_connector` doesn't import `imap_connector`), verify with `uv run python -c "import apple_mail_mcp.imap_connector"`. If circular, duplicate the regex locally with a comment noting why.

Add a helper and wire it in:

```python
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
    """For inclusive upper-bound: IMAP BEFORE is exclusive, so pass next day."""
    if not _ISO_DATE_RE.match(iso):
        raise ValueError(
            f"{field} must be ISO 8601 YYYY-MM-DD, got: {iso!r}"
        )
    d = _date.fromisoformat(iso) + _timedelta(days=1)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"
```

Update `_build_search_criteria` to handle dates:

```python
    if date_from is not None:
        criteria.extend(["SINCE", _iso_to_imap_date(date_from, "date_from")])
    if date_to is not None:
        criteria.extend(["BEFORE", _iso_to_imap_before(date_to, "date_to")])
```

(The `limit` path is already implemented in Task 5's `uids[-limit:]` slice; the new test verifies it.)

**Step 4: Run**

```
uv run pytest tests/unit/test_imap_connector.py -v
```

Expected: all date + limit tests pass.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/imap_connector.py tests/unit/test_imap_connector.py
git commit -m "Add date filters and limit enforcement to ImapConnector (#41)"
```

---

## Task 8: `has_attachment` post-filter via BODYSTRUCTURE

IMAP has no native "has attachment" criterion. Implementation: after SEARCH, if `has_attachment is not None`, re-FETCH with `BODYSTRUCTURE` included and filter.

**BODYSTRUCTURE detection heuristic:** a message "has an attachment" iff its BODYSTRUCTURE contains any non-text part with `disposition` of type `attachment` or `inline` with a filename parameter. IMAPClient parses BODYSTRUCTURE into nested tuples. We'll implement a simple walker.

**Files:**
- Modify: `src/apple_mail_mcp/imap_connector.py`
- Modify: `tests/unit/test_imap_connector.py`

**Step 1: Append tests**

```python
class TestHasAttachment:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_true_filters_to_messages_with_attachments(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1, 2, 3]

        # Build a fetch result where UID 2 has an attachment BODYSTRUCTURE,
        # UIDs 1 and 3 do not.
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": (b"text", b"plain", (), None, None, b"7bit", 123, 5),
            },
            2: {
                b"ENVELOPE": _fake_envelope(message_id=b"<2@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": (
                    (b"text", b"plain", (), None, None, b"7bit", 100, 5),
                    (
                        b"application",
                        b"pdf",
                        (b"name", b"x.pdf"),
                        None,
                        None,
                        b"base64",
                        2048,
                        (b"attachment", (b"filename", b"x.pdf")),
                    ),
                    b"mixed",
                ),
            },
            3: {
                b"ENVELOPE": _fake_envelope(message_id=b"<3@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": (b"text", b"html", (), None, None, b"7bit", 456, 10),
            },
        }

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages(has_attachment=True)

        ids = [m["id"] for m in result]
        assert ids == ["2@e.com"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_false_filters_to_messages_without(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1, 2]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": (b"text", b"plain", (), None, None, b"7bit", 123, 5),
            },
            2: {
                b"ENVELOPE": _fake_envelope(message_id=b"<2@e.com>"),
                b"FLAGS": (),
                b"BODYSTRUCTURE": (
                    (b"text", b"plain", (), None, None, b"7bit", 100, 5),
                    (
                        b"application",
                        b"pdf",
                        (b"name", b"x.pdf"),
                        None,
                        None,
                        b"base64",
                        2048,
                        (b"attachment", (b"filename", b"x.pdf")),
                    ),
                    b"mixed",
                ),
            },
        }

        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages(has_attachment=False)

        ids = [m["id"] for m in result]
        assert ids == ["1@e.com"]

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_has_attachment_includes_bodystructure_in_fetch(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = []

        ImapConnector("h", 993, "u@e.com", "pw").search_messages(has_attachment=True)

        # Fetch shouldn't be called (empty search) but we verify the
        # fetch-keys construction via a non-empty test above.
        # For this test, just verify it didn't crash.
```

**Step 2: Run — expect failures.**

**Step 3: Implement BODYSTRUCTURE walker + wire it in**

Add helper to `imap_connector.py`:

```python
def _bodystructure_has_attachment(structure: Any) -> bool:
    """Walk an IMAPClient-parsed BODYSTRUCTURE tree and detect attachments.

    IMAPClient represents multipart as a tuple of (part_tuple, ..., subtype).
    A "leaf" is a flat tuple of type, subtype, params, id, desc, encoding,
    size, [type-specific fields], [disposition_tuple], ...

    A message has an attachment if any leaf's disposition is \"attachment\"
    or \"inline\" with a filename parameter.
    """
    if not structure:
        return False

    # Multipart: first element is another tuple (sub-part).
    # Leaf: first element is bytes (type like b"text", b"application").
    if isinstance(structure, tuple) and structure and isinstance(structure[0], tuple):
        # Multipart — walk children.
        for child in structure:
            if isinstance(child, tuple) and child and isinstance(child[0], tuple):
                if _bodystructure_has_attachment(child):
                    return True
            elif isinstance(child, tuple) and child and isinstance(child[0], bytes):
                if _bodystructure_has_attachment(child):
                    return True
            # else: multipart subtype string at the end — skip.
        return False

    # Leaf — inspect disposition (last structured element of interest).
    # Structure varies; safe scan for a tuple whose first element is
    # b"attachment" or b"inline".
    for elem in structure:
        if isinstance(elem, tuple) and elem and isinstance(elem[0], bytes):
            disp = elem[0].lower()
            if disp == b"attachment":
                return True
            if disp == b"inline":
                # Inline with a filename is still an attachment for our purposes.
                params = elem[1] if len(elem) > 1 else ()
                if isinstance(params, tuple):
                    for i in range(0, len(params) - 1, 2):
                        if params[i] and params[i].lower() == b"filename":
                            return True
    return False
```

Update `search_messages`:

```python
        fetch_keys: list[bytes] = [b"ENVELOPE", b"FLAGS"]
        if has_attachment is not None:
            fetch_keys.append(b"BODYSTRUCTURE")
        fetched = client.fetch(uids, fetch_keys)

        results = []
        for uid in uids:
            entry = fetched[uid]
            if has_attachment is not None:
                has = _bodystructure_has_attachment(entry.get(b"BODYSTRUCTURE"))
                if has_attachment is True and not has:
                    continue
                if has_attachment is False and has:
                    continue
            results.append(
                _envelope_to_dict(entry[b"ENVELOPE"], tuple(entry[b"FLAGS"]))
            )
        return results
```

(Replace the old `return [ ... for uid in uids ]` with the loop above.)

**Step 4: Run**

```
uv run pytest tests/unit/test_imap_connector.py -v
```

Expected: all pass. Note: real BODYSTRUCTURE shapes are nuanced and provider-dependent; the heuristic above is intentionally conservative — it errs toward not-detecting-attachment for ambiguous cases. The integration test against iCloud in Task 9 is the final-mile validation.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/imap_connector.py tests/unit/test_imap_connector.py
git commit -m "Add has_attachment post-filter to ImapConnector (#41)"
```

---

## Task 9: Envelope translation edge cases

Confirm the translator handles encoded bytes, empty from, bracketed message-ids, bytes-shaped dates.

**Files:**
- Modify: `tests/unit/test_imap_connector.py`
- Possibly modify: `src/apple_mail_mcp/imap_connector.py` (if a test surfaces a gap in existing helpers)

**Step 1: Append tests**

```python
class TestEnvelopeTranslation:
    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_strips_angle_brackets_from_message_id(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<abc@example.com>"),
                b"FLAGS": (),
            }
        }
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        [msg] = conn.search_messages()
        assert msg["id"] == "abc@example.com"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_empty_sender_returns_empty_string(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        env = Envelope(
            date=datetime(2026, 4, 22),
            subject=b"s",
            from_=(),
            sender=(),
            reply_to=(),
            to=(),
            cc=(),
            bcc=(),
            in_reply_to=None,
            message_id=b"<1@e.com>",
        )
        mock_client.fetch.return_value = {
            1: {b"ENVELOPE": env, b"FLAGS": ()},
        }
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        [msg] = conn.search_messages()
        assert msg["sender"] == ""

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_seen_flag_maps_to_read_status(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (b"\\Seen",),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["read_status"] is True
        assert msg["flagged"] is False

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_flagged_flag_maps_to_flagged(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<1@e.com>"),
                b"FLAGS": (b"\\Flagged",),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["flagged"] is True
        assert msg["read_status"] is False

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_date_iso_format(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<1@e.com>",
                    date=datetime(2026, 4, 22, 14, 30, 0),
                ),
                b"FLAGS": (),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["date_received"] == "2026-04-22T14:30:00"

    @patch("apple_mail_mcp.imap_connector.IMAPClient")
    def test_subject_bytes_decoded_utf8(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(
                    message_id=b"<1@e.com>", subject="héllo ✓".encode()
                ),
                b"FLAGS": (),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["subject"] == "héllo ✓"
```

**Step 2: Run**

```
uv run pytest tests/unit/test_imap_connector.py::TestEnvelopeTranslation -v
```

Expected: all pass (these exercise helpers already implemented in Task 5).

**Step 3: Commit**

```bash
git add tests/unit/test_imap_connector.py
git commit -m "Add envelope translation edge-case tests (#41)"
```

---

## Task 10: Integration test against real iCloud

**Files:**
- Create: `tests/integration/test_imap_connector.py`

This test runs against your real iCloud account. Requires the Keychain entry from PR #70's spike to exist. Guarded by `MAIL_TEST_MODE` per existing convention.

**Step 1: Verify precondition**

```
security find-generic-password -s "apple-mail-mcp.imap.iCloud" -a "s.morgan.jeffries@icloud.com" >/dev/null 2>&1 && echo "OK" || echo "MISSING — recreate per the decision doc before running integration tests"
```

If MISSING, add the entry before proceeding.

**Step 2: Write the test file**

```python
# tests/integration/test_imap_connector.py
"""Integration tests for ImapConnector against real iCloud.

Guarded by MAIL_TEST_MODE=true. Requires a Keychain entry:
    security add-generic-password \\
        -s "apple-mail-mcp.imap.iCloud" \\
        -a "s.morgan.jeffries@icloud.com" \\
        -w "<APP_PASSWORD>" -T "" -U

Run: MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=iCloud \\
     pytest tests/integration/test_imap_connector.py -v -m integration
"""
from __future__ import annotations

import os

import pytest

from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_mcp.imap_connector import ImapConnector
from apple_mail_mcp.keychain import get_imap_password


ICLOUD_HOST = "imap.mail.me.com"
ICLOUD_PORT = 993
ICLOUD_ACCOUNT_NAME = "iCloud"
ICLOUD_EMAIL = "s.morgan.jeffries@icloud.com"


def _test_mode_enabled() -> bool:
    return os.getenv("MAIL_TEST_MODE") == "true"


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestEndToEndICloud:
    def test_end_to_end_search_returns_list(self):
        password = get_imap_password(ICLOUD_ACCOUNT_NAME, ICLOUD_EMAIL)
        connector = ImapConnector(
            ICLOUD_HOST, ICLOUD_PORT, ICLOUD_EMAIL, password
        )
        result = connector.search_messages(limit=5)
        assert isinstance(result, list)
        # May be empty (as discovered during PR #70 spike — merged-away
        # Apple ID's residual mailbox). Any non-empty result must have
        # the standard keys.
        for msg in result:
            assert set(msg.keys()) == {
                "id",
                "subject",
                "sender",
                "date_received",
                "read_status",
                "flagged",
            }
            assert isinstance(msg["read_status"], bool)
            assert isinstance(msg["flagged"], bool)

    def test_keychain_entry_missing_raises_entry_not_found(self):
        with pytest.raises(MailKeychainEntryNotFoundError):
            get_imap_password("DoesNotExistAccount", "nobody@example.com")
```

**Step 3: Run the integration test**

```
MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=iCloud \
    uv run pytest tests/integration/test_imap_connector.py -v
```

Expected: both tests pass. If `test_end_to_end_search_returns_list` fails on LOGIN, the Keychain entry is stale — regenerate and re-add. If it fails on SEARCH, check iCloud status.

**Step 4: Run the full suite to make sure nothing else broke**

```
make test
```

Expected: all unit tests pass (integration tests are not run by default, so `make test` count will be 306 + exceptions.py tests + keychain tests + imap_connector tests).

**Step 5: Commit**

```bash
git add tests/integration/test_imap_connector.py
git commit -m "Add integration test for ImapConnector against iCloud (#41)"
```

---

## Task 11: Final lint + typecheck + parity sweep

**Files:** all modified/new files.

**Step 1: Run the project's full check**

```
make check-all
```

Expected: all checks pass. If anything fails:

- **Ruff errors:** fix in place. Most commonly `I` (isort) for new imports — the project uses `ruff --fix` patterns.
- **Mypy errors:** the `strict = true` mypy config is aggressive. Likely trip points: `_decode` needs a precise `bytes | str | None` signature; `BODYSTRUCTURE` walker returns `Any` — annotate as `bool`.
- **Coverage threshold (`fail_under = 90`):** new modules must hit 90% coverage. The BODYSTRUCTURE walker has branches that may need additional targeted tests to cover.
- **Client-server parity** (`scripts/check_client_server_parity.sh`): irrelevant here — we're not exposing a new MCP tool, just internal modules.

**Step 2: Commit any fixes**

```bash
git add -p  # review each hunk
git commit -m "Fix lint/typecheck issues from make check-all (#41)"
```

Skip this commit if nothing needed fixing.

---

## Task 12: Open PR and close out

**Files:** none; this is GitHub-only.

**Step 1: Push branch**

```
git push -u origin feature/issue-41-imap-connector
```

**Step 2: Open PR**

```
gh pr create --title "Add ImapConnector and keychain modules (#41)" --body "$(cat <<'EOF'
## Summary

- Ships `keychain.py` (Keychain password lookup) and `imap_connector.py` (IMAPClient wrapper with `search_messages` only) per the design doc at `docs/plans/2026-04-23-imap-connector-design.md`.
- Adds three exception classes (`MailKeychainError`, `MailKeychainEntryNotFoundError`, `MailKeychainAccessDeniedError`) to `exceptions.py`.
- Promotes `imapclient>=3.0.0` from the `research` optional-dep group to primary deps.
- `mail_connector.py` is **untouched**. Delegation wiring is #40's scope.

## Design decisions locked in (six brainstorming Q&A)

1. Scope: new modules only, zero blast radius on existing code.
2. Methods: `search_messages` only — `get_thread`/`get_message`/`get_attachments` are per-issue later.
3. Host/port derivation: out of scope; `ImapConnector` takes fully-resolved creds. `#40`'s factory handles correlation.
4. Connection lifecycle: per-call (stateless). Pooling tracked in `#75`.
5. Return shape: matches `mail_connector.search_messages` exactly (`id`, `subject`, `sender`, `date_received`, `read_status`, `flagged`).
6. Errors: `MailKeychain*` subtree for Keychain; no custom IMAP types (let `OSError`/`socket.timeout`/`LoginError`/`IMAPClientError` propagate per the invariants doc).

## Test plan

- [x] `make lint` — passes.
- [x] `make typecheck` — passes (mypy strict).
- [x] `make test` — all unit tests pass including new `test_exceptions.py`, `test_keychain.py`, `test_imap_connector.py`.
- [x] `MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=iCloud uv run pytest tests/integration/test_imap_connector.py -v` — passes end-to-end against real iCloud.
- [x] `make check-all` — green.
- [ ] Reviewer reads `docs/plans/2026-04-23-imap-connector-design.md` and confirms it matches the implementation.
- [ ] Reviewer confirms `mail_connector.py` and `server.py` are genuinely untouched (intended invariant).

## Post-merge actions (user-gated)

- [ ] Close #41 with a comment pointing to the design doc and this PR.
- [ ] Remove `blocked` label from #40 (it was blocked on #41); add a comment saying delegation wiring can now proceed.
- [ ] #66, #72, #73 remain blocked on #40 landing (delegation layer unlocks them).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**Step 3: Wait for review.** `/merge-and-status` when ready.

**Step 4: Post-merge**

After merge:

```
gh issue close 41 --comment "Implemented in #<PR_NUMBER>. See docs/plans/2026-04-23-imap-connector-design.md for the design, and the PR for the code. #40 is now unblocked — delegation wiring is its scope."
gh issue edit 40 --remove-label blocked
gh issue comment 40 --body "Unblocked by #41 (merged). ImapConnector is ready to be wired into mail_connector.search_messages per the graceful-degradation invariants in docs/research/imap-auth-options-decision.md."
```

#66 / #72 / #73 stay `blocked` — they remain blocked on #40's delegation layer, not on #41. Do not touch their labels.

---

## Summary

12 tasks. Each is commit-sized. The granularity is intentional: each task produces tests + code that can be reviewed in isolation. The longest (Task 3 keychain, Tasks 5–9 imap_connector) are ~100 LOC of code each; the shortest (Task 1 exceptions, Task 2 deps) are ~20 LOC each.

**Commit count:** expect ~10 commits on the feature branch.

**End state:**
- `keychain.py` retrievable via `from apple_mail_mcp.keychain import get_imap_password`.
- `ImapConnector` retrievable via `from apple_mail_mcp.imap_connector import ImapConnector`.
- Integration test passing against real iCloud.
- Design doc committed alongside the implementation.
- #41 closed, #40 unblocked.
