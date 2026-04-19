# Testing Guide

## Test Levels

| Level | Command | Purpose |
|-------|---------|---------|
| Unit | `make test` | Python logic with mocked AppleScript (~1s) |
| Integration | `make test-integration` | Real Mail.app operations (~30s) |
| E2E | `make test-e2e` | FastMCP dispatch layer in-process |
| Benchmarks | `pytest tests/benchmarks/ -v` | Performance regression detection |

### E2E Scope

`make test-e2e` covers two layers:

1. **In-process FastMCP dispatch** ([tests/e2e/test_mcp_tools.py](../../tests/e2e/test_mcp_tools.py)) — tool registration, schemas, and happy-path invocation for all 14 tools, with the connector mocked.
2. **Real stdio transport** ([tests/e2e/test_stdio_transport.py](../../tests/e2e/test_stdio_transport.py)) — spawns the server as a subprocess, completes the MCP handshake over stdio, and asserts `list_tools` returns the expected 14 tools. Catches startup errors, banner/stdout contamination, and JSON-RPC framing bugs that the in-process layer cannot surface.

## Running Tests

```bash
# All unit tests (default)
make test

# With coverage report
make coverage

# Integration tests (requires Mail.app)
MAIL_TEST_ACCOUNT="Gmail" make test-integration

# Specific test file
uv run pytest tests/unit/test_utils.py -v
```

## Writing Tests

### Unit Tests

Mock at the `_run_applescript()` boundary:

```python
from unittest.mock import patch, MagicMock
from apple_mail_mcp.mail_connector import AppleMailConnector

class TestMyFeature:
    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_my_method(self, mock_run, connector):
        mock_run.return_value = "expected|output"
        result = connector.my_method("param")
        assert result == expected
```

### Integration Tests

```python
pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default."
)

def test_real_operation(self, connector):
    result = connector.list_mailboxes("Gmail")
    assert isinstance(result, list)
```

### Test Organization

Each test file follows:
1. Fixtures (connector instance)
2. Happy path tests
3. Filter/parameter tests
4. Error handling tests
5. Edge cases
6. Security tests (dedicated class per feature)

## Coverage

- Target: 90%
- Enforced: `fail_under = 90` in pyproject.toml
- Run `make coverage` for the current report.
