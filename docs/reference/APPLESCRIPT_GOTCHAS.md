# Apple Mail AppleScript Gotchas

Known issues, limitations, and workarounds for Apple Mail automation via AppleScript. The
`applescript-mail` skill (`.claude/skills/applescript-mail/`) is the deeper reference for
contributors writing connector AppleScript.

## JSON output via ASObjC (not pipe-delimited)

Scripts emit JSON via ASObjC + `NSJSONSerialization`, not fragile `field1|field2` text. Wrap a
tell-block body with `_wrap_as_json_script(body, timeout=…)` and parse the result with
`parse_applescript_json()` (both in `mail_connector.py` / `utils.py`). The body must assign its
result to a variable named `resultData` (a record/list/scalar) inside the `tell` block.

```applescript
-- body passed to _wrap_as_json_script:
tell application "Mail"
    set resultData to {|id|:(id of msg as text), |subject|:(subject of msg)}
end tell
-- the wrapper appends the NSJSONSerialization epilogue and returns a JSON string
```

### Quote the `|name|:` record key — always

Use `|name|:(name of acc)`, **never** bare `name:`. The bare form collides with NSObject's `name`
selector and is **silently dropped** during the NSDictionary→JSON conversion, leaving records missing
their name field. This applies to any key that shadows a Cocoa selector; quoting with `|…|` is the
safe default for all record keys.

### Coerce `missing value` before serializing

`NSJSONSerialization` rejects AppleScript's `missing value`. Coerce optional properties to safe
defaults *before* building the record, or the whole script errors:

```applescript
set accEmails to email addresses of acc
if accEmails is missing value then set accEmails to {}
```

## `whose id is` vs `whose message id is`

Two different identifiers, two very different costs:

- `whose id is "<n>"` — Mail.app's **internal numeric id** (the AppleScript-path `id`). Indexed; fast.
- `whose message id is "<rfc>"` — the **RFC 5322 `Message-ID`** header. **Not indexed** (~20s per
  lookup on a real mailbox), so always subject-prefilter first before matching on it.

This is why the dual-emit ID model (#148) matters — read tools return both `id` (path-native) and
`rfc_message_id`, and lookups pick the cheap path. See
[ARCHITECTURE.md](ARCHITECTURE.md#dual-emit-message-id-model-148).

## String escaping is critical

Always run user text through `escape_applescript_string()` (after `sanitize_input()`). Unescaped
quotes/backslashes fail silently with a generic "Can't make" error and no indication of the cause.

## Gmail label-based system

Gmail doesn't support standard IMAP move. `update_message` / `move_messages` use `gmail_mode=true`
(copy + delete) for Gmail accounts.

## Attachment paths

Use POSIX file references — `POSIX file "/path/to/file"`. Convert Python `Path` objects via
`.as_posix()`. Never concatenate an attacker-influenced attachment `name` into a path inside
AppleScript (path-traversal → arbitrary write); sanitize and compute the target path on the Python
side first.

## Error parsing

AppleScript errors surface on stderr; the connector maps them to typed exceptions (note macOS uses
curly apostrophes — they're normalized before matching):
- `Can't get account` → `MailAccountNotFoundError`
- `Can't get mailbox` → `MailMailboxNotFoundError`
- `Can't get message` → `MailMessageNotFoundError`
- `Can't get rule` → `MailRuleNotFoundError`

## Timeout

Default 60 s. Mail's own AppleEvent timeout (also 60 s) is overridden inside the generated scripts via
`with timeout of N seconds`, so server-bound operations on slow accounts don't trip `AppleEvent timed
out (-1712)` before the connector's subprocess timer. Configurable via `AppleMailConnector(timeout=N)`.

## Known Mail.app limitations

| Feature | Status |
|---------|--------|
| Scheduled / delayed sending | Not available via AppleScript |
| Conversation grouping | No native `thread` class — reconstructed via headers (IMAP) or subject+references (AppleScript) |
| Smart mailbox access | Not exposed to AppleScript |
| HTML body reading | `content of message` returns plain text only |
| Read receipts | Cannot request or detect |
| Permanent delete | No primitive bypasses Trash (#111) |

(Rules and drafts *are* manageable — see the `*_rule` and `*_draft` tools — though rules have no
stable id and are addressed positionally.)
