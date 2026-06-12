"""
Utility functions for Apple Mail MCP.
"""

import base64
import json
import re
from typing import Any

_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)

# Cap for a single message body returned inline by get_messages (#365).
# Bodies are returned as part of a possibly multi-message JSON-RPC response;
# an unbounded body can produce an oversized frame the stdio client rejects,
# which takes the whole server down. Overridable via the server layer
# (APPLE_MAIL_MCP_MAX_BODY_BYTES), mirroring the attachment byte caps.
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB per message body

# C0 control characters minus tab (09), newline (0a), carriage return (0d) —
# the rest corrupt the JSON-RPC stream and carry no body content.
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def is_account_uuid(value: str) -> bool:
    """True iff the string matches the standard UUID format Mail.app emits.

    Mail.app account display names are user-chosen strings and won't collide
    with the 8-4-4-4-12 hex-with-hyphens UUID format.
    """
    return bool(_UUID_RE.match(value))


def applescript_account_clause(account: str) -> str:
    """Return an AppleScript object specifier for a Mail.app account.

    Returns ``account id "<uuid>"`` when ``account`` matches a UUID,
    otherwise ``account "<name>"``. Input is always escaped for safe
    AppleScript embedding regardless of form.

    Args:
        account: Either a Mail.app account display name (e.g. "Gmail") or
            its stable UUID (as returned by ``list_accounts``).

    Returns:
        The AppleScript fragment that resolves to that account, ready to
        embed in a tell-block.
    """
    safe = escape_applescript_string(sanitize_input(account))
    if is_account_uuid(account):
        return f'account id "{safe}"'
    return f'account "{safe}"'


def escape_applescript_string(s: str) -> str:
    """
    Escape string for safe AppleScript insertion.

    Args:
        s: String to escape

    Returns:
        Escaped string safe for AppleScript

    Examples:
        >>> escape_applescript_string('Hello "World"')
        'Hello \\\\"World\\\\"'
        >>> escape_applescript_string('Path\\to\\file')
        'Path\\\\to\\\\file'
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_applescript_list(result: str) -> list[str]:
    """
    Parse AppleScript list result into Python list.

    AppleScript returns lists as comma-separated values.

    Args:
        result: AppleScript output

    Returns:
        List of strings
    """
    if not result or result == "":
        return []

    # Handle empty list
    if result.strip() in ["{}", ""]:
        return []

    # Remove braces if present
    result = result.strip()
    if result.startswith("{") and result.endswith("}"):
        result = result[1:-1]

    # Split by comma and clean up
    items = [item.strip() for item in result.split(",") if item.strip()]
    return items


def format_applescript_list(items: list[str]) -> str:
    """
    Format Python list for AppleScript.

    Args:
        items: List of strings

    Returns:
        AppleScript list format

    Examples:
        >>> format_applescript_list(['a', 'b', 'c'])
        '{"a", "b", "c"}'
    """
    escaped_items = [f'"{escape_applescript_string(item)}"' for item in items]
    return "{" + ", ".join(escaped_items) + "}"


def validate_email(email: str) -> bool:
    """
    Validate email address format.

    Args:
        email: Email address to validate

    Returns:
        True if valid, False otherwise
    """
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def sanitize_input(value: Any) -> str:
    """
    Sanitize user input for safety.

    Args:
        value: User input value

    Returns:
        Sanitized string
    """
    if value is None:
        return ""

    # Convert to string
    s = str(value)

    # Remove null bytes
    s = s.replace("\x00", "")

    # Limit length
    max_length = 10000
    if len(s) > max_length:
        s = s[:max_length]

    return s


def make_body_safe(content: str, max_bytes: int) -> tuple[str, bool, int]:
    """Make a message body safe to return over the stdio MCP transport (#365).

    A single message body that is either huge or not UTF-8-encodable can take
    the whole server down: the oversized/invalid JSON-RPC frame is rejected by
    the client (pipe closes, server process exits) or raises UnicodeEncodeError
    on the stdout write — both *outside* the tool's ``try/except`` boundary.
    This bounds and scrubs the body so neither can happen.

    Scrub: coerce to encodable UTF-8 (lone surrogates / un-encodable codepoints
    become ``?``) and drop C0 control characters except tab, newline, and
    carriage return. Bound: if the scrubbed body exceeds ``max_bytes`` UTF-8
    bytes, truncate on a character boundary.

    Args:
        content: Raw message body text.
        max_bytes: Maximum UTF-8 byte length to return.

    Returns:
        ``(safe_content, truncated, original_bytes)`` where ``original_bytes``
        is the UTF-8 byte length of the scrubbed body *before* any truncation.
    """
    if not content:
        return "", False, 0
    # Coerce to encodable UTF-8 first — a lone surrogate would otherwise raise
    # UnicodeEncodeError when the response is written to stdout.
    scrubbed = content.encode("utf-8", "replace").decode("utf-8")
    # Drop control chars that corrupt the JSON-RPC stream, keeping the
    # whitespace that carries real body structure.
    scrubbed = _C0_CONTROL_RE.sub("", scrubbed)
    encoded = scrubbed.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return scrubbed, False, original_bytes
    # Truncate on a character boundary: slice bytes, then drop any partial
    # trailing multibyte sequence via errors="ignore".
    return encoded[:max_bytes].decode("utf-8", "ignore"), True, original_bytes


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename for safe file operations.

    Removes path traversal attempts, dangerous characters, and null bytes.

    Args:
        filename: Filename to sanitize

    Returns:
        Sanitized filename

    Example:
        >>> sanitize_filename("../../../etc/passwd")
        'etc_passwd'
        >>> sanitize_filename("my-file_v2.txt")
        'my-file_v2.txt'
    """
    import re
    from pathlib import Path

    # Remove null bytes
    filename = filename.replace("\x00", "")

    # Get basename only (no path components)
    filename = Path(filename).name

    # Replace dangerous characters with underscore
    # Keep: letters, numbers, dash, underscore, period
    filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)

    # Remove leading dots (hidden files)
    filename = filename.lstrip('.')

    # Limit length
    max_length = 255
    if len(filename) > max_length:
        # Preserve extension
        name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        if ext:
            name = name[:max_length - len(ext) - 1]
            filename = f"{name}.{ext}"
        else:
            filename = filename[:max_length]

    # Ensure not empty
    if not filename:
        filename = "unnamed_file"

    return filename


