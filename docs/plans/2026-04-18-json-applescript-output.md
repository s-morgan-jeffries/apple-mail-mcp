# JSON AppleScript Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace pipe-delimited AppleScript output with JSON across all 5 affected connector methods, removing the project's biggest parsing-fragility bug.

**Architecture:** Add a `parse_applescript_json()` helper in `utils.py` and a `_wrap_as_json_script()` helper in `mail_connector.py` that wraps a tell-block body with `use framework "Foundation"` + a trailing `NSJSONSerialization` encoding step. Each of the 5 methods builds a native AppleScript list/record of results, lets the wrapper serialize it, then Python-side calls `parse_applescript_json()`.

**Tech Stack:** Python 3.10+, AppleScript with ASObjC (`use framework "Foundation"`), NSJSONSerialization, pytest.

**Design doc:** [`docs/plans/2026-04-18-json-applescript-output-design.md`](./2026-04-18-json-applescript-output-design.md)

---

## Preflight

**Verified (smoke test in the design doc):** `osascript` with `use framework "Foundation"` + `NSJSONSerialization's dataWithJSONObject:l options:0 |error|:(missing value)` correctly serializes an AppleScript `list of records` to JSON, handling embedded quotes (`\"`), pipes (literal), apostrophes (literal), booleans (`true`/`false`), and integers. No encoding library needed on the Python side beyond the stdlib `json` module.

**Affected methods:**

| Method | Line (approx) | Output shape after refactor |
|---|---|---|
| `list_accounts` | 86–126 | `list[dict]` with `name`, `email_addresses` |
| `list_mailboxes` | 128–161 | `list[dict]` with `name`, `unread_count` |
| `search_messages` | 163–255 | `list[dict]` with `id`, `subject`, `sender`, `date_received`, `read_status` |
| `get_message` | 257–319 | `dict` with `id`, `subject`, `sender`, `date_received`, `read_status`, `flagged`, `content` |
| `get_attachments` | 530–596 | `list[dict]` with `name`, `mime_type`, `size`, `downloaded` |

**Existing mocks to update:**

- `tests/unit/test_mail_connector.py:85` (`test_list_mailboxes` — currently asserts only `len(result) > 0` on nonsense string)
- `tests/unit/test_mail_connector.py:95` (`test_search_messages_basic`)
- `tests/unit/test_mail_connector.py:133` (`test_get_message`)
- `tests/unit/test_attachments.py:120` (`test_get_attachments_list`)

**No changes needed in `server.py`** — it passes connector return values through unchanged. The existing `list_mailboxes` tool docstring already documents the target shape (`{"name": "INBOX", "unread_count": 5}`), so this refactor aligns with the documented contract.

---

## Task 1: `parse_applescript_json` helper + tests

**Files:**
- Modify: `src/apple_mail_mcp/utils.py` (add function at end, with `import json` at top)
- Modify: `tests/unit/test_utils.py` (add `TestParseAppleScriptJson` class)

**Step 1: Write failing tests**

At the end of `tests/unit/test_utils.py`, add:

```python
import json
import pytest

from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.utils import parse_applescript_json


class TestParseAppleScriptJson:
    def test_parses_valid_json_list(self) -> None:
        result = parse_applescript_json('[{"name": "INBOX", "unread_count": 5}]')
        assert result == [{"name": "INBOX", "unread_count": 5}]

    def test_parses_valid_json_object(self) -> None:
        result = parse_applescript_json('{"id": "abc", "read_status": true}')
        assert result == {"id": "abc", "read_status": True}

    def test_parses_empty_list(self) -> None:
        assert parse_applescript_json("[]") == []

    def test_strips_whitespace(self) -> None:
        assert parse_applescript_json("  [1,2,3]  \n") == [1, 2, 3]

    def test_raises_on_error_prefix(self) -> None:
        with pytest.raises(MailAppleScriptError, match="boom"):
            parse_applescript_json("ERROR: boom")

    def test_raises_on_error_prefix_with_whitespace(self) -> None:
        with pytest.raises(MailAppleScriptError, match="something broke"):
            parse_applescript_json("ERROR:   something broke  ")

    def test_raises_on_malformed_json(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_applescript_json("{not valid")
```

**Step 2: Run — expect fail**

`uv run pytest tests/unit/test_utils.py::TestParseAppleScriptJson -v`

