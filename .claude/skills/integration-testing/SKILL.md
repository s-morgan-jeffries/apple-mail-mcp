---
name: integration-testing
description: Use when setting up, running, or debugging integration tests against real Apple Mail. Also use when unit tests pass but behavior seems wrong, when adding new AppleScript operations, or when you need to understand why mocked tests are insufficient for this project.
---

# Apple Mail Integration Testing

## Why Integration Tests Matter

Unit tests mock `_run_applescript()` and test Python logic only. They CANNOT catch:
- AppleScript syntax errors
- Variable naming conflicts in AppleScript
- Mail.app API behavior differences between versions
- Silently-dropped record keys from NSJSONSerialization (e.g., `name`, `id`, `size` selector collisions)
- Gmail-specific behavior differences
- Timeout issues with real mailbox sizes

**The OmniFocus project's story:** A variable naming typo went undetected by 400+ unit tests because they all mocked the AppleScript boundary. Only integration tests against the real app caught it. This lesson applies equally to Apple Mail.

## Three-Tier Testing Strategy

| Tier | Speed | What it catches | When to run |
|------|-------|-----------------|-------------|
| Unit (mocked) | ~1s, 99 tests | Python logic, parsing, validation | Every change |
| Integration (real) | ~30s | AppleScript bugs, Mail.app quirks | New AppleScript code |
| E2E (full MCP) | ~30s | Tool registration, parameter passing | New/modified tools |

## Setting Up Integration Tests

### Prerequisites
1. Apple Mail configured with at least one account
2. macOS Automation permission granted to Terminal/IDE

### Test Account Setup
```bash
# Set test account (default: "Gmail")
export MAIL_TEST_ACCOUNT="Gmail"

# Run integration tests
make test-integration
```

### Running Tests
```bash
# Integration tests are opt-in
pytest tests/integration/ --run-integration -v

# Or via Makefile
make test-integration
```

## Writing Integration Tests

```python
import pytest
from apple_mail_mcp.mail_connector import AppleMailConnector

# Skip unless explicitly enabled
pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default."
)

class TestMailIntegration:
    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector()

    @pytest.fixture
    def test_account(self) -> str:
        import os
        return os.getenv("MAIL_TEST_ACCOUNT", "Gmail")

    def test_list_mailboxes(self, connector, test_account):
        """Verify we can list mailboxes from a real account."""
        result = connector.list_mailboxes(test_account)
        assert isinstance(result, list)
        assert len(result) > 0
        # INBOX should always exist
        assert any("INBOX" in mb for mb in result)

    @pytest.mark.skip(reason="Sends real email - enable manually")
    def test_send_email(self, connector):
        """Test sending real email - enable manually only."""
        ...
```

## Key Patterns

### Never Auto-Run Destructive Tests
```python
# Destructive operations (send, delete, move) should be:
# 1. Skipped by default
# 2. Require explicit --run-integration flag
# 3. Require MAIL_TEST_ACCOUNT environment variable
# 4. Target a specific test account, never "all accounts"
```

### Test Account Configuration
```python
@pytest.fixture
def test_account(self) -> str:
    """Configurable test account - never hardcode."""
    import os
    return os.getenv("MAIL_TEST_ACCOUNT", "Gmail")
```

### Cleanup After Tests
```python
# If test creates data (mailbox, flag), clean up in fixture teardown
@pytest.fixture
def test_mailbox(self, connector, test_account):
    name = f"MCP-Test-{uuid.uuid4()}"
    connector.create_mailbox(test_account, name)
    yield name
    try:
        # Cleanup - don't fail if already deleted
        connector.delete_mailbox(test_account, name)
    except Exception:
        pass
```

## Hard Rule

**If you wrote or modified AppleScript in `mail_connector.py`, integration tests must cover that operation before merge.**

This is not optional. This is not "nice to have." Unit tests with mocked `_run_applescript()` give false confidence about AppleScript correctness.

## Common Integration Test Failures

1. **"Not authorized to send Apple events"** — Grant Automation permission in System Settings > Privacy & Security > Automation
2. **Account not found** — Verify `MAIL_TEST_ACCOUNT` matches an actual configured account name in Mail.app
3. **Timeout** — Large mailboxes may exceed 60s default. Use `AppleMailConnector(timeout=120)`
4. **Gmail behavior** — Gmail move/delete may behave differently than IMAP accounts. Test both if possible.
