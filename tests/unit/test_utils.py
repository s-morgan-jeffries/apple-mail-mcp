"""Unit tests for utility functions."""


from apple_mail_mcp.utils import (
    escape_applescript_string,
    format_applescript_list,
    parse_applescript_list,
    parse_date_filter,
    sanitize_input,
    validate_email,
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
