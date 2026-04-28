"""Unit tests for mail connector."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailMailboxNotFoundError,
    MailMessageNotFoundError,
)
from apple_mail_mcp.mail_connector import AppleMailConnector, _wrap_as_json_script


class TestAppleMailConnector:
    """Tests for AppleMailConnector."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        """Create a connector instance."""
        return AppleMailConnector(timeout=30)

    @patch("subprocess.run")
    def test_run_applescript_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test successful AppleScript execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="result",
            stderr=""
        )

        result = connector._run_applescript("test script")
        assert result == "result"

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["/usr/bin/osascript", "-"]

    @patch("subprocess.run")
    def test_run_applescript_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test account not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get account \"NonExistent\""
        )

        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test mailbox not found error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can't get mailbox \"NonExistent\""
        )

        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_timeout(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test timeout handling."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)

        with pytest.raises(MailAppleScriptError, match="timeout"):
            connector._run_applescript("test script")

    @patch("subprocess.run")
    def test_run_applescript_curly_apostrophe_still_maps_to_typed_error(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Real macOS stderr uses curly apostrophes — must still dispatch typed errors.

        Regression guard for a bug where `Can\u2019t get account "X"` (curly
        apostrophe, as emitted by Mail.app) bypassed the typed-exception
        mapping and surfaced as a generic MailAppleScriptError, defeating the
        server-layer not-found routing.
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get account \"NonExistent\"",
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._run_applescript("test script")

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Can\u2019t get mailbox \"NonExistent\"",
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector._run_applescript("test script")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true},'
            '{"id":"UUID-2","name":"Work","email_addresses":["me@work.com","alt@work.com"],'
            '"account_type":"iCloud","enabled":false}]'
        )
        result = connector.list_accounts()
        assert result == [
            {"id": "UUID-1", "name": "Gmail",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            {"id": "UUID-2", "name": "Work",
             "email_addresses": ["me@work.com", "alt@work.com"],
             "account_type": "iCloud", "enabled": False},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_accounts()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_handles_empty_email_addresses(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """An account with no email addresses must return email_addresses as []."""
        mock_run.return_value = (
            '[{"id":"UUID-3","name":"LocalOnly","email_addresses":[],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result == [{
            "id": "UUID-3", "name": "LocalOnly", "email_addresses": [],
            "account_type": "imap", "enabled": True,
        }]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_includes_type_and_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Generated AppleScript must extract account_type (as text) and enabled."""
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|account_type|:((account type of acc) as text)" in script
        assert "|enabled|:(enabled of acc)" in script
        assert "|id|:(id of acc as text)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"index":1,"name":"News From Apple","enabled":false},'
            '{"index":2,"name":"Junk filter","enabled":true}]'
        )
        result = connector.list_rules()
        assert result == [
            {"index": 1, "name": "News From Apple", "enabled": False},
            {"index": 2, "name": "Junk filter", "enabled": True},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        result = connector.list_rules()
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_allows_duplicate_names(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail allows multiple rules with the same name — connector returns both
        with distinct positional indices."""
        mock_run.return_value = (
            '[{"index":3,"name":"Send to OmniFocus","enabled":false},'
            '{"index":4,"name":"Send to OmniFocus","enabled":true}]'
        )
        result = connector.list_rules()
        assert len(result) == 2
        assert result[0]["name"] == result[1]["name"]
        assert result[0]["enabled"] != result[1]["enabled"]
        # The duplicate-name disambiguator: the index field.
        assert result[0]["index"] != result[1]["index"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_script_emits_one_based_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Per #63, list_rules' return shape must include a 1-based index
        matching Mail.app's AppleScript ``rule N`` reference."""
        mock_run.return_value = "[]"
        connector.list_rules()
        script = mock_run.call_args[0][0]
        # Iterates by index, not by reference, so the loop variable is the index.
        assert "repeat with i from 1 to ruleCount" in script
        assert "|index|:i" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_rules_script_quotes_keys(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Record keys must be |quoted| per the v0.4.1 selector-collision rule."""
        mock_run.return_value = "[]"
        connector.list_rules()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of r)" in script
        assert "|enabled|:(enabled of r)" in script
        assert "|index|:i" in script

    # --- set_rule_enabled ------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_set_rule_enabled_true_emits_correct_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = ""
        connector.set_rule_enabled(rule_index=2, enabled=True)
        script = mock_run.call_args[0][0]
        assert "set enabled of rule 2 to true" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_set_rule_enabled_false_emits_correct_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = ""
        connector.set_rule_enabled(rule_index=3, enabled=False)
        script = mock_run.call_args[0][0]
        assert "set enabled of rule 3 to false" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_set_rule_enabled_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector.set_rule_enabled(rule_index=99, enabled=True)

    def test_set_rule_enabled_rejects_zero_or_negative_index(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        with pytest.raises(MailRuleNotFoundError):
            connector.set_rule_enabled(rule_index=0, enabled=True)
        with pytest.raises(MailRuleNotFoundError):
            connector.set_rule_enabled(rule_index=-1, enabled=True)

    # --- delete_rule -----------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_rule_returns_deleted_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "Junk filter"
        result = connector.delete_rule(rule_index=2)
        assert result == "Junk filter"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_rule_emits_correct_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "X"
        connector.delete_rule(rule_index=2)
        script = mock_run.call_args[0][0]
        # Reads name before deleting (so we can echo it back).
        assert "name of rule 2" in script
        assert "delete rule 2" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_rule_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector.delete_rule(rule_index=99)

    def test_delete_rule_rejects_zero_or_negative_index(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        with pytest.raises(MailRuleNotFoundError):
            connector.delete_rule(rule_index=0)
        with pytest.raises(MailRuleNotFoundError):
            connector.delete_rule(rule_index=-5)

    # --- _check_supported_actions ---------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_passes_for_clean_rule(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """A rule with only supported actions does not raise."""
        mock_run.return_value = (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        # Should not raise.
        connector._check_supported_actions(rule_index=1)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_rejects_run_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_run.return_value = (
            '{"run_script_set":true,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError, match="run script"):
            connector._check_supported_actions(rule_index=1)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_lists_all_unsupported(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_run.return_value = (
            '{"run_script_set":true,"play_sound_set":true,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":true,"highlight_text":false,'
            '"color_message":"none"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError) as excinfo:
            connector._check_supported_actions(rule_index=2)
        msg = str(excinfo.value)
        assert "run script" in msg
        assert "play sound" in msg
        assert "reply text" in msg

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_treats_color_message_none_as_clean(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """color_message == 'none' is the default — not a customization."""
        mock_run.return_value = (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        connector._check_supported_actions(rule_index=1)  # no raise

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_rejects_non_none_color_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        mock_run.return_value = (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"red"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError, match="color message"):
            connector._check_supported_actions(rule_index=1)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_check_supported_actions_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector._check_supported_actions(rule_index=99)

    # --- create_rule -----------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_returns_new_rule_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "6"
        new_index = connector.create_rule(
            name="My Rule",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "X"}
            ],
            actions={"mark_read": True},
        )
        assert new_index == 6

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_emits_correct_field_and_operator(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "from", "operator": "contains", "value": "@apple.com"}
            ],
            actions={"delete": True},
        )
        script = mock_run.call_args[0][0]
        assert "rule type:from header" in script
        assert "qualifier:does contain value" in script
        assert 'expression:"@apple.com"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_header_name_includes_header_field(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {
                    "field": "header_name",
                    "operator": "equals",
                    "value": "yes",
                    "header_name": "X-Important",
                }
            ],
            actions={"mark_flagged": True},
        )
        script = mock_run.call_args[0][0]
        assert "rule type:header key" in script
        assert 'header:"X-Important"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_match_logic_any_emits_false(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"delete": True},
            match_logic="any",
        )
        script = mock_run.call_args[0][0]
        assert "all conditions must be met of newRule to false" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_move_action_emits_mailbox_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"move_to": {"account": "Gmail", "mailbox": "Archive"}},
        )
        script = mock_run.call_args[0][0]
        assert "set should move message of newRule to true" in script
        assert (
            'set move message of newRule to mailbox "Archive" of '
            'account "Gmail"' in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_mark_flagged_with_color_sets_flag_index(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"mark_flagged": True, "flag_color": "yellow"},
        )
        script = mock_run.call_args[0][0]
        assert "set mark flagged of newRule to true" in script
        assert "set mark flag index of newRule to 2" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_create_rule_forward_to_uses_comma_string(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_rule(
            name="X",
            conditions=[
                {"field": "subject", "operator": "contains", "value": "Y"}
            ],
            actions={"forward_to": ["a@example.com", "b@example.com"]},
        )
        script = mock_run.call_args[0][0]
        # forward_message is a string, not a list — recipients are
        # comma-separated.
        assert (
            'set forward message of newRule to "a@example.com, b@example.com"'
            in script
        )

    def test_create_rule_rejects_empty_name(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="name"):
            connector.create_rule(
                name="",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "X"}
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_empty_conditions(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="conditions"):
            connector.create_rule(
                name="X",
                conditions=[],
                actions={"delete": True},
            )

    def test_create_rule_rejects_empty_actions(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="actions"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={},
            )

    def test_create_rule_rejects_invalid_field(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="field"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "bogus", "operator": "contains", "value": "Y"}
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_invalid_operator(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="operator"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "BOGUS", "value": "Y"}
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_header_name_field_without_header_name(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="header_name"):
            connector.create_rule(
                name="X",
                conditions=[
                    {
                        "field": "header_name",
                        "operator": "contains",
                        "value": "v",
                    }
                ],
                actions={"delete": True},
            )

    def test_create_rule_rejects_invalid_forward_to_email(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="email"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={"forward_to": ["not-an-email"]},
            )

    def test_create_rule_rejects_invalid_match_logic(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="match_logic"):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={"delete": True},
                match_logic="bogus",
            )

    def test_create_rule_rejects_invalid_flag_color(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError):
            connector.create_rule(
                name="X",
                conditions=[
                    {"field": "subject", "operator": "contains", "value": "Y"}
                ],
                actions={"mark_flagged": True, "flag_color": "neon"},
            )

    # --- update_rule -----------------------------------------------------

    @staticmethod
    def _supported_actions_clean_response() -> str:
        """Mock _check_supported_actions JSON for a rule with no
        unsupported actions set."""
        return (
            '{"run_script_set":false,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_name_only_emits_minimal_script(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # Two AppleScript calls happen: _check_supported_actions, then update.
        mock_run.side_effect = [
            self._supported_actions_clean_response(),
            "",  # the update itself returns nothing
        ]
        connector.update_rule(rule_index=2, name="Renamed")
        update_script = mock_run.call_args_list[1][0][0]
        assert "set newRule to rule 2" in update_script
        assert 'set name of newRule to "Renamed"' in update_script
        # Patch semantics: enabled/match_logic/conditions/actions not touched.
        assert "set enabled of newRule" not in update_script
        assert "set rule conditions of newRule" not in update_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_enabled_only_changes_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            self._supported_actions_clean_response(),
            "",
        ]
        connector.update_rule(rule_index=3, enabled=False)
        update_script = mock_run.call_args_list[1][0][0]
        assert "set enabled of newRule to false" in update_script
        assert "set name of newRule" not in update_script

    def test_update_rule_conditions_refused_due_to_mail_bug(
        self, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError
        # Mail.app on macOS Tahoe has a recursion bug in
        # removeFromCriteriaAtIndex: that crashes Mail on any AppleScript
        # path that removes a rule condition. update_rule must refuse
        # `conditions=` with a typed error instead of attempting it.
        with pytest.raises(MailUnsupportedRuleActionError, match="Tahoe"):
            connector.update_rule(
                rule_index=4,
                conditions=[
                    {"field": "from", "operator": "contains", "value": "@x.com"}
                ],
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_actions_resets_then_applies(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = [
            self._supported_actions_clean_response(),
            "",
        ]
        connector.update_rule(
            rule_index=2,
            actions={"mark_read": True},
        )
        update_script = mock_run.call_args_list[1][0][0]
        # All action flags reset first
        assert "set mark flagged of newRule to false" in update_script
        assert "set delete message of newRule to false" in update_script
        # Then provided action applied
        assert "set mark read of newRule to true" in update_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_no_args_after_index_makes_no_changes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Calling update_rule with only rule_index does the supported-action
        check and then exits — no script for an empty update."""
        mock_run.return_value = self._supported_actions_clean_response()
        connector.update_rule(rule_index=2)
        # Only one AppleScript call: the supported-actions check.
        assert mock_run.call_count == 1

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_refuses_unsupported_actions(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailUnsupportedRuleActionError

        # _check_supported_actions response indicates run-script is set.
        mock_run.return_value = (
            '{"run_script_set":true,"play_sound_set":false,'
            '"redirect_set":false,"forward_text_set":false,'
            '"reply_text_set":false,"highlight_text":false,'
            '"color_message":"none"}'
        )
        with pytest.raises(MailUnsupportedRuleActionError):
            connector.update_rule(rule_index=4, enabled=False)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_update_rule_propagates_rule_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailRuleNotFoundError

        mock_run.side_effect = MailRuleNotFoundError("Can't get rule 99")
        with pytest.raises(MailRuleNotFoundError):
            connector.update_rule(rule_index=99, enabled=False)

    def test_update_rule_rejects_invalid_match_logic(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="match_logic"):
            connector.update_rule(rule_index=2, match_logic="bogus")

    def test_update_rule_rejects_empty_name(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="name"):
            connector.update_rule(rule_index=2, name="")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| (quoted) so NSJSONSerialization keeps it.

        Unquoted `name:` in the record literal causes the key to be silently
        dropped during ASObjC -> NSDictionary conversion because `name` collides
        with NSObject's `name` property. Regression guard for real Mail.app bug.
        """
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|name|:(name of acc)" in script
        assert "{name:(name of acc)" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_returns_structured_data(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '[{"name":"INBOX","unread_count":5},'
            '{"name":"Sent","unread_count":0},'
            '{"name":"Projects/Client A","unread_count":3}]'
        )
        result = connector.list_mailboxes("Gmail")
        assert result == [
            {"name": "INBOX", "unread_count": 5},
            {"name": "Sent", "unread_count": 0},
            {"name": "Projects/Client A", "unread_count": 3},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAccountNotFoundError("Can't get account \"NoSuch\".")
        with pytest.raises(MailAccountNotFoundError):
            connector.list_mailboxes("NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_script_quotes_name_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The AppleScript must use |name| so NSJSONSerialization preserves it."""
        mock_run.return_value = "[]"
        connector.list_mailboxes("Gmail")
        script = mock_run.call_args[0][0]
        assert "|name|:(name of mb)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_with_name_uses_account_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector.list_mailboxes("Gmail")
        script = mock_run.call_args[0][0]
        assert 'set accountRef to account "Gmail"' in script
        assert "account id" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_mailboxes_with_uuid_uses_account_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = "[]"
        connector.list_mailboxes(uuid)
        script = mock_run.call_args[0][0]
        assert f'set accountRef to account id "{uuid}"' in script

    # --- _resolve_imap_config --------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_prefers_first_email_address(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Primary path: first email_addresses entry wins over user_name.

        iCloud's IMAP server rejects the Apple-ID user_name as LOGIN
        but accepts the aliases in email_addresses.
        """
        mock_run.return_value = (
            '{"host":"imap.mail.me.com",'
            '"port":993,'
            '"user_name":"apple.id@gmail.com",'
            '"email_addresses":["user@icloud.com","user@me.com"]}'
        )
        result = connector._resolve_imap_config("iCloud")
        assert result == ("imap.mail.me.com", 993, "user@icloud.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_falls_back_to_user_name_when_emails_empty(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Fallback path: empty email_addresses → use user_name."""
        mock_run.return_value = (
            '{"host":"imap.gmail.com",'
            '"port":993,'
            '"user_name":"me@gmail.com",'
            '"email_addresses":[]}'
        )
        result = connector._resolve_imap_config("Gmail")
        assert result == ("imap.gmail.com", 993, "me@gmail.com")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAccountNotFoundError(
            "Can't get account \"NoSuch\"."
        )
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_imap_config("NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_script_has_quoted_keys(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """NSJSONSerialization requires |key| form for record keys."""
        mock_run.return_value = (
            '{"host":"h","port":993,'
            '"user_name":"u@e.com","email_addresses":["u@e.com"]}'
        )
        connector._resolve_imap_config("iCloud")
        script = mock_run.call_args[0][0]
        assert "|host|:(server name of acctRef)" in script
        assert "|port|:(port of acctRef)" in script
        assert "|user_name|:(user name of acctRef)" in script
        assert "|email_addresses|:acctEmails" in script
        # Must assign to resultData for _wrap_as_json_script to serialize.
        assert "set resultData to" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_escapes_account_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"host":"h","port":993,'
            '"user_name":"u@e.com","email_addresses":["u@e.com"]}'
        )
        connector._resolve_imap_config('Weird "Name" Acct')
        script = mock_run.call_args[0][0]
        # The quote must be escaped; raw quotes would break the script.
        assert 'Weird \\"Name\\" Acct' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_resolve_imap_config_with_uuid_uses_account_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = (
            '{"host":"h","port":993,'
            '"user_name":"u@e.com","email_addresses":["u@e.com"]}'
        )
        connector._resolve_imap_config(uuid)
        script = mock_run.call_args[0][0]
        assert f'set acctRef to account id "{uuid}"' in script

    # --- _imap_failures state + _log_imap_fallback -----------------------

    def test_imap_failures_starts_empty(
        self, connector: AppleMailConnector
    ) -> None:
        assert connector._imap_failures == set()

    def test_log_imap_fallback_keychain_entry_not_found_is_silent(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing Keychain entry is a benign opt-out signal — DEBUG only."""
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback(
                "iCloud", MailKeychainEntryNotFoundError("missing")
            )
        # Not in the failures set — benign signals don't count as failures.
        assert "iCloud" not in connector._imap_failures
        # Should log at DEBUG, never WARNING.
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_records) == 1
        assert "iCloud" in debug_records[0].getMessage()

    def test_log_imap_fallback_first_failure_logs_warning(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("iCloud", OSError("network down"))
        assert "iCloud" in connector._imap_failures
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "iCloud" in msg
        assert "OSError" in msg

    def test_log_imap_fallback_subsequent_failure_same_account_is_debug(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Seed: first failure.
        connector._log_imap_fallback("iCloud", OSError("first"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("iCloud", OSError("second"))
        # Set unchanged (already contains iCloud).
        assert connector._imap_failures == {"iCloud"}
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_records == []
        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_records) == 1

    def test_log_imap_fallback_failure_new_account_logs_warning(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        connector._log_imap_fallback("iCloud", OSError("iCloud first"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback("Gmail", OSError("Gmail first"))
        assert connector._imap_failures == {"iCloud", "Gmail"}
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "Gmail" in warning_records[0].getMessage()

    def test_log_imap_fallback_access_denied_counts_as_failure(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Access denied is a misconfiguration worth surfacing, unlike missing entry."""
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector._log_imap_fallback(
                "iCloud", MailKeychainAccessDeniedError("ACL refused")
            )
        assert "iCloud" in connector._imap_failures
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1

    # --- _imap_search helper ---------------------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.return_value = [{"id": "1", "subject": "S"}]

        result = connector._imap_search("iCloud", "INBOX", limit=5)

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password"
        )
        # Parameters forwarded 1:1 to the IMAP connector (minus `account`).
        mock_imap.search_messages.assert_called_once_with(
            mailbox="INBOX",
            sender_contains=None,
            subject_contains=None,
            read_status=None,
            is_flagged=None,
            date_from=None,
            date_to=None,
            has_attachment=None,
            limit=5,
        )
        assert result == [{"id": "1", "subject": "S"}]

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_search("iCloud", "INBOX")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_search("iCloud", "INBOX")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_search_oserror_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.search_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_search("iCloud", "INBOX")

    # --- _imap_get_thread helper -----------------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_get_thread_happy_path(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "app-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.find_thread_members.return_value = [
            {"id": "anchor@x", "subject": "S"},
        ]

        anchor = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "Hello",
            "in_reply_to": None,
            "references": ["parent@x"],
        }
        result = connector._imap_get_thread(anchor)

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password"
        )
        mock_imap.find_thread_members.assert_called_once_with(
            anchor_rfc_message_id="anchor@x",
            anchor_references=["parent@x"],
        )
        assert result == [{"id": "anchor@x", "subject": "S"}]

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_get_thread_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        anchor = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "Hello",
            "in_reply_to": None,
            "references": [],
        }
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_get_thread(anchor)

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_get_thread_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "pw"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.find_thread_members.side_effect = LoginError("rejected")
        anchor = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "Hello",
            "in_reply_to": None,
            "references": [],
        }
        with pytest.raises(LoginError):
            connector._imap_get_thread(anchor)

    # --- get_thread delegation -------------------------------------------

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_uses_imap_on_success(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_anchor.return_value = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "S",
            "in_reply_to": None,
            "references": [],
        }
        mock_imap.return_value = [{"id": "anchor@x", "subject": "from imap"}]
        result = connector.get_thread("500")
        assert result == [{"id": "anchor@x", "subject": "from imap"}]
        mock_collect.assert_not_called()

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_falls_back_on_keychain_missing(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_anchor.return_value = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "S",
            "in_reply_to": None,
            "references": [],
        }
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_collect.return_value = [{"id": "500", "subject": "from applescript"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.get_thread("500")
        assert result == [{"id": "500", "subject": "from applescript"}]
        mock_collect.assert_called_once()
        # Missing-entry = silent (no WARNING).
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        assert "iCloud" not in connector._imap_failures

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_falls_back_on_oserror_with_warning(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_anchor.return_value = {
            "internal_id": "500",
            "account": "iCloud",
            "rfc_message_id": "anchor@x",
            "subject": "S",
            "in_reply_to": None,
            "references": [],
        }
        mock_imap.side_effect = OSError("unreachable")
        mock_collect.return_value = [{"id": "500"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.get_thread("500")
        assert result == [{"id": "500"}]
        mock_collect.assert_called_once()
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "iCloud" in connector._imap_failures

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_falls_back_on_login_error(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_anchor.return_value = {
            "internal_id": "500", "account": "iCloud",
            "rfc_message_id": "anchor@x", "subject": "S",
            "in_reply_to": None, "references": [],
        }
        mock_imap.side_effect = LoginError("rejected")
        mock_collect.return_value = [{"id": "500"}]
        result = connector.get_thread("500")
        assert result == [{"id": "500"}]
        mock_collect.assert_called_once()

    @patch.object(AppleMailConnector, "_collect_thread_applescript")
    @patch.object(AppleMailConnector, "_imap_get_thread")
    @patch.object(AppleMailConnector, "_resolve_thread_anchor_applescript")
    def test_get_thread_anchor_not_found_propagates(
        self,
        mock_anchor: MagicMock,
        mock_imap: MagicMock,
        mock_collect: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """MailMessageNotFoundError from anchor resolution must propagate,
        not fall back — the message just doesn't exist anywhere."""
        mock_anchor.side_effect = MailMessageNotFoundError("Can't get message")
        with pytest.raises(MailMessageNotFoundError):
            connector.get_thread("nonexistent")
        mock_imap.assert_not_called()
        mock_collect.assert_not_called()

    # --- search_messages delegation --------------------------------------

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_uses_imap_on_success(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap_search.return_value = [{"id": "1", "subject": "from imap"}]
        result = connector.search_messages(account="iCloud", mailbox="INBOX")
        assert result == [{"id": "1", "subject": "from imap"}]
        mock_as_search.assert_not_called()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_keychain_missing(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_imap_search.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_as_search.return_value = [{"id": "1", "subject": "from applescript"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1", "subject": "from applescript"}]
        mock_as_search.assert_called_once()
        # Missing-entry = silent (no WARNING).
        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records == []
        # Account not tracked as a failure.
        assert "iCloud" not in connector._imap_failures

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_oserror_with_warning(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_imap_search.side_effect = OSError("unreachable")
        mock_as_search.return_value = [{"id": "1"}]
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1"}]
        mock_as_search.assert_called_once()
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "iCloud" in connector._imap_failures

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_login_error(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import LoginError

        mock_imap_search.side_effect = LoginError("rejected")
        mock_as_search.return_value = [{"id": "1"}]
        result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1"}]
        mock_as_search.assert_called_once()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_falls_back_on_imap_protocol_error(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import IMAPClientError

        mock_imap_search.side_effect = IMAPClientError("bad thing")
        mock_as_search.return_value = [{"id": "1"}]
        result = connector.search_messages(account="iCloud")
        assert result == [{"id": "1"}]
        mock_as_search.assert_called_once()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_forwards_all_parameters(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap_search.return_value = []
        connector.search_messages(
            account="iCloud",
            mailbox="Sent",
            sender_contains="alice",
            subject_contains="invoice",
            read_status=True,
            is_flagged=False,
            date_from="2026-04-01",
            date_to="2026-04-22",
            has_attachment=True,
            limit=10,
        )
        mock_imap_search.assert_called_once_with(
            "iCloud",
            "Sent",
            "alice",
            "invoice",
            True,
            False,
            "2026-04-01",
            "2026-04-22",
            True,
            10,
        )

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_does_not_catch_value_error(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Invalid-input errors must propagate to the caller, not silently fall back."""
        mock_imap_search.side_effect = ValueError("bad date")
        with pytest.raises(ValueError, match="bad date"):
            connector.search_messages(account="iCloud", date_from="not-a-date")
        mock_as_search.assert_not_called()

    @patch.object(AppleMailConnector, "_search_messages_applescript")
    @patch.object(AppleMailConnector, "_imap_search")
    def test_search_messages_does_not_catch_mailaccountnotfound(
        self,
        mock_imap_search: MagicMock,
        mock_as_search: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """A truly-missing account must surface, not be papered over by fallback."""
        mock_imap_search.side_effect = MailAccountNotFoundError("No such account")
        with pytest.raises(MailAccountNotFoundError):
            connector.search_messages(account="Ghost")
        mock_as_search.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_basic(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test basic message search."""
        mock_run.return_value = (
            '[{"id":"12345","subject":"Test Subject",'
            '"sender":"sender@example.com","date_received":"Mon Jan 1 2024",'
            '"read_status":false}]'
        )

        result = connector._search_messages_applescript("Gmail", "INBOX")

        assert len(result) == 1
        assert result[0]["id"] == "12345"
        assert result[0]["subject"] == "Test Subject"
        assert result[0]["sender"] == "sender@example.com"
        assert result[0]["read_status"] is False

    # Note: validates the Python-side JSON parse. Real end-to-end correctness
    # (AppleScript actually emitting valid JSON when the data contains '|')
    # is proven by integration tests.
    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_handles_pipe_in_subject(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Subject containing '|' must not break parsing (the bug this refactor fixes)."""
        mock_run.return_value = (
            '[{"id":"abc","subject":"Q3 Report | Draft",'
            '"sender":"boss@example.com","date_received":"Wed Feb 5 2025",'
            '"read_status":true}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert len(result) == 1
        assert result[0]["subject"] == "Q3 Report | Draft"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_account_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """If _run_applescript raises MailAccountNotFoundError, search_messages must not swallow it.

        Regression guard: a previous version wrapped the tell-block in try/on error,
        which downgraded MailAccountNotFoundError to MailAppleScriptError.
        """
        mock_run.side_effect = MailAccountNotFoundError("Can't get account \"NoSuch\".")
        with pytest.raises(MailAccountNotFoundError):
            connector._search_messages_applescript("NoSuch", "INBOX")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_propagates_mailbox_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Similar regression guard for MailMailboxNotFoundError."""
        mock_run.side_effect = MailMailboxNotFoundError("Can't get mailbox \"NoSuch\".")
        with pytest.raises(MailMailboxNotFoundError):
            connector._search_messages_applescript("Gmail", "NoSuch")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_with_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test message search with filters."""
        mock_run.return_value = "[]"

        connector._search_messages_applescript(
            "Gmail",
            "INBOX",
            sender_contains="john@example.com",
            subject_contains="meeting",
            read_status=False,
            limit=10
        )

        # Verify the script includes filter conditions
        call_args = mock_run.call_args[0][0]
        assert 'sender contains "john@example.com"' in call_args
        assert 'subject contains "meeting"' in call_args
        assert "read status is false" in call_args
        # Limit is enforced by accumulating matches and exiting the repeat
        # when count of resultData reaches the bound. `items 1 thru N of`
        # is avoided — Mail rejects it on live message collection references.
        assert "if (count of resultData) >= 10 then exit repeat" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_without_filters_omits_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """AppleScript rejects `whose true` — no-filter searches must drop `whose`.

        Regression guard for a bug where `search_messages("X", "INBOX")` with no
        filters emitted `messages of mailboxRef whose true`, which Mail.app
        rejects with `Illegal comparison or logical (-1726)`.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "whose true" not in script
        # With NO filters, the generated source must reference `mailboxRef`
        # without a `whose` clause.
        assert "messages of mailboxRef\n" in script or "messages of mailboxRef " in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_does_not_slice_message_reference(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail rejects `items 1 thru N of (messages ...)` with error -1728.

        The limit must be enforced via `count of` + indexed `item i of`, not
        by slicing the live message collection reference.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", limit=5)
        script = mock_run.call_args[0][0]
        assert "items 1 thru" not in script
        assert "if (count of resultData) >= 5 then exit repeat" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_is_flagged_in_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", is_flagged=True)
        script = mock_run.call_args[0][0]
        assert "flagged status is true" in script

        connector._search_messages_applescript("Gmail", "INBOX", is_flagged=False)
        script = mock_run.call_args[0][0]
        assert "flagged status is false" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_date_range_in_whose_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector._search_messages_applescript(
            "Gmail", "INBOX", date_from="2026-04-01", date_to="2026-04-15"
        )
        script = mock_run.call_args[0][0]
        assert 'date received >= (date "2026-04-01")' in script
        # date_to gets +1 day so the full day is inclusive
        assert 'date received < (date "2026-04-16")' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_rejects_malformed_date_from(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Malformed dates must raise ValueError, not be sent to AppleScript.

        Prevents AppleScript injection via unescaped date strings.
        """
        with pytest.raises(ValueError, match="date_from"):
            connector._search_messages_applescript(
                "Gmail", "INBOX",
                date_from='2024-01-01", delete mailbox',
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_rejects_malformed_date_to(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="date_to"):
            connector._search_messages_applescript("Gmail", "INBOX", date_to="not-a-date")
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_has_attachment_true_post_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """has_attachment=True can't go in whose — applied inside the loop."""
        mock_run.return_value = "[]"
        # Combine with a read_status filter so the whose clause exists and the
        # "count attachments not in whose" assertion is meaningful.
        connector._search_messages_applescript(
            "Gmail", "INBOX", read_status=True, has_attachment=True
        )
        script = mock_run.call_args[0][0]
        # The attachment check MUST NOT appear in the whose clause line.
        whose_line = [
            ln for ln in script.splitlines() if "whose" in ln and "messages of" in ln
        ][0]
        assert "mail attachments" not in whose_line
        # But it MUST appear as a post-filter inside the loop.
        assert (
            "if (count of mail attachments of msg) = 0 then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_has_attachment_false_post_filters(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", has_attachment=False)
        script = mock_run.call_args[0][0]
        assert (
            "if (count of mail attachments of msg) > 0 then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_no_attachment_filter_has_no_check(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """When has_attachment is None, no attachment post-filter code appears."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "mail attachments of msg" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_result_includes_flagged(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """New in #28: result rows include the flagged status."""
        mock_run.return_value = (
            '[{"id":"1","subject":"s","sender":"a@b.c",'
            '"date_received":"Mon","read_status":false,"flagged":true}]'
        )
        result = connector._search_messages_applescript("Gmail", "INBOX")
        assert result[0]["flagged"] is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_script_quotes_id_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Guard against NSJSONSerialization silently dropping the 'id' key.

        AppleScript record key `id:` collides with NSObject's id selector and
        gets stripped during NSDictionary conversion. Must be quoted as `|id|:`.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX")
        script = mock_run.call_args[0][0]
        assert "|id|:(id of msg as text)" in script
        # The bare form must not appear in the msgRecord literal — it would collide.
        assert ", id:(id of msg" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_applescript_with_uuid_uses_account_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        uuid = "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"
        mock_run.return_value = "[]"
        connector._search_messages_applescript(uuid, "INBOX")
        script = mock_run.call_args[0][0]
        assert f'set accountRef to account id "{uuid}"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting a message."""
        mock_run.return_value = (
            '{"id":"12345","subject":"Subject","sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false,'
            '"content":"Message body"}'
        )

        result = connector.get_message("12345", include_content=True)

        assert result["id"] == "12345"
        assert result["subject"] == "Subject"
        assert result["content"] == "Message body"
        assert result["read_status"] is True
        assert result["flagged"] is False

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_handles_pipe_in_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Body containing '|' must not break parsing."""
        mock_run.return_value = (
            '{"id":"99","subject":"x","sender":"a@b.com",'
            '"date_received":"Mon Jan 1 2024","read_status":false,"flagged":false,'
            '"content":"col1|col2|col3"}'
        )
        result = connector.get_message("99", include_content=True)
        assert result["content"] == "col1|col2|col3"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_script_quotes_id_key(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Same guard as test_search_messages_script_quotes_id_key, for get_message."""
        mock_run.return_value = '{"id":"x","subject":"","sender":"","date_received":"","read_status":false,"flagged":false,"content":""}'
        connector.get_message("x")
        script = mock_run.call_args[0][0]
        assert "|id|:(id of msg as text)" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_email_basic(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test sending a basic email."""
        mock_run.return_value = "sent"

        result = connector.send_email(
            subject="Test",
            body="Test body",
            to=["recipient@example.com"]
        )

        assert result is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_send_email_with_cc_bcc(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test sending email with CC and BCC."""
        mock_run.return_value = "sent"

        result = connector.send_email(
            subject="Test",
            body="Test body",
            to=["recipient@example.com"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"]
        )

        assert result is True

        # Verify script includes recipients
        call_args = mock_run.call_args[0][0]
        assert "recipient@example.com" in call_args
        assert "cc@example.com" in call_args
        assert "bcc@example.com" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_read(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test marking messages as read."""
        mock_run.return_value = "2"

        result = connector.mark_as_read(["12345", "12346"], read=True)

        assert result == 2

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_unread(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test marking messages as unread."""
        mock_run.return_value = "1"

        result = connector.mark_as_read(["12345"], read=False)

        assert result == 1

        # Verify script sets read status to false
        call_args = mock_run.call_args[0][0]
        assert "set read status of msg to false" in call_args

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_single(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting a single selected message."""
        # Modernized: single AppleScript call returns JSON array of records.
        mock_run.return_value = (
            '[{"id":"12345","subject":"Selected Subject",'
            '"sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024",'
            '"read_status":true,"flagged":false,"content":"Body text"}]'
        )

        result = connector.get_selected_messages(include_content=True)

        assert len(result) == 1
        assert result[0]["id"] == "12345"
        assert result[0]["subject"] == "Selected Subject"
        assert result[0]["sender"] == "sender@example.com"
        assert result[0]["content"] == "Body text"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_multiple(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test getting multiple selected messages."""
        mock_run.return_value = (
            '[{"id":"111","subject":"Subject One","sender":"a@example.com",'
            '"date_received":"Mon Jan 1 2024","read_status":true,'
            '"flagged":false,"content":"Body one"},'
            '{"id":"222","subject":"Subject Two","sender":"b@example.com",'
            '"date_received":"Tue Jan 2 2024","read_status":false,'
            '"flagged":true,"content":"Body two"}]'
        )

        result = connector.get_selected_messages(include_content=True)

        assert len(result) == 2
        assert result[0]["id"] == "111"
        assert result[1]["id"] == "222"
        assert result[1]["flagged"] is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_none_selected(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test when no message is selected — script returns empty JSON array."""
        mock_run.return_value = "[]"

        result = connector.get_selected_messages()

        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_selected_messages_no_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Test that include_content=False emits the no-content branch in
        the AppleScript and the returned record has empty content."""
        mock_run.return_value = (
            '[{"id":"12345","subject":"Subject",'
            '"sender":"sender@example.com",'
            '"date_received":"Mon Jan 1 2024",'
            '"read_status":false,"flagged":false,"content":""}]'
        )

        result = connector.get_selected_messages(include_content=False)

        # Verify the script took the no-content branch (no `set msgContent
        # to content of msg`).
        script = mock_run.call_args[0][0]
        assert 'set msgContent to ""' in script
        assert "set msgContent to content of msg" not in script

        assert len(result) == 1
        assert result[0]["content"] == ""

    def test_mark_as_read_empty_list(self, connector: AppleMailConnector) -> None:
        """Test marking with empty list."""
        result = connector.mark_as_read([])
        assert result == 0

    # ---- get_thread ----

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_resolution_script_shape(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor-resolution AppleScript must query by internal id and quote keys."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>","subject":"Q3",'
            '"in_reply_to":"","references_raw":""}',
            "[]",
        ]
        connector._get_thread_applescript("12345")
        anchor_script = mock_run.call_args_list[0][0][0]
        # All record keys must be |quoted| per the v0.4.1 selector-collision rule.
        assert "|rfc_message_id|:(message id of msg)" in anchor_script
        assert "|subject|:(subject of msg)" in anchor_script
        # Anchor lookup iterates by internal id; id must be wrapped in
        # AppleScript string quotes (otherwise UUID-style ids tokenize
        # as invalid syntax — see TestWhoseIdQuoting).
        assert 'whose id is "12345"' in anchor_script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_anchor_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor lookup failure propagates MailMessageNotFoundError."""
        mock_run.side_effect = MailMessageNotFoundError("Can't get message")
        with pytest.raises(MailMessageNotFoundError):
            connector._get_thread_applescript("99999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_returns_anchor_plus_replies_sorted(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor + 2 replies in candidates → all 3 sorted by date_received."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Re: Q3","in_reply_to":"","references_raw":""}',
            '['
            '{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"date_received":"Mon Jan 1 2024","read_status":true,"flagged":false},'
            '{"id":"101","rfc_message_id":"<r1@x>","in_reply_to":"<anchor@x>",'
            '"references_raw":"<anchor@x>","subject":"Re: Q3","sender":"b@x",'
            '"date_received":"Tue Jan 2 2024","read_status":true,"flagged":false},'
            '{"id":"102","rfc_message_id":"<r2@x>","in_reply_to":"<r1@x>",'
            '"references_raw":"<anchor@x> <r1@x>","subject":"Re: Q3","sender":"a@x",'
            '"date_received":"Wed Jan 3 2024","read_status":false,"flagged":false}'
            ']'
        ]
        result = connector._get_thread_applescript("100")
        assert len(result) == 3
        assert [m["id"] for m in result] == ["100", "101", "102"]
        # Response rows match search_messages shape (6 fields).
        for m in result:
            assert set(m.keys()) == {
                "id", "subject", "sender", "date_received", "read_status", "flagged",
            }

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_drops_threading_internals_from_output(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Response rows must NOT leak rfc_message_id / in_reply_to / references_raw."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<anchor@x>",'
            '"subject":"Q3","in_reply_to":"","references_raw":""}',
            '[{"id":"100","rfc_message_id":"<anchor@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Q3","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector._get_thread_applescript("100")
        for m in result:
            assert "rfc_message_id" not in m
            assert "in_reply_to" not in m
            assert "references_raw" not in m
            assert "references_parsed" not in m

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_orphan_anchor_returns_single_message(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Anchor with no threading headers → thread = [anchor] only."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<orphan@x>","subject":"Standalone",'
            '"in_reply_to":"","references_raw":""}',
            '[{"id":"500","rfc_message_id":"<orphan@x>","in_reply_to":"",'
            '"references_raw":"","subject":"Standalone","sender":"a@x",'
            '"date_received":"Mon","read_status":false,"flagged":false}]'
        ]
        result = connector._get_thread_applescript("500")
        assert len(result) == 1
        assert result[0]["id"] == "500"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_thread_candidate_script_uses_base_subject_and_account(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Candidate script must use normalized subject and scope to anchor's account."""
        mock_run.side_effect = [
            '{"account":"Gmail","rfc_message_id":"<a@x>",'
            '"subject":"Re: Re: Q3 Report","in_reply_to":"","references_raw":""}',
            '[]',
        ]
        connector._get_thread_applescript("1")
        candidate_script = mock_run.call_args_list[1][0][0]
        assert 'account "Gmail"' in candidate_script
        # Base subject strips all Re: prefixes.
        assert 'subject contains "Q3 Report"' in candidate_script
        assert 'subject contains "Re:' not in candidate_script


class TestMessageIdAppleScriptInjection:
    """Regression guards for AppleScript-injection via message IDs.

    Two bug families this class protects against:

    1. Multi-id list methods (mark_as_read, move_messages, flag_message,
       delete_messages) used to do `", ".join(message_ids)` directly into
       an AppleScript list literal — a crafted id containing a `"` could
       escape the list and inject arbitrary script.

    2. Single-id `whose id is "..."` clauses used to interpolate the raw
       message_id without escaping in reply_to_message and forward_message.

    See PR #34 (martparve) for the original report.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_read_quotes_and_escapes_each_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        # Crafted id with a quote and backslash: a naive join would
        # break out of the list literal.
        connector.mark_as_read(['abc"; do evil; --', "back\\slash"])
        script = mock_run.call_args[0][0]
        # Both ids appear inside their own quoted string.
        assert '"abc\\"; do evil; --"' in script
        assert '"back\\\\slash"' in script
        # The injected `do evil` must NOT appear unquoted at the script level
        # (i.e., outside the list).
        assert "{\"abc\\\"; do evil; --\", \"back\\\\slash\"}" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_messages_quotes_and_escapes_each_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.move_messages(['evil"; foo', "ok"], "Gmail", "Archive")
        script = mock_run.call_args[0][0]
        assert '"evil\\"; foo"' in script
        assert '"ok"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_message_quotes_and_escapes_each_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.flag_message(['evil"', "ok"], "red")
        script = mock_run.call_args[0][0]
        assert '"evil\\""' in script
        assert '"ok"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_quotes_and_escapes_each_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.delete_messages(['evil"', "ok"])
        script = mock_run.call_args[0][0]
        assert '"evil\\""' in script
        assert '"ok"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_to_message_escapes_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "msg-id"
        connector.reply_to_message('craft"ed-id', body="hi")
        script = mock_run.call_args[0][0]
        # Quoted and escaped — no raw quote breaks out of the string.
        assert 'whose id is "craft\\"ed-id"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_message_escapes_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "msg-id"
        connector.forward_message(
            'craft"ed-id', to=["a@example.com"], body="hi"
        )
        script = mock_run.call_args[0][0]
        assert 'whose id is "craft\\"ed-id"' in script


class TestWhoseIdQuoting:
    """Regression guards for #86: `whose id is X` must wrap X in quotes
    even when X is already escape_applescript_string'd.

    Without quotes, AppleScript chokes on UUID-style ids like
    'CF7C3761-...@icloud.com' because the dashes/dots/@ get parsed as
    syntax (dash = subtraction, @ = bare identifier, etc.).
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_message_quotes_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"id":"x","subject":"s","sender":"","date_received":"","read_status":false,"flagged":false,"content":""}'
        uuid_id = "CF7C3761-C190-40BA-B94E-3EBC321980ED@icloud.com"
        connector.get_message(uuid_id, include_content=False)
        script = mock_run.call_args[0][0]
        assert f'whose id is "{uuid_id}"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_quotes_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        uuid_id = "CF7C3761-C190-40BA-B94E-3EBC321980ED@icloud.com"
        connector.get_attachments(uuid_id)
        script = mock_run.call_args[0][0]
        assert f'whose id is "{uuid_id}"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_save_attachments_quotes_id_in_whose(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "[]"
        uuid_id = "CF7C3761-C190-40BA-B94E-3EBC321980ED@icloud.com"
        # save_attachments takes a Path (uses .exists()).
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            connector.save_attachments(uuid_id, Path(td))
        # Multiple AppleScript calls may happen; check at least one
        # contained the quoted-id pattern.
        scripts = [c[0][0] for c in mock_run.call_args_list]
        assert any(f'whose id is "{uuid_id}"' in s for s in scripts), (
            f"expected quoted id in one of the scripts: {scripts}"
        )


class TestWrapAsJsonScript:
    def test_wrapper_contains_framework_directive(self) -> None:
        script = _wrap_as_json_script('tell application "Mail"\n    set resultData to {}\nend tell')
        assert 'use framework "Foundation"' in script
        assert "use scripting additions" in script

    def test_wrapper_appends_json_serialization(self) -> None:
        script = _wrap_as_json_script('tell application "Mail"\n    set resultData to {}\nend tell')
        assert "NSJSONSerialization" in script
        assert "dataWithJSONObject:resultData" in script

    def test_wrapper_preserves_body(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body)
        assert body in script

    def test_wrapper_orders_framework_before_body_before_epilogue(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = _wrap_as_json_script(body)
        framework_idx = script.index('use framework "Foundation"')
        body_idx = script.index(body)
        epilogue_idx = script.index("NSJSONSerialization")
        assert framework_idx < body_idx < epilogue_idx


class TestAutoTemplateVars:
    """auto_template_vars() builds the auto-fill dict for render_template."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_no_message_id_returns_only_today(
        self, connector: AppleMailConnector
    ) -> None:
        result = connector.auto_template_vars(message_id=None)
        assert set(result.keys()) == {"today"}
        # ISO date format
        assert len(result["today"]) == 10
        assert result["today"][4] == "-" and result["today"][7] == "-"

    @patch.object(AppleMailConnector, "get_message")
    def test_with_message_id_extracts_sender_fields(
        self, mock_get: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_get.return_value = {
            "id": "abc",
            "subject": "Project Q3 plan",
            "sender": "Alice Smith <alice@example.com>",
            "content": "...",
        }
        result = connector.auto_template_vars(message_id="abc")
        assert result["recipient_name"] == "Alice Smith"
        assert result["recipient_email"] == "alice@example.com"
        assert result["original_subject"] == "Project Q3 plan"
        assert "today" in result
        # Confirm we called get_message without fetching content
        mock_get.assert_called_once_with("abc", include_content=False)

    @patch.object(AppleMailConnector, "get_message")
    def test_sender_without_display_name_falls_back_to_email(
        self, mock_get: MagicMock, connector: AppleMailConnector
    ) -> None:
        # Sender field is just an email, no display name
        mock_get.return_value = {
            "id": "x",
            "subject": "hi",
            "sender": "bob@example.com",
            "content": "",
        }
        result = connector.auto_template_vars(message_id="x")
        # When no display name, recipient_name falls back to the email
        assert result["recipient_name"] == "bob@example.com"
        assert result["recipient_email"] == "bob@example.com"
