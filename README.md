# Apple Mail MCP Server

[![Tests](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server that provides programmatic access to Apple Mail, enabling AI assistants like Claude to read, send, search, and manage emails on macOS.

> ⚠️ **Pre-1.0 — expect breaking changes.** The MCP tool surface (tool names, parameters, return shapes) is still evolving as the project matures. Pin to a specific version (for example, `apple-mail-fast-mcp==0.10.2`) and review the [CHANGELOG](CHANGELOG.md) before upgrading.

## Tools (24)

Grouped by lifecycle (10 read-only, 14 mutating):

- **Discovery** — `list_accounts`, `list_mailboxes`, `list_rules`, `list_templates`: enumerate what's configured (no external cache — call per account).
- **Read** — `search_messages`, `get_messages`, `get_thread`, `get_attachment_content`, `get_template`, `render_template`: read messages/threads, pull an attachment's content inline, and render templates.
- **Message actions** — `update_message` (read/flag/move in one pass), `delete_messages` (→ Trash), `save_attachments` (to disk, byte-capped).
- **Drafts** — `create_draft` (new / reply / forward, optionally `send_now`), `update_draft`, `delete_draft`.
- **Mailbox CRUD** — `create_mailbox`, `update_mailbox` (rename or move), `delete_mailbox`.
- **Rules** — `create_rule`, `update_rule`, `delete_rule`.
- **Templates (write)** — `save_template`, `delete_template`.

Destructive operations (`delete_*`, `create_rule` with move/forward/delete actions, `create_draft` with `send_now=true`) prompt for confirmation via MCP elicitation. See [docs/reference/TOOLS.md](docs/reference/TOOLS.md) for full parameters and return shapes.

## Prerequisites

- macOS 10.15 (Catalina) or later
- Python 3.10 or later
- Apple Mail configured with at least one account
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
# From source (recommended for development)
git clone https://github.com/s-morgan-jeffries/apple-mail-fast-mcp.git
cd apple-mail-fast-mcp
uv sync --dev
```

## Configuration

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`). `uv sync` installs a console script at `.venv/bin/apple-mail-fast-mcp`; point Claude Desktop at its **absolute path** — it's the most reliable form under Claude Desktop's restricted spawn environment (no reliance on `uv` being on `PATH`):

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/path/to/apple-mail-fast-mcp/.venv/bin/apple-mail-fast-mcp"
    }
  }
}
```

(Equivalent alternative if you prefer driving it through uv: `"command": "uv", "args": ["--directory", "/path/to/apple-mail-fast-mcp", "run", "apple-mail-fast-mcp"]`.)

### Optional: split read / write servers

Claude Desktop prompts per-tool for permission. If you want to **batch-approve the 9 read tools** (list / search / get) and still gate the 14 mutating tools per call, run the connector twice — once with `--read-only`, once without — under two separate `mcpServers` entries:

```json
{
  "mcpServers": {
    "apple-mail-read": {
      "command": "/path/to/apple-mail-fast-mcp/.venv/bin/apple-mail-fast-mcp",
      "args": ["--read-only"]
    },
    "apple-mail-write": {
      "command": "/path/to/apple-mail-fast-mcp/.venv/bin/apple-mail-fast-mcp"
    }
  }
}
```

The `--read-only` server exposes only the 9 read tools, so Claude Desktop's per-server permission UI naturally groups them. The full server still gates writes individually. Trade-off: 2× connector processes. See [`docs/reference/TOOLS.md`](docs/reference/TOOLS.md) for the per-tool classification and a note on MCP annotation hints (`readOnlyHint` / `destructiveHint` / `idempotentHint`) which forward-compatible hosts may use to provide the same UX without the split.

## Permissions

On first run, macOS will prompt for Automation access. Grant permission in:
**System Settings > Privacy & Security > Automation > Terminal (or your IDE)**

## Optional: faster search via IMAP

`search_messages` works out of the box via AppleScript. For large mailboxes (thousands of messages), AppleScript's `whose` clause can take 1–5 seconds per query. If you want faster server-side search, you can enable IMAP delegation per account by adding a Keychain entry.

**How it works.** If credentials exist for an account, the server uses IMAP (fast, server-side SEARCH). Otherwise — or on any IMAP failure (offline, wrong password, timeout) — it silently falls back to AppleScript. You never lose functionality; you only gain speed when IMAP is configured and reachable. The normal opt-in is a Keychain entry (below); an environment-variable fallback ([further down](#environment-variable-fallback-uvx--headless--ci)) covers contexts where the Keychain isn't usable.

**One-time setup per account.**

1. Generate an app-specific password at your provider. The procedure varies:
   - **iCloud:** [appleid.apple.com/account/manage](https://appleid.apple.com/account/manage) → App-Specific Passwords. Requires 2FA on your Apple ID (default).
   - **Gmail:** [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Requires 2-Step Verification on your Google account.
   - **Yahoo / Fastmail / AOL:** generate an app password in the provider's account-security settings.

2. Run the `setup-imap` subcommand. It prompts for the password (no echo), writes the Keychain entry, and verifies by connecting:
   ```bash
   apple-mail-fast-mcp setup-imap --account iCloud
   ```
   Substitute the Mail.app account name exactly — whatever it's labeled in Mail.app (e.g. `iCloud`, `Gmail`, `"Yahoo!"`). The CLI:
   - looks up the account's primary email from Mail.app (override with `--email`, which is **persisted** so runtime uses the same login — see the iCloud quirk below),
   - prompts via `getpass` so the password never lands in shell history,
   - writes to Keychain at `apple-mail-mcp.imap.<account>` (idempotent — re-running with a new password updates the existing entry),
   - opens an IMAP connection and runs a real LOGIN to confirm the password works. On rejection it rolls back the Keychain entry so you can retry without leaving a broken item behind.

3. If you see a one-time "security wants to use the 'login' keychain" prompt on the next IMAP-backed call, click **Always Allow**.

To remove the entry later: `apple-mail-fast-mcp setup-imap --account iCloud --uninstall`.

### Environment-variable fallback (uvx / headless / CI)

Some contexts have no usable Keychain: `uvx` runs (ephemeral binary paths break the Keychain ACL, causing re-prompts or failures), Docker / CI (no Keychain at all), and background services (the ACL prompt blocks forever with no UI attached). For those, you can supply the IMAP password via an environment variable instead:

```
APPLE_MAIL_MCP_IMAP_PASSWORD_<SUFFIX>
```

`<SUFFIX>` is the Mail.app account name **uppercased**, with each run of non-alphanumeric characters collapsed to a single underscore and leading/trailing underscores trimmed:

| Account name | Environment variable |
|---|---|
| `iCloud` | `APPLE_MAIL_MCP_IMAP_PASSWORD_ICLOUD` |
| `Gmail` | `APPLE_MAIL_MCP_IMAP_PASSWORD_GMAIL` |
| `Yahoo!` | `APPLE_MAIL_MCP_IMAP_PASSWORD_YAHOO` |
| `My Gmail` | `APPLE_MAIL_MCP_IMAP_PASSWORD_MY_GMAIL` |

When set to a non-empty value, the env var is used **in preference to** any Keychain entry for that account (it's checked first, with no `security` shell-out). An empty or whitespace-only value is ignored and the Keychain path is used. The lookup composes with the name↔UUID fallback, so an env var keyed on the account name is still found when a caller passes the account's UUID.

> ⚠️ **Security tradeoff.** Environment variables are far less private than the Keychain — they're visible via `ps -E`, `launchctl getenv`, `/proc`-style introspection, and process crash dumps, and they're easy to leak into logs or shell history. **Use this only when the Keychain genuinely isn't an option** (uvx, Docker, CI, headless). For Claude Desktop and standard local installs, stick with `setup-imap` + Keychain.
>
> Caveat: the name→suffix mapping isn't reversible — `Yahoo!` and `Yahoo` both map to `YAHOO`, and an account name with no ASCII letters/digits has no env-var form (use the Keychain for those).

**Verifying the setup.** The `setup-imap` command does this for you. If you want to spot-check post-hoc:
```bash
uv run python -c "from apple_mail_mcp.mail_connector import AppleMailConnector; \
    print(AppleMailConnector().search_messages(account='<ACCOUNT_NAME>', limit=1))"
