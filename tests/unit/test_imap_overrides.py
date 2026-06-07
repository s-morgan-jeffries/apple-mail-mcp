"""Unit tests for the persisted IMAP login overrides (#341)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_mail_mcp import imap_overrides


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a temp APPLE_MAIL_MCP_HOME for every test."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    return tmp_path


class TestImapOverrides:
    def test_home_env_honored(self, _home: Path) -> None:
        assert imap_overrides._overrides_path() == (
            _home / "imap_login_overrides.json"
        )

    def test_missing_file_returns_none(self) -> None:
        assert imap_overrides.get_login_override("iCloud") is None

    def test_set_get_roundtrip(self, _home: Path) -> None:
        imap_overrides.set_login_override("iCloud", "me@icloud.com")
        assert imap_overrides.get_login_override("iCloud") == "me@icloud.com"
        # Persisted as JSON on disk.
        data = json.loads(
            (_home / "imap_login_overrides.json").read_text()
        )
        assert data == {"iCloud": "me@icloud.com"}

    def test_set_strips_whitespace(self) -> None:
        imap_overrides.set_login_override("iCloud", "  me@icloud.com\n")
        assert imap_overrides.get_login_override("iCloud") == "me@icloud.com"

    def test_set_merges_multiple_accounts(self) -> None:
        imap_overrides.set_login_override("iCloud", "me@icloud.com")
        imap_overrides.set_login_override("Work", "me@work.example")
        assert imap_overrides.get_login_override("iCloud") == "me@icloud.com"
        assert imap_overrides.get_login_override("Work") == "me@work.example"

    def test_delete_removes_entry(self) -> None:
        imap_overrides.set_login_override("iCloud", "me@icloud.com")
        imap_overrides.set_login_override("Work", "me@work.example")
        imap_overrides.delete_login_override("iCloud")
        assert imap_overrides.get_login_override("iCloud") is None
        assert imap_overrides.get_login_override("Work") == "me@work.example"

    def test_delete_last_entry_removes_file(self, _home: Path) -> None:
        imap_overrides.set_login_override("iCloud", "me@icloud.com")
        imap_overrides.delete_login_override("iCloud")
        assert not (_home / "imap_login_overrides.json").exists()

    def test_delete_missing_is_noop(self) -> None:
        imap_overrides.delete_login_override("Nope")  # must not raise

    def test_corrupt_file_returns_none(self, _home: Path) -> None:
        (_home / "imap_login_overrides.json").write_text("{not json")
        assert imap_overrides.get_login_override("iCloud") is None

    def test_non_dict_json_returns_none(self, _home: Path) -> None:
        (_home / "imap_login_overrides.json").write_text("[1, 2, 3]")
        assert imap_overrides.get_login_override("iCloud") is None

    def test_empty_value_treated_as_absent(self, _home: Path) -> None:
        (_home / "imap_login_overrides.json").write_text('{"iCloud": "  "}')
        assert imap_overrides.get_login_override("iCloud") is None