Expected: all 7 tests fail with `ImportError` or `AttributeError` (function doesn't exist).

**Step 3: Implement**

At the top of `src/apple_mail_mcp/utils.py`, add `import json` next to the existing `import re`. At the bottom of the file, add:

```python
def parse_applescript_json(result: str) -> Any:
    """Parse JSON emitted by an AppleScript helper, or raise on ERROR: prefix.

    AppleScript scripts wrapped with _wrap_as_json_script return either:
    - A JSON-serialized string (list, dict, or scalar), or
    - "ERROR: <message>" when the tell-block catches an error.

    Args:
        result: Raw stdout from _run_applescript().

    Returns:
        Deserialized JSON (list, dict, str, int, bool, or None).

    Raises:
        MailAppleScriptError: If the result starts with "ERROR:".
        json.JSONDecodeError: If the result is neither an error nor valid JSON.
    """
    from .exceptions import MailAppleScriptError

    stripped = result.strip()
    if stripped.startswith("ERROR:"):
        raise MailAppleScriptError(stripped[len("ERROR:"):].strip())
    return json.loads(stripped)
```

(The local import of `MailAppleScriptError` avoids a circular import — `utils.py` must not import from the connector layer.)

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_utils.py::TestParseAppleScriptJson -v`

Expected: 7 passed.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/utils.py tests/unit/test_utils.py
git commit -m "Add parse_applescript_json helper for JSON-emitting scripts (#23)"
```

---

## Task 2: `_wrap_as_json_script` helper on the connector

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py` (add module-level function below the `AppleMailConnector` class, or as a private method — plan uses module-level for easier testing)

**Step 1: Write the failing test**

In `tests/unit/test_mail_connector.py` add a new class at the end:

```python
from apple_mail_mcp.mail_connector import _wrap_as_json_script


class TestWrapAsJsonScript:
    def test_wrapper_contains_framework_directive(self) -> None:
        script = _wrap_as_json_script('tell application "Mail"\n    set resultData to {}\nend tell')
        assert 'use framework "Foundation"' in script
        assert "use scripting additions" in script

    def test_wrapper_appends_json_serialization(self) -> None:
        script = _wrap_as_json_script('tell application "Mail"\n    set resultData to {}\nend tell')
        assert "NSJSONSerialization" in script
        assert "dataWithJSONObject:resultData" in script

    def test_wrapper_preserves_body(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body)
        assert body in script
```

**Step 2: Run — expect fail**

`uv run pytest tests/unit/test_mail_connector.py::TestWrapAsJsonScript -v`

Expected: ImportError — `_wrap_as_json_script` doesn't exist yet.

**Step 3: Implement**

In `src/apple_mail_mcp/mail_connector.py`, after the `import` block and before `class AppleMailConnector`, add:

```python
def _wrap_as_json_script(body: str) -> str:
    """Wrap a tell-block body with ASObjC imports and an NSJSONSerialization return.

    The `body` must:
      - Contain a `tell application "Mail" ... end tell` block.
      - Assign the final result to an AppleScript variable named `resultData`
        inside that tell block.
      - Use `try/on error errMsg / return "ERROR: " & errMsg / end try` for
        failure cases (the wrapper does not add a try/catch).

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
```

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_mail_connector.py::TestWrapAsJsonScript -v`

Expected: 3 passed.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Add _wrap_as_json_script connector helper (#23)"
```

---

## Task 3: Refactor `search_messages` to JSON

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py:163-255`
- Modify: `tests/unit/test_mail_connector.py:90-103` (`test_search_messages_basic`)

**Step 1: Update the test mock first (RED)**

Replace the mock line at `tests/unit/test_mail_connector.py:95`:

```python
# Before:
mock_run.return_value = "12345|Test Subject|sender@example.com|Mon Jan 1 2024|false"

# After:
mock_run.return_value = (
    '[{"id":"12345","subject":"Test Subject",'
    '"sender":"sender@example.com","date_received":"Mon Jan 1 2024",'
    '"read_status":false}]'
)
```

Leave the assertions unchanged — they already check `id`, `subject`, `sender`, `read_status`.

Add a second test that the old code couldn't handle (this is the raison d'être of the refactor):

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_handles_pipe_in_subject(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Subject containing '|' must not break parsing (the bug this refactor fixes)."""
        mock_run.return_value = (
            '[{"id":"abc","subject":"Q3 Report | Draft",'
            '"sender":"boss@example.com","date_received":"Wed Feb 5 2025",'
            '"read_status":true}]'
        )
        result = connector.search_messages("Gmail", "INBOX")
        assert len(result) == 1
        assert result[0]["subject"] == "Q3 Report | Draft"
```

**Step 2: Run — expect fail**

`uv run pytest tests/unit/test_mail_connector.py::TestAppleMailConnector::test_search_messages_basic tests/unit/test_mail_connector.py::TestAppleMailConnector::test_search_messages_handles_pipe_in_subject -v`

Expected: both fail. The existing code does `line.split("\n")` then `line.split("|")` on JSON text — shapes won't match.

**Step 3: Refactor the method**

Replace the body of `search_messages` in `src/apple_mail_mcp/mail_connector.py` (lines 210-255). The new script uses a native AppleScript list of records and `_wrap_as_json_script`:

```python
        tell_body = f"""
        tell application "Mail"
            try
                set accountRef to account "{account_safe}"
                set mailboxRef to mailbox "{mailbox_safe}" of accountRef
                set matchedMessages to {limit_clause} (messages of mailboxRef whose {whose_clause})

                set resultData to {{}}
                repeat with msg in matchedMessages
                    set msgRecord to {{id:(id of msg as text), subject:(subject of msg), sender:(sender of msg), date_received:(date received of msg as text), read_status:(read status of msg)}}
                    set end of resultData to msgRecord
                end repeat
            on error errMsg
                return "ERROR: " & errMsg
            end try
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return parse_applescript_json(result)
```

At the top of the file, add to the existing `from .utils import ...`:

```python
from .utils import escape_applescript_string, parse_applescript_json, sanitize_input
```

Remove the old `for line in result.split("\n")` parsing loop entirely.

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_mail_connector.py::TestAppleMailConnector::test_search_messages_basic tests/unit/test_mail_connector.py::TestAppleMailConnector::test_search_messages_handles_pipe_in_subject -v`

Expected: 2 passed.

Also run the `test_search_messages_with_filters` test — it asserts on the AppleScript string contents and may break because of the refactor:

`uv run pytest tests/unit/test_mail_connector.py::TestAppleMailConnector::test_search_messages_with_filters -v`

If it fails because the test asserts on a substring that's still present in the new script (e.g., `'sender contains "john@example.com"'`), it should pass unchanged. If an assertion was on a substring we removed (e.g., the old pipe-concatenation line), update the test to assert on the equivalent JSON-era substring instead.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Refactor search_messages to emit JSON (#23)"
```

---

## Task 4: Refactor `get_message` to JSON

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py:257-319`
- Modify: `tests/unit/test_mail_connector.py:128-141` (`test_get_message`)

**Step 1: Update the mock (RED)**

Replace the mock at `tests/unit/test_mail_connector.py:133`:

```python
# Before:
mock_run.return_value = "12345|Subject|sender@example.com|Mon Jan 1 2024|true|false|Message body"

# After:
mock_run.return_value = (
    '{"id":"12345","subject":"Subject","sender":"sender@example.com",'
    '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false,'
    '"content":"Message body"}'
)
```

Add a new pipe-tolerance test:

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_handles_pipe_in_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Body containing '|' must not break parsing."""
        mock_run.return_value = (
            '{"id":"99","subject":"x","sender":"a@b.com",'
            '"date_received":"Mon Jan 1 2024","read_status":false,"flagged":false,'
            '"content":"col1|col2|col3"}'
        )
        result = connector.get_message("99", include_content=True)
        assert result["content"] == "col1|col2|col3"
```

**Step 2: Run — expect fail**

Both tests fail (old code does `result.split("|", 6)`).

**Step 3: Refactor**

Replace the body of `get_message`:

```python
        content_clause = (
            'set msgContent to content of msg'
            if include_content
            else 'set msgContent to ""'
        )

        tell_body = f"""
        tell application "Mail"
            try
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
                    return "ERROR: Message not found"
                end if
            on error errMsg
                return "ERROR: " & errMsg
            end try
        end tell
        """
```

**Important:** The original script relied on an `error "Message not found"` thrown from the outer AppleScript block after the loops — which `_run_applescript` would convert to stderr and raise `MailMessageNotFoundError` via the "Can't get message" string match (this may or may not match; verify). The new version explicitly returns `"ERROR: Message not found"` from the wrapper, which `parse_applescript_json` turns into `MailAppleScriptError`, not `MailMessageNotFoundError`.

**Decision needed before committing:** preserve `MailMessageNotFoundError` semantics by either:
- (a) Catching the AppleScript error inside the inner `try` as before, then after the loops `error "Message not found"` (keeps stderr path).
- (b) Keeping `return "ERROR: Message not found"` and updating `parse_applescript_json` OR the method to raise `MailMessageNotFoundError` on that specific message.

**Go with (a)** — minimal change to error surface:

```python
        tell_body = f"""
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
                error "Message not found"
            end if
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return parse_applescript_json(result)
```

The `error` call propagates to stderr; `_run_applescript` maps "Can't get message" / "Message not found" to `MailMessageNotFoundError`. Verify by reading `_run_applescript` — if the error-string match is "Can't get message" exactly, change the AppleScript to `error "Can't get message: not found"` to hit the existing mapping. Otherwise update the mapping to also recognize "Message not found".

Remove the old parsing block in Python (`parts = result.split("|", 6)` etc.).

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_mail_connector.py::TestAppleMailConnector -k "get_message" -v`

Expected: existing `test_get_message_not_found` still raises `MailMessageNotFoundError` (via the stderr path); the refactored basic and pipe-tolerance tests pass.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Refactor get_message to emit JSON (#23)"
```

---

## Task 5: Refactor `get_attachments` to JSON

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py:530-596`
- Modify: `tests/unit/test_attachments.py:115-139` (`test_get_attachments_list`, `test_get_attachments_empty`)

**Step 1: Update mocks (RED)**

`test_get_attachments_list` mock at `tests/unit/test_attachments.py:120`:

```python
mock_run.return_value = (
    '[{"name":"document.pdf","mime_type":"application/pdf","size":524288,"downloaded":true},'
    '{"name":"image.jpg","mime_type":"image/jpeg","size":102400,"downloaded":true}]'
)
```

`test_get_attachments_empty` mock at `:135`:

```python
mock_run.return_value = "[]"
```

Add a pipe-tolerance case:

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_handles_pipe_in_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"q1|q2.pdf","mime_type":"application/pdf","size":1000,"downloaded":true}]'
        )
        result = connector.get_attachments("12345")
        assert result[0]["name"] == "q1|q2.pdf"
```

**Step 2: Run — expect fail**

Old parsing `line.split("|")` can't handle JSON.

**Step 3: Refactor**

Replace the body of `get_attachments`:

```python
        tell_body = f"""
        tell application "Mail"
            set resultData to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is {message_id_safe}
                        set attList to mail attachments of msg

                        set resultData to {{}}
                        repeat with att in attList
                            set attRecord to {{name:(name of att), mime_type:(MIME type of att), size:(file size of att), downloaded:(downloaded of att)}}
                            set end of resultData to attRecord
                        end repeat
                        exit repeat
                    end try
                end repeat
                if resultData is not missing value then exit repeat
            end repeat

            if resultData is missing value then
                error "Message not found"
            end if
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return parse_applescript_json(result)
```

Remove the old pipe-split loop. Keep the existing `test_get_attachments_message_not_found` test as-is — it mocks `_run_applescript` to raise directly.

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_attachments.py::TestGetAttachments -v`

Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_attachments.py
git commit -m "Refactor get_attachments to emit JSON (#23)"
```

---

## Task 6: Refactor `list_mailboxes` to JSON (finishes the TODO)

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py:128-161`
- Modify: `tests/unit/test_mail_connector.py:80-88` (`test_list_mailboxes`)

**Step 1: Update the test (RED)**

Replace `test_list_mailboxes`:

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"INBOX","unread_count":5},'
            '{"name":"Sent","unread_count":0},'
            '{"name":"Projects/Client A","unread_count":3}]'
        )
        result = connector.list_mailboxes("Gmail")
        assert result == [
            {"name": "INBOX", "unread_count": 5},
            {"name": "Sent", "unread_count": 0},
            {"name": "Projects/Client A", "unread_count": 3},
        ]