def sanitize_mailbox_name(name: str) -> str:
    """
    Sanitize mailbox/folder name for safe operations.

    Args:
        name: Mailbox name to sanitize

    Returns:
        Sanitized mailbox name

    Example:
        >>> sanitize_mailbox_name("Valid Name")
        'Valid Name'
        >>> sanitize_mailbox_name("../../../etc")
        ''
    """
    import re

    # Remove null bytes
    name = name.replace("\x00", "")

    # Remove path traversal attempts
    name = name.replace("..", "")
    name = name.replace("/", "")
    name = name.replace("\\", "")

    # Remove dangerous characters but keep spaces, dashes, underscores
    name = re.sub(r'[<>:"|?*]', '', name)

    # Trim whitespace
    name = name.strip()

    return name


def is_gmail_system_label(name: str) -> bool:
    """Return True if ``name`` is the bare Gmail system parent
    (``[Gmail]``) or any ``[Gmail]/...`` child path.

    Used by update_mailbox / delete_mailbox to refuse operations on
    Gmail's IMAP-system labels (Drafts, Sent Mail, Trash, Spam,
    Important, Starred, etc.). Localized Gmail prefixes such as
    ``[Google Mail]/`` are not detected — proper localization handling
    requires an IMAP session for SPECIAL-USE flag enumeration; tracked
    as a follow-up to #164.
    """
    return name == "[Gmail]" or name.startswith("[Gmail]/")


def validate_flag_color(color: str) -> bool:
    """
    Validate flag color name.

    Args:
        color: Flag color name

    Returns:
        True if valid color, False otherwise

    Example:
        >>> validate_flag_color("red")
        True
        >>> validate_flag_color("invalid")
        False
    """
    valid_colors = {"none", "orange", "red", "yellow", "blue", "green", "purple", "gray"}
    return color.lower() in valid_colors


