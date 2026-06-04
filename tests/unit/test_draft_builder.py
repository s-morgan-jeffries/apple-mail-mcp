"""Unit tests for the clean RFC822 draft builder (issue #245).

The builder exists so drafts can be created via IMAP APPEND instead of
Mail.app's AppleScript ``content`` setter, which wraps every body in an
``Apple-Mail-URLShareWrapper`` ``<blockquote type="cite">`` (renders as a
quote on iOS). These tests pin the clean output.
"""

from __future__ import annotations

import email
from email import policy

from apple_mail_mcp.draft_builder import build_draft_mime


def test_builds_plain_text_draft_without_quote_wrapper():
    msgid, raw = build_draft_mime(
        sender="email@fmasi.eu",
        to=["lazar@hadleigh.co.uk"],
        subject="Re: Flat 9 Constable House",
        body="Hi Lazar,\n\nLine two.",
    )
    text = raw.decode("utf-8")
    # The whole point of the fix: no cite-blockquote wrapper.
    assert "blockquote" not in text.lower()
    assert "urlshare" not in text.lower()

    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["From"] == "email@fmasi.eu"
    assert msg["To"] == "lazar@hadleigh.co.uk"
    assert msg["Subject"] == "Re: Flat 9 Constable House"
    assert msg["Message-ID"] == msgid
    assert msg.get_content_type() == "text/plain"
    assert "Line two." in msg.get_content()


