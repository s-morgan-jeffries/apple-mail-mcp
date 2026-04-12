"""Unit tests for security module."""

from __future__ import annotations

from unittest.mock import patch

from apple_mail_mcp.security import (
    OPERATION_TIERS,
    TIER_LIMITS,
    OperationLogger,
    RateLimiter,
    check_rate_limit,
    operation_logger,
    rate_limiter,
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


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Tests for the sliding-window RateLimiter."""

    def setup_method(self) -> None:
        self.limiter = RateLimiter()

    def test_allows_calls_up_to_limit(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            assert self.limiter.check("sends") is True

    def test_rejects_call_over_limit(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            self.limiter.check("sends")
        assert self.limiter.check("sends") is False

    def test_allows_after_window_expires(self) -> None:
        max_calls, window = TIER_LIMITS["sends"]
        for _ in range(max_calls):
            self.limiter.check("sends")

        fake_time = [0.0]

        def monotonic() -> float:
            return fake_time[0]

        with patch("apple_mail_mcp.security.time") as mock_time:
            mock_time.monotonic = monotonic
            # First, fill to limit at t=0
            limiter = RateLimiter()
            for _ in range(max_calls):
                limiter.check("sends")
            assert limiter.check("sends") is False

            # Advance past window
            fake_time[0] = window + 1.0
            assert limiter.check("sends") is True

    def test_tiers_are_independent(self) -> None:
        max_sends = TIER_LIMITS["sends"][0]
        for _ in range(max_sends):
            self.limiter.check("sends")
        assert self.limiter.check("sends") is False
        assert self.limiter.check("cheap_reads") is True
        assert self.limiter.check("expensive_ops") is True

    def test_reset_clears_all_tiers(self) -> None:
        max_sends = TIER_LIMITS["sends"][0]
        for _ in range(max_sends):
            self.limiter.check("sends")
        assert self.limiter.check("sends") is False

        self.limiter.reset()
        assert self.limiter.check("sends") is True

    def test_module_level_instance_exists(self) -> None:
        assert isinstance(rate_limiter, RateLimiter)


# ---------------------------------------------------------------------------
# check_rate_limit helper
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    """Tests for the check_rate_limit helper function."""

    def setup_method(self) -> None:
        rate_limiter.reset()
        operation_logger.operations.clear()

    def test_returns_none_when_under_limit(self) -> None:
        result = check_rate_limit("list_mailboxes", {"account": "Gmail"})
        assert result is None

    def test_returns_error_dict_when_over_limit(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            check_rate_limit("send_email", {"subject": "x"})

        result = check_rate_limit("send_email", {"subject": "x"})
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert "sends" in result["error"]

    def test_logs_rate_limited_to_operation_logger(self) -> None:
        max_calls = TIER_LIMITS["sends"][0]
        for _ in range(max_calls):
            check_rate_limit("send_email", {"subject": "x"})

        check_rate_limit("send_email", {"subject": "blocked"})

        recent = operation_logger.get_recent_operations(limit=10)
        rate_limited_entries = [
            op for op in recent if op["result"] == "rate_limited"
        ]
        assert len(rate_limited_entries) == 1
        assert rate_limited_entries[0]["operation"] == "send_email"
        assert rate_limited_entries[0]["parameters"] == {"subject": "blocked"}

    def test_error_message_includes_limit_and_window(self) -> None:
        max_calls, window = TIER_LIMITS["sends"]
        for _ in range(max_calls):
            check_rate_limit("send_email", {"subject": "x"})

        result = check_rate_limit("send_email", {"subject": "x"})
        assert result is not None
        assert str(max_calls) in result["error"]
        assert str(int(window)) in result["error"]

    def test_all_operations_have_tier_assigned(self) -> None:
        expected_ops = {
            "list_mailboxes", "get_message", "get_attachments", "save_attachments",
            "search_messages", "mark_as_read", "move_messages", "flag_message",
            "create_mailbox", "delete_messages", "reply_to_message",
            "send_email", "send_email_with_attachments", "forward_message",
        }
        assert set(OPERATION_TIERS.keys()) == expected_ops

    def test_tier_limits_config_exists_for_all_tiers(self) -> None:
        expected_tiers = {"cheap_reads", "expensive_ops", "sends"}
        assert set(TIER_LIMITS.keys()) == expected_tiers
        for _tier, (max_calls, window) in TIER_LIMITS.items():
            assert max_calls > 0
            assert window > 0
