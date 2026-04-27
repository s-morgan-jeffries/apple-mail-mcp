"""Benchmarks for attachment-handling operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

from .conftest import (
    BenchmarkResult,
    assert_within_baseline,
    measure_median,
)


@pytest.fixture(scope="module")
def message_with_attachment(
    connector: AppleMailConnector, test_account: str
) -> str:
    """Find a message in the test account that has at least one
    attachment. Skips the module if none exists."""
    for mb in ("INBOX", "Archive", "Sent Messages"):
        try:
            results = connector.search_messages(
                account=test_account, mailbox=mb, has_attachment=True, limit=1
            )
        except Exception:
            continue
        if results:
            return results[0]["id"]
    pytest.skip(
        f"No messages with attachments in account {test_account!r}. "
        f"Benchmark requires at least one such message in INBOX, Archive, "
        f"or Sent Messages."
    )


def test_save_attachments_one_file(
    connector: AppleMailConnector,
    message_with_attachment: str,
    tmp_path: Path,
    baselines: dict[str, float],
    capture_mode: bool,
) -> None:
    """Baseline: save attachments from one message into a tmp dir.

    Each iteration uses a fresh subdirectory to avoid filename-collision
    overwrites, but does NOT redownload from the IMAP server — Mail.app
    serves attachments from its local cache for read messages.
    """
    name = "save_attachments_one_file"

    iteration = [0]

    def run() -> None:
        iteration[0] += 1
        out_dir = tmp_path / f"run_{iteration[0]}"
        out_dir.mkdir()
        connector.save_attachments(message_with_attachment, out_dir)

    result: BenchmarkResult = measure_median(run, name=name)
    assert_within_baseline(name, result, baselines, capture_mode)
