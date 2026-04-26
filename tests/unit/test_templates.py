"""Unit tests for the email template module."""

from pathlib import Path

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


class TestTemplateStore:
    """Storage layer: list/get/save/delete + name validation."""

    def _store(self, tmp_path: Path):
        from apple_mail_mcp.templates import TemplateStore

        return TemplateStore(root=tmp_path)

    def test_save_new_returns_created_true(self, tmp_path: Path):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        t = Template(name="foo", subject="hi", body="body\n")
        assert store.save(t) is True
        # File on disk:
        assert (tmp_path / "foo.md").exists()

    def test_save_overwrite_returns_created_false(self, tmp_path: Path):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        store.save(Template(name="foo", subject=None, body="v1\n"))
        assert (
            store.save(Template(name="foo", subject=None, body="v2\n"))
            is False
        )
        assert store.get("foo").body == "v2\n"

    def test_get_round_trips_through_disk(self, tmp_path: Path):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        original = Template(
            name="r",
            subject="Re: {orig}",
            body="Hi {name}.\n\nBye.\n",
        )
        store.save(original)
        loaded = store.get("r")
        assert loaded == original

    def test_get_nonexistent_raises_not_found(self, tmp_path: Path):
        store = self._store(tmp_path)
        with pytest.raises(MailTemplateNotFoundError, match="missing"):
            store.get("missing")

    def test_delete_nonexistent_raises_not_found(self, tmp_path: Path):
        store = self._store(tmp_path)
        with pytest.raises(MailTemplateNotFoundError):
            store.delete("never-existed")

    def test_delete_removes_file(self, tmp_path: Path):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        store.save(Template(name="x", subject=None, body="body\n"))
        store.delete("x")
        assert not (tmp_path / "x.md").exists()
        with pytest.raises(MailTemplateNotFoundError):
            store.get("x")

    def test_list_returns_empty_when_no_templates(self, tmp_path: Path):
        store = self._store(tmp_path)
        assert store.list() == []

    def test_list_returns_sorted_by_name(self, tmp_path: Path):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        store.save(Template(name="zebra", subject=None, body="z\n"))
        store.save(Template(name="alpha", subject=None, body="a\n"))
        store.save(Template(name="middle", subject="hi", body="m\n"))
        names = [t.name for t in store.list()]
        assert names == ["alpha", "middle", "zebra"]

    def test_list_skips_non_md_files(self, tmp_path: Path):
        # A README.txt or other file in the dir shouldn't show up
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        store.save(Template(name="real", subject=None, body="ok\n"))
        (tmp_path / "README.txt").write_text("not a template")
        names = [t.name for t in store.list()]
        assert names == ["real"]

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            "..",
            "../escape",
            "with space",
            "with/slash",
            "a" * 65,  # too long
            "with.dot",
            "with$",
        ],
    )
    def test_save_rejects_invalid_names(self, tmp_path: Path, bad_name: str):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        with pytest.raises(MailTemplateInvalidNameError):
            store.save(Template(name=bad_name, subject=None, body="x\n"))

    @pytest.mark.parametrize(
        "bad_name",
        ["", "..", "../etc/passwd", "with space", "x" * 65],
    )
    def test_get_rejects_invalid_names(self, tmp_path: Path, bad_name: str):
        store = self._store(tmp_path)
        with pytest.raises(MailTemplateInvalidNameError):
            store.get(bad_name)

    @pytest.mark.parametrize(
        "good_name",
        [
            "a",
            "polite_decline",
            "polite-decline",
            "v2",
            "ABC",
            "X" * 64,  # max length
        ],
    )
    def test_save_accepts_valid_names(self, tmp_path: Path, good_name: str):
        from apple_mail_mcp.templates import Template

        store = self._store(tmp_path)
        # Just shouldn't raise.
        store.save(Template(name=good_name, subject=None, body="ok\n"))

    def test_corrupted_file_raises_invalid_format(self, tmp_path: Path):
        # Drop a malformed file directly into the dir, then read it.
        store = self._store(tmp_path)
        (tmp_path / "broken.md").write_text("frobnicate: nope\n\nbody\n")
        with pytest.raises(MailTemplateInvalidFormatError):
            store.get("broken")

    def test_root_created_lazily_on_first_save(self, tmp_path: Path):
        from apple_mail_mcp.templates import Template, TemplateStore

        nested = tmp_path / "nested" / "templates"
        # Doesn't exist yet:
        assert not nested.exists()
        store = TemplateStore(root=nested)
        # list() on a missing dir is fine — returns empty.
        assert store.list() == []
        # save() creates the dir.
        store.save(Template(name="x", subject=None, body="ok\n"))
        assert nested.is_dir()