```
If IMAP is working, the call returns in ~1 second. If it logs a WARNING about falling back (visible with `--log-level=DEBUG`), check that the account name matches Mail.app's account name exactly and that the email in your Keychain entry matches what `email addresses of account` returns.

**Known provider quirks.**

- **iCloud:** the IMAP server accepts `@icloud.com` / `@me.com` aliases as LOGIN username, not the Apple ID email. The server (and `setup-imap`) reads `email addresses of account` from Mail.app for that reason. If your iCloud Apple ID is a *third-party* address (e.g. a `@gmail.com` Apple ID) **and** Mail.app reports no `@icloud.com` address for the account, auto-detection can't find the right login — `setup-imap` will fail with a hint to re-run with `--email <your @icloud.com/@me.com address>`. That `--email` value is **persisted** (in `~/.apple_mail_mcp/imap_login_overrides.json`) so runtime resolution uses the same login (#341). It's a general override — use it for any account whose auto-detected IMAP login is wrong.
- **Yahoo:** app passwords have been progressively deprecated; the option may not be available for all accounts. If Yahoo's account-security page doesn't show the option, IMAP setup isn't possible for that account and AppleScript is the only path.
- **Gmail:** requires 2-Step Verification enabled. If your Google Workspace admin has disabled app passwords at the tenant level, IMAP setup isn't possible for that account.
- **Gmail thread retrieval — All Mail visibility tradeoff.** `find_thread_members` (used internally by thread-aware queries) is fastest when `[Gmail]/All Mail` is exposed over IMAP — that path is ~5 round-trips, mailbox-count-independent. Many users hide All Mail (Gmail Settings → Forwarding and POP/IMAP → Folder size limits → "Do not show in IMAP") because it duplicates every message. When hidden, the connector falls back to a per-mailbox X-GM-THRID iteration (still ~6× faster than the universal BFS, but proportional to your label count — ~25s on a 92-label account). Expose All Mail if you want the headline speed; keep it hidden if you prefer the cleaner IMAP folder list.

**Write operations** (`create_draft`, `update_draft`, including the `send_now=true` send path) always use AppleScript regardless of IMAP configuration — these need Mail.app's compose UI.

## Development

```bash
# Setup
uv sync --dev

