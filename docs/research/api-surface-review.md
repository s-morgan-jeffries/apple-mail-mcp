# API surface review

**Status:** Decision doc / output of issue #129.
**Date:** 2026-05-03.
**Outcome:** Recommend consolidating the 27-tool surface to 20 tools across six follow-up implementation issues. Final reduction: ~26%.

## Context

v0.6.0 ships **27 MCP tools**. Each tool occupies a slot in the prompt context Claude sees on every request, and each adds a decision the model has to make when it picks one. As the surface grew through the v0.5.0–v0.6.0 cycle, several pairs (and one quartet) emerged where the verbs are the same and the parameter shapes mostly overlap. This audit walks through those candidates, decides each, and files implementation follow-ups for the merges.

Worth being explicit: this isn't optimization for line count. The point is *cognitive load on the LLM* — fewer tools, more obvious verb→tool mapping, less validation logic the model has to carry between request and response.

## The 27 starting tools

Source-order, grouped by responsibility:

```
ACCOUNTS (1)              MAILBOXES (2)            RULES (5)
  list_accounts             list_mailboxes           list_rules
                            create_mailbox           set_rule_enabled
                                                     create_rule
                                                     update_rule          [async]
                                                     delete_rule          [async]

MESSAGES — READ (5)       MESSAGES — SEND (4)      MESSAGES — OPERATE (5)
  search_messages           send_email      [async]   mark_as_read
  get_message               send_email_with_         move_messages
  get_selected_messages       attachments   [async]   flag_message
  get_thread                reply_to_message          delete_messages
  get_attachments           forward_message [async]   save_attachments

TEMPLATES (5)
  list_templates
  get_template
  save_template
  delete_template          [async]
  render_template
```

The six `async def` tools elicit user confirmation for destructive or visible-to-others operations.

## Per-tool inventory

Mechanical extraction from `src/apple_mail_mcp/server.py` and `tests/unit/`. Line counts include the tool's full decorator-to-final-return body. Test counts are unit tests that reference the tool by name.

| Tool | Line | Body | Tests |
|------|-----:|-----:|------:|
| `list_accounts` | 125 | 44 | 10 |
| `list_rules` | 170 | 58 | 19 |
| `set_rule_enabled` | 229 | 64 | 10 |
| `delete_rule` | 294 | 78 | 14 |
| `create_rule` | 373 | 82 | 22 |
| `update_rule` | 456 | 110 | 16 |
| `list_mailboxes` | 567 | 57 | 21 |
| `search_messages` | 625 | 115 | 90 |
| `get_message` | 741 | 70 | 40 |
| `get_selected_messages` | 812 | 41 | 8 |
| `send_email` | 854 | 105 | 39 |
| `mark_as_read` | 960 | 73 | 21 |
| `send_email_with_attachments` | 1034 | 135 | 21 |
| `get_attachments` | 1170 | 81 | 46 |
| `get_thread` | 1252 | 58 | 22 |
| `save_attachments` | 1311 | 96 | 13 |
| `move_messages` | 1408 | 92 | 23 |
| `flag_message` | 1501 | 79 | 18 |
| `create_mailbox` | 1581 | 87 | 16 |
| `delete_messages` | 1669 | 96 | 22 |
| `reply_to_message` | 1766 | 64 | 16 |
| `forward_message` | 1831 | 130 | 29 |
| `list_templates` | 1962 | 29 | 3 |
| `get_template` | 1992 | 30 | 7 |
| `save_template` | 2023 | 44 | 14 |
| `delete_template` | 2068 | 44 | 5 |
| `render_template` | 2113 | 125 | 7 |

`search_messages` (90 tests) and `get_attachments` (46 tests) are the most heavily exercised; `list_templates` (3) and `delete_template` (5) the least.

## Consolidation conclusions

### 1. `update_rule` absorbs `set_rule_enabled`

`update_rule(rule_index, enabled=None, ...)` already has the `enabled: bool | None` parameter. The split exists for one real reason: `update_rule` is async and elicits user confirmation (destructive condition/action replacement is irrecoverable), while `set_rule_enabled` is sync because toggling enable/disable is trivially reversible.

**Resolution: merge, with conditional elicitation.** `update_rule` skips the confirmation prompt when the patch only modifies `enabled` and/or `name` (both reversible without losing data). Triggers elicitation when conditions / actions / match_logic are touched. Net: -1 tool.

Trade-off: param surface stays the same, but the LLM now has one entry point for "modify a rule" instead of two. Internal logic gains a one-line "any destructive field set?" check.

### 2. `search_messages` absorbs `get_selected_messages`

`get_selected_messages` is conceptually "search by Mail.app's UI selection state." Add `source: Literal["all", "selected"] = "all"` to `search_messages`. When `source="selected"`, other filter params don't apply (a user who's clicked specific messages doesn't want them filtered). Net: -1 tool.

