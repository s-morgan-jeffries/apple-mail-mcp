#!/bin/bash
# Release-artifact freshness gate (#356).
#
# Phase 8.5 of the release refreshes derived artifacts that need real resources
# (a Mail.app account for the benchmark baseline; an OpenRouter key for the
# blind-eval snapshot). Those steps used to be skippable silently — at v0.10.0
# the eval snapshot shipped stamped "v0.9.0". This gate makes a skip impossible
# to do silently: each artifact carries the release version it was produced for,
# and this check FAILS when that stamp doesn't match the release being cut —
# unless an explicit waiver (with a tracking issue) is recorded.
#
# Resolutions when this fails:
#   1. Refresh the artifact (`make benchmark-baseline` / `make eval-tools`), OR
#   2. If it's genuinely unchanged for this release, re-stamp it (re-run the
#      capture, or update scored_results.md's `**Version:**` line), OR
#   3. Waive: add a line to release_artifact_waivers.txt naming the release,
#      the artifact, and a tracking issue. See docs/guides/RELEASE_ARTIFACTS.md.
set -euo pipefail

# Release version: explicit arg, else pyproject.toml (authoritative).
if [ "$#" -ge 1 ]; then
    VERSION="$1"
else
    VERSION=$(grep '^version = ' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')
fi
if [ -z "$VERSION" ]; then
    echo "ERROR: could not determine the release version."
    exit 1
fi

echo "Checking release artifacts are stamped for v$VERSION..."

VERSION="$VERSION" python3 - <<'PY'
from __future__ import annotations  # 3.9-safe: keep `X | None` annotations lazy

import json
import os
import re
import sys
from pathlib import Path

version = os.environ["VERSION"]
waivers_path = Path("release_artifact_waivers.txt")

# Parse waivers: lines `v<version> <artifact> #<issue> <reason>`. A matching
# (version, artifact) line lets a stale artifact pass with a warning.
waived: set[tuple[str, str]] = set()
if waivers_path.exists():
    for line in waivers_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("v") and "#" in parts[2]:
            waived.add((parts[0].lstrip("v"), parts[1]))


def baseline_version() -> str | None:
    try:
        data = json.loads(
            Path("tests/benchmarks/baseline.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    v = data.get("_version") if isinstance(data, dict) else None
    return str(v) if v else None


def eval_version() -> str | None:
    try:
        text = Path(
            "evals/agent_tool_usability/results/scored_results.md"
        ).read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"(?m)^\*\*Version:\*\*\s*v?([0-9]+\.[0-9]+\.[0-9]+)", text)
    return m.group(1) if m else None


# (artifact key, human label, found stamp, refresh command)
checks = [
    ("benchmark", "tests/benchmarks/baseline.json",
     baseline_version(), "make benchmark-baseline"),
    ("eval", "evals/agent_tool_usability/results/scored_results.md",
     eval_version(), "make eval-tools (+ refresh the Claude row)"),
]

problems = False
for key, path, found, refresh in checks:
    if found == version:
        print(f"  OK: {path} stamped for v{version}.")
    elif (version, key) in waived:
        print(
            f"  WAIVED: {path} is stamped {found!r} (expected v{version}) "
            f"but waived for this release in release_artifact_waivers.txt."
        )
    else:
        problems = True
        print(
            f"  STALE: {path} is stamped {found!r}, expected v{version}.\n"
            f"         Refresh it (`{refresh}`), or record a waiver line\n"
            f"         `v{version} {key} #<issue> <reason>` in "
            f"release_artifact_waivers.txt."
        )

if problems:
    print("")
    print("FAIL: one or more release artifacts are stale and unwaived.")
    print("See docs/guides/RELEASE_ARTIFACTS.md.")
    sys.exit(1)

print(f"Release artifacts OK for v{version}.")
PY
