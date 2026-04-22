# get_thread Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `get_thread(message_id)` MCP tool that returns all messages in the conversation containing the anchor message, chronologically sorted, using an AppleScript-based subject-prefilter + RFC 5322 header graph walk.

**Architecture:** Two AppleScript calls (resolve anchor; collect subject-matching candidates with their threading headers), then a pure-Python graph walk over the candidate set. Subject prefilter is mandatory for feasibility — `whose message id is "X"` is ~21s per lookup on a real Gmail INBOX vs sub-second for `whose subject contains "X"`.

**Tech Stack:** Python 3.10+, FastMCP, AppleScript + ASObjC for JSON emission (existing `_wrap_as_json_script` helper), NSJSONSerialization for output.

**Design doc:** [`docs/plans/2026-04-21-get-thread-design.md`](2026-04-21-get-thread-design.md)

---

## Preflight

**Verified against the user's real Gmail account:**

- `whose subject contains "X"` on all mailboxes of Gmail: sub-second per mailbox.
- Candidate-collection script (iterate all mailboxes, subject-prefilter, read `in-reply-to` / `references` / `message-id` per hit, return JSON via NSJSONSerialization) returns 370 candidates on a very broad `"Welcome"` probe. Full-mailbox iteration completes; returns clean JSON.
- `whose message id is "X"` (single exact-match) took ~21 s on the user's INBOX. Not usable in a graph walk.
- `message id of msg` works; `headers of msg` yields header objects with lowercase names (`in-reply-to`, `references`, `message-id`).

**Tool count:** MCP surface goes 16 → 17. Update `EXPECTED_TOOLS` in both e2e test files.

---

## Task 1: `_normalize_subject` helper

**Files:**
- Modify: `src/apple_mail_mcp/utils.py` (add function at end)
- Modify: `tests/unit/test_utils.py` (new `TestNormalizeSubject` class; place imports at top of file)

**Step 1 — failing tests**

Append to `tests/unit/test_utils.py`:

```python
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
```

Add the import at the top of the file alongside the existing `from apple_mail_mcp.utils import ...` line: `normalize_subject`.

Run: `uv run pytest tests/unit/test_utils.py::TestNormalizeSubject -v`. Expect 9 failures (ImportError or NameError).

**Step 2 — implement.** Append to `src/apple_mail_mcp/utils.py`:

```python
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
        Normalized subject. Empty input → empty output.
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
```

**Step 3 — verify.** Run the test again. Expect 9 passed.

**Step 4 — commit**

```bash
git add src/apple_mail_mcp/utils.py tests/unit/test_utils.py
git commit -m "Add normalize_subject helper for thread matching (#29)"
```

---

## Task 2: `parse_rfc822_ids` helper

**Files:**
- Modify: `src/apple_mail_mcp/utils.py`
- Modify: `tests/unit/test_utils.py`

**Step 1 — failing tests**

Append to `tests/unit/test_utils.py`:

```python
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
```

Add `parse_rfc822_ids` to the `from apple_mail_mcp.utils import ...` line.

Run: `uv run pytest tests/unit/test_utils.py::TestParseRfc822Ids -v`. Expect failures.

**Step 2 — implement.** Append to `utils.py`:

```python
def parse_rfc822_ids(raw: str) -> list[str]:
    """Parse an In-Reply-To or References header into a list of Message-IDs.

    RFC 5322 canonical form is `<id@domain>` separated by whitespace or
    folded newlines. Some clients emit bare ids without angle brackets —
    we accept both. Returns ids without angle brackets, deduplicated order
    preserved.

    Args:
        raw: Header content (e.g., "<a@x> <b@x>").

    Returns:
        List of cleaned message-id strings. Empty input → empty list.
    """
    tokens = raw.split()
    out: list[str] = []
    for tok in tokens:
        cleaned = tok.strip().lstrip("<").rstrip(">").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out
```

**Step 3 — verify.** Run tests. Expect 7 passed.

**Step 4 — commit**

```bash
git add src/apple_mail_mcp/utils.py tests/unit/test_utils.py
git commit -m "Add parse_rfc822_ids helper for threading headers (#29)"
```

---

## Task 3: `walk_thread_graph` helper

**Files:**
- Modify: `src/apple_mail_mcp/utils.py`
- Modify: `tests/unit/test_utils.py`

