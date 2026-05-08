"""Unit tests for the DraftStateStore module."""

from pathlib import Path

import pytest

from apple_mail_mcp.exceptions import MailDraftInvalidIdError


class TestValidateDraftId:
    def test_accepts_numeric_id(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        # Mail.app internal ids are numeric strings in practice.
        _validate_draft_id("160991")

    def test_accepts_alphanumeric_with_dashes_and_underscores(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        _validate_draft_id("abc_123-xyz")

    def test_rejects_path_traversal(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        with pytest.raises(MailDraftInvalidIdError):
            _validate_draft_id("../etc/passwd")

    def test_rejects_slashes(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        with pytest.raises(MailDraftInvalidIdError):
            _validate_draft_id("foo/bar")

    def test_rejects_empty_string(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        with pytest.raises(MailDraftInvalidIdError):
            _validate_draft_id("")

    def test_rejects_non_string(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        with pytest.raises(MailDraftInvalidIdError):
            _validate_draft_id(160991)  # type: ignore[arg-type]

    def test_rejects_overlong(self):
        from apple_mail_mcp.drafts import _validate_draft_id

        with pytest.raises(MailDraftInvalidIdError):
            _validate_draft_id("a" * 129)


class TestDefaultRoot:
    def test_uses_home_default_when_no_override(self, monkeypatch):
        from apple_mail_mcp.drafts import default_root

        monkeypatch.delenv("APPLE_MAIL_MCP_HOME", raising=False)
        assert default_root() == Path.home() / ".apple_mail_mcp" / "drafts"

    def test_honors_apple_mail_mcp_home_env(self, monkeypatch, tmp_path):
        from apple_mail_mcp.drafts import default_root

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        assert default_root() == tmp_path / "drafts"

    def test_resolves_at_call_time_not_import_time(self, monkeypatch, tmp_path):
        from apple_mail_mcp.drafts import default_root

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "first"))
        first = default_root()
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "second"))
        second = default_root()
        assert first != second


class TestDraftStateStore:
    def test_get_seed_returns_none_when_no_state(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        assert store.get_seed("160991") is None

    def test_set_then_get_reply_seed(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        store = DraftStateStore(root=tmp_path)
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="reply", seed_id="999888", reply_all=True),
        )
        seed = store.get_seed("160991")
        assert seed is not None
        assert seed.seed_kind == "reply"
        assert seed.seed_id == "999888"
        assert seed.reply_all is True

    def test_set_then_get_forward_seed(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        store = DraftStateStore(root=tmp_path)
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="forward", seed_id="999888"),
        )
        seed = store.get_seed("160991")
        assert seed is not None
        assert seed.seed_kind == "forward"
        assert seed.seed_id == "999888"
        assert seed.reply_all is False

    def test_set_creates_root_directory_if_missing(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        nested = tmp_path / "nested" / "drafts"
        assert not nested.exists()
        store = DraftStateStore(root=nested)
        store.set_seed(
            "160991", SeedRecord(seed_kind="reply", seed_id="999888")
        )
        assert nested.is_dir()

    def test_set_overwrites_existing_entry(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        store = DraftStateStore(root=tmp_path)
        store.set_seed(
            "160991", SeedRecord(seed_kind="reply", seed_id="aaa")
        )
        store.set_seed(
            "160991", SeedRecord(seed_kind="forward", seed_id="bbb")
        )
        seed = store.get_seed("160991")
        assert seed is not None
        assert seed.seed_kind == "forward"
        assert seed.seed_id == "bbb"

    def test_delete_removes_state(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        store = DraftStateStore(root=tmp_path)
        store.set_seed(
            "160991", SeedRecord(seed_kind="reply", seed_id="999888")
        )
        store.delete("160991")
        assert store.get_seed("160991") is None

    def test_delete_is_idempotent(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        # No exception when nothing to delete.
        store.delete("160991")

    def test_get_with_invalid_id_raises(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        with pytest.raises(MailDraftInvalidIdError):
            store.get_seed("../escape")

    def test_set_with_invalid_id_raises(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        store = DraftStateStore(root=tmp_path)
        with pytest.raises(MailDraftInvalidIdError):
            store.set_seed(
                "../escape", SeedRecord(seed_kind="reply", seed_id="x")
            )

    def test_delete_with_invalid_id_raises(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        with pytest.raises(MailDraftInvalidIdError):
            store.delete("../escape")

    def test_get_handles_corrupt_json_gracefully(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "160991.json").write_text("{not valid json")
        # Corrupt state should be treated as "no state" rather than crashing
        # an update_draft call.
        assert store.get_seed("160991") is None

    def test_get_handles_missing_seed_kind_key(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "160991.json").write_text('{"seed_id": "999"}')
        assert store.get_seed("160991") is None

    def test_get_handles_invalid_seed_kind(self, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        store = DraftStateStore(root=tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        # "new" is not a persisted seed kind — fresh drafts get no file.
        (tmp_path / "160991.json").write_text(
            '{"seed_kind": "new", "seed_id": "x"}'
        )
        assert store.get_seed("160991") is None

    def test_default_constructor_uses_default_root(self, monkeypatch, tmp_path):
        from apple_mail_mcp.drafts import DraftStateStore

        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        store = DraftStateStore()
        assert store.root == tmp_path / "drafts"

    def test_forward_seed_does_not_persist_reply_all(self, tmp_path):
        """Forward seeds shouldn't carry reply_all (it's reply-only)."""
        import json

        from apple_mail_mcp.drafts import DraftStateStore, SeedRecord

        store = DraftStateStore(root=tmp_path)
        store.set_seed(
            "160991",
            SeedRecord(seed_kind="forward", seed_id="999888", reply_all=True),
        )
        # The on-disk JSON shouldn't have reply_all for forward seeds.
        data = json.loads((tmp_path / "160991.json").read_text())
        assert "reply_all" not in data
