"""Persistent state for drafts created by ``create_draft``.

Stores seed metadata so ``update_draft`` can rebuild a draft via the
correct AppleScript primitive. Mail.app forbids mutating saved drafts,
so update is implemented as delete + recreate; for reply / forward
seeds we need to know the original message to re-invoke ``reply`` /
``forward``. Looking the seed up via ``whose message id is`` against
Mail.app on demand takes 30+ seconds on large mailboxes, so we
persist seed metadata at create time instead.

File layout: one JSON file per draft at ``<root>/<draft_id>.json``,
shape::

    {"seed_kind": "reply",   "seed_id": "160989", "reply_all": false}
    {"seed_kind": "forward", "seed_id": "160989"}

Fresh drafts (no seed) get no file.

``draft_id`` is regex-validated before any path is constructed so
user-controlled input cannot escape the drafts directory.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .exceptions import MailDraftInvalidIdError

# Mail.app internal message ids are numeric strings in practice
# (e.g. "160991"). Allow alphanumerics + - _ for safety. The 128
# char cap is generous for any conceivable id format.
_DRAFT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_EXT = ".json"

SeedKind = Literal["reply", "forward"]


@dataclass(frozen=True)
class SeedRecord:
    """Persisted seed metadata for a draft created by ``create_draft``."""

    seed_kind: SeedKind
    seed_id: str
    reply_all: bool = False


def _validate_draft_id(draft_id: str) -> None:
    if not isinstance(draft_id, str) or not _DRAFT_ID_RE.match(draft_id):
        raise MailDraftInvalidIdError(
            f"draft_id {draft_id!r} must match {_DRAFT_ID_RE.pattern}"
        )


def default_root() -> Path:
    """Default drafts state directory, honoring ``APPLE_MAIL_MCP_HOME``.

    Resolved at call time so env-var overrides and test-time monkeypatching
    are honored.
    """
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = Path(home_override) if home_override else Path.home() / ".apple_mail_mcp"
    return base / "drafts"


class DraftStateStore:
    """File-backed store for draft seed metadata."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    def _path_for(self, draft_id: str) -> Path:
        _validate_draft_id(draft_id)
        return self.root / f"{draft_id}{_EXT}"

    def get_seed(self, draft_id: str) -> SeedRecord | None:
        """Return the seed record for ``draft_id``, or None.

        Corrupt or unreadable state files are treated as "no state"
        rather than raised — they would just block update_draft for a
        draft we can't recover anyway, and the user can still
        delete + re-create.
        """
        path = self._path_for(draft_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        kind = data.get("seed_kind")
        seed_id = data.get("seed_id")
        if kind not in ("reply", "forward"):
            return None
        if not isinstance(seed_id, str) or not seed_id:
            return None
        reply_all = bool(data.get("reply_all", False))
        return SeedRecord(seed_kind=kind, seed_id=seed_id, reply_all=reply_all)

    def set_seed(self, draft_id: str, seed: SeedRecord) -> None:
        """Persist the seed record for ``draft_id``."""
        path = self._path_for(draft_id)
        self.root.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "seed_kind": seed.seed_kind,
            "seed_id": seed.seed_id,
        }
        if seed.seed_kind == "reply":
            payload["reply_all"] = seed.reply_all
        path.write_text(json.dumps(payload), encoding="utf-8")

    def delete(self, draft_id: str) -> None:
        """Remove the state file for ``draft_id``. Idempotent."""
        path = self._path_for(draft_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