**Step 1 — failing tests**

Append to `tests/unit/test_utils.py`:

```python
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
            known_ids={"rfc-a"},  # rfc-a is anchor
            candidates=[c1, c2],
        )
        ids = {c["id"] for c in accepted}
        assert ids == {"a", "b"}
```

Add `walk_thread_graph` to the test-file import line.

Run: `uv run pytest tests/unit/test_utils.py::TestWalkThreadGraph -v`. Expect failures.

**Step 2 — implement.** Append to `utils.py`:

```python
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
        candidates: List of dicts with keys `id`, `rfc_message_id`,
            `in_reply_to`, `references_parsed` (list[str]). Anchor itself
            should NOT appear in this list.
        max_iterations: Cycle-safety cap. Real threads stabilize in 1–2
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
```

**Step 3 — verify.** Run tests. Expect 6 passed.

**Step 4 — commit**

```bash
git add src/apple_mail_mcp/utils.py tests/unit/test_utils.py
git commit -m "Add walk_thread_graph helper for thread reconstruction (#29)"
```

---

## Task 4: Connector `get_thread` — anchor resolution

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py`
- Modify: `tests/unit/test_mail_connector.py`

This task adds the method skeleton + anchor-resolution AppleScript. The candidate-collection call is added in Task 5.

**Step 1 — failing tests**

Append to `tests/unit/test_mail_connector.py`, inside `TestAppleMailConnector`:

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_resolution_script_shape(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor-resolution AppleScript must query for the internal id and
        return the anchor's threading headers quoted."""
        # Return enough for Task 4's anchor-resolution to succeed, but a
        # trivial candidate list so Task 5's call returns [].
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            "[]",
        ]
        connector.get_thread("12345")
        anchor_script = mock_run.call_args_list[0][0][0]
        # All record keys must be |quoted| per the v0.4.1 selector-collision rule.
        assert "|rfc_message_id|:(message id of msg)" in anchor_script
        assert "|subject|:(subject of msg)" in anchor_script
        # Anchor lookup iterates accounts/mailboxes for the internal id.
        assert "whose id is 12345" in anchor_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor lookup failure propagates MailMessageNotFoundError."""
        mock_run.side_effect = MailMessageNotFoundError("Can't get message")
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("99999")
```

Run: `uv run pytest tests/unit/test_mail_connector.py -k get_thread -v`. Expect 2 failures (AttributeError: `connector.get_thread` not defined).

**Step 2 — implement (partial).** Add to `mail_connector.py` below `get_attachments`:

```python
    def get_thread(self, message_id: str) -> list[dict[str, Any]]:
        """Return all messages in the thread containing message_id, chronological.

        Uses Mail.app's indexed `whose subject contains "..."` filter as a
        pre-filter, then reconstructs the thread by walking RFC 5322
        Message-ID / In-Reply-To / References headers across the candidate
        set. Members whose subject was rewritten mid-thread are not found
        (documented limitation).

        Args:
            message_id: Internal Mail.app id of any message in the thread
                (the anchor). Typically obtained from search_messages or
                get_message results.

        Returns:
            List of message dicts sorted by date_received ascending.
            Each dict has the search_messages shape:
            id, subject, sender, date_received, read_status, flagged.
            Thread of 1 is valid (anchor has no threading headers).

        Raises:
            MailMessageNotFoundError: If no message with the given id exists.
        """
        from .utils import (
            normalize_subject,
            parse_applescript_json,
            parse_rfc822_ids,
            walk_thread_graph,
        )

        message_id_safe = escape_applescript_string(sanitize_input(message_id))

        # ---- Call 1: resolve anchor ----
        anchor_body = f'''
        tell application "Mail"
            set anchorResult to missing value
            repeat with acc in accounts
                repeat with mb in mailboxes of acc
                    try
                        set msg to first message of mb whose id is {message_id_safe}
                        set anchorInReplyTo to ""
                        set anchorRefs to ""
                        try
                            repeat with h in headers of msg
                                set hname to name of h
                                if hname is "in-reply-to" then set anchorInReplyTo to (content of h)
                                if hname is "references" then set anchorRefs to (content of h)
                            end repeat
                        end try
                        set resultData to {{|account|:(name of acc), |rfc_message_id|:(message id of msg), |subject|:(subject of msg), |in_reply_to|:anchorInReplyTo, |references_raw|:anchorRefs}}
                        set anchorResult to resultData
                        exit repeat
                    end try
                end repeat
                if anchorResult is not missing value then exit repeat
            end repeat

            if anchorResult is missing value then
                error "Can't get message: not found"
            end if
        end tell
        '''

        anchor_script = _wrap_as_json_script(anchor_body)
        anchor_raw = self._run_applescript(anchor_script)
        anchor = cast(dict[str, Any], parse_applescript_json(anchor_raw))

        # Placeholder: Task 5 adds candidate collection + graph walk.
        # Return single-anchor thread for now so Task 4 tests pass.
        return []
```

