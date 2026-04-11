"""Unit tests for security module."""


from apple_mail_mcp.security import (
    OperationLogger,
    validate_bulk_operation,
    validate_send_operation,
)


class TestOperationLogger:
    """Tests for OperationLogger."""

    def test_logs_operation(self) -> None:
        logger = OperationLogger()
        logger.log_operation("test_op", {"key": "value"}, "success")

        operations = logger.get_recent_operations(limit=1)
        assert len(operations) == 1
        assert operations[0]["operation"] == "test_op"
        assert operations[0]["parameters"] == {"key": "value"}
        assert operations[0]["result"] == "success"

    def test_limits_recent_operations(self) -> None:
        logger = OperationLogger()

        for i in range(20):
            logger.log_operation(f"op_{i}", {}, "success")

        recent = logger.get_recent_operations(limit=5)
        assert len(recent) == 5
        assert recent[-1]["operation"] == "op_19"


class TestValidateSendOperation:
    """Tests for validate_send_operation."""

    def test_valid_single_recipient(self) -> None:
        is_valid, error = validate_send_operation(["user@example.com"])
        assert is_valid is True
        assert error == ""

    def test_valid_multiple_recipients(self) -> None:
        is_valid, error = validate_send_operation(
            to=["user1@example.com"],
            cc=["user2@example.com"],
            bcc=["user3@example.com"]
        )
        assert is_valid is True
        assert error == ""

    def test_no_recipients(self) -> None:
        is_valid, error = validate_send_operation([])
        assert is_valid is False
        assert "required" in error.lower()

    def test_invalid_email(self) -> None:
        is_valid, error = validate_send_operation(["invalid-email"])
        assert is_valid is False
        assert "invalid" in error.lower()

    def test_too_many_recipients(self) -> None:
        recipients = [f"user{i}@example.com" for i in range(150)]
        is_valid, error = validate_send_operation(recipients)
        assert is_valid is False
        assert "too many" in error.lower()


class TestValidateBulkOperation:
    """Tests for validate_bulk_operation."""

    def test_valid_count(self) -> None:
        is_valid, error = validate_bulk_operation(50, max_items=100)
        assert is_valid is True
        assert error == ""

    def test_zero_items(self) -> None:
        is_valid, error = validate_bulk_operation(0)
        assert is_valid is False
        assert "no items" in error.lower()

    def test_too_many_items(self) -> None:
        is_valid, error = validate_bulk_operation(150, max_items=100)
        assert is_valid is False
        assert "too many" in error.lower()

    def test_exactly_max_items(self) -> None:
        is_valid, error = validate_bulk_operation(100, max_items=100)
        assert is_valid is True
