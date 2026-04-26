"""Email templates: storage and rendering.

A template is a plain-text file with an optional `subject:` header
followed by a blank line and a body. Placeholders use Python `str.format`
syntax: `{name}`. Escape literal braces as `{{` / `}}`. No conditionals
or loops.

File format:

    subject: Re: {original_subject}

    Hi {recipient_name},

    Thanks for reaching out.

The header block (above the first blank line) supports only `subject:`
in v1. The body is everything after the first blank line. Trailing
newlines in the body are preserved.
"""

from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exceptions import (
    MailTemplateInvalidFormatError,
    MailTemplateInvalidNameError,
    MailTemplateMissingVariableError,
    MailTemplateNotFoundError,
)

# Template name validation: alphanumerics, underscore, hyphen; 1-64 chars.
# Path-traversal protection — names are also used as filename stems, so
# anything that would let a name escape the templates directory must be
# rejected here.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# File extension. Markdown for syntax highlighting in editors; the
# parser doesn't care.
_EXT = ".md"

# Identifiers that count as placeholders. Must be a valid Python
# identifier (letter/underscore start, then alphanumerics/underscores).
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Header keys we recognize in the file format. Add to this list when
# expanding the schema (and update parse_template_file accordingly).
_KNOWN_HEADER_KEYS = {"subject"}


def extract_placeholders(text: str) -> list[str]:
    """Return sorted, deduped list of `{name}` placeholders in `text`.

    Escaped braces (`{{`, `}}`) are stripped before scanning so they
    don't produce spurious matches.
    """
    if not text:
        return []
    # Remove escaped braces so they don't get scanned as placeholders.
    cleaned = text.replace("{{", "").replace("}}", "")
    return sorted(set(_PLACEHOLDER_RE.findall(cleaned)))


@dataclass(frozen=True)
class Template:
    name: str
    subject: str | None
    body: str

    def placeholders(self) -> list[str]:
        """Sorted, deduped list of placeholders across subject and body."""
        combined = (self.subject or "") + "\n" + self.body
        return extract_placeholders(combined)

    def render(self, vars: dict[str, Any]) -> dict[str, str | None]:
        """Substitute placeholders in subject and body.

        Returns ``{"subject": str | None, "body": str}``. Missing
        placeholders raise MailTemplateMissingVariableError naming
        every unresolved placeholder (sorted).
        """
        rendered_subject = (
            _substitute(self.subject, vars) if self.subject is not None else None
        )
        rendered_body = _substitute(self.body, vars)
        return {"subject": rendered_subject, "body": rendered_body}


def _substitute(text: str, vars: dict[str, Any]) -> str:
    """str.format_map with a clear error on missing keys."""
    try:
        return string.Formatter().vformat(text, (), vars)
    except KeyError as e:
        # Collect ALL missing placeholders, not just the first.
        needed = set(extract_placeholders(text))
        missing = sorted(k for k in needed if k not in vars)
        if not missing:
            # Shouldn't happen, but fall back to the underlying KeyError.
            raise MailTemplateMissingVariableError(
                f"missing placeholder: {e.args[0]!r}"
            ) from e
        raise MailTemplateMissingVariableError(
            f"missing placeholder(s): {', '.join(missing)}"
        ) from e


def parse_template_file(text: str, *, name: str) -> Template:
    """Parse a template file's text into a Template.

    The file is split on the first blank line. Lines before the blank
    line form the header block (`key: value` pairs); lines after form
    the body. A file with no blank line is rejected as malformed.
    """
    if not text:
        raise MailTemplateInvalidFormatError(
            f"template {name!r} is empty"
        )

    # Split into header_block and body on the first blank line.
    # A leading blank line means "no headers, body follows".
    lines = text.splitlines(keepends=True)
    blank_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "":
            blank_idx = i
            break

    if blank_idx is None:
        raise MailTemplateInvalidFormatError(
            f"template {name!r} has no blank line separating headers from body"
        )

    header_lines = lines[:blank_idx]
    body_lines = lines[blank_idx + 1 :]
    body = "".join(body_lines)
    if not body.strip():
        raise MailTemplateInvalidFormatError(
            f"template {name!r} body is empty"
        )

    headers: dict[str, str] = {}
    for raw in header_lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        if ":" not in line:
            raise MailTemplateInvalidFormatError(
                f"template {name!r}: malformed header line {line!r}"
            )
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key not in _KNOWN_HEADER_KEYS:
            raise MailTemplateInvalidFormatError(
                f"template {name!r}: unknown header key {key!r}"
            )
        headers[key] = value

    return Template(
        name=name,
        subject=headers.get("subject"),
        body=body,
    )


def serialize_template(t: Template) -> str:
    """Inverse of parse_template_file. Output ends with the body's
    trailing newline (or a single newline if the body had none)."""
    header_block = ""
    if t.subject is not None:
        header_block = f"subject: {t.subject}\n"
    return f"{header_block}\n{t.body}"


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise MailTemplateInvalidNameError(
            f"template name {name!r} must match {_NAME_RE.pattern}"
        )


def default_root() -> Path:
    """Default templates directory, honoring APPLE_MAIL_MCP_HOME."""
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = Path(home_override) if home_override else Path.home() / ".apple_mail_mcp"
    return base / "templates"


class TemplateStore:
    """File-backed template store. One file per template at
    ``<root>/<name>.md``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    def _path_for(self, name: str) -> Path:
        _validate_name(name)
        return self.root / f"{name}{_EXT}"

    def list(self) -> list[Template]:
        """All templates, sorted by name. Missing directory returns
        empty list. Files that fail to parse are skipped silently
        here — call get() to surface format errors per-template."""
        if not self.root.is_dir():
            return []
        out: list[Template] = []
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name):
            if entry.suffix != _EXT or not entry.is_file():
                continue
            name = entry.stem
            if not _NAME_RE.match(name):
                continue
            try:
                out.append(self.get(name))
            except MailTemplateInvalidFormatError:
                continue
        return out

    def get(self, name: str) -> Template:
        path = self._path_for(name)
        if not path.is_file():
            raise MailTemplateNotFoundError(
                f"no template named {name!r}"
            )
        text = path.read_text(encoding="utf-8")
        return parse_template_file(text, name=name)

    def save(self, template: Template) -> bool:
        """Write template to disk. Returns True if newly created,
        False if it overwrote an existing template."""
        path = self._path_for(template.name)
        existed = path.is_file()
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize_template(template), encoding="utf-8")
        return not existed

    def delete(self, name: str) -> None:
        path = self._path_for(name)
        if not path.is_file():
            raise MailTemplateNotFoundError(
                f"no template named {name!r}"
            )
        path.unlink()