def test_multiple_recipients_and_cc_bcc():
    _msgid, raw = build_draft_mime(
        sender="email@fmasi.eu",
        to=["a@example.invalid", "b@example.invalid"],
        cc=["c@example.invalid"],
        bcc=["d@example.invalid"],
        subject="hi",
        body="body",
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["To"] == "a@example.invalid, b@example.invalid"
    assert msg["Cc"] == "c@example.invalid"
    assert msg["Bcc"] == "d@example.invalid"


def test_attachment_is_included_with_body(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfake")
    _msgid, raw = build_draft_mime(
        sender="email@fmasi.eu",
        to=["x@example.invalid"],
        subject="hi",
        body="see attached",
        attachments=[pdf],
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    body_part = next(p for p in parts if p.get_content_type() == "text/plain")
    assert "see attached" in body_part.get_content()
    att = next(p for p in parts if p.get_filename() == "invoice.pdf")
    assert att.get_content_type() == "application/pdf"
    assert att.get_payload(decode=True) == b"%PDF-1.7\nfake"
    assert "blockquote" not in raw.decode("utf-8", "replace").lower()


def test_sanitizes_header_injection_chars():
    # NUL and CR/LF in header-bound fields must not survive into the
    # serialized headers (parity with the AppleScript path, #173, and
    # header-injection safety).
    _msgid, raw = build_draft_mime(
        sender="Alice\x00Smith <me@x.com>",
        to=["a@example.invalid\r\nBcc: evil@example.invalid"],
        subject="hi\r\nX-Injected: yes",
        body="body",
    )
    assert b"\x00" not in raw
    msg = email.message_from_bytes(raw, policy=policy.default)
    # No header injection: the CR/LF collapse into a single (harmless)
    # value, so the smuggled headers never materialize as real headers.
    assert msg["From"] == "AliceSmith <me@x.com>"
    assert msg["Bcc"] is None
    assert msg["X-Injected"] is None


# --- HTML body support (issue #251) --------------------------------------


def test_body_html_only_builds_multipart_alternative_with_derived_plain():
    """body_html alone -> multipart/alternative with a text/html part and a
    text/plain part auto-derived from the HTML (so non-HTML readers and
    reply-quoting still have something)."""
    _msgid, raw = build_draft_mime(
        sender="me@example.invalid",
        to=["you@example.invalid"],
        subject="Q2 numbers",
        body="",
        body_html="<p>Revenue <b>up 12%</b></p>",
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/alternative"

    html_part = msg.get_body(preferencelist=("html",))
    plain_part = msg.get_body(preferencelist=("plain",))
    assert html_part is not None and plain_part is not None
    assert "<b>up 12%</b>" in html_part.get_content()
    # Derived plain strips tags but keeps the text.
    derived = plain_part.get_content()
    assert "Revenue" in derived and "up 12%" in derived
    assert "<b>" not in derived


def test_body_and_body_html_uses_body_as_plain_part():
    """When both are given, body is the text/plain alternative verbatim and
    body_html is the text/html part (standard multipart shape)."""
    _msgid, raw = build_draft_mime(
        sender="me@example.invalid",
        to=["you@example.invalid"],
        subject="hi",
        body="plain fallback text",
        body_html="<p>rich text</p>",
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/alternative"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == (
        "plain fallback text"
    )
    assert "<p>rich text</p>" in msg.get_body(
        preferencelist=("html",)
    ).get_content()


def test_body_only_stays_single_text_plain():
    """No body_html -> unchanged behavior: a single text/plain part."""
    _msgid, raw = build_draft_mime(
        sender="me@example.invalid",
        to=["you@example.invalid"],
        subject="hi",
        body="just text",
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "text/plain"
    assert "just text" in msg.get_content()


def test_body_html_with_attachment_nests_alternative_in_mixed(tmp_path):
    """body_html + attachment -> multipart/mixed wrapping the
    multipart/alternative and the attachment."""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfake")
    _msgid, raw = build_draft_mime(
        sender="me@example.invalid",
        to=["you@example.invalid"],
        subject="report",
        body="see attached",
        body_html="<p>see <i>attached</i></p>",
        attachments=[pdf],
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/mixed"
    # Both alternatives still reachable.
    assert msg.get_body(preferencelist=("html",)) is not None
    assert msg.get_body(preferencelist=("plain",)) is not None
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert "report.pdf" in names


# --- Reply/forward extensions (issue #245 follow-up) ---------------------

from apple_mail_mcp.draft_builder import (  # noqa: E402
    build_forward_body,
    build_reply_body,
    derive_reply_recipients,
    forward_subject,
    reply_subject,
)


def test_threading_headers_and_no_blockquote():
    msgid, raw = build_draft_mime(
        sender="email@fmasi.eu",
        to=["lazar@hadleigh.co.uk"],
        subject="Re: Flat 9 Constable House",
        body="Hi Lazar,\n\n> quoted line",
        in_reply_to="<orig@mail.example.com>",
        references=["<root@x>", "<orig@mail.example.com>"],
    )
    text = raw.decode("utf-8")
    assert "blockquote" not in text.lower()
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["In-Reply-To"] == "<orig@mail.example.com>"
    assert msg["References"] == "<root@x> <orig@mail.example.com>"
    assert msg.get_content_type() == "text/plain"
    assert msgid != "<orig@mail.example.com>"


def test_forwarded_attachments_travel_with_draft():
    _msgid, raw = build_draft_mime(
        sender="email@fmasi.eu",
        to=["someone@example.com"],
        subject="Fwd: Statement",
        body="See attached.",
        forwarded_attachments=[("statement.pdf", "application", "pdf", b"%PDF-1.4 fake")],
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert "statement.pdf" in names
    att = next(p for p in msg.iter_attachments() if p.get_filename() == "statement.pdf")
    assert att.get_content_type() == "application/pdf"
    assert att.get_payload(decode=True) == b"%PDF-1.4 fake"


def test_reply_subject_no_double_prefix():
    assert reply_subject("Flat 9") == "Re: Flat 9"
    assert reply_subject("Re: Flat 9") == "Re: Flat 9"
    assert reply_subject("re: flat 9") == "re: flat 9"
    assert reply_subject("") == "Re:"


def test_forward_subject_no_double_prefix():
    assert forward_subject("Flat 9") == "Fwd: Flat 9"
    assert forward_subject("Fwd: Flat 9") == "Fwd: Flat 9"
    assert forward_subject("FW: Flat 9") == "FW: Flat 9"


def test_derive_reply_recipients_simple():
    to, cc = derive_reply_recipients(
        from_header="Lazar <lazar@hadleigh.co.uk>",
        to_header="email@fmasi.eu, someone@else.com",
        cc_header="cc@x.com",
        self_addresses=["email@fmasi.eu"],
        reply_all=False,
    )
    assert to == ["Lazar <lazar@hadleigh.co.uk>"]
    assert cc == []


def test_derive_reply_all_excludes_self_and_primary():
    to, cc = derive_reply_recipients(
        from_header="Lazar <lazar@hadleigh.co.uk>",
        to_header="email@fmasi.eu, Bob <bob@x.com>",
        cc_header="lazar@hadleigh.co.uk, carol@y.com",
        self_addresses=["email@fmasi.eu"],
        reply_all=True,
    )
    assert to == ["Lazar <lazar@hadleigh.co.uk>"]
    cc_emails = cc
    # self excluded, primary (lazar) excluded, the rest kept once
    assert any("bob@x.com" in c for c in cc_emails)
    assert any("carol@y.com" in c for c in cc_emails)
    assert not any("email@fmasi.eu" in c for c in cc_emails)
    assert not any("lazar@hadleigh.co.uk" in c for c in cc_emails)


def test_reply_to_overrides_from():
    to, _cc = derive_reply_recipients(
        from_header="noreply@bot.com",
        reply_to_header="Real Person <real@person.com>",
    )
    assert to == ["Real Person <real@person.com>"]


def test_build_reply_body_quotes_original():
    out = build_reply_body(
        new_body="Thanks Lazar.",
        original_from="Lazar <lazar@hadleigh.co.uk>",
        original_date="29 May 2026 14:10",
        original_text="Hi Frederic,\n\nConfirming the invoice.",
    )
    assert out.startswith("Thanks Lazar.")
    assert "On 29 May 2026 14:10, Lazar <lazar@hadleigh.co.uk> wrote:" in out
    assert "> Hi Frederic," in out
    assert ">\n" in out  # blank original line quoted as bare ">"
    assert "> Confirming the invoice." in out


def test_build_forward_body_has_header_block():
    out = build_forward_body(
        new_body="FYI",
        original_from="Lazar <lazar@hadleigh.co.uk>",
        original_date="29 May 2026 14:10",
        original_subject="Flat 9",
        original_to="email@fmasi.eu",
        original_text="Body here.",
    )
    assert out.startswith("FYI")
    assert "---------- Forwarded message ----------" in out
    assert "From: Lazar <lazar@hadleigh.co.uk>" in out
    assert "Subject: Flat 9" in out
    assert "Body here." in out


def test_parse_original_message_extracts_fields_and_attachment():
    from apple_mail_mcp.draft_builder import build_draft_mime, parse_original_message
    # Build a representative original (with an attachment) and round-trip it.
    _mid, raw = build_draft_mime(
        sender="Lazar <lazar@hadleigh.co.uk>",
        to=["email@fmasi.eu", "Bob <bob@x.com>"],
        subject="Flat 9 Constable House",
        body="Hi Frederic,\n\nConfirming the invoice.",
        cc=["carol@y.com"],
        in_reply_to="<prev@x>",
        references=["<root@x>", "<prev@x>"],
        forwarded_attachments=[("inv.pdf", "application", "pdf", b"%PDF data")],
    )
    orig = parse_original_message(raw)
    assert "lazar@hadleigh.co.uk" in orig.from_header
    assert "email@fmasi.eu" in orig.to_header
    assert "carol@y.com" in orig.cc_header
    assert orig.subject == "Flat 9 Constable House"
    assert orig.references == ["<root@x>", "<prev@x>"]
    assert "Confirming the invoice." in orig.text
    assert any(a[0] == "inv.pdf" and a[3] == b"%PDF data" for a in orig.attachments)


def test_parse_original_html_only_falls_back_to_text():
    from email.message import EmailMessage

    from apple_mail_mcp.draft_builder import parse_original_message
    m = EmailMessage()
    m["From"] = "x@y.com"
    m["Subject"] = "HTML only"
    m.set_content("<p>Hello <b>there</b></p>", subtype="html")
    orig = parse_original_message(m.as_bytes())
    assert "Hello" in orig.text and "there" in orig.text
    assert "<p>" not in orig.text
