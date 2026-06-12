"""Unit tests for utility functions."""

import json

import pytest

from apple_mail_mcp.exceptions import MailAppleScriptError
from apple_mail_mcp.utils import (
    DEFAULT_MAX_BODY_BYTES,
    applescript_account_clause,
    coerce_json_dict,
    coerce_json_list,
    escape_applescript_string,
    format_applescript_list,
    get_flag_index,
    is_account_uuid,
    is_apple_hosted_address,
    is_gmail_system_label,
    is_icloud_imap_host,
    make_body_safe,
    normalize_subject,
    parse_applescript_json,
    parse_applescript_list,
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


class TestIsAccountUUID:
    @pytest.mark.parametrize("value", [
        "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5",  # uppercase
        "dc5ac137-2f7a-4299-b3d0-4d3e06c18dd5",  # lowercase
        "Dc5Ac137-2F7a-4299-B3d0-4D3e06C18DD5",  # mixed case
        "00000000-0000-0000-0000-000000000000",  # all zeros
        "ffffffff-ffff-ffff-ffff-ffffffffffff",  # all f
    ])
    def test_recognizes_uuid_formats(self, value: str) -> None:
        assert is_account_uuid(value) is True

    @pytest.mark.parametrize("value", [
        "iCloud", "Gmail", "Yahoo!", "Work Account",
        "MobileMe", "",
        "DC5AC137-2F7A-4299-B3D0",  # too short
        "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5-extra",  # too long
        "DC5AC137_2F7A_4299_B3D0_4D3E06C18DD5",  # underscores not dashes
        "GHIJKLMN-2F7A-4299-B3D0-4D3E06C18DD5",  # non-hex chars
        " DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5",  # leading whitespace
        "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5 ",  # trailing whitespace
    ])
    def test_rejects_non_uuid_strings(self, value: str) -> None:
        assert is_account_uuid(value) is False


class TestAppleScriptAccountClause:
    def test_uuid_input_emits_account_id_clause(self) -> None:
        result = applescript_account_clause(
            "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        )
        assert result == 'account id "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"'

    def test_name_input_emits_account_clause(self) -> None:
        assert applescript_account_clause("iCloud") == 'account "iCloud"'

    def test_name_with_quote_is_escaped(self) -> None:
        result = applescript_account_clause('Weird "Name" Acct')
        assert '\\"Name\\"' in result
        assert result.startswith('account "')
        assert not result.startswith('account id')

    def test_lowercase_uuid_works(self) -> None:
        # Real Mail.app emits uppercase, but accepting either is harmless.
        result = applescript_account_clause(
            "dc5ac137-2f7a-4299-b3d0-4d3e06c18dd5"
        )
        assert result == 'account id "dc5ac137-2f7a-4299-b3d0-4d3e06c18dd5"'


class TestIsGmailSystemLabel:
    """Tests for is_gmail_system_label — used by update_mailbox /
    delete_mailbox to refuse operations on Gmail's IMAP-system labels."""

    def test_bare_gmail_parent_is_system_label(self) -> None:
        assert is_gmail_system_label("[Gmail]") is True

    def test_gmail_drafts_is_system_label(self) -> None:
        assert is_gmail_system_label("[Gmail]/Drafts") is True

    def test_gmail_sent_mail_is_system_label(self) -> None:
        assert is_gmail_system_label("[Gmail]/Sent Mail") is True

    def test_gmail_all_mail_is_system_label(self) -> None:
        assert is_gmail_system_label("[Gmail]/All Mail") is True

    def test_localized_google_mail_prefix_is_not_detected(self) -> None:
        # Italian Gmail; explicit defer per #164 follow-up notes.
        assert is_gmail_system_label("[Google Mail]/Tutta la posta") is False

    def test_user_folder_is_not_system_label(self) -> None:
        assert is_gmail_system_label("Newsletters") is False

    def test_word_gmail_without_brackets_is_not_system_label(self) -> None:
        assert is_gmail_system_label("Gmail") is False

    def test_empty_string_is_not_system_label(self) -> None:
        assert is_gmail_system_label("") is False

    def test_gmail_substring_in_middle_of_path_is_not_system_label(
        self,
    ) -> None:
        # Path traversal/spoof attempt: only the exact prefix counts.
        assert is_gmail_system_label("Archive/[Gmail]/Drafts") is False


class TestGetFlagIndex:
    """Issue #185: lock in the empirically-verified color → flag index
    mapping. Originally the codebase had orange↔red and blue↔green swapped
    relative to what Mail.app actually rendered (verified Gmail/Mail.app
    2026-05-12). No tests existed on get_flag_index before this fix —
    that is how the bug went undetected."""

    @pytest.mark.parametrize("color,expected_index", [
        ("none", -1),
        ("red", 0),
        ("orange", 1),
        ("yellow", 2),
        ("green", 3),
        ("blue", 4),
        ("purple", 5),
        ("gray", 6),
    ])
    def test_returns_correct_index_for_each_color(
        self, color: str, expected_index: int
    ) -> None:
        assert get_flag_index(color) == expected_index

    def test_raises_on_unknown_color(self) -> None:
        with pytest.raises(ValueError, match="Invalid flag color"):
            get_flag_index("magenta")

    def test_case_insensitive(self) -> None:
        assert get_flag_index("RED") == 0


class TestIsAppleHostedAddress:
    """#299: detect the account's own Apple-hosted iCloud Mail addresses."""

    def test_icloud_me_mac_are_apple_hosted(self) -> None:
        assert is_apple_hosted_address("a@icloud.com") is True
        assert is_apple_hosted_address("a@me.com") is True
        assert is_apple_hosted_address("a@mac.com") is True

    def test_case_and_whitespace_insensitive(self) -> None:
        assert is_apple_hosted_address("  A@ICloud.Com ") is True

    def test_third_party_and_custom_domains_not_apple_hosted(self) -> None:
        assert is_apple_hosted_address("a@gmail.com") is False
        assert is_apple_hosted_address("a@example.com") is False
        assert is_apple_hosted_address("") is False

    def test_substring_spoof_not_apple_hosted(self) -> None:
        # The Apple domain must be the actual host, not a prefix of a
        # look-alike domain.
        assert is_apple_hosted_address("a@icloud.com.evil.com") is False
        assert is_apple_hosted_address("icloud.com@evil.com") is False


class TestIsIcloudImapHost:
    """#299: detect iCloud Mail IMAP servers (*.mail.me.com)."""

    def test_partition_and_canonical_hosts(self) -> None:
        assert is_icloud_imap_host("p42-imap.mail.me.com") is True
        assert is_icloud_imap_host("imap.mail.me.com") is True
        assert is_icloud_imap_host("P66-IMAP.MAIL.ME.COM") is True

    def test_non_icloud_hosts(self) -> None:
        assert is_icloud_imap_host("imap.gmail.com") is False
        assert is_icloud_imap_host("imap.mail.yahoo.com") is False
        assert is_icloud_imap_host("") is False

    def test_substring_spoof_not_matched(self) -> None:
        assert is_icloud_imap_host("mail.me.com.evil.com") is False


class TestCoerceJsonList:
    """#309: coerce stringified arrays (MCP hosts that serialize params)."""

    def test_json_array_string_becomes_list(self) -> None:
        assert coerce_json_list('["a@b.com", "c@d.com"]') == ["a@b.com", "c@d.com"]

    def test_json_int_array_string_becomes_list(self) -> None:
        assert coerce_json_list("[0, 2]") == [0, 2]

    def test_bare_string_becomes_single_element(self) -> None:
        assert coerce_json_list("john@example.com") == ["john@example.com"]

    def test_empty_string_becomes_empty_list(self) -> None:
        assert coerce_json_list("") == []
        assert coerce_json_list("   ") == []

    def test_non_list_json_string_wraps_as_single_element(self) -> None:
        # A JSON scalar string isn't a list → treat the original as one elem.
        assert coerce_json_list("5") == ["5"]

    def test_real_list_passes_through(self) -> None:
        v = ["x@y.com"]
        assert coerce_json_list(v) is v

    def test_none_passes_through(self) -> None:
        assert coerce_json_list(None) is None


class TestCoerceJsonDict:
    """#309: coerce stringified objects."""

    def test_json_object_string_becomes_dict(self) -> None:
        assert coerce_json_dict('{"name": "Bob"}') == {"name": "Bob"}

    def test_real_dict_passes_through(self) -> None:
        v = {"k": "v"}
        assert coerce_json_dict(v) is v

    def test_none_passes_through(self) -> None:
        assert coerce_json_dict(None) is None

    def test_non_object_json_passes_through_to_fail_validation(self) -> None:
        # A JSON array/scalar isn't a dict → leave as-is for Pydantic to reject.
        assert coerce_json_dict("[1, 2]") == "[1, 2]"

    def test_garbage_string_passes_through(self) -> None:
        assert coerce_json_dict("not json") == "not json"
        assert get_flag_index("Red") == 0
        assert get_flag_index("oRaNgE") == 1


# --- Attachment content encoding (#250) ----------------------------------

import base64 as _base64  # noqa: E402

from apple_mail_mcp.utils import (  # noqa: E402
    attachment_content_encoding,
    is_texty_mime,
)


class TestIsTextyMime:
    @pytest.mark.parametrize(
        "mime",
        [
            "text/plain",
            "text/html",
            "TEXT/CSV",
            "application/json",
            "application/xml",
            "application/ld+json",
            "image/svg+xml",
        ],
    )
    def test_texty(self, mime):
        assert is_texty_mime(mime) is True

    @pytest.mark.parametrize(
        "mime",
        ["application/pdf", "image/png", "application/octet-stream", "", "audio/mpeg"],
    )
    def test_not_texty(self, mime):
        assert is_texty_mime(mime) is False


class TestAttachmentContentEncoding:
    def test_text_mime_utf8_returns_text(self):
        content, encoding = attachment_content_encoding(
            b"hello, world\n", "text/plain"
        )
        assert encoding == "text"
        assert content == "hello, world\n"

    def test_json_mime_returns_text(self):
        content, encoding = attachment_content_encoding(
            b'{"a": 1}', "application/json"
        )
        assert encoding == "text"
        assert content == '{"a": 1}'

    def test_texty_mime_invalid_utf8_falls_back_to_base64(self):
        payload = b"\xff\xfe\x00\x01bad utf8"
        content, encoding = attachment_content_encoding(payload, "text/plain")
        assert encoding == "base64"
        assert _base64.b64decode(content) == payload

    def test_binary_mime_returns_base64(self):
        payload = b"%PDF-1.7\n\x00\x01\x02"
        content, encoding = attachment_content_encoding(
            payload, "application/pdf"
        )
        assert encoding == "base64"
        assert _base64.b64decode(content) == payload

    def test_empty_payload_text_mime(self):
        content, encoding = attachment_content_encoding(b"", "text/plain")
        assert encoding == "text"
        assert content == ""


class TestMakeBodySafe:
    """Tests for make_body_safe (#365): message bodies must be bounded and
    serialization-safe before they leave get_messages, or a single oversized
    or non-UTF8-encodable body crashes the whole stdio MCP server."""

    def test_small_clean_content_unchanged(self):
        safe, truncated, original = make_body_safe(
            "hello world", DEFAULT_MAX_BODY_BYTES
        )
        assert safe == "hello world"
        assert truncated is False
        assert original == len(b"hello world")

    def test_empty_content(self):
        assert make_body_safe("", DEFAULT_MAX_BODY_BYTES) == ("", False, 0)

    def test_preserves_tab_newline_carriage_return(self):
        safe, truncated, _ = make_body_safe("a\tb\nc\rd", DEFAULT_MAX_BODY_BYTES)
        assert safe == "a\tb\nc\rd"
        assert truncated is False

    def test_strips_nul_and_c0_control_chars(self):
        safe, _, _ = make_body_safe(
            "a\x00b\x07c\x1fd", DEFAULT_MAX_BODY_BYTES
        )
        assert safe == "abcd"

    def test_scrubs_lone_surrogate_to_be_serializable(self):
        # A lone surrogate is the classic cause of UnicodeEncodeError on the
        # stdout write — the failure that escapes the tool's try/except.
        safe, _, _ = make_body_safe("hi\ud800there", DEFAULT_MAX_BODY_BYTES)
        safe.encode("utf-8")  # must not raise
        assert "\ud800" not in safe

    def test_truncates_oversized_content_and_reports_original(self):
        big = "x" * (DEFAULT_MAX_BODY_BYTES + 5000)
        safe, truncated, original = make_body_safe(big, DEFAULT_MAX_BODY_BYTES)
        assert truncated is True
        assert original == DEFAULT_MAX_BODY_BYTES + 5000
        assert len(safe.encode("utf-8")) <= DEFAULT_MAX_BODY_BYTES

    def test_truncation_does_not_split_multibyte_char(self):
        # "😀" is 4 UTF-8 bytes; a 10-byte cap must yield 2 whole emoji
        # (8 bytes), never a broken trailing byte sequence.
        safe, truncated, _ = make_body_safe("😀" * 100, 10)
        assert truncated is True
        safe.encode("utf-8")  # valid
        assert "�" not in safe  # no replacement char from a broken split
        assert len(safe.encode("utf-8")) <= 10

    def test_output_always_json_serializable(self):
        pathological = [
            "plain",
            "a\x00b\x1fc",
            "x" * (DEFAULT_MAX_BODY_BYTES + 100),
            "surrogate\ud800tail",
        ]
        for content in pathological:
            safe, _, _ = make_body_safe(content, DEFAULT_MAX_BODY_BYTES)
            # The assertion that the original bug would have failed.
            json.dumps({"content": safe}).encode("utf-8")
