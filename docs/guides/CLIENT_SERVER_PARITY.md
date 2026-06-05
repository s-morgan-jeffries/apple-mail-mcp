# Client/Server Parity

This project enforces that every **public** method on `AppleMailConnector` (in [`mail_connector.py`](../../src/apple_mail_mcp/mail_connector.py)) is either exposed as an MCP tool in [`server.py`](../../src/apple_mail_mcp/server.py) or explicitly marked intentionally-internal. The gate is [`scripts/check_client_server_parity.sh`](../../scripts/check_client_server_parity.sh), run as part of `make check-all` and in CI.

## What it enforces

The connector is the capability layer; the server is the MCP surface. When a new public connector method lands, it almost always should become a tool — otherwise the capability exists but no client can reach it. The gate catches the case where someone adds a public method and forgets to expose it.

The script:

1. Extracts public connector methods (`def <name>`, excluding `_private` and `__init__`).
2. Extracts server tools (functions decorated with `@_tool(...)` or `@mcp.tool(...)`).
3. Computes the methods present on the connector but **not** exposed as tools.
4. Checks each against the **allowlist** of intentionally-internal methods.

**The gate fails (`exit 1`) when:**

- A public connector method is **neither** a tool **nor** in the allowlist (real parity drift), or
- An allowlist entry is **stale** — the method is now exposed as a tool, or no longer exists (renamed/removed).

Otherwise it prints `Parity OK: N intentionally-internal method(s), all allowlisted.` and exits 0.

## The allowlist

[`scripts/check_client_server_parity.sh`](../../scripts/check_client_server_parity.sh) carries an `ALLOWLIST` dict mapping each intentionally-internal method name to a one-line reason. It's a **shrinking ratchet**: when you expose a previously-internal method as a tool, remove its entry (the stale check enforces this).

These methods are public on the connector but deliberately not tools — they're subsumed by a CRUD-style tool (per the [api-design](../../.claude/skills/api-design/SKILL.md) "no per-field tools" rule) or used only as internal helpers:

| Method | Why it's internal |
|---|---|
| `mark_as_read` | Subsumed by `update_message(read_status=…)`. |
| `move_messages` | Subsumed by `update_message(destination_mailbox=…)`. |
| `flag_message` | Subsumed by `update_message(flagged=/flag_color=…)`. |
| `set_rule_enabled` | Subsumed by `update_rule(enabled=…)`. |
| `get_message` | Singular fetch; the tool is `get_messages` (plural). |
| `get_attachments` | Attachment metadata; surfaced via `get_messages(include_attachments=True)`. |
| `get_selected_messages` | Mail.app UI selection; surfaced via the read tools (#92), not standalone. |
| `get_draft_state` | Reads a draft's current fields for `update_draft`'s merge. |
| `find_message_by_message_id` | Internal RFC-id→Mail-id lookup (e.g. `update_draft` seed recovery). |
| `extract_draft_attachments` | Helper for `update_draft`'s delete-and-recreate attachment preservation. |
| `auto_template_vars` | Auto-fills template variables; used by the template/draft path, not standalone. |

## Resolving a failure

**"public connector method not exposed as a tool and not allowlisted":** you added a public method. Either —

- **Expose it** — add an `@_tool({...})` function in `server.py` that calls it (the common case; also update `docs/reference/TOOLS.md`, the README tool count, and regenerate eval descriptions — the doc-drift gate enforces these), or
- **Keep it internal** — add an entry to `ALLOWLIST` with a one-line reason and a row to the table above, in the same PR.

If you can't write a one-sentence reason for keeping it internal, it probably should be a tool.

**"stale ALLOWLIST entry":** you exposed or removed a method that's still allowlisted. Delete its `ALLOWLIST` entry (and table row).

## Checking locally

```bash
./scripts/check_client_server_parity.sh
```

This mirrors the per-function allowlist mechanism documented in [COMPLEXITY.md](COMPLEXITY.md) — same "enforce, don't just warn; allowlist with reasons; ratchet down" philosophy (#277).
