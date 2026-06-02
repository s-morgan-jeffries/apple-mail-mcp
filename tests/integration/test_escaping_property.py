"""Operational property test for AppleScript string escaping (#214).

The "gold check": run the escaped+quoted string through the *real* AppleScript
parser (`osascript`) and confirm it decodes back to the input — proving no
generated input can break out of, or fail to parse as, a single string
literal. This is the non-circular complement to the pure-Python structural
properties in tests/unit/test_property_security.py.

It needs only `osascript` (not Mail.app), but it spawns a subprocess per
Hypothesis example, so it's gated behind --run-integration and runs via
`make test-integration` — kept out of the fast CI unit suite per #298.
"""

import subprocess

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apple_mail_mcp.utils import escape_applescript_string, sanitize_input

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Spawns osascript per example. Use --run-integration.",
)


def _osascript_return(escaped: str) -> tuple[int, str, str]:
    r = subprocess.run(
        ["/usr/bin/osascript", "-e", f'return "{escaped}"'],
        capture_output=True, text=True, timeout=10,
    )
    # osascript appends exactly one trailing newline to its output.
    out = r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout
    return r.returncode, out, r.stderr.strip()


# Exclude surrogates (Cs) — they can't be UTF-8 encoded for the subprocess —
# and carriage return, which AppleScript normalizes to LF (covered by the
# explicit example below). Everything else (quotes, backslashes, newlines,
# tabs, unicode) must round-trip exactly.
_TEXT = st.text(
    alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\r")
)


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_TEXT)
def test_escaped_string_round_trips_through_osascript(s: str) -> None:
    expected = sanitize_input(s)
    escaped = escape_applescript_string(expected)
    rc, out, err = _osascript_return(escaped)
    assert rc == 0, f"osascript failed to parse the literal: {err}"
    assert out == expected, (
        f"round-trip mismatch: in={expected!r} out={out!r}"
    )


def test_carriage_return_is_normalized_to_newline() -> None:
    """Documented quirk: AppleScript normalizes a CR to LF. Not an injection
    (the literal still parses cleanly) — just a lossy value normalization, so
    CR is excluded from the property strategy above."""
    escaped = escape_applescript_string(sanitize_input("a\rb"))
    rc, out, _ = _osascript_return(escaped)
    assert rc == 0
    assert out == "a\nb"