Also add `MailMessageNotFoundError` raise is handled by `_run_applescript` itself when the inner `error "Can't get message..."` bubbles up, so no Python re-raise needed. The test `test_get_thread_anchor_not_found_raises` mocks `_run_applescript` to raise directly.

**Step 3 — verify.** Run: `uv run pytest tests/unit/test_mail_connector.py -k get_thread -v`. Expect 2 passed.

**Step 4 — commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Add get_thread connector skeleton with anchor resolution (#29)"
```

---

## Task 5: Connector `get_thread` — candidate collection + graph walk

**Files:**
- Modify: `src/apple_mail_mcp/mail_connector.py` (complete `get_thread`)
- Modify: `tests/unit/test_mail_connector.py`

**Step 1 — failing tests**

Append to `TestAppleMailConnector`:

```python
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_returns_anchor_plus_replies_sorted(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Given an anchor and 2 replies in candidates, returns all 3 sorted by date."""
        mock_run.side_effect = [
            # Call 1: anchor
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Re: Q3","in_reply_to":"","references_raw":""}',
            # Call 2: candidates (includes anchor itself + 2 replies)
            '['
            '{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x","date_received":"Mon Jan 1 2024","read_status":true,"flagged":false},'
            '{"id":"101","rfc_message_id":"<r1@x>","in_reply_to":"<anchor@x>",'
            '"references_raw":"<anchor@x>","subject":"Re: Q3","sender":"b@x","date_received":"Tue Jan 2 2024","read_status":true,"flagged":false},'
            '{"id":"102","rfc_message_id":"<r2@x>","in_reply_to":"<r1@x>",'
            '"references_raw":"<anchor@x> <r1@x>","subject":"Re: Q3","sender":"a@x","date_received":"Wed Jan 3 2024","read_status":false,"flagged":false}'
            ']'
        ]
        result = connector.get_thread("100")
        assert len(result) == 3
        assert [m["id"] for m in result] == ["100", "101", "102"]
        # Result shape matches search_messages (6 fields)
        for m in result:
            assert set(m.keys()) == {"id", "subject", "sender", "date_received", "read_status", "flagged"}

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_drops_threading_internals_from_output(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Response rows must NOT leak rfc_message_id / in_reply_to / references_raw."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>","subject":"Q3","in_reply_to":"","references_raw":""}',
            '[{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector.get_thread("100")
        for m in result:
            assert "rfc_message_id" not in m
            assert "in_reply_to" not in m
            assert "references_raw" not in m

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_orphan_anchor_returns_single_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor with no threading headers → thread = [anchor] only."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<orphan@x>","subject":"Standalone","in_reply_to":"","references_raw":""}',
            '[{"id":"500","rfc_message_id":"<orphan@x>","in_reply_to":"","references_raw":"",'
            '"subject":"Standalone","sender":"a@x","date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector.get_thread("500")
        assert len(result) == 1
        assert result[0]["id"] == "500"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_candidate_script_uses_base_subject_and_account(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Candidate collection must use the normalized subject in the whose
        clause and scope to the anchor's account."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<a@x>","subject":"Re: Re: Q3 Report","in_reply_to":"","references_raw":""}',
            '[]',
        ]
        connector.get_thread("1")
        candidate_script = mock_run.call_args_list[1][0][0]
        assert 'account "Gmail"' in candidate_script
        # Base subject should be 'Q3 Report', prefixes stripped.
        assert 'subject contains "Q3 Report"' in candidate_script
        assert 'subject contains "Re:' not in candidate_script
```

Run. Expect failures (method currently returns `[]`).

**Step 2 — implement.** Replace the placeholder at the end of `get_thread` with:

```python
        # ---- Call 2: collect subject-prefiltered candidates with their
        # threading headers across all mailboxes of the anchor's account ----
        account_name = anchor["account"]
        base_subject = normalize_subject(anchor["subject"])
        account_safe = escape_applescript_string(sanitize_input(account_name))
        subject_safe = escape_applescript_string(sanitize_input(base_subject))

        candidates_body = f'''
        tell application "Mail"
            set acctRef to account "{account_safe}"
            set resultData to {{}}
            repeat with mbRef in mailboxes of acctRef
                try
                    set hits to (messages of mbRef whose subject contains "{subject_safe}")
                    repeat with m in hits
                        set inReplyTo to ""
                        set refs to ""
                        try
                            repeat with h in headers of m
                                set hname to name of h
                                if hname is "in-reply-to" then set inReplyTo to (content of h)
                                if hname is "references" then set refs to (content of h)
                            end repeat
                        end try
                        set candRecord to {{|id|:(id of m as text), |rfc_message_id|:(message id of m), |in_reply_to|:inReplyTo, |references_raw|:refs, |subject|:(subject of m), |sender|:(sender of m), |date_received|:(date received of m as text), |read_status|:(read status of m), |flagged|:(flagged status of m)}}
                        set end of resultData to candRecord
                    end repeat
                on error
                    -- Some mailboxes (e.g. Gmail smart labels) reject whose clauses; skip
                end try
            end repeat
        end tell
        '''

        candidates_script = _wrap_as_json_script(candidates_body)
        candidates_raw = self._run_applescript(candidates_script)
        candidates = cast(
            list[dict[str, Any]],
            parse_applescript_json(candidates_raw),
        )

        # Enrich each candidate with parsed reference list (Python-side).
        for cand in candidates:
            cand["references_parsed"] = parse_rfc822_ids(
                cand.get("references_raw", "")
            )

        # Seed the known-id frontier with the anchor and its own references.
        anchor_rfc = anchor["rfc_message_id"]
        known_ids: set[str] = {anchor_rfc}
        if anchor["in_reply_to"]:
            known_ids.add(anchor["in_reply_to"])
        known_ids.update(parse_rfc822_ids(anchor["references_raw"]))

        # The anchor itself should always be in the thread. It may or may not
        # appear in the candidate list (depends on whether its subject matches
        # the base_subject after normalization — usually yes). Separate it out
        # so the graph walk doesn't duplicate it.
        anchor_candidate: dict[str, Any] | None = None
        non_anchor_candidates: list[dict[str, Any]] = []
        for cand in candidates:
            if cand["rfc_message_id"] == anchor_rfc:
                if anchor_candidate is None:
                    anchor_candidate = cand
                # Duplicate (same anchor surfaced via multiple Gmail labels)
                # falls into neither bucket — skipped.
            else:
                non_anchor_candidates.append(cand)

        accepted = walk_thread_graph(
            known_ids=known_ids,
            candidates=non_anchor_candidates,
        )

        # Assemble the final thread: anchor (if found in candidates) +
        # accepted replies. If the anchor wasn't in the candidate set (its
        # subject didn't match base_subject for some reason), we still
        # include its identity by constructing a minimal row from the
        # anchor-resolution data.
        thread: list[dict[str, Any]] = []
        if anchor_candidate is not None:
            thread.append(anchor_candidate)
        else:
            # Minimal row — we don't have the full 6 fields from the anchor
            # call. Do a final supplementary pull here would cost another
            # AppleScript call; accept the partial row instead, filled with
            # what we know.
            logger.warning(
                "get_thread: anchor (rfc=%s) not in candidate set; "
                "result row will be incomplete",
                anchor_rfc,
            )
            thread.append({
                "id": message_id,
                "subject": anchor["subject"],
                "sender": "",
                "date_received": "",
                "read_status": False,
                "flagged": False,
            })
        thread.extend(accepted)

        # Sort by date_received ascending. AppleScript emits dates as
        # locale-formatted strings; lexicographic sort is adequate for the
        # v1 (matches how messages arrive chronologically within a thread
        # in practice). If date strings are empty (supplementary anchor
        # row), treat as earliest.
        thread.sort(key=lambda m: m.get("date_received") or "")

        # Drop threading internals from output rows.
        for m in thread:
            m.pop("rfc_message_id", None)
            m.pop("in_reply_to", None)
            m.pop("references_raw", None)
            m.pop("references_parsed", None)

        return thread
```

**Step 3 — verify.** Run: `uv run pytest tests/unit/test_mail_connector.py -k get_thread -v`. Expect 6 passed.

Also run the whole unit suite: `uv run pytest tests/unit/ -q`. Expect all passing.

**Step 4 — commit**

```bash
git add src/apple_mail_mcp/mail_connector.py tests/unit/test_mail_connector.py
git commit -m "Complete get_thread connector with graph walk + candidate collection (#29)"
```

---

## Task 6: Server MCP tool + security tier

**Files:**
- Modify: `src/apple_mail_mcp/server.py` (add `@mcp.tool() get_thread` after `get_attachments`)
- Modify: `src/apple_mail_mcp/security.py` (`get_thread` → `cheap_reads`)
- Modify: `tests/unit/test_server.py` (new `TestGetThread` class, import `get_thread`)
- Modify: `tests/unit/test_security.py` (tier-assignment test)

**Step 1 — failing tests**

In `tests/unit/test_server.py`, extend the `from apple_mail_mcp.server import ...` block to include `get_thread`. Then add after `TestGetAttachments`:

```python
class TestGetThread:
    def test_success_returns_thread_and_logs(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.return_value = [
            {"id": "1", "subject": "Q3", "sender": "a@b", "date_received": "Mon", "read_status": True, "flagged": False},
            {"id": "2", "subject": "Re: Q3", "sender": "c@d", "date_received": "Tue", "read_status": False, "flagged": False},
        ]
        result = get_thread("1")
        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["thread"]) == 2
        mock_mail.get_thread.assert_called_once_with("1")
        mock_logger.log_operation.assert_called_once_with(
            "get_thread", {"message_id": "1"}, "success"
        )

    def test_not_found_maps_to_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.side_effect = MailMessageNotFoundError("x")
        result = get_thread("nope")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        mock_logger.log_operation.assert_not_called()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_thread.side_effect = RuntimeError("boom")
        result = get_thread("1")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
```

In `tests/unit/test_security.py::test_all_operations_have_tier_assigned`, add `"get_thread"` to `expected_ops`.

Run: `uv run pytest tests/unit/test_server.py::TestGetThread tests/unit/test_security.py::TestCheckRateLimit::test_all_operations_have_tier_assigned -v`. Expect ImportErrors + a security failure.

**Step 2 — implement.** In `src/apple_mail_mcp/server.py`, add after the `get_attachments` tool:

```python
@mcp.tool()
def get_thread(message_id: str) -> dict[str, Any]:
    """
    Return all messages in the thread containing the given message.

    Looks up the message by its internal id, then reconstructs the
    conversation by reading RFC 5322 threading headers (Message-ID,
    In-Reply-To, References) across messages in the same account.
    Results are sorted by date_received ascending.

    Known limitation: thread members whose subject was rewritten
    mid-conversation are missed (subject prefilter tradeoff).

    Args:
        message_id: Internal id of any message in the thread
            (from search_messages or get_message results).

    Returns:
        Dictionary with the thread list.

    Example:
        >>> get_thread("12345")
        {"success": True, "thread": [{...}, {...}], "count": 2}
    """
    try:
        rate_err = check_rate_limit("get_thread", {"message_id": message_id})
        if rate_err:
            return rate_err

        logger.info(f"Getting thread for message: {message_id}")

        thread = mail.get_thread(message_id)

        operation_logger.log_operation(
            "get_thread", {"message_id": message_id}, "success"
        )

        return {
            "success": True,
            "thread": thread,
            "count": len(thread),
        }

    except MailMessageNotFoundError as e:
        logger.error(f"Message not found: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "not_found",
        }
    except Exception as e:
        logger.error(f"Error getting thread: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unknown",
        }
```

In `src/apple_mail_mcp/security.py`, add `"get_thread": "cheap_reads"` to `OPERATION_TIERS`.

**Step 3 — verify.** Re-run tests. Expect all passing.

**Step 4 — commit**

```bash
git add src/apple_mail_mcp/server.py src/apple_mail_mcp/security.py tests/unit/test_server.py tests/unit/test_security.py
git commit -m "Expose get_thread as MCP tool, rate-limited cheap_reads (#29)"
```

---

## Task 7: E2E — EXPECTED_TOOLS bump + invocation case

**Files:**
- Modify: `tests/e2e/test_mcp_tools.py`
- Modify: `tests/e2e/test_stdio_transport.py`

**Step 1 — update both EXPECTED_TOOLS sets.** Add `"get_thread",` to each `EXPECTED_TOOLS` constant.

**Step 2 — add an invocation case.** In `tests/e2e/test_mcp_tools.py`, append to `INVOCATION_CASES`:

```python
    (
        "get_thread",
        {"message_id": "msg-1"},
        "get_thread",
        [{"id": "msg-1", "subject": "Q3", "sender": "a@b",
          "date_received": "Mon", "read_status": True, "flagged": False}],
    ),
```

**Step 3 — verify.** `uv run pytest tests/e2e/ -v`. Expect 23 passed (22 existing + 1 new invocation case).

**Step 4 — commit**

```bash
git add tests/e2e/test_mcp_tools.py tests/e2e/test_stdio_transport.py
git commit -m "Wire get_thread into E2E EXPECTED_TOOLS (16 -> 17) (#29)"
```

---

## Task 8: Integration test

**Files:**
- Modify: `tests/integration/test_mail_integration.py`

**Step 1 — write the test.** Add to `TestMailIntegration`:

```python
    def test_get_thread_orphan_anchor(
        self, connector: AppleMailConnector, test_account: str
    ) -> None:
        """For any message (probably has no replies in a test inbox),
        get_thread should at minimum return the anchor itself.

        This exercises the anchor-resolution + candidate-collection path
        end-to-end without requiring a specific known-threaded message.
        """
        matches = connector.search_messages(
            account=test_account, mailbox="INBOX", limit=1
        )
        if not matches:
            pytest.skip("test inbox has no messages")

        thread = connector.get_thread(matches[0]["id"])
        assert isinstance(thread, list)
        assert len(thread) >= 1
        for m in thread:
            assert set(m.keys()) >= {
                "id", "subject", "sender", "date_received",
                "read_status", "flagged",
            }
        # The anchor must be in the result.
        assert any(m["id"] == matches[0]["id"] for m in thread)

    def test_get_thread_rejects_nonexistent_anchor(
        self, connector: AppleMailConnector
    ) -> None:
        """Nonexistent anchor raises MailMessageNotFoundError."""
        from apple_mail_mcp.exceptions import MailMessageNotFoundError
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("99999999999")
```

**Step 2 — verify.** Run:

```bash
MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=Gmail uv run pytest \
  tests/integration/test_mail_integration.py::TestMailIntegration::test_get_thread_orphan_anchor \
  tests/integration/test_mail_integration.py::TestMailIntegration::test_get_thread_rejects_nonexistent_anchor \
  --run-integration -v
```

Expect both passed.

**Step 3 — commit**

```bash
git add tests/integration/test_mail_integration.py
git commit -m "Add integration tests for get_thread (#29)"
```

---

## Task 9: Documentation

**Files:**
- Modify: `.claude/CLAUDE.md` (API surface line: `16 MCP tools` → `17`; Phase 4 bullet expands to include `get_thread`)
- Modify: `.claude/skills/api-design/SKILL.md` (tool count 16 → 17)
- Modify: `.claude/skills/applescript-mail/SKILL.md` (**correct the stale "No thread/conversation access" bullet** — messages are standalone in the API, but threads are reconstructable via `headers of msg` → `in-reply-to` / `references`; subject prefilter required for feasibility because `whose message id is` is not indexed)
- Modify: `docs/guides/TESTING.md` (15 or 16 → 17 tool count references)
- Modify: `docs/reference/TOOLS.md` (add `get_thread` entry under Phase 4)

**Step 1 — doc edits.** Straightforward find-and-replace + new section writing. The TOOLS.md entry follows the same shape as the `list_rules` entry — parameters (just `message_id`), returns example JSON, field notes, **limitations** subsection listing the subject-rewrite caveat and single-account scope.

Specifically for `.claude/skills/applescript-mail/SKILL.md`, find the line:

> **No thread/conversation access** — Messages are individual objects; no thread grouping in AppleScript API

Replace with:

> **Thread reconstruction is possible but not native** — Mail.app has no `thread` or `conversation` class. Reconstruct threads by reading `headers of msg` for `in-reply-to`, `references`, and comparing against `message id of msg` (the RFC 822 header value) across candidate messages. **`whose message id is "X"` is NOT indexed** (~21 s per lookup); always subject-prefilter first. See `get_thread` in `mail_connector.py`.

**Step 2 — verify.** `make check-all` green. Open TOOLS.md, visually confirm the entry is well-formed.

**Step 3 — commit**

```bash
git add .claude/CLAUDE.md .claude/skills/api-design/SKILL.md .claude/skills/applescript-mail/SKILL.md docs/guides/TESTING.md docs/reference/TOOLS.md
git commit -m "Document get_thread; correct stale applescript-mail skill claim (#29)"
```

---

## Task 10: Evals

**Files:**
- Modify: `evals/agent_tool_usability/run_eval.py` (add `"get_thread"` to `TOOL_NAMES`)
- Modify: `evals/agent_tool_usability/tool_descriptions.md` (new entry alphabetically — between `get_message` and `list_accounts`)
- Modify: `evals/agent_tool_usability/scenarios.py` (add one scenario)

**Step 1 — scenario.** Insert a Read-category scenario:

```python
    {
        "id": 42,
        "category": "Read",
        "name": "Show full thread containing a message",
        "prompt": "Show me the full conversation for message with id msg-42.",
        "expected": {
            "tools": ["get_thread"],
            "key_params": {"get_thread": {"message_id": "msg-42"}},
        },
        "scoring_notes": (
            "PASS: Calls get_thread with message_id=msg-42. "
            "PARTIAL: Calls get_thread but param named wrong. "
            "FAIL: Calls get_message and tries to guess the thread manually."
        ),
        "safety_critical": False,
    },
```

**Step 2 — tool_descriptions.md entry** (placed between `get_message` and `list_accounts`):

```markdown
### get_thread

Return all messages in the thread containing the given message. Uses the internal message id as the anchor; walks RFC 5322 threading headers across the anchor's account to find related messages. Sorted by date_received ascending. Known limitation: members with mid-thread subject rewrites are not found.

**Parameters:**
- `message_id` (str, required): Internal id of any message in the thread.
```

**Step 3 — verify + commit**

```bash
git add evals/agent_tool_usability/
git commit -m "Add get_thread to evals (tool descriptions + scenario 42) (#29)"
```

---

## Task 11: Full green gate + PR

**Step 1 — full suite**

```bash
make check-all
make test-e2e
MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=Gmail make test-integration
```

Expect all green.

**Step 2 — push + PR**

```bash
git push -u origin feature/issue-29-get-thread
gh pr create --title "Add get_thread MCP tool for conversation reconstruction (#29)" --body "..."
```

PR body should:
- Summarize: Phase 4 tool, surface 16 → 17, algorithm = subject-prefilter + RFC 822 graph walk, two AppleScript calls.
- Call out the **key empirical finding** that justified the subject-prefilter: `whose message id is X` = 21 s vs `whose subject contains X` = sub-second.
- Call out the known limitation (subject-rewrite misses).
- Link the design doc.
- Link follow-up **#66** for the eventual IMAP path (removes the limitation).
- Include test plan checklist.
- Use `Closes #29`.

**Step 3 — wait for CI, then use `/merge-and-status`.**

---

## Verification (end-to-end)

- `make test` — 280+ unit tests pass (adds ~22 new across utils/connector/server/security).
- `make test-e2e` — 23 pass (22 existing + 1 new invocation case).
- `make check-all` — green.
- `MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=Gmail make test-integration` — new `test_get_thread_*` pass.
- Manually: run `get_thread` via the MCP client on a known-threaded message; confirm thread length matches expected.
- Parity check shows `get_thread` wrapped; no warnings.

## Out of scope (explicit — deferred)

- **IMAP-based threading** — follow-up #66. Blocks on #41.
- **Cross-account threading** — not implemented; single-account scope only.
- **Thread metadata** — participant list, dates range, size. Separate follow-up if requested.
- **Tree shape return** — flat chronological list only.
- **Preview field** — not included; callers chain `get_message` for bodies.