```

**Step 2: Run — expect fail**

The existing code returns `[{"raw": result}]` — shape doesn't match.

**Step 3: Refactor**

Replace the body of `list_mailboxes`:

```python
        account_safe = escape_applescript_string(sanitize_input(account))

        tell_body = f"""
        tell application "Mail"
            try
                set accountRef to account "{account_safe}"
                set resultData to {{}}

                repeat with mb in mailboxes of accountRef
                    set mbRecord to {{name:(name of mb), unread_count:(unread count of mb)}}
                    set end of resultData to mbRecord
                end repeat
            on error errMsg
                return "ERROR: " & errMsg
            end try
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return parse_applescript_json(result)
```

The `{"raw": result}` placeholder code is removed. The TODO comment goes away.

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_mail_connector.py::TestAppleMailConnector::test_list_mailboxes_returns_structured_data -v`

Also run e2e to confirm the tool's `TestToolInvocation` parametrized case for `list_mailboxes` still works (server layer unchanged):

`uv run pytest tests/e2e/test_mcp_tools.py -v`

Expected: all pass.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Refactor list_mailboxes to emit JSON, remove TODO placeholder (#23)"
```

---

## Task 7: Refactor `list_accounts` to JSON (finishes the half-baked pseudo-JSON)

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py:86-126`
- Modify: `tests/unit/test_mail_connector.py` (add new test — no existing test for this)

**Step 1: Write the new test (RED)**

In `tests/unit/test_mail_connector.py`, add to `TestAppleMailConnector`:

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"Gmail","email_addresses":["me@gmail.com"]},'
            '{"name":"Work","email_addresses":["me@work.com","alt@work.com"]}]'
        )
        result = connector.list_accounts()
        assert result == [
            {"name": "Gmail", "email_addresses": ["me@gmail.com"]},
            {"name": "Work", "email_addresses": ["me@work.com", "alt@work.com"]},
        ]
```

**Step 2: Run — expect fail**

The existing code returns `[{"raw": str}]` shapes.

**Step 3: Refactor**

Replace the body of `list_accounts`:

```python
    def list_accounts(self) -> list[dict[str, Any]]:
        """List all mail accounts.

        Returns:
            List of account dictionaries with keys:
              - name: account display name
              - email_addresses: list of associated email addresses
        """
        tell_body = """
        tell application "Mail"
            try
                set resultData to {}
                repeat with acc in accounts
                    set accRecord to {name:(name of acc), email_addresses:(email addresses of acc)}
                    set end of resultData to accRecord
                end repeat
            on error errMsg
                return "ERROR: " & errMsg
            end try
        end tell
        """

        script = _wrap_as_json_script(tell_body)
        result = self._run_applescript(script)
        return parse_applescript_json(result)
```

Important check during implementation: `email addresses of acc` may return `missing value` for accounts with no address set. If so, wrap the property access with a `try` or use a fallback. Verify against a real Mail.app account during integration testing (Task 9).

**Step 4: Run — expect pass**

`uv run pytest tests/unit/test_mail_connector.py::TestAppleMailConnector::test_list_accounts_returns_structured_data -v`

Expected: pass.

**Step 5: Commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Refactor list_accounts to emit proper JSON (#23)"
```

---

## Task 8: Full unit + e2e suite green

**Files:** None modified unless issues surface.

**Step 1: Run the whole suite**

```bash
make test
make test-e2e
```

Expected:
- Unit: all pass. New coverage for `parse_applescript_json`, `_wrap_as_json_script`, `list_accounts`, `list_mailboxes`, plus pipe-tolerance tests.
- E2E: 20 still pass (connector is mocked at server layer; refactor is internal).

**Step 2: Run `make check-all`**

```bash
make check-all
```

Expected: lint, typecheck, tests, complexity, version-sync, parity all green. Coverage ≥ 90%.

**Step 3: If anything fails**

Fix and commit separately with a descriptive message. Do not amend prior per-task commits.

No commit if everything was already green — this is a gate task.

---

## Task 9: Integration sanity check (optional but recommended)

**Files:** `tests/integration/test_mail_integration.py` (if adding tests)

**Goal:** Prove the ASObjC JSON pattern works against a real Mail.app, not just in isolated osascript smoke tests.

**Step 1: Minimal live smoke**

Run a one-shot osascript smoke test against the user's Mail.app:

```bash
osascript <<'EOF'
use framework "Foundation"
use scripting additions

tell application "Mail"
    set resultData to {}
    repeat with acc in accounts
        set accRecord to {name:(name of acc), email_addresses:(email addresses of acc)}
        set end of resultData to accRecord
    end repeat
end tell

set jsonData to (current application's NSJSONSerialization's dataWithJSONObject:resultData options:0 |error|:(missing value))
return (current application's NSString's alloc()'s initWithData:jsonData encoding:4) as text
EOF
```

Expected: valid JSON printed. If `jsonData` returns `missing value`, NSJSONSerialization rejected the record — likely because one property was `missing value` (e.g., an account with no email addresses). If that happens, wrap `email addresses of acc` with:

```applescript
try
    set addrs to email addresses of acc
    if addrs is missing value then set addrs to {}
on error
    set addrs to {}
end try
```

Adjust Task 7's tell-body accordingly before shipping.

**Step 2: Optional — add 1 integration test**

If the project wants at least one integration test that proves JSON end-to-end, add to `tests/integration/test_mail_integration.py`:

```python
@pytest.mark.integration
def test_list_accounts_returns_json(connector: AppleMailConnector) -> None:
    accounts = connector.list_accounts()
    assert isinstance(accounts, list)
    # Every entry is a dict with the documented keys (no {"raw": ...} shapes)
    for acct in accounts:
        assert set(acct.keys()) >= {"name", "email_addresses"}
        assert isinstance(acct["name"], str)
        assert isinstance(acct["email_addresses"], list)
```

Run: `make test-integration` — requires real Mail.app and passes `MAIL_TEST_MODE=true`.

**Step 3: Commit if integration tests added**

```bash
git add tests/integration/test_mail_integration.py
git commit -m "Add integration test for list_accounts JSON shape (#23)"
```

If step 1 surfaced edge cases (missing-value handling), include the connector fix in the same commit as the test.

---

## Task 10: Update documentation

**Files:**
- Modify: `.claude/skills/applescript-mail/SKILL.md` (the "Critical: Pipe-Delimited Output Parsing" section is now outdated)
- Modify: `.claude/CLAUDE.md` (the "Pipe-delimited output parsing" gotcha entry)

**Step 1: Update the skill**

In `.claude/skills/applescript-mail/SKILL.md`, replace the "Critical: Pipe-Delimited Output Parsing" section with a note that the project now uses JSON + ASObjC, and briefly document the `_wrap_as_json_script` / `parse_applescript_json` pattern so future contributors follow it. Keep the section short (≤ 20 lines).

**Step 2: Update CLAUDE.md**

In `.claude/CLAUDE.md`, find the "AppleScript Gotchas" section and either remove the "Pipe-delimited output parsing" bullet or replace it with a one-line note that output is JSON (via `_wrap_as_json_script`). Do not touch other gotchas.

**Step 3: Run verification**

```bash
make check-all
```

Expected: green. No Python code changes in this task.

**Step 4: Commit**

```bash
git add .claude/skills/applescript-mail/SKILL.md .claude/CLAUDE.md
git commit -m "Document JSON AppleScript pattern, remove pipe-delimited gotcha (#23)"
```

---

## Task 11: Push and open PR closing #23

**Step 1: Push**

```bash
git push -u origin refactor/issue-23-json-applescript-output
```

**Step 2: Open PR**

```bash
gh pr create --title "Emit JSON from AppleScript instead of pipe-delimited strings (#23)" --body "..."
```

PR body should:

- Summarize the refactor: `parse_applescript_json`, `_wrap_as_json_script`, 5 methods converted.
- Call out the user-visible wins: `list_accounts` and `list_mailboxes` now return real structured data (previously `[{"raw": str}]`).
- Call out the bug fix: subjects/content containing `|` no longer break parsing.
- Reference the design doc (`docs/plans/2026-04-18-json-applescript-output-design.md`).
- Include the test-plan checklist: `make test`, `make test-e2e`, `make check-all`, optional `make test-integration`.
- Use `Closes #23`.

**Step 3: Wait for CI**

`gh pr checks <pr-number> --watch`

**Step 4: On green, use `/merge-and-status` to merge.**

---

## Verification (end-to-end)

- `make test` — unit tests green; new `parse_applescript_json` and per-method pipe-tolerance tests pass.
- `make test-e2e` — 20 E2E tests still pass (connector is mocked; dispatch layer unaffected).
- `make check-all` — all gates green; coverage ≥ 90%.
- `grep -r 'split("|")' src/apple_mail_mcp/` returns no matches.
- `grep -r '{"raw":' src/apple_mail_mcp/` returns no matches (both placeholder shapes gone).
- Manual osascript smoke or `make test-integration` if the user runs it locally confirms real Mail.app produces valid JSON.
