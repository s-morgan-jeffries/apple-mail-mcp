"""Unit tests for utility functions."""

import json

import pytest

from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.utils import (
    escape_applescript_string,
    format_applescript_list,
    normalize_subject,
    parse_applescript_json,
    parse_applescript_list,
    parse_date_filter,
    parse_rfc822_ids,
    sanitize_input,
    validate_email,
    walk_thread_graph,
)


class TestEscapeAppleScriptString:
    """Tests for escape_applescript_string."""

    def test_escapes_backslashes(self) -> None:
        result = escape_applescript_string("path\\to\\file")
        assert result == "path\\\\to\\\\file"

    def test_escapes_double_quotes(self) -> None:
        result = escape_applescript_string('Hello "World"')
        assert result == 'Hello \\"World\\"'

    def test_escapes_both(self) -> None:
        result = escape_applescript_string('Path\\to\\"file"')
        assert result == 'Path\\\\to\\\\\\"file\\"'

    def test_empty_string(self) -> None:
        result = escape_applescript_string("")
        assert result == ""

    def test_no_special_chars(self) -> None:
        result = escape_applescript_string("Hello World")
        assert result == "Hello World"


class TestParseAppleScriptList:
    """Tests for parse_applescript_list."""

    def test_empty_list(self) -> None:
        assert parse_applescript_list("{}") == []
        assert parse_applescript_list("") == []

    def test_simple_list(self) -> None:
        result = parse_applescript_list("{a, b, c}")
        assert result == ["a", "b", "c"]

    def test_list_without_braces(self) -> None:
        result = parse_applescript_list("a, b, c")
        assert result == ["a", "b", "c"]

    def test_single_item(self) -> None:
        result = parse_applescript_list("{item}")
        assert result == ["item"]


class TestFormatAppleScriptList:
    """Tests for format_applescript_list."""

    def test_empty_list(self) -> None:
        result = format_applescript_list([])
        assert result == "{}"

    def test_simple_list(self) -> None:
        result = format_applescript_list(["a", "b", "c"])
        assert result == '{"a", "b", "c"}'

    def test_escapes_special_chars(self) -> None:
        result = format_applescript_list(['hello "world"'])
        assert result == '{"hello \\"world\\""}'


class TestParseDateFilter:
    """Tests for parse_date_filter."""

    def test_days_ago(self) -> None:
        result = parse_date_filter("7 days ago")
        assert result == "(current date) - (7 * days)"

    def test_weeks_ago(self) -> None:
        result = parse_date_filter("2 weeks ago")
        assert result == "(current date) - (2 * weeks)"

    def test_last_week(self) -> None:
        result = parse_date_filter("last week")
        assert result == "(current date) - (1 * weeks)"

    def test_iso_date(self) -> None:
        result = parse_date_filter("2024-01-15")
        assert result == 'date "2024-01-15"'


class TestValidateEmail:
    """Tests for validate_email."""

    def test_valid_emails(self) -> None:
        assert validate_email("user@example.com") is True
        assert validate_email("first.last@company.co.uk") is True
        assert validate_email("user+tag@example.com") is True

    def test_invalid_emails(self) -> None:
        assert validate_email("invalid") is False
        assert validate_email("@example.com") is False
        assert validate_email("user@") is False
        assert validate_email("user example.com") is False


class TestSanitizeInput:
    """Tests for sanitize_input."""

    def test_removes_null_bytes(self) -> None:
        result = sanitize_input("hello\x00world")
        assert result == "helloworld"

    def test_handles_none(self) -> None:
        result = sanitize_input(None)
        assert result == ""

    def test_converts_to_string(self) -> None:
        result = sanitize_input(123)
        assert result == "123"

    def test_limits_length(self) -> None:
        long_string = "a" * 20000
        result = sanitize_input(long_string)
        assert len(result) == 10000


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

    def test_parses_null(self) -> None:
        assert parse_applescript_json("null") is None

    def test_parses_quoted_string(self) -> None:
        assert parse_applescript_json('"hello"') == "hello"

    def test_parses_integer(self) -> None:
        assert parse_applescript_json("42") == 42

    def test_raises_on_empty_error_message(self) -> None:
        """'ERROR:' with no message still raises (edge case)."""
        with pytest.raises(MailAppleScriptError):
            parse_applescript_json("ERROR:")


