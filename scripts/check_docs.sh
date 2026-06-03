#!/bin/bash
# Doc/artifact drift checks (#288). Catches content drift that the numeric
# gates (check_version_sync / check_readme_claims) miss:
#   (a) tool-set coverage — TOOLS.md documents exactly the live tool set
#   (b) no removed-tool names presented as current tools in user-facing docs
#   (c) every relative doc cross-reference resolves
#   (d) the generated eval tool_descriptions.md is in sync with the live server
# Runnable standalone, in `make check-all`, and in CI.
set -euo pipefail

cd "$(dirname "$0")/.."

uv run python - <<'PYEOF'
import asyncio
import re
import sys
from pathlib import Path

import apple_mail_mcp.server as server

ROOT = Path.cwd()
errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


# Live tool set (authoritative).
tools = asyncio.run(server.mcp.list_tools())
live = {t.name for t in tools}

readme = ROOT / "README.md"
tools_md = ROOT / "docs/reference/TOOLS.md"
readme_text = readme.read_text()
tools_text = tools_md.read_text()

# --- (a) tool-set coverage -------------------------------------------------
# Every live tool has a `### <name>` section in TOOLS.md and is named in README.
documented_headers = set(re.findall(r"(?m)^###\s+([a-z][a-z0-9_]+)\s*$", tools_text))
for name in sorted(live):
    if not re.search(rf"(?m)^###\s+{re.escape(name)}\s*$", tools_text):
        err(f"(a) TOOLS.md has no `### {name}` section (tool documented?)")
    if not re.search(rf"\b{re.escape(name)}\b", readme_text):
        err(f"(a) README.md never mentions tool `{name}`")
# A snake_case `### header` that isn't a live tool is a removed-but-documented tool.
for header in sorted(documented_headers - live):
    err(f"(a) TOOLS.md documents `### {header}` but it is not a live tool")

# --- (b) removed-tool names presented as current tools ---------------------
# Scope: user-facing docs only (exclude CHANGELOG + historical plans/research).
REMOVED = [
    "send_email", "send_email_with_attachments",
    "reply_to_message", "forward_message", "get_message",
]
SKIP = re.compile(r"removed|replaced|renamed|docs-allow", re.IGNORECASE)
current_docs = [readme, tools_md, *sorted((ROOT / "docs/guides").glob("*.md"))]
for f in current_docs:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if SKIP.search(line):
            continue
        for name in REMOVED:
            # Flag only when presented AS a tool: a call `name(` or a header.
            if re.search(rf"\b{re.escape(name)}\s*\(", line) or re.search(
                rf"^###\s+{re.escape(name)}\b", line
            ):
                rel = f.relative_to(ROOT)
                err(f"(b) {rel}:{i} references removed tool `{name}`: {line.strip()[:80]}")

# --- (c) cross-reference resolution ----------------------------------------
# Maintained docs only — docs/plans + docs/research are historical (per #220)
# and their as-authored links aren't kept current.
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
maintained = [
    p for p in sorted((ROOT / "docs").rglob("*.md"))
    if "plans" not in p.relative_to(ROOT).parts
    and "research" not in p.relative_to(ROOT).parts
]
for f in [readme, *maintained]:
    base = f.parent
    for i, line in enumerate(f.read_text().splitlines(), 1):
        for target in LINK.findall(line):
            if SCHEME.match(target) or target.startswith(("mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if not (base / path_part).resolve().exists():
                rel = f.relative_to(ROOT)
                err(f"(c) {rel}:{i} broken link -> {target}")

# --- (d) generated eval descriptions in sync -------------------------------
desc = ROOT / "evals/agent_tool_usability/tool_descriptions.md"
gen = ROOT / "evals/agent_tool_usability/generate_descriptions.py"
if desc.exists() and gen.exists():
    before = desc.read_text()
    import subprocess
    subprocess.run([sys.executable, str(gen)], check=True, cwd=ROOT,
                   capture_output=True)
    after = desc.read_text()
    if before != after:
        desc.write_text(before)  # non-destructive: restore committed version
        err("(d) evals tool_descriptions.md is stale — run `make eval-descriptions`")

# --- report ----------------------------------------------------------------
if errors:
    print(f"Doc drift check FAILED ({len(errors)} issue(s)):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print(f"Doc drift check passed ({len(live)} tools; coverage, removed-name, "
      f"cross-ref, and eval-description sync all OK).")
PYEOF