Mail.app's selection is inherently *multi*-message (shift-click), which is why `get_selected_messages` returned a list. `search_messages` already returns a list, so the merge target's natural — folding into singular `get_message` would force a list return there too.

### 3. `search_messages` absorbs `get_thread`

Same merge target — threads are "messages matching this thread anchor." Add `thread_of: str | None = None` to `search_messages`. When set, the function does the same anchor-resolution + thread-walk it does in `get_thread` today, but returns a sorted list (the existing get_thread shape). Tier 1 / Tier 3 IMAP dispatch (#122) preserved. Net: -1 tool.

Why not fold `get_thread` into `get_message`? Return-shape divergence: `get_message` returns one dict, threads are lists. Conditional return shapes are awkward for typed callers and confusing for LLMs. `search_messages` was always going to be list-shaped, so the impedance mismatch goes away.

### 4. `get_message` absorbs `get_attachments`

Add `include_attachments: bool = False` to `get_message`. Returns the existing dict plus an `attachments: [...]` array when set. `save_attachments` stays separate — it does I/O (writes bytes to disk), distinct verb. Net: -1 tool.

### 5. The send/draft consolidation

The current four-tool send group (`send_email`, `send_email_with_attachments`, `reply_to_message`, `forward_message`) collapses into a draft-lifecycle trio:

```python
create_draft(
    # mutually-exclusive seed (None = new):
    reply_to: str | None = None,            # message ID — recipients/subject derived
    forward_of: str | None = None,          # message ID — content/attachments derived
    # required for new + forward (auto-derived for reply):
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    # always meaningful:
    body: str = "",
    attachment_paths: list[str] | None = None,
    # reply-specific:
    reply_all: bool = False,
    # template support (folds the render-then-send flow into one call):
    template_name: str | None = None,
    template_vars: dict[str, str] | None = None,
    # action:
    send_now: bool = False,
) -> {success, draft_id, sent_message_id?}

update_draft(draft_id, ...same fields..., send_now=False)
delete_draft(draft_id)
```

The mental model matches Mail.app's actual primitive — every outgoing message is a draft until you `send` it. Reply and forward are draft creation paths seeded with the original. `send_now=False` saves the draft for later editing; `send_now=True` saves and immediately sends. Net: 4 tools → 3, with two of those (`update_draft`, `delete_draft`) being new capabilities the surface didn't have at all.

**Why `delete_draft` is separate from `delete_messages`:**
- Lifecycle symmetry with `create_draft` and `update_draft`.
- Type-signature footgun avoidance — the LLM can't accidentally trash a received message by passing what it thought was a draft id.
- Future expansion room (scheduled-send, undo-send, edit-after-send) is draft-shaped and benefits from a distinct resource.

**Template integration:** `create_draft(template_name=..., template_vars=...)` folds the common `render_template` → `reply_to_message` two-step into one call. `render_template` stays standalone for the "preview without creating a draft" case (rare but valid). Net: 0 tool reduction here, but a usability win — most template-driven sends become one tool call.

**AppleScript fidelity caveat:** Mail.app's `make new outgoing message` creates a draft, but threading headers (`In-Reply-To` / `References`) are correctly set by Mail's `reply` and `forward` commands, not by manually populating headers on a fresh draft. Implementation will branch on `reply_to` / `forward_of` to pick the right primitive — visible only as different code paths inside `create_draft`. Verify `send_now=False` works on all three branches (creates a draft *with* threading headers without sending).

### 6. `update_message` absorbs `mark_as_read`, `move_messages`, `flag_message`

```python
update_message(
    message_ids: list[str],
    read_status: bool | None = None,
    flagged: bool | None = None,
    flag_index: int | None = None,
    destination_mailbox: str | None = None,
    account: str | None = None,
    source_mailbox: str | None = None,
)
```

Caller sets whichever fields they want changed; tool applies in one pass. The `account` + `source_mailbox` IMAP / AppleScript narrow-path machinery composes cleanly. Net: -2 tools.

**`delete_messages` stays separate** — different verb (removal, not mutation). Folding it in (e.g. `update_message(destination_mailbox="<Trash>")`) would obscure intent for marginal tool-count savings.

#### Trash-restore semantics

Worth documenting the move-to-Trash + restore behavior since it's a non-obvious consequence of how `delete_messages` and `update_message` interact:

- **Received message → restore from Trash**: works cleanly. After `delete_messages([id])`, the message is in `Deleted Messages`. To restore: `update_message([new_id], destination_mailbox="INBOX", source_mailbox="Deleted Messages", account="iCloud")`. The IMAP UID changes on move (you'll need to find the trashed ID via `search_messages(mailbox="Deleted Messages")` first). No special "restore" verb — restore is just an inverse move.

- **Draft → restore from Trash**: deliberately *not* in scope. After `delete_draft(draft_id)`, the draft moves to Trash. Technically it can be moved back via `update_message`, but Mail.app no longer treats a draft-shaped message in Trash as an editable draft (behavior varies across Mail.app versions and IMAP/POP). The lifecycle we ship is `create_draft → update_draft → (send_now | delete_draft)`. Restoring a discarded draft is an edge case; users who need the contents can `search_messages(mailbox="Deleted Messages")` and copy by hand.

So the asymmetry is intentional: received messages are recoverable; drafts are discardable. Both contents are recoverable; only message-level semantics survive.

### 7. Kept as-is (no merger)

- **4 list_* discovery tools** (`list_accounts`, `list_mailboxes`, `list_rules`, `list_templates`): param shapes and return shapes too different for a polymorphic `list(kind=...)` to be cleaner.
- **Template CRUD** (`get_template`, `save_template`, `delete_template`, `render_template`): four tools serving a coherent resource; render is genuinely different (transformation, not state mutation). LLMs do well with explicit verbs.
- **Rule CRUD** (`create_rule`, `update_rule`, `delete_rule`, after `set_rule_enabled` is folded): same reasoning as templates.
- **`save_attachments`**: bytes-to-disk I/O, distinct from metadata reads.
- **`delete_messages`**: see Step 6.
- **`render_template`**: kept standalone *and* surfaced via `create_draft`'s `template_name` shortcut — supports both "preview" and "compose" workflows.

## Final shape: 20 tools

| Bucket | Tools | Count |
|--------|-------|------:|
| Discovery | `list_accounts`, `list_mailboxes`, `list_rules`, `list_templates` | 4 |
| Mailbox | `create_mailbox` | 1 |
| Rule CRUD | `create_rule`, `update_rule`, `delete_rule` | 3 |
| Template CRUD | `get_template`, `save_template`, `delete_template`, `render_template` | 4 |
| Search/read | `search_messages`, `get_message` | 2 |
| Drafts | `create_draft`, `update_draft`, `delete_draft` | 3 |
| Operate | `update_message`, `delete_messages`, `save_attachments` | 3 |
| **Total** | | **20** |

**27 → 20**, a 26% reduction. Two of the new tools (`update_draft`, `delete_draft`) are new capabilities, not just renames — the surface gains real expressiveness while shrinking.

## Backwards compatibility

The README banner declares pre-1.0 status — "expect breaking changes" with a recommendation to pin to a specific version. That's the warrant for the cutover. Each merger PR:

- Lands its rename / removal in one PR with the new tool registered and the old tools unregistered.
- Adds a CHANGELOG entry under **Changed** with a one-paragraph migration note.
- The cumulative migration becomes part of v0.7.0's release notes.

**No deprecation period inside v0.7.0.** The surface change is a single coordinated cutover at the release boundary. Users pin a version per the banner; v0.6.0 stays available for those who can't migrate yet.

## Pending: #102 re-evaluation

#102 currently proposes adding `update_mailbox` and `delete_mailbox` tools to complete the mailbox CRUD set (alongside the existing `list_mailboxes` and `create_mailbox`). Audit recommendation: **proceed with #102 as separate verbs** (matches rules and templates), **not** as a `manage_mailbox(verb, ...)` polymorphic tool. The four-CRUD-verb pattern is consistent across the surface; consolidating just mailboxes would be the odd one out.

That brings the count to 22 if #102 lands, still significantly below 27.

## Follow-up issues

One PR per merger so each lands with focused tests + CHANGELOG. Filed:

- **#130** (A): `update_rule` absorbs `set_rule_enabled`; conditional elicitation for `enabled`/`name`-only patches.
- **#131** (B): `search_messages` gains `source="selected"`; remove `get_selected_messages`.
- **#132** (C): `search_messages` gains `thread_of`; remove `get_thread`.
- **#133** (D): `get_message` gains `include_attachments`; remove `get_attachments`.
- **#134** (E): New `create_draft` + `update_draft` + `delete_draft`; remove `send_email`, `send_email_with_attachments`, `reply_to_message`, `forward_message`. Includes `template_name` + `template_vars`. Verifies AppleScript `reply` / `forward` primitives produce correct threading headers when `send_now=False`.
- **#135** (F): New `update_message`; remove `mark_as_read`, `move_messages`, `flag_message`.

Plus existing **#102** (mailbox CRUD) carries forward with the audit's recommendation to keep it as separate verbs (matches the rules and templates pattern).

## Provenance

- Inventory extracted 2026-05-03 from `src/apple_mail_mcp/server.py` (line 125-2168) and `tests/unit/` directory.
- Audit walkthrough: interactive Q&A session 2026-05-03 covering all 27 tools, rule-by-rule consolidation reasoning recorded above.
- Open question deferred: the per-tool inventory's "tests" column counts unit tests by name match; some tests may exercise multiple tools and get counted twice. Numbers are directionally correct but not exact.