class TestNormalizeSubject:
    def test_strips_leading_re(self) -> None:
        assert normalize_subject("Re: Q3 Report") == "Q3 Report"

    def test_strips_leading_fwd(self) -> None:
        assert normalize_subject("Fwd: Budget update") == "Budget update"

    def test_strips_leading_fw(self) -> None:
        assert normalize_subject("Fw: heads up") == "heads up"

    def test_strips_nested_prefixes(self) -> None:
        assert normalize_subject("Re: Re: Fwd: Re: Q3") == "Q3"

    def test_case_insensitive(self) -> None:
        assert normalize_subject("RE: hello") == "hello"
        assert normalize_subject("FWD: hi") == "hi"
        assert normalize_subject("re: yo") == "yo"

    def test_preserves_subject_without_prefix(self) -> None:
        assert normalize_subject("Q3 Report") == "Q3 Report"

    def test_handles_empty_string(self) -> None:
        assert normalize_subject("") == ""

    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_subject("  Re:   Q3 Report  ") == "Q3 Report"

    def test_preserves_internal_whitespace(self) -> None:
        assert normalize_subject("Re: Q3   Report") == "Q3   Report"


class TestParseRfc822Ids:
    def test_single_angle_wrapped_id(self) -> None:
        assert parse_rfc822_ids("<abc@example.com>") == ["abc@example.com"]

    def test_multiple_space_separated(self) -> None:
        assert parse_rfc822_ids("<a@x.com> <b@x.com> <c@x.com>") == [
            "a@x.com", "b@x.com", "c@x.com",
        ]

    def test_multiline_references(self) -> None:
        raw = "<a@x.com>\n <b@x.com>\n <c@x.com>"
        assert parse_rfc822_ids(raw) == ["a@x.com", "b@x.com", "c@x.com"]

    def test_preserves_bare_ids(self) -> None:
        """Some clients emit ids without angle brackets."""
        assert parse_rfc822_ids("bare@example.com") == ["bare@example.com"]

    def test_empty_string_returns_empty_list(self) -> None:
        assert parse_rfc822_ids("") == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        assert parse_rfc822_ids("   \n  ") == []

    def test_malformed_trailing_angle(self) -> None:
        """Lenient: strip stray brackets around otherwise-valid ids."""
        assert parse_rfc822_ids("<a@x.com> <malformed") == ["a@x.com", "malformed"]


class TestWalkThreadGraph:
    def test_single_anchor_no_candidates(self) -> None:
        """Thread of one message returns just that message."""
        accepted = walk_thread_graph(
            known_ids={"rfc-anchor"},
            candidates=[],
        )
        assert accepted == []

    def test_direct_reply_found(self) -> None:
        """A candidate whose in_reply_to matches the anchor joins the thread."""
        cand = {
            "id": "reply-1",
            "rfc_message_id": "rfc-reply-1",
            "in_reply_to": "rfc-anchor",
            "references_parsed": [],
        }
        accepted = walk_thread_graph(
            known_ids={"rfc-anchor"},
            candidates=[cand],
        )
        assert accepted == [cand]

    def test_nested_reply_discovered_in_second_pass(self) -> None:
        """A reply-to-the-reply is added after its parent is added."""
        c1 = {
            "id": "reply-1",
            "rfc_message_id": "rfc-1",
            "in_reply_to": "rfc-anchor",
            "references_parsed": [],
        }
        c2 = {
            "id": "reply-2",
            "rfc_message_id": "rfc-2",
            "in_reply_to": "rfc-1",
            "references_parsed": [],
        }
        accepted = walk_thread_graph(
            known_ids={"rfc-anchor"},
            candidates=[c2, c1],  # c2 first — requires iteration to stability
        )
        ids = {c["id"] for c in accepted}
        assert ids == {"reply-1", "reply-2"}

    def test_references_chain_expands_known_set(self) -> None:
        """A candidate whose references list overlaps known_ids joins."""
        cand = {
            "id": "branch",
            "rfc_message_id": "rfc-branch",
            "in_reply_to": "",
            "references_parsed": ["rfc-ancient", "rfc-anchor"],
        }
        accepted = walk_thread_graph(
            known_ids={"rfc-anchor"},
            candidates=[cand],
        )
        assert accepted == [cand]

    def test_unrelated_candidate_rejected(self) -> None:
        cand = {
            "id": "unrelated",
            "rfc_message_id": "rfc-other",
            "in_reply_to": "rfc-completely-different",
            "references_parsed": [],
        }
        accepted = walk_thread_graph(
            known_ids={"rfc-anchor"},
            candidates=[cand],
        )
        assert accepted == []

    def test_cycle_terminates(self) -> None:
        """Malformed client references that form a cycle don't loop forever."""
        c1 = {"id": "a", "rfc_message_id": "rfc-a", "in_reply_to": "rfc-b", "references_parsed": []}
        c2 = {"id": "b", "rfc_message_id": "rfc-b", "in_reply_to": "rfc-a", "references_parsed": []}
        accepted = walk_thread_graph(
            known_ids={"rfc-a"},
            candidates=[c1, c2],
        )
        ids = {c["id"] for c in accepted}
        assert ids == {"a", "b"}