def get_flag_index(color: str) -> int:
    """
    Get AppleScript flag index for a color name.

    Args:
        color: Flag color name

    Returns:
        Flag index for AppleScript (-1 to 6)

    Raises:
        ValueError: If color is invalid

    Example:
        >>> get_flag_index("red")
        0
        >>> get_flag_index("none")
        -1
    """
    # Mapping verified empirically against Mail.app's rendering
    # (Gmail/Mail.app, 2026-05-12 — see #185). The codebase previously
    # had orange↔red and blue↔green swapped, so callers asking for
    # "orange" got red, "red" got orange, etc.
    color_map = {
        "none": -1,
        "red": 0,
        "orange": 1,
        "yellow": 2,
        "green": 3,
        "blue": 4,
        "purple": 5,
        "gray": 6,
    }

    color_lower = color.lower()
    if color_lower not in color_map:
        raise ValueError(
            f"Invalid flag color: {color}. "
            f"Valid colors: {', '.join(color_map.keys())}"
        )

    return color_map[color_lower]


def parse_applescript_json(result: str) -> Any:
    """Parse JSON emitted by an AppleScript helper, or raise on ERROR: prefix.

    AppleScript scripts wrapped with _wrap_as_json_script return either:
    - A JSON-serialized string (list, dict, or scalar), or
    - "ERROR: <message>" when the tell-block catches an error.

    Note: the "ERROR:" prefix is a sentinel only valid because _wrap_as_json_script
    returns list/dict payloads — a bare JSON string starting with "ERROR:" would be
    misread. Wrapped scripts must never return a bare-string top-level value.

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


# Subject prefixes that indicate a reply or forward, case-insensitive.
# Order doesn't matter; we strip the first match each pass and repeat.
_REPLY_PREFIXES = ("re:", "fwd:", "fw:")


def normalize_subject(subject: str) -> str:
    """Strip reply/forward prefixes from a subject for thread matching.

    Iteratively removes leading "Re:", "Fwd:", "Fw:" (case-insensitive) and
    surrounding whitespace so that all messages in a thread share one base
    key regardless of how many times the subject has been Re:'d.

    Args:
        subject: Raw subject line.

    Returns:
        Normalized subject. Empty input returns empty output.
    """
    s = subject.strip()
    changed = True
    while changed:
        changed = False
        for prefix in _REPLY_PREFIXES:
            if s.lower().startswith(prefix):
                s = s[len(prefix):].lstrip()
                changed = True
                break
    return s


def parse_rfc822_ids(raw: str) -> list[str]:
    """Parse an In-Reply-To or References header into a list of Message-IDs.

    RFC 5322 canonical form is `<id@domain>` separated by whitespace or
    folded newlines. Some clients emit bare ids without angle brackets —
    we accept both. Returns ids without angle brackets, order preserved,
    duplicates removed.

    Args:
        raw: Header content (e.g., "<a@x> <b@x>").

    Returns:
        List of cleaned message-id strings. Empty input returns empty list.
    """
    tokens = raw.split()
    out: list[str] = []
    for tok in tokens:
        cleaned = tok.strip().lstrip("<").rstrip(">").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def walk_thread_graph(
    known_ids: set[str],
    candidates: list[dict[str, Any]],
    max_iterations: int = 100,
) -> list[dict[str, Any]]:
    """Graph-walk a candidate set, accepting members whose references
    transitively connect to known_ids.

    Iterates until stable. Each pass may add candidates whose rfc_message_id,
    in_reply_to, or any parsed references overlap the known-id frontier.
    Accepted candidates contribute their own ids back into the frontier.

    Args:
        known_ids: Seed set of RFC 822 Message-IDs known to belong to the
            thread (typically {anchor.rfc_message_id} plus the anchor's own
            in_reply_to and references).
        candidates: List of dicts with keys ``id``, ``rfc_message_id``,
            ``in_reply_to``, ``references_parsed`` (list[str]). Anchor
            itself should NOT appear in this list.
        max_iterations: Cycle-safety cap. Real threads stabilize in 1-2
            passes; the cap only matters for malformed header chains.

    Returns:
        Accepted candidates in their original order.
    """
    accepted: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    frontier = set(known_ids)

    for _ in range(max_iterations):
        changed = False
        for cand in candidates:
            if cand["id"] in accepted_ids:
                continue
            refs = {cand["rfc_message_id"]}
            if cand["in_reply_to"]:
                refs.add(cand["in_reply_to"])
            refs.update(cand["references_parsed"])
            if refs & frontier:
                accepted.append(cand)
                accepted_ids.add(cand["id"])
                frontier |= refs
                changed = True
        if not changed:
            break

    return accepted


def coerce_json_list(v: Any) -> Any:
    """Coerce a stringified array back to a list (#309).

    Some MCP hosts (e.g. Cowork) serialize every tool argument as a string,
    so a ``list[...]`` param arrives as ``'["a@b.com"]'`` and Pydantic
    rejects it with a ``list_type`` error. Used as a Pydantic
    ``BeforeValidator`` on list-typed tool params: a JSON-array string
    becomes the list, a bare non-JSON string becomes a single-element list,
    an empty string becomes ``[]``. Real lists and ``None`` pass through
    untouched, so well-behaved clients are unaffected. Element types are
    still validated by Pydantic afterwards.
    """
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return [v]
        return parsed if isinstance(parsed, list) else [v]
    return v


def coerce_json_dict(v: Any) -> Any:
    """Coerce a stringified object back to a dict (#309).

    Companion to :func:`coerce_json_list` for ``dict``-typed tool params
    (e.g. rule ``actions``, template ``vars``). A JSON-object string becomes
    the dict; anything else (real dict, ``None``, non-object JSON, garbage)
    passes through to be validated/rejected normally.
    """
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return v
        return parsed if isinstance(parsed, dict) else v
    return v


def is_apple_hosted_address(address: str) -> bool:
    """True if ``address`` is on an Apple-hosted iCloud Mail domain.

    iCloud Mail's IMAP server authenticates one of the account's own
    Apple-hosted addresses (``@icloud.com`` / ``@me.com`` / legacy
    ``@mac.com``). Used by ``_resolve_imap_config`` to pick the right IMAP
    login when an iCloud account's Apple ID is a third-party email (#299).
    """
    return address.strip().lower().endswith(
        ("@icloud.com", "@me.com", "@mac.com")
    )


def is_icloud_imap_host(host: str) -> bool:
    """True if ``host`` is an iCloud Mail IMAP server (``*.mail.me.com``).

    Covers the per-partition hostnames Mail.app reports (e.g.
    ``p42-imap.mail.me.com``) as well as ``imap.mail.me.com``.
    """
    return host.strip().lower().endswith(".mail.me.com")


def is_texty_mime(mime_type: str) -> bool:
    """True for MIME types whose payload is human-readable text and worth
    returning as a UTF-8 string rather than base64 (#250).

    Covers ``text/*`` plus the common structured-text application types
    (``application/json``, ``application/xml``, and any ``+json`` / ``+xml``
    structured-syntax suffix, e.g. ``image/svg+xml``).
    """
    m = (mime_type or "").strip().lower()
    if m.startswith("text/"):
        return True
    if m in ("application/json", "application/xml"):
        return True
    return m.endswith("+json") or m.endswith("+xml")


def attachment_content_encoding(
    payload: bytes, mime_type: str
) -> tuple[str, str]:
    """Encode attachment bytes for inline return (#250).

    Returns ``(content, encoding)``. For texty MIME types
    (:func:`is_texty_mime`) the payload is returned as a UTF-8 string with
    ``encoding="text"``; if it isn't valid UTF-8 — or the type isn't texty —
    it's base64-encoded with ``encoding="base64"``. Fail-safe: anything that
    can't be decoded as text round-trips losslessly through base64.
    """
    if is_texty_mime(mime_type):
        try:
            return payload.decode("utf-8"), "text"
        except UnicodeDecodeError:
            pass
    return base64.b64encode(payload).decode("ascii"), "base64"
