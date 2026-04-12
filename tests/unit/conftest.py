"""Shared fixtures for unit tests."""

import pytest

from apple_mail_mcp.security import rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state between tests to prevent cross-contamination."""
    rate_limiter.reset()