# Common commands
make test              # Run unit tests
make lint              # Lint with ruff
make typecheck         # Type check with mypy
make check-all         # All checks (lint, typecheck, test, complexity, version-sync, parity)
make coverage          # Coverage report
make test-integration  # Integration tests (requires Mail.app)

# Validation scripts
./scripts/check_version_sync.sh          # Version consistency
./scripts/check_client_server_parity.sh  # Connector-server alignment
./scripts/check_complexity.sh            # Cyclomatic complexity
./scripts/check_applescript_safety.sh    # AppleScript safety audit
```

### Branch Convention

`{type}/issue-{num}-{description}` — e.g., `feature/issue-42-thread-support`

## Architecture

```
server.py (FastMCP tools — thin orchestration, validation, elicitation gates)
  -> mail_connector.py (dispatch + domain logic)
     -> AppleScript path:  subprocess.run(["osascript", ...]) -> Apple Mail.app   (universal baseline)
     -> IMAP fast path:    imap_connector.py -> the account's IMAP server          (when hinted + Keychain creds)
```

**Dispatch model.** AppleScript is the always-available baseline. When a read/mutation call supplies
an `account` (and, where relevant, `mailbox`) hint **and** the account has Keychain IMAP credentials,
the connector takes a server-side IMAP fast path; on any IMAP failure it falls back to AppleScript, so
you never lose functionality — you only gain speed. See
[docs/reference/ARCHITECTURE.md](docs/reference/ARCHITECTURE.md) for the full dispatch model, the
dual-emit message-ID scheme, the drafts lifecycle, and the IMAP thread tiers.

- **server.py** — MCP tool registration, input validation, confirmation (elicitation) gates, response formatting
- **mail_connector.py** — AppleScript generation/execution + IMAP-fast-path dispatch
- **imap_connector.py** — IMAP client + connection pool (search, fetch, bulk-mutation fast paths)
- **security.py** — Input sanitization, audit logging, confirmation flows
- **utils.py** — Pure functions: escaping, parsing, validation
- **exceptions.py** — Typed exception hierarchy

## Security

- Local execution only (no cloud processing)
- Uses existing Mail.app authentication; IMAP app-passwords (opt-in) live in the macOS Keychain, never in the repo or config
- All inputs sanitized and AppleScript-escaped (defense against AppleScript injection)
- Destructive operations require user confirmation via MCP elicitation; rate limits + audit logging on top
- `save_attachments` is byte-capped (per-attachment + aggregate) against disk-fill DoS

Docs:
- [SECURITY.md](SECURITY.md) — vulnerability-reporting policy
- [docs/SECURITY.md](docs/SECURITY.md) — user-facing security posture & privacy
- [docs/guides/THREAT_MODEL.md](docs/guides/THREAT_MODEL.md) — STRIDE trust-boundary analysis
- [docs/guides/SECURITY_CHECKLIST.md](docs/guides/SECURITY_CHECKLIST.md) — per-feature contributor checklist

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, coding standards, and PR process.

## License

[MIT](LICENSE)
