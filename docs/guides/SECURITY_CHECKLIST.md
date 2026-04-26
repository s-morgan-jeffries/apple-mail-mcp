# Security Checklist

Every new feature should be reviewed against the five concerns below before opening a PR. Each section names the canonical implementation in the codebase — reuse it rather than rolling your own. Linked file:line numbers are stable references; if a function moves, update this doc.

## Input sanitization

Any string that originates from an MCP tool argument, an environment variable, or any other external source must pass through [`sanitize_input`](../../src/apple_mail_mcp/utils.py#L156) before further processing. It strips null bytes, truncates oversized strings (currently 10000 chars), and coerces non-strings to strings.

This protects against null-byte injection in shell or AppleScript contexts and bounds memory use for pathological inputs. It does **not** make a string safe for interpolation — see *AppleScript escaping* and *Path-traversal-safe name validation* below.

## AppleScript escaping

Any sanitized string that gets interpolated into AppleScript source must additionally pass through [`escape_applescript_string`](../../src/apple_mail_mcp/utils.py#L44), which escapes backslashes and quotes. The combined idiom is:

```python
safe = escape_applescript_string(sanitize_input(user_value))
```

A common mistake is escaping but forgetting the literal AppleScript string quotes around the interpolation site — `whose id is {safe}` parses dashes as subtraction operators on UUID-style ids and crashes. Always wrap in quotes: `whose id is "{safe}"`. See [#86](https://github.com/s-morgan-jeffries/apple-mail-mcp/issues/86) for the bug this prevented.

For lists of values (e.g. message-id lists), each element must be individually sanitized + escaped + quoted, then joined:

```python
id_list = ", ".join(
    f'"{escape_applescript_string(sanitize_input(mid))}"'
    for mid in message_ids
)
```

## Path-traversal-safe name validation

Any user-supplied string used as a filename stem must pass a strict regex *before* being handed to `Path()`. The canonical example is [`_validate_name`](../../src/apple_mail_mcp/templates.py#L180) in the templates module:

```python
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
```

This rejects `..`, slashes, dots, spaces, control characters, and oversized lengths — anything that could let a name escape its intended directory. Validate before any filesystem work; never `Path(user_input)` first and check existence after.

## Rate limiting

Every MCP tool wrapper in [`server.py`](../../src/apple_mail_mcp/server.py) must call [`check_rate_limit`](../../src/apple_mail_mcp/security.py#L180) as its first action and return immediately if the call is rate-limited. The tool name must be registered in [`OPERATION_TIERS`](../../src/apple_mail_mcp/security.py#L122) under one of three tiers:

| Tier | Cap | Use for |
|------|-----|---------|
| `cheap_reads` | 60 / 60s | List/get/render operations; local file I/O |
| `expensive_ops` | 20 / 60s | Search, mutations on Mail.app state, multi-mailbox scans |
| `sends` | 3 / 60s | Anything that delivers email externally |

There's a unit test in [`test_security.py`](../../tests/unit/test_security.py) (`test_all_operations_have_tier_assigned`) that fails if a registered MCP tool isn't in `OPERATION_TIERS` — adding a new tool without picking a tier breaks the build, by design.

## Audit logging

Every server-side tool wrapper must call [`operation_logger.log_operation`](../../src/apple_mail_mcp/security.py#L25) on its success path with the operation name, the params it received, and a status string. Failure paths log via the per-`error_type` return shape; the audit log captures the successful actions.

This produces a per-process record of what the server actually did — useful for debugging, for confirming that destructive operations were preceded by elicitation, and for users who want to inspect what an LLM caused to happen on their behalf.

## When in doubt

Search for an existing tool wrapper that does something analogous to what you're building (e.g. another mutation tool, another file-I/O tool) and copy its gate sequence. The five concerns above are addressed in roughly the same order in every tool — reusing the established pattern is much safer than reinventing it.
