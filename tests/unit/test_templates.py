"""Unit tests for the email template module."""

import pytest

from apple_mail_mcp.exceptions import (
    MailTemplateInvalidFormatError,
    MailTemplateInvalidNameError,
    MailTemplateMissingVariableError,
    MailTemplateNotFoundError,
)


class TestExtractPlaceholders:
    def test_finds_simple_placeholders(self):
        from apple_mail_mcp.templates import extract_placeholders

        text = "Hi {recipient_name}, today is {today}."
        assert extract_placeholders(text) == ["recipient_name", "today"]

    def test_dedupes_repeated_placeholders(self):
        from apple_mail_mcp.templates import extract_placeholders

        text = "{name} {name} {other}"
        assert extract_placeholders(text) == ["name", "other"]

    def test_returns_sorted_alphabetically(self):
        from apple_mail_mcp.templates import extract_placeholders

        text = "{zebra} {alpha} {middle}"
        assert extract_placeholders(text) == ["alpha", "middle", "zebra"]

    def test_ignores_escaped_double_braces(self):
        from apple_mail_mcp.templates import extract_placeholders

        # str.format-style escapes: {{ → literal {, }} → literal }
        text = "{{not_a_placeholder}} and {real}"
        assert extract_placeholders(text) == ["real"]

    def test_ignores_invalid_identifiers(self):
        from apple_mail_mcp.templates import extract_placeholders

        # Things that aren't valid Python identifiers shouldn't match
        text = "{1foo} {-bar} { space} {} {valid_one}"
        assert extract_placeholders(text) == ["valid_one"]

    def test_empty_string_yields_empty_list(self):
        from apple_mail_mcp.templates import extract_placeholders

        assert extract_placeholders("") == []


class TestParseTemplateFile:
    def test_parses_subject_header_and_body(self):
        from apple_mail_mcp.templates import parse_template_file

        text = "subject: Re: {original_subject}\n\nHi {recipient_name},\n\nBody.\n"
        t = parse_template_file(text, name="my-template")
        assert t.name == "my-template"
        assert t.subject == "Re: {original_subject}"
        assert t.body == "Hi {recipient_name},\n\nBody.\n"

    def test_no_header_block_means_subject_is_none(self):
        from apple_mail_mcp.templates import parse_template_file

        # File starts directly with body — no subject header.
        text = "\nJust body text, no subject.\n"
        t = parse_template_file(text, name="x")
        assert t.subject is None
        assert t.body == "Just body text, no subject.\n"

    def test_body_can_contain_blank_lines(self):
        from apple_mail_mcp.templates import parse_template_file

        text = "subject: foo\n\nLine 1\n\nLine 3\n\nLine 5\n"
        t = parse_template_file(text, name="x")
        assert t.body == "Line 1\n\nLine 3\n\nLine 5\n"

    def test_body_can_contain_colons(self):
        from apple_mail_mcp.templates import parse_template_file

        text = "subject: hi\n\nThanks: I'll handle this.\n"
        t = parse_template_file(text, name="x")
        assert t.body == "Thanks: I'll handle this.\n"

    def test_unknown_header_raises(self):
        from apple_mail_mcp.templates import parse_template_file

        text = "frobnicate: yes\n\nbody\n"
        with pytest.raises(MailTemplateInvalidFormatError, match="frobnicate"):
            parse_template_file(text, name="x")

    def test_empty_body_raises(self):
        from apple_mail_mcp.templates import parse_template_file

        text = "subject: hi\n\n"
        with pytest.raises(MailTemplateInvalidFormatError, match="empty"):
            parse_template_file(text, name="x")

    def test_completely_empty_file_raises(self):
        from apple_mail_mcp.templates import parse_template_file

        with pytest.raises(MailTemplateInvalidFormatError):
            parse_template_file("", name="x")


class TestSerializeTemplate:
    def test_with_subject(self):
        from apple_mail_mcp.templates import Template, serialize_template

        t = Template(name="x", subject="Re: foo", body="Hello.\n")
        assert serialize_template(t) == "subject: Re: foo\n\nHello.\n"

    def test_without_subject(self):
        from apple_mail_mcp.templates import Template, serialize_template

        t = Template(name="x", subject=None, body="Hello.\n")
        assert serialize_template(t) == "\nHello.\n"

    def test_round_trip(self):
        from apple_mail_mcp.templates import (
            Template,
            parse_template_file,
            serialize_template,
        )

        original = Template(
            name="r", subject="Hi {name}", body="Body line.\n\nMore.\n"
        )
        text = serialize_template(original)
        parsed = parse_template_file(text, name="r")
        assert parsed == original


class TestTemplateRender:
    def test_substitutes_placeholders(self):
        from apple_mail_mcp.templates import Template

        t = Template(
            name="x",
            subject="Re: {topic}",
            body="Hi {name},\nThanks.\n",
        )
        result = t.render({"topic": "Q3 plan", "name": "Alice"})
        assert result["subject"] == "Re: Q3 plan"
        assert result["body"] == "Hi Alice,\nThanks.\n"

    def test_missing_variable_raises_with_name(self):
        from apple_mail_mcp.templates import Template

        t = Template(name="x", subject=None, body="Hi {missing}.\n")
        with pytest.raises(MailTemplateMissingVariableError, match="missing"):
            t.render({})

    def test_missing_variable_lists_all_missing(self):
        from apple_mail_mcp.templates import Template

        t = Template(name="x", subject=None, body="{a} and {b} and {c}.\n")
        with pytest.raises(
            MailTemplateMissingVariableError, match=r"a.*b.*c|c.*b.*a"
        ):
            t.render({"a": "1"})

    def test_extra_vars_are_ignored(self):
        from apple_mail_mcp.templates import Template

        t = Template(name="x", subject=None, body="Hello.\n")
        result = t.render({"unused": "ignored"})
        assert result["body"] == "Hello.\n"

    def test_escaped_braces_are_literal(self):
        from apple_mail_mcp.templates import Template

        t = Template(name="x", subject=None, body="Use {{name}} as a slot.\n")
        result = t.render({})
        assert result["body"] == "Use {name} as a slot.\n"

    def test_subject_none_returns_subject_none(self):
        from apple_mail_mcp.templates import Template

        t = Template(name="x", subject=None, body="Hi {name}.\n")
        result = t.render({"name": "Bob"})
        assert result["subject"] is None
        assert result["body"] == "Hi Bob.\n"


class TestTemplatePlaceholders:
    def test_combines_subject_and_body(self):
        from apple_mail_mcp.templates import Template

        t = Template(
            name="x",
            subject="{a}",
            body="{b} and {a}.",
        )
        assert t.placeholders() == ["a", "b"]

    def test_no_placeholders_returns_empty(self):
        from apple_mail_mcp.templates import Template

        t = Template(name="x", subject=None, body="static body")
        assert t.placeholders() == []
