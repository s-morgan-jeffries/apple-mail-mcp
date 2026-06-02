"""Property-based tests (Hypothesis) for the security boundary (#214).

`escape_applescript_string` + `sanitize_input` are the last line of defense
for every AppleScript path; `_validate_name` / `_validate_draft_id` are the
equivalent for filesystem-path / id safety. Example-based tests (elsewhere)
catch the inputs we thought of; these generate hundreds of adversarial inputs
to catch the ones we didn't. Scope is deliberately these four functions.

The osascript round-trip — the operational gold check that runs the escaped
string through the real AppleScript parser — lives in
tests/integration/test_escaping_property.py (it shells out per example, so it
stays out of the fast unit suite per #298).
"""

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from apple_mail_mcp.drafts import _DRAFT_ID_RE, _validate_draft_id
from apple_mail_mcp.exceptions import (
    MailDraftInvalidIdError,
    MailTemplateInvalidNameError,
)
from apple_mail_mcp.templates import _NAME_RE, _validate_name
from apple_mail_mcp.utils import escape_applescript_string, sanitize_input


class TestSanitizeInputProperties:
    @given(st.text())
    def test_strips_nulls_and_bounds_length(self, s: str) -> None:
        out = sanitize_input(s)
        assert isinstance(out, str)
        assert "\x00" not in out
        assert len(out) <= 10000

    @given(st.text())
    def test_idempotent(self, s: str) -> None:
        once = sanitize_input(s)
        assert sanitize_input(once) == once

    @given(
        st.one_of(
            st.integers(), st.floats(allow_nan=False), st.booleans(),
            st.binary(), st.lists(st.integers()),
        )
    )
    def test_non_str_inputs_become_str(self, value: object) -> None:
        out = sanitize_input(value)
        assert isinstance(out, str)
        assert "\x00" not in out

    def test_none_becomes_empty(self) -> None:
        assert sanitize_input(None) == ""


class TestEscapeApplescriptStringProperties:
    @given(st.text())
    def test_no_break_out(self, s: str) -> None:
        """After removing every escape pair, no bare delimiter remains — so
        no input can terminate the string literal early (injection) or leave
        a dangling backslash."""
        esc = escape_applescript_string(s)
        stripped = esc.replace("\\\\", "").replace('\\"', "")
        assert '"' not in stripped
        assert "\\" not in stripped

    @given(st.text())
    def test_only_adds_escaping_backslashes(self, s: str) -> None:
        esc = escape_applescript_string(s)
        assert len(esc) == len(s) + s.count('"') + s.count("\\")

    @given(st.text())
    def test_composes_with_sanitize_without_nulls(self, s: str) -> None:
        esc = escape_applescript_string(sanitize_input(s))
        assert "\x00" not in esc
        stripped = esc.replace("\\\\", "").replace('\\"', "")
        assert '"' not in stripped and "\\" not in stripped


class TestValidateNameProperties:
    # A fixed absolute base (need not exist — Path.resolve normalizes either
    # way). Hypothesis-driven tests can't take function-scoped fixtures like
    # tmp_path, so we don't use one.
    _BASE = Path("/amm/property/base")

    @given(st.text())
    def test_reject_or_path_contained(self, s: str) -> None:
        """Any input is either rejected, or accepted and provably contained
        directly under the base directory (no `..`, no separators, no
        absolute-path escape)."""
        try:
            _validate_name(s)
        except MailTemplateInvalidNameError:
            return
        # Accepted → must match the allowlist and stay under base.
        assert _NAME_RE.fullmatch(s)
        resolved = (self._BASE / f"{s}.md").resolve()
        assert resolved.parent == self._BASE.resolve()

    @given(st.from_regex(_NAME_RE, fullmatch=True))
    def test_valid_names_always_accepted_and_round_trip(self, name: str) -> None:
        _validate_name(name)  # must not raise
        assert _NAME_RE.fullmatch(name)


class TestValidateDraftIdProperties:
    _FORBIDDEN = ('"', "\\", "/", "\n", "\r", "\x00", "..")

    @given(st.text())
    def test_reject_or_safe_charset(self, s: str) -> None:
        """Any input is either rejected, or accepted and free of AppleScript-
        breaking / path-traversal characters."""
        try:
            _validate_draft_id(s)
        except MailDraftInvalidIdError:
            return
        assert _DRAFT_ID_RE.fullmatch(s)
        for bad in self._FORBIDDEN:
            assert bad not in s

    @given(st.from_regex(_DRAFT_ID_RE, fullmatch=True))
    def test_valid_ids_always_accepted(self, draft_id: str) -> None:
        _validate_draft_id(draft_id)  # must not raise
