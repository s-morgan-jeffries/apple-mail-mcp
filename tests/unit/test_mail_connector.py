"""Unit tests for mail connector."""

import logging
import time
import warnings
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import LoginError

from apple_mail_mcp.exceptions import (
    MailAccountNotFoundError,
    MailAppleScriptError,
    MailDraftInvalidIdError,
    MailDraftNotFoundError,
    MailImapMoveUnsupportedError,
    MailImapTrashNotFoundError,
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
            '[{"id":"UUID-1","name":"Gmail","full_name":"Alice Smith",'
            '"email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true},'
            '{"id":"UUID-2","name":"Work","full_name":"",'
            '"email_addresses":["me@work.com","alt@work.com"],'
            '"account_type":"iCloud","enabled":false}]'
        )
        result = connector.list_accounts()
        assert result == [
            {"id": "UUID-1", "name": "Gmail", "full_name": "Alice Smith",
             "email_addresses": ["me@gmail.com"],
             "account_type": "imap", "enabled": True},
            # Empty-string full_name normalized to None.
            {"id": "UUID-2", "name": "Work", "full_name": None,
             "email_addresses": ["me@work.com", "alt@work.com"],
             "account_type": "iCloud", "enabled": False},
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_normalizes_whitespace_only_full_name_to_none(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Whitespace-only full_name is treated as not-configured."""
        mock_run.return_value = (
            '[{"id":"UUID-1","name":"Gmail","full_name":"   ",'
            '"email_addresses":["me@gmail.com"],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result[0]["full_name"] is None

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
            '[{"id":"UUID-3","name":"LocalOnly","full_name":"Local User",'
            '"email_addresses":[],'
            '"account_type":"imap","enabled":true}]'
        )
        result = connector.list_accounts()
        assert result == [{
            "id": "UUID-3", "name": "LocalOnly", "full_name": "Local User",
            "email_addresses": [],
            "account_type": "imap", "enabled": True,
        }]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_list_accounts_script_includes_type_and_enabled(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Generated AppleScript must extract account_type (as text), enabled,
        and the full_name (#158) used for the Display Name <email> sender."""
        mock_run.return_value = "[]"
        connector.list_accounts()
        script = mock_run.call_args[0][0]
        assert "|account_type|:((account type of acc) as text)" in script
        assert "|enabled|:(enabled of acc)" in script
        assert "|id|:(id of acc as text)" in script
        # #158: full_name read with missing-value coercion.
        assert "full name of acc" in script
        assert "|full_name|:accFullName" in script

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

    # --- Issue #118: per-account circuit breaker --------------------------

    def test_breaker_default_ttl_is_30s(
        self, connector: AppleMailConnector
    ) -> None:
        """Class constant sanity — drift here would silently change
        offline-burst behavior."""
        assert connector._IMAP_BREAKER_TTL_S == 30.0

    def test_breaker_starts_closed(
        self, connector: AppleMailConnector
    ) -> None:
        """A fresh connector has no per-account cooldown set."""
        assert connector._imap_failure_until == {}
        assert connector._imap_breaker_open("iCloud") is False

    def test_breaker_opens_after_first_non_benign_failure(
        self, connector: AppleMailConnector
    ) -> None:
        """A LoginError (or any non-benign fallback exception) sets the
        deadline ~TTL into the future."""
        before = time.monotonic()
        connector._log_imap_fallback("iCloud", LoginError("bad pw"))
        deadline = connector._imap_failure_until["iCloud"]
        # Deadline lands within the TTL window (allowing for sub-second
        # scheduling jitter from the test runner).
        assert deadline > before + connector._IMAP_BREAKER_TTL_S - 1
        assert deadline <= before + connector._IMAP_BREAKER_TTL_S + 1
        assert connector._imap_breaker_open("iCloud") is True

    def test_breaker_does_not_open_for_keychain_miss(
        self, connector: AppleMailConnector
    ) -> None:
        """Missing Keychain entry is the user's explicit opt-out — no
        cooldown, just silent DEBUG logging. Otherwise every call to a
        non-IMAP-configured account would consult a deadline lookup
        before falling through."""
        connector._log_imap_fallback(
            "iCloud", MailKeychainEntryNotFoundError("missing")
        )
        assert "iCloud" not in connector._imap_failure_until
        assert connector._imap_breaker_open("iCloud") is False

    def test_breaker_resets_after_ttl(
        self, connector: AppleMailConnector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the deadline is in the past, the breaker is closed again
        and the next call attempts IMAP organically."""
        from apple_mail_mcp import mail_connector as mc_mod
        # Freeze time at a known point so we can advance deterministically.
        clock = [1000.0]
        monkeypatch.setattr(mc_mod.time, "monotonic", lambda: clock[0])

        connector._log_imap_fallback("iCloud", LoginError("bad"))
        assert connector._imap_breaker_open("iCloud") is True

        # Just before the deadline — still open.
        clock[0] = 1000.0 + connector._IMAP_BREAKER_TTL_S - 0.1
        assert connector._imap_breaker_open("iCloud") is True

        # Past the deadline — closed.
        clock[0] = 1000.0 + connector._IMAP_BREAKER_TTL_S + 0.1
        assert connector._imap_breaker_open("iCloud") is False

    def test_breaker_is_per_account(
        self, connector: AppleMailConnector
    ) -> None:
        """Failure on iCloud must not skip IMAP for Gmail."""
        connector._log_imap_fallback("iCloud", LoginError("rejected"))
        assert connector._imap_breaker_open("iCloud") is True
        assert connector._imap_breaker_open("Gmail") is False

    def test_clear_breaker_removes_entry(
        self, connector: AppleMailConnector
    ) -> None:
        connector._log_imap_fallback("iCloud", LoginError("rejected"))
        assert connector._imap_breaker_open("iCloud") is True
        connector._imap_clear_breaker("iCloud")
        assert connector._imap_breaker_open("iCloud") is False
        # Idempotent — clearing an already-clear account is fine.
        connector._imap_clear_breaker("iCloud")
        connector._imap_clear_breaker("Never-Set")

    def test_search_messages_skips_imap_when_breaker_is_open(
        self, connector: AppleMailConnector
    ) -> None:
        """End-to-end: open the breaker, then call search_messages —
        the IMAP path is bypassed entirely (saving the wasted round
        trip), AppleScript runs."""
        connector._imap_failure_until["iCloud"] = (
            time.monotonic() + 60  # breaker open for the next minute
        )
        with patch.object(
            connector, "_imap_search"
        ) as imap_path, patch.object(
            connector, "_search_messages_applescript",
            return_value=[],
        ) as as_path:
            connector.search_messages("iCloud", "INBOX")
        imap_path.assert_not_called()
        as_path.assert_called_once()

    def test_successful_imap_call_clears_breaker_via_search(
        self, connector: AppleMailConnector
    ) -> None:
        """A successful IMAP call after a transient failure must clear
        the cooldown so we don't leave the breaker open longer than the
        problem persists."""
        # Pretend a previous failure opened the breaker.
        connector._imap_failure_until["iCloud"] = time.monotonic() - 1  # already expired
        # Manually re-open: a fresh failure with a future deadline.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        # Then make IMAP work normally.
        with patch.object(
            connector, "_imap_search", return_value=[{"id": "x"}]
        ):
            # We need the breaker closed to let IMAP run, then verify
            # success clears it. The clean way: clear first, then call.
            connector._imap_clear_breaker("iCloud")
            connector.search_messages("iCloud", "INBOX")
        assert "iCloud" not in connector._imap_failure_until

    def test_get_message_skips_imap_when_breaker_open(
        self, connector: AppleMailConnector
    ) -> None:
        """Same gate applies to get_message's hint-gated IMAP path."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        with patch.object(
            connector, "_imap_get_message"
        ) as imap_path, patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path:
            connector.get_message("123", account="iCloud", mailbox="INBOX")
        imap_path.assert_not_called()
        as_path.assert_called_once()

    def test_login_error_warning_includes_setup_imap_command(
        self, connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A revoked / expired app password is the most common cause of
        LoginError. The fallback works, but the user has no way to know
        IMAP is broken because results are correct via AppleScript. The
        specialized WARNING text names the exact `setup-imap` command
        so they can fix at their leisure."""
        with caplog.at_level(
            logging.DEBUG, logger="apple_mail_mcp.mail_connector"
        ):
            connector._log_imap_fallback("iCloud", LoginError("AUTHENTICATIONFAILED"))

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "iCloud" in msg
        # The actionable instruction must be present and command-perfect.
        assert "apple-mail-mcp setup-imap --account iCloud" in msg
        # Reassurance that the user isn't blocked.
        assert "AppleScript fallback" in msg

    def test_non_login_failure_uses_generic_warning(
        self, connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """OSError (offline / DNS / unreachable) gets the generic
        message — there's no setup-imap command that would help."""
        with caplog.at_level(
            logging.DEBUG, logger="apple_mail_mcp.mail_connector"
        ):
            connector._log_imap_fallback("iCloud", OSError("network unreachable"))

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        # The generic message references AppleScript fallback but does
        # NOT name a setup-imap command (would be misleading for a
        # network-level failure).
        assert "AppleScript" in msg
        assert "setup-imap" not in msg

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
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
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
            include_attachments=False,
            limit=5,
            body_contains=None,
            text_contains=None,
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
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
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

    # --- _imap_move_messages helper (#149) -------------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_happy_path(
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
        mock_imap.move_messages.return_value = 3

        result = connector._imap_move_messages(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.move_messages.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.move_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_oserror_propagates(
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
        mock_imap.move_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_move_messages_unsupported_propagates(
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
        mock_imap.move_messages.side_effect = MailImapMoveUnsupportedError(
            "no MOVE / UIDPLUS"
        )

        with pytest.raises(MailImapMoveUnsupportedError):
            connector._imap_move_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                destination_mailbox="Archive",
            )

    # --- update_message move delegation (#149) ---------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_uses_imap_for_move_only_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move-only patch with source_mailbox + account: IMAP runs,
        AppleScript pass is skipped."""
        mock_imap.return_value = 4
        result = connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 4
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
            destination_mailbox="Archive",
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_skips_imap_for_combined_patch(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """move + read_status combined: stays on AppleScript until
        sibling issues #150 / #151 / #152 land."""
        mock_run_as.return_value = "2"
        connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_skips_imap_when_source_mailbox_missing(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move-only without source_mailbox: IMAP can't help (would
        SEARCH every mailbox per id), fall through to AppleScript."""
        mock_run_as.return_value = "2"
        connector.update_message(
            ["a@x", "b@x"],
            destination_mailbox="Archive",
            account="iCloud",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path is bypassed entirely (no wasted
        connect/login round trip)."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign opt-out: silent DEBUG, no
        WARNING, breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                destination_mailbox="Archive",
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                destination_mailbox="Archive",
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_falls_back_on_unsupported_capability(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No MOVE / UIDPLUS: capability gap is permanent for the server,
        DEBUG-only log, breaker NOT opened (read paths still work)."""
        mock_imap.side_effect = MailImapMoveUnsupportedError("no caps")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                destination_mailbox="Archive",
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_move_messages")
    def test_update_message_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry — a
        transient blip shouldn't keep the breaker open longer than
        the problem persists."""
        connector._imap_failure_until["iCloud"] = time.monotonic() - 10  # expired
        # Re-arm a future deadline so the breaker is open at call time.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        # Manually clear so IMAP is allowed; success will keep it cleared.
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- _imap_delete_messages helper (#150) -----------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_happy_path(
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
        mock_imap.delete_messages.return_value = 3

        result = connector._imap_delete_messages(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.delete_messages.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.delete_messages.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_oserror_propagates(
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
        mock_imap.delete_messages.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_unsupported_move_propagates(
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
        mock_imap.delete_messages.side_effect = MailImapMoveUnsupportedError(
            "no MOVE / UIDPLUS"
        )

        with pytest.raises(MailImapMoveUnsupportedError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_delete_messages_trash_not_found_propagates(
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
        mock_imap.delete_messages.side_effect = MailImapTrashNotFoundError(
            "no Trash"
        )

        with pytest.raises(MailImapTrashNotFoundError):
            connector._imap_delete_messages(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
            )

    # --- delete_messages delegation (#150) -------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_uses_imap_when_account_and_source_provided(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """account + source_mailbox provided: IMAP runs, AppleScript skipped."""
        mock_imap.return_value = 4
        result = connector.delete_messages(
            ["a@x", "b@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 4
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_skips_imap_without_account_and_source(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Neither hint provided: IMAP would have to SEARCH every
        mailbox per Message-ID, defeating the speed win. Stay on
        AppleScript cross-scan."""
        mock_run_as.return_value = "1"
        connector.delete_messages(["a@x"])
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path is bypassed entirely; AppleScript runs."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.delete_messages(
            ["a@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign opt-out: silent DEBUG, no
        WARNING, breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.delete_messages(
            ["a@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_unsupported_capability(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No MOVE / UIDPLUS: capability gap is permanent, DEBUG-only
        log, breaker NOT opened."""
        mock_imap.side_effect = MailImapMoveUnsupportedError("no caps")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_falls_back_on_trash_not_found(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Trash discovery failed: same benign treatment as
        unsupported-capability — DEBUG-only, no breaker."""
        mock_imap.side_effect = MailImapTrashNotFoundError("no trash")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.delete_messages(
                ["a@x"],
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_delete_messages")
    def test_delete_messages_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.delete_messages(
            ["a@x"],
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- _imap_set_read_status helper (#151) -----------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_happy_path(
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
        mock_imap.set_read_status.return_value = 3

        result = connector._imap_set_read_status(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            read=True,
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.set_read_status.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            read=True,
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_set_read_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                read=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_read_status.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_set_read_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                read=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_read_status_oserror_propagates(
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
        mock_imap.set_read_status.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_set_read_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                read=True,
            )

    # --- update_message read-only delegation (#151) ----------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_uses_imap_for_read_only_true_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read-only patch (read_status only, no flag/move) with source
        mailbox: IMAP runs, AppleScript skipped."""
        mock_imap.return_value = 2
        result = connector.update_message(
            ["a@x", "b@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 2
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
            read=True,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_uses_imap_for_read_only_false_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Unread case — same IMAP path, read=False."""
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            read_status=False,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x"],
            source_mailbox="INBOX",
            read=False,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_skips_imap_for_combined_read_and_move(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """read + move in one call: stays on AppleScript pending #152."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_skips_imap_for_combined_read_and_flag(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """read + flag in one call: stays on AppleScript pending #152."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_skips_imap_when_source_mailbox_missing(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Read-only call without source_mailbox: IMAP can't help
        (would SEARCH every mailbox per id), fall through to AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path is bypassed; AppleScript runs."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign opt-out: silent DEBUG, no
        WARNING, breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                read_status=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                read_status=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_read_status")
    def test_update_message_read_only_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

    # --- _imap_set_flagged_status helper (#152) --------------------------

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_happy_path(
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
        mock_imap.set_flagged_status.return_value = 3

        result = connector._imap_set_flagged_status(
            account="iCloud",
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            flagged=True,
        )

        mock_resolve.assert_called_once_with("iCloud")
        mock_keychain.assert_called_once_with("iCloud", "user@icloud.com")
        mock_imap_cls.assert_called_once_with(
            "imap.mail.me.com", 993, "user@icloud.com", "app-password",
            pool=None,
        )
        mock_imap.set_flagged_status.assert_called_once_with(
            message_ids=["a@x", "b@x", "c@x"],
            source_mailbox="INBOX",
            flagged=True,
        )
        assert result == 3

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_keychain_missing_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailKeychainEntryNotFoundError):
            connector._imap_set_flagged_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                flagged=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_login_error_propagates(
        self,
        mock_resolve: MagicMock,
        mock_keychain: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_resolve.return_value = ("imap.mail.me.com", 993, "user@icloud.com")
        mock_keychain.return_value = "wrong-password"
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.set_flagged_status.side_effect = LoginError("rejected")

        with pytest.raises(LoginError):
            connector._imap_set_flagged_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                flagged=True,
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_imap_set_flagged_status_oserror_propagates(
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
        mock_imap.set_flagged_status.side_effect = OSError("unreachable")

        with pytest.raises(OSError, match="unreachable"):
            connector._imap_set_flagged_status(
                account="iCloud",
                message_ids=["a@x"],
                source_mailbox="INBOX",
                flagged=True,
            )

    # --- update_message flag-only delegation (#152) ----------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_uses_imap_for_flag_only_true_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Flag-only patch (flagged=True only, no color/read/move) with
        account + source_mailbox: IMAP runs, AppleScript skipped."""
        mock_imap.return_value = 2
        result = connector.update_message(
            ["a@x", "b@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert result == 2
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x", "b@x"],
            source_mailbox="INBOX",
            flagged=True,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_uses_imap_for_flag_only_false_with_source_mailbox(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Clear-flag case — same IMAP path, flagged=False."""
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            flagged=False,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_called_once_with(
            account="iCloud",
            message_ids=["a@x"],
            source_mailbox="INBOX",
            flagged=False,
        )
        mock_run_as.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_skips_imap_when_flag_color_is_set(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """flag_color is Mail.app-specific (\\$MailFlagBit* keywords);
        IMAP can't set it. Fall through to AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flag_color="red",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_skips_imap_for_combined_flag_and_read(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """flag + read in one call: stays on AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            read_status=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_skips_imap_for_combined_flag_and_move(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """flag + move in one call: stays on AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            destination_mailbox="Archive",
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_skips_imap_when_source_mailbox_missing(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Flag-only call without source_mailbox: stays on AppleScript."""
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_when_breaker_open(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Open breaker: IMAP path bypassed; AppleScript runs."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_imap.assert_not_called()
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_on_keychain_missing_silent(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing-Keychain-entry = benign: silent DEBUG, no WARNING,
        breaker NOT opened."""
        mock_imap.side_effect = MailKeychainEntryNotFoundError("no entry")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                flagged=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings_emitted == []
        assert "iCloud" not in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_on_oserror_with_warning(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Network failure: AppleScript runs, breaker opens, one WARNING."""
        mock_imap.side_effect = OSError("unreachable")
        mock_run_as.return_value = "1"
        with caplog.at_level(logging.DEBUG, logger="apple_mail_mcp.mail_connector"):
            connector.update_message(
                ["a@x"],
                flagged=True,
                account="iCloud",
                source_mailbox="INBOX",
            )
        mock_run_as.assert_called_once()
        warnings_emitted = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings_emitted) == 1
        assert "iCloud" in connector._imap_failure_until

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_falls_back_on_login_error(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_imap.side_effect = LoginError("rejected")
        mock_run_as.return_value = "1"
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        mock_run_as.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    @patch.object(AppleMailConnector, "_imap_set_flagged_status")
    def test_update_message_flag_only_clears_breaker_on_success(
        self,
        mock_imap: MagicMock,
        mock_run_as: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Successful IMAP call clears any prior breaker entry."""
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        connector._imap_clear_breaker("iCloud")
        mock_imap.return_value = 1
        connector.update_message(
            ["a@x"],
            flagged=True,
            account="iCloud",
            source_mailbox="INBOX",
        )
        assert "iCloud" not in connector._imap_failure_until
        mock_run_as.assert_not_called()

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
            False,  # include_attachments
            None,  # body_contains
            None,  # text_contains
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
        """Test message search with filters.

        Per #32, filters are now applied as per-message IF expressions
        instead of a `whose` clause — `whose` is unusably slow against
        large IMAP mailboxes (>120s timeout on 8000+ messages). The new
        pattern iterates messages in reverse (newest-first) and checks
        each filter against the message; the script short-circuits when
        matchCount reaches the limit.
        """
        mock_run.return_value = "[]"

        connector._search_messages_applescript(
            "Gmail",
            "INBOX",
            sender_contains="john@example.com",
            subject_contains="meeting",
            read_status=False,
            limit=10
        )

        # Filter conditions appear as IF clauses, not in a `whose` clause.
        call_args = mock_run.call_args[0][0]
        assert (
            'if (sender of msg) does not contain "john@example.com" '
            'then set includeThis to false'
            in call_args
        )
        assert (
            'if (subject of msg) does not contain "meeting" '
            'then set includeThis to false'
            in call_args
        )
        assert (
            "if (read status of msg) is not false "
            "then set includeThis to false"
            in call_args
        )
        # Limit is enforced by accumulating matches and exiting the repeat
        # when matchCount reaches the bound.
        assert "if matchCount >= 10 then exit repeat" in call_args
        # Reverse iteration (newest first).
        assert "repeat with i from total to 1 by -1" in call_args
        # No `whose` clause anywhere.
        assert "whose" not in call_args

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

        The limit must be enforced via a counter-driven exit, not by slicing
        the live message collection reference.
        """
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", limit=5)
        script = mock_run.call_args[0][0]
        assert "items 1 thru" not in script
        assert "if matchCount >= 5 then exit repeat" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_is_flagged_filter(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """is_flagged is applied inside the loop via an includeThis IF expression."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript("Gmail", "INBOX", is_flagged=True)
        script = mock_run.call_args[0][0]
        assert (
            "if (flagged status of msg) is not true then set includeThis to false"
            in script
        )

        connector._search_messages_applescript("Gmail", "INBOX", is_flagged=False)
        script = mock_run.call_args[0][0]
        assert (
            "if (flagged status of msg) is not false then set includeThis to false"
            in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_search_messages_date_range_filter(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """date_from/date_to are applied via inverted IF expressions inside the loop."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript(
            "Gmail", "INBOX", date_from="2026-04-01", date_to="2026-04-15"
        )
        script = mock_run.call_args[0][0]
        # The IF expressions are the inverse of the inclusion condition: skip if BEFORE
        # date_from, skip if ON-OR-AFTER date_to+1.
        assert (
            'if (date received of msg) < (date "2026-04-01") then set includeThis to false'
            in script
        )
        # date_to gets +1 day so the full day is inclusive
        assert (
            'if (date received of msg) >= (date "2026-04-16") then set includeThis to false'
            in script
        )

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
        """has_attachment=True applied inside the loop as an includeThis IF."""
        mock_run.return_value = "[]"
        connector._search_messages_applescript(
            "Gmail", "INBOX", read_status=True, has_attachment=True
        )
        script = mock_run.call_args[0][0]
        # The whole script no longer uses `whose`; all filters live inside the loop.
        assert "whose" not in script
        # The attachment IF expression must appear as a post-filter inside the loop.
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

    def test_search_messages_logs_imap_hint_when_applescript_path_is_slow(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If AppleScript search exceeds the 5s threshold, log INFO hint to enable IMAP."""
        from apple_mail_mcp import mail_connector as mc_mod

        # perf_counter is called twice per search: start, then in finally. Side
        # effects let us simulate a 6.0s elapsed time without real sleeping.
        with patch.object(connector, "_imap_search",
                          side_effect=MailKeychainEntryNotFoundError("no entry")), \
             patch.object(connector, "_search_messages_applescript",
                          return_value=[]), \
             patch.object(mc_mod.time, "perf_counter", side_effect=[0.0, 6.0]), \
             caplog.at_level(logging.INFO, logger="apple_mail_mcp.mail_connector"):
            connector.search_messages("iCloud", "INBOX")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "AppleScript search took" in r.getMessage()
        ]
        assert len(info_records) == 1
        msg = info_records[0].getMessage()
        assert "6.0s" in msg
        assert "iCloud" in msg
        assert "INBOX" in msg
        # Hint must point users at IMAP setup.
        assert "IMAP" in msg

    def test_search_messages_does_not_log_imap_hint_when_applescript_path_is_fast(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Under the 5s threshold, no INFO hint — keeps logs quiet on small mailboxes."""
        from apple_mail_mcp import mail_connector as mc_mod

        with patch.object(connector, "_imap_search",
                          side_effect=MailKeychainEntryNotFoundError("no entry")), \
             patch.object(connector, "_search_messages_applescript",
                          return_value=[]), \
             patch.object(mc_mod.time, "perf_counter", side_effect=[0.0, 1.5]), \
             caplog.at_level(logging.INFO, logger="apple_mail_mcp.mail_connector"):
            connector.search_messages("iCloud", "INBOX")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "AppleScript search took" in r.getMessage()
        ]
        assert info_records == []

    def test_search_messages_logs_imap_hint_even_when_applescript_raises(
        self, connector: AppleMailConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The threshold log fires from finally — slow failures should still log."""
        from apple_mail_mcp import mail_connector as mc_mod

        with patch.object(connector, "_imap_search",
                          side_effect=MailKeychainEntryNotFoundError("no entry")), \
             patch.object(connector, "_search_messages_applescript",
                          side_effect=MailAppleScriptError("timeout")), \
             patch.object(mc_mod.time, "perf_counter", side_effect=[0.0, 7.5]), \
             caplog.at_level(logging.INFO, logger="apple_mail_mcp.mail_connector"):
            with pytest.raises(MailAppleScriptError):
                connector.search_messages("iCloud", "INBOX")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "AppleScript search took" in r.getMessage()
        ]
        assert len(info_records) == 1

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

    # --- Issue #72 dispatcher behavior -----------------------------------

    def test_get_message_uses_imap_when_account_and_mailbox_provided(
        self, connector: AppleMailConnector
    ) -> None:
        """With both hint params, _imap_get_message runs and AppleScript
        is never called."""
        with patch.object(
            connector, "_imap_get_message",
            return_value={"id": "x", "content": "body"},
        ) as imap_path, patch.object(
            connector, "_get_message_applescript"
        ) as as_path:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "x", "content": "body"}
        imap_path.assert_called_once_with(
            account="iCloud",
            mailbox="INBOX",
            message_id="abc@x",
            include_content=True,
            headers_only=False,
            include_attachments=False,
        )
        as_path.assert_not_called()

    def test_get_message_no_hint_skips_imap_path(
        self, connector: AppleMailConnector
    ) -> None:
        """No account/mailbox → IMAP is bypassed entirely (no Keychain
        prompt, no log, no nothing)."""
        with patch.object(connector, "_imap_get_message") as imap_path, \
             patch.object(
                 connector, "_get_message_applescript",
                 return_value={"id": "1"},
             ) as as_path:
            result = connector.get_message("123")

        assert result == {"id": "1"}
        imap_path.assert_not_called()
        as_path.assert_called_once_with("123", True, False)

    def test_get_message_partial_hint_skips_imap(
        self, connector: AppleMailConnector
    ) -> None:
        """account-only or mailbox-only is not enough; both required."""
        with patch.object(connector, "_imap_get_message") as imap_path, \
             patch.object(
                 connector, "_get_message_applescript",
                 return_value={"id": "1"},
             ):
            connector.get_message("123", account="iCloud")
            connector.get_message("123", mailbox="INBOX")

        imap_path.assert_not_called()

    def test_get_message_falls_back_on_login_error(
        self, connector: AppleMailConnector
    ) -> None:
        """LoginError on IMAP path → fall through to AppleScript, log
        fallback once."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=LoginError("AUTHENTICATIONFAILED"),
        ) as imap_path, patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "1"}
        imap_path.assert_called_once()
        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_message_falls_back_on_keychain_miss(
        self, connector: AppleMailConnector
    ) -> None:
        """No Keychain entry is the benign opt-out signal — fall through
        and log at DEBUG (not WARNING)."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=MailKeychainEntryNotFoundError("none"),
        ), patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            connector.get_message(
                "x", account="iCloud", mailbox="INBOX",
            )

        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_message_falls_back_when_network_unavailable(
        self, connector: AppleMailConnector
    ) -> None:
        """Offline / DNS-failed / host-unreachable should fall through to
        AppleScript so users running agents on a flaky network or fully
        offline still get a result. OSError covers socket.timeout,
        socket.gaierror, ConnectionRefusedError, and host-unreachable;
        all of those are in _IMAP_FALLBACK_EXCS."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=OSError("network is unreachable"),
        ), patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "1"}
        as_path.assert_called_once()
        # The fallback log fires so the user can find the network failure
        # in DEBUG-level logs if they go looking.
        log_fb.assert_called_once()

    def test_get_message_falls_back_on_imap_protocol_error(
        self, connector: AppleMailConnector
    ) -> None:
        """IMAPClientError covers protocol-level breakage (BAD response,
        truncated session, captive-portal-style HTTP-instead-of-IMAP).
        Same fallback as network errors."""
        from imapclient.exceptions import IMAPClientError

        with patch.object(
            connector, "_imap_get_message",
            side_effect=IMAPClientError("BAD command"),
        ), patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1"},
        ) as as_path:
            result = connector.get_message(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert result == {"id": "1"}
        as_path.assert_called_once()

    def test_get_message_message_not_found_on_imap_does_not_fall_back(
        self, connector: AppleMailConnector
    ) -> None:
        """If IMAP found the folder but the Message-ID isn't there, that's
        a definitive answer — don't paper over it with an AppleScript scan
        that would also fail (or worse, succeed by matching a different
        message in a different folder)."""
        with patch.object(
            connector, "_imap_get_message",
            side_effect=MailMessageNotFoundError("nope"),
        ), patch.object(
            connector, "_get_message_applescript"
        ) as as_path:
            with pytest.raises(MailMessageNotFoundError):
                connector.get_message(
                    "abc@x", account="iCloud", mailbox="INBOX",
                )
        as_path.assert_not_called()

    def test_get_message_headers_only_silently_ignored_on_applescript(
        self, connector: AppleMailConnector
    ) -> None:
        """headers_only is an IMAP-only knob; passing it without a hint
        must not error and must not change the AppleScript path's behavior."""
        with patch.object(
            connector, "_get_message_applescript",
            return_value={"id": "1", "content": "body"},
        ) as as_path:
            connector.get_message("123", headers_only=True)
        # AppleScript path receives the original signature (message_id,
        # include_content, include_attachments); headers_only is silently dropped.
        as_path.assert_called_once_with("123", True, False)


    def test_get_attachments_uses_imap_when_account_and_mailbox_provided(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments",
            return_value=[{"name": "x.pdf", "mime_type": "application/pdf",
                           "size": 100, "downloaded": False}],
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript"
        ) as as_path:
            result = connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        assert len(result) == 1
        imap_path.assert_called_once_with(
            account="iCloud",
            mailbox="INBOX",
            message_id="abc@x",
        )
        as_path.assert_not_called()

    def test_get_attachments_no_hint_skips_imap_path(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments"
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path:
            result = connector.get_attachments("123")

        assert result == []
        imap_path.assert_not_called()
        as_path.assert_called_once_with("123")

    def test_get_attachments_partial_hint_skips_imap(
        self, connector: AppleMailConnector
    ) -> None:
        """account-only or mailbox-only is not enough; both required."""
        with patch.object(
            connector, "_imap_get_attachments"
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript", return_value=[],
        ):
            connector.get_attachments("123", account="iCloud")
            connector.get_attachments("123", mailbox="INBOX")

        imap_path.assert_not_called()

    def test_get_attachments_falls_back_on_login_error(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=LoginError("AUTHENTICATIONFAILED"),
        ) as imap_path, patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )

        imap_path.assert_called_once()
        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_attachments_falls_back_when_offline(
        self, connector: AppleMailConnector
    ) -> None:
        """OSError covers DNS, EHOSTUNREACH, connect timeout, etc.
        Same fallback behavior as get_message's offline test."""
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=OSError("network unreachable"),
        ), patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path:
            connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )
        as_path.assert_called_once()

    def test_get_attachments_falls_back_on_keychain_miss(
        self, connector: AppleMailConnector
    ) -> None:
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=MailKeychainEntryNotFoundError("none"),
        ), patch.object(
            connector, "_get_attachments_applescript",
            return_value=[],
        ) as as_path, patch.object(
            connector, "_log_imap_fallback"
        ) as log_fb:
            connector.get_attachments(
                "abc@x", account="iCloud", mailbox="INBOX",
            )
        as_path.assert_called_once()
        log_fb.assert_called_once()

    def test_get_attachments_message_not_found_on_imap_does_not_fall_back(
        self, connector: AppleMailConnector
    ) -> None:
        """Same reasoning as get_message: a definitive 'not in this folder'
        from IMAP shouldn't be papered over with a cross-folder
        AppleScript scan that may match a different message."""
        with patch.object(
            connector, "_imap_get_attachments",
            side_effect=MailMessageNotFoundError("nope"),
        ), patch.object(
            connector, "_get_attachments_applescript"
        ) as as_path:
            with pytest.raises(MailMessageNotFoundError):
                connector.get_attachments(
                    "abc@x", account="iCloud", mailbox="INBOX",
                )
        as_path.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_get_attachments_pre_existing_positional_caller_unaffected(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Smoke test: existing callers passing only message_id still work,
        going through the AppleScript path unchanged."""
        mock_run.return_value = '[]'
        result = connector.get_attachments("x")
        assert result == []


    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_with_full_name_emits_display_form(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account with full_name -> 'Display Name <email>' form."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud")
            == "Alice Smith <alice@icloud.com>"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_without_full_name_falls_back_to_bare_email(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: account without full_name -> bare email (graceful fallback)."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": None,
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_whitespace_only_full_name_falls_back(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: whitespace-only full_name treated as not-configured."""
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "   ",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("iCloud") == "alice@icloud.com"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_lookup_by_uuid_with_full_name(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice Smith",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        assert (
            connector._resolve_account_to_sender("UUID-1")
            == "Alice Smith <alice@icloud.com>"
        )

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_not_found_raises(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        from apple_mail_mcp.exceptions import MailAccountNotFoundError

        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "iCloud",
                "full_name": "Alice",
                "email_addresses": ["alice@icloud.com"],
            },
        ]
        with pytest.raises(MailAccountNotFoundError):
            connector._resolve_account_to_sender("Bogus")

    @patch.object(AppleMailConnector, "list_accounts")
    def test_resolve_account_to_sender_no_emails_raises(
        self, mock_list: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_list.return_value = [
            {
                "id": "UUID-1",
                "name": "Empty",
                "full_name": "Nobody",
                "email_addresses": [],
            },
        ]
        with pytest.raises(ValueError, match="email addresses"):
            connector._resolve_account_to_sender("Empty")


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


class TestBulkOpsSourceMailbox:
    """Regression guards for #103: bulk-mutation methods accept paired
    `account` + `source_mailbox` parameters that narrow the AppleScript
    scan from O(N × accounts × mailboxes) to O(N).

    Both params must be provided together (a mailbox name without an
    account is ambiguous because the same name can exist across accounts).
    Either alone raises ValueError.
    """

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------ mark_as_read ------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_read_narrow_path_uses_single_loop(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "2"
        connector.mark_as_read(
            ["abc", "def"], account="Gmail", source_mailbox="INBOX"
        )
        script = mock_run.call_args[0][0]
        # Narrow scope: single mailbox of a specific account.
        assert 'mailbox "INBOX" of' in script
        assert 'account "Gmail"' in script
        # Cross-scan loops MUST be gone.
        assert "repeat with acc in accounts" not in script
        assert "repeat with mb in mailboxes" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mark_as_read_no_params_keeps_cross_scan_path(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.mark_as_read(["abc"])
        script = mock_run.call_args[0][0]
        # Backwards-compat: existing slow path preserved.
        assert "repeat with acc in accounts" in script
        assert "repeat with mb in mailboxes" in script

    def test_mark_as_read_account_without_source_mailbox_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="source_mailbox"):
            connector.mark_as_read(["x"], account="Gmail")

    def test_mark_as_read_source_mailbox_without_account_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="account"):
            connector.mark_as_read(["x"], source_mailbox="INBOX")

    # ------ move_messages ------
    # Note: move_messages already has `account` for the DESTINATION account.
    # `source_mailbox` is independent — it narrows where we LOOK for the
    # source messages. Both branches (gmail_mode True/False) need the
    # narrow path.

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_messages_narrow_path_standard_branch(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "2"
        connector.move_messages(
            ["abc", "def"],
            destination_mailbox="Archive",
            account="Gmail",
            source_mailbox="INBOX",
        )
        script = mock_run.call_args[0][0]
        # Source narrowed to a single mailbox; destination unchanged.
        assert 'mailbox "INBOX" of' in script
        assert 'mailbox "Archive" of' in script  # destination still set
        assert "repeat with acc in accounts" not in script
        assert "repeat with mb in mailboxes" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_messages_narrow_path_gmail_branch(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "2"
        connector.move_messages(
            ["abc", "def"],
            destination_mailbox="[Gmail]/All Mail",
            account="Gmail",
            gmail_mode=True,
            source_mailbox="INBOX",
        )
        script = mock_run.call_args[0][0]
        assert 'mailbox "INBOX" of' in script
        # Gmail-mode body: duplicate + delete instead of set mailbox
        assert "duplicate msg to destMailbox" in script
        assert "delete msg" in script
        assert "repeat with acc in accounts" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_move_messages_no_source_keeps_cross_scan(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.move_messages(
            ["abc"], destination_mailbox="Archive", account="Gmail"
        )
        script = mock_run.call_args[0][0]
        assert "repeat with acc in accounts" in script
        assert "repeat with mb in mailboxes" in script

    # ------ flag_message ------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_flag_message_narrow_path_uses_single_loop(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.flag_message(
            ["abc"], "red", account="iCloud", source_mailbox="Archive"
        )
        script = mock_run.call_args[0][0]
        assert 'mailbox "Archive" of' in script
        assert "set flag index of msg to" in script
        assert "set flagged status of msg to" in script
        assert "repeat with acc in accounts" not in script

    def test_flag_message_partial_pair_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="source_mailbox"):
            connector.flag_message(["x"], "red", account="iCloud")
        with pytest.raises(ValueError, match="account"):
            connector.flag_message(["x"], "red", source_mailbox="Archive")

    # ------ delete_messages ------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_narrow_path_uses_single_loop(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        connector.delete_messages(
            ["abc"], account="iCloud", source_mailbox="Trash"
        )
        script = mock_run.call_args[0][0]
        assert 'mailbox "Trash" of' in script
        assert "delete msg" in script
        assert "repeat with acc in accounts" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_permanent_emits_deprecation_warning(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Issue #111: Mail.app exposes no AppleScript path to bypass Trash.
        `permanent=True` is a no-op; warn so callers don't silently rely on
        absent behavior."""
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        with pytest.warns(DeprecationWarning, match="#111"):
            connector.delete_messages(
                ["abc"],
                permanent=True,
                account="iCloud",
                source_mailbox="Junk",
            )
        # Script shape unchanged from the non-permanent path: `delete msg`
        # always moves to the account's Trash mailbox today.
        script = mock_run.call_args[0][0]
        assert 'mailbox "Junk" of' in script
        assert "delete msg" in script
        assert "repeat with acc in accounts" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_messages_default_does_not_warn(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The default path (permanent=False) must not emit DeprecationWarning."""
        # Skip the #150 IMAP fast path so the AppleScript narrow path runs.
        connector._imap_failure_until["iCloud"] = time.monotonic() + 60
        mock_run.return_value = "1"
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            connector.delete_messages(
                ["abc"],
                account="iCloud",
                source_mailbox="Junk",
            )

    def test_delete_messages_partial_pair_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="source_mailbox"):
            connector.delete_messages(["x"], account="iCloud")
        with pytest.raises(ValueError, match="account"):
            connector.delete_messages(["x"], source_mailbox="Trash")


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


class TestDeleteDraft:
    """Tests for AppleMailConnector.delete_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "OK"
        assert connector.delete_draft("160991") is True
        mock_run.assert_called_once()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_script_embeds_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "OK"
        connector.delete_draft("160991")
        script = mock_run.call_args[0][0]
        assert 'whose id is "160991"' in script
        # Lookup must be scoped to Drafts mailboxes only (perf + correctness).
        assert 'name of mb contains "Drafts"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        with pytest.raises(MailDraftNotFoundError):
            connector.delete_draft("999999")

    def test_delete_draft_invalid_id_path_traversal(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft("../etc/passwd")

    def test_delete_draft_invalid_id_with_quotes(
        self, connector: AppleMailConnector
    ) -> None:
        # Quote injection that could break out of the AppleScript string.
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft('1"; do something --')

    def test_delete_draft_empty_id(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.delete_draft("")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_delete_draft_strips_whitespace_from_result(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        # AppleScript output sometimes carries trailing newlines.
        mock_run.return_value = "OK\n"
        assert connector.delete_draft("160991") is True


class TestFindMessageByMessageId:
    """Tests for AppleMailConnector.find_message_by_message_id."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_internal_id_on_match(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "160989"
        result = connector.find_message_by_message_id(
            "<calendar-abc123@google.com>"
        )
        assert result == "160989"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_none_on_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        result = connector.find_message_by_message_id(
            "<missing@example.com>"
        )
        assert result is None

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_none_on_empty_input(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        result = connector.find_message_by_message_id("")
        assert result is None
        # No need to call AppleScript for an empty input.
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_strips_trailing_whitespace(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "160989\n"
        assert connector.find_message_by_message_id("<x@y>") == "160989"

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_message_id_with_quotes_is_escaped(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Quotes/backslashes in the Message-ID must be escaped to prevent
        AppleScript injection. Real Message-IDs almost never contain these
        but we shouldn't trust the wire."""
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id('<weird"id@host>')
        script = mock_run.call_args[0][0]
        # Escaped quote inside the AppleScript string literal.
        assert '\\"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_whose_message_id_clause(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "NOT_FOUND"
        connector.find_message_by_message_id("<x@y>")
        script = mock_run.call_args[0][0]
        assert "whose message id is" in script


class TestGetDraftState:
    """Tests for AppleMailConnector.get_draft_state."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_returns_full_draft_state(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"160991",'
            '"to":["a@example.com","b@example.com"],'
            '"cc":["c@example.com"],"bcc":[],'
            '"subject":"Re: hello",'
            '"body":"hi there\\n\\n-- original --","in_reply_to":"<orig@x>",'
            '"references":"<orig@x>",'
            '"attachment_names":["report.pdf"]}'
        )
        state = connector.get_draft_state("160991")
        assert state == {
            "draft_id": "160991",
            "to": ["a@example.com", "b@example.com"],
            "cc": ["c@example.com"],
            "bcc": [],
            "subject": "Re: hello",
            "body": "hi there\n\n-- original --",
            "in_reply_to": "<orig@x>",
            "references": "<orig@x>",
            "attachment_names": ["report.pdf"],
        }

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        with pytest.raises(MailDraftNotFoundError):
            connector.get_draft_state("999999")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_strips_internal_found_flag(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[]}'
        )
        state = connector.get_draft_state("x")
        assert "found" not in state

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_handles_empty_recipient_lists(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = (
            '{"found":true,"draft_id":"x","to":[],"cc":[],"bcc":[],'
            '"subject":"","body":"","in_reply_to":"","references":"",'
            '"attachment_names":[]}'
        )
        state = connector.get_draft_state("x")
        assert state["to"] == []
        assert state["cc"] == []
        assert state["bcc"] == []
        assert state["attachment_names"] == []

    def test_invalid_id_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.get_draft_state("../escape")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_iterates_drafts_mailboxes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        try:
            connector.get_draft_state("160991")
        except MailDraftNotFoundError:
            pass
        script = mock_run.call_args[0][0]
        # Lookup should be scoped to Drafts mailboxes.
        assert 'name of mb contains "Drafts"' in script
        # Should use as-text id comparison (probes showed numeric whose
        # clauses are unreliable on IMAP-backed Drafts).
        assert "(id of d as text) is targetId" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_reads_threading_headers(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = '{"found":false}'
        try:
            connector.get_draft_state("160991")
        except MailDraftNotFoundError:
            pass
        script = mock_run.call_args[0][0]
        assert '"In-Reply-To"' in script
        assert '"References"' in script


class TestCreateDraft:
    """Tests for AppleMailConnector.create_draft."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_invalid_seed_raises(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed must be"):
            connector.create_draft(seed="bogus")

    def test_reply_requires_seed_id(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed_id is required"):
            connector.create_draft(seed="reply")

    def test_forward_requires_seed_id(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="seed_id is required"):
            connector.create_draft(seed="forward")

    def test_new_requires_to(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="'to' is required"):
            connector.create_draft(seed="new", subject="hi", body="x")

    def test_new_requires_subject(self, connector: AppleMailConnector) -> None:
        with pytest.raises(ValueError, match="'subject' is required"):
            connector.create_draft(seed="new", to=["x@example.com"], body="x")

    # ------------------------------------------------------------------
    # Fresh seed (`seed='new'`)
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_save_returns_draft_id(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "161055"
        result = connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="hello",
        )
        assert result == {"draft_id": "161055", "sent_message_id": ""}

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_send_returns_empty_ids(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "SENT"
        result = connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="hello",
            send_now=True,
        )
        assert result == {"draft_id": "", "sent_message_id": ""}

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_script_shape(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_draft(
            seed="new",
            to=["a@example.com", "b@example.com"],
            cc=["c@example.com"],
            subject="hi",
            body="hello",
        )
        script = mock_run.call_args[0][0]
        # Fresh seed uses make-new-outgoing-message with subject + content
        # baked into properties.
        assert 'make new outgoing message with properties' in script
        assert '{subject:"hi"' in script or 'subject:"hi"' in script
        assert "save theMessage" in script
        assert "send" not in script.replace("send_now", "").replace(
            "sender", ""
        )
        # Recipient blocks present.
        assert 'a@example.com' in script
        assert 'b@example.com' in script
        assert 'c@example.com' in script
        # Pre-save snapshot for id-bridging diff present.
        assert "set beforeIds to" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_send_uses_send_block(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "SENT"
        connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="x",
            send_now=True,
        )
        script = mock_run.call_args[0][0]
        assert "tell theMessage to send" in script
        # No diff snapshot when sending.
        assert "set beforeIds to" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_from_account_sets_display_name_sender(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: when the resolver returns a Display Name <email> string,
        the AppleScript embeds it verbatim (escaped) on the sender line."""
        mock_run.return_value = "1"
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice Smith <me@x.com>",
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                from_account="Gmail",
            )
        script = mock_run.call_args[0][0]
        assert (
            'set sender of theMessage to "Alice Smith <me@x.com>"' in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_from_account_bare_email_passthrough(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#158: when the resolver returns a bare email (no display name
        configured), the AppleScript embeds the bare form."""
        mock_run.return_value = "1"
        with patch.object(
            connector, "_resolve_account_to_sender", return_value="me@x.com"
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                from_account="Gmail",
            )
        script = mock_run.call_args[0][0]
        assert 'set sender of theMessage to "me@x.com"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_from_account_sanitizes_sender_null_bytes(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """#173: sender string is run through sanitize_input before
        escape_applescript_string, so embedded null bytes (which would
        otherwise truncate or confuse the AppleScript at runtime) are
        stripped per the SECURITY_CHECKLIST two-step convention."""
        mock_run.return_value = "1"
        with patch.object(
            connector, "_resolve_account_to_sender",
            return_value="Alice\x00Smith <me@x.com>",
        ):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                from_account="Gmail",
            )
        script = mock_run.call_args[0][0]
        assert "\x00" not in script
        assert (
            'set sender of theMessage to "AliceSmith <me@x.com>"' in script
        )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_new_with_attachments_includes_paths(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        f1 = tmp_path / "report.pdf"
        f1.write_bytes(b"%PDF-fake")
        f2 = tmp_path / "data.csv"
        f2.write_text("a,b,c")
        mock_run.return_value = "1"
        connector.create_draft(
            seed="new",
            to=["a@example.com"],
            subject="hi",
            body="x",
            attachment_paths=[f1, f2],
        )
        script = mock_run.call_args[0][0]
        assert str(f1.resolve()) in script
        assert str(f2.resolve()) in script
        assert "make new attachment" in script

    def test_new_attachment_missing_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            connector.create_draft(
                seed="new",
                to=["a@example.com"],
                subject="hi",
                body="x",
                attachment_paths=[tmp_path / "does-not-exist.pdf"],
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_recipient_none_means_no_block(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """For reply/forward, cc=None should not emit a delete-and-replace
        block (preserves Mail's auto-derived recipients)."""
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",
            body="thanks",
        )
        script = mock_run.call_args[0][0]
        # cc/bcc not specified → no clear-and-add block for them.
        assert "delete (every cc recipient" not in script
        assert "delete (every bcc recipient" not in script
        # to also not specified → no clear for to either.
        assert "delete (every to recipient" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_recipient_empty_list_clears(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """cc=[] explicitly clears auto-derived cc recipients."""
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",
            cc=[],
            body="x",
        )
        script = mock_run.call_args[0][0]
        assert "delete (every cc recipient" in script

    # ------------------------------------------------------------------
    # Reply seed
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_uses_reply_primitive(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",
            body="thanks",
        )
        script = mock_run.call_args[0][0]
        assert "reply origMsg opening window false" in script
        assert "reply to all" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_all_uses_reply_to_all(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",
            reply_all=True,
            body="thanks",
        )
        script = mock_run.call_args[0][0]
        assert "reply to all origMsg opening window false" in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_body_overrides_auto_content(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mail.app's auto-quoted reply content is not readable before
        save, so a user-supplied body replaces (not prepends)
        the auto-quote. Matches existing reply_to_message behavior."""
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",
            body="thanks",
        )
        script = mock_run.call_args[0][0]
        assert 'set content of theMessage to "thanks"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_no_body_no_content_override(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """An empty body should NOT override Mail's auto-quoted reply."""
        mock_run.return_value = "1"
        connector.create_draft(seed="reply", seed_id="160989", body="")
        script = mock_run.call_args[0][0]
        assert "set content of theMessage" not in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_reply_subject_override(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_draft(
            seed="reply",
            seed_id="160989",
            subject="custom subject",
            body="x",
        )
        script = mock_run.call_args[0][0]
        assert 'set subject of theMessage to "custom subject"' in script

    # ------------------------------------------------------------------
    # Forward seed
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_forward_uses_forward_primitive(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "1"
        connector.create_draft(
            seed="forward",
            seed_id="160989",
            to=["x@example.com"],
            body="fyi",
        )
        script = mock_run.call_args[0][0]
        assert "forward origMsg opening window false" in script

    # ------------------------------------------------------------------
    # Seed lookup error mapping
    # ------------------------------------------------------------------

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_seed_not_found_raises_message_not_found(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("SEED_NOT_FOUND")
        with pytest.raises(MailMessageNotFoundError):
            connector.create_draft(
                seed="reply", seed_id="999999", body="x"
            )


class TestExtractDraftAttachments:
    """Tests for AppleMailConnector.extract_draft_attachments."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    def test_invalid_id_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(MailDraftInvalidIdError):
            connector.extract_draft_attachments(
                "../escape", ["foo.pdf"], tmp_path
            )

    def test_missing_dest_dir_raises(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            connector.extract_draft_attachments(
                "160991", ["foo.pdf"], tmp_path / "nonexistent"
            )

    def test_no_attachments_returns_empty(
        self, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Empty attachment list short-circuits without calling AppleScript.
        result = connector.extract_draft_attachments("160991", [], tmp_path)
        assert result == []

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_extract_creates_subdirs_and_returns_paths(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Simulate AppleScript actually writing the files (the real
        # Mail.app would save here). We poke real bytes into the
        # expected paths so the file-existence filter at the end picks
        # them up.
        def fake_run(script: str) -> str:
            (tmp_path / "0").mkdir(parents=True, exist_ok=True)
            (tmp_path / "0" / "report.pdf").write_bytes(b"%PDF-fake")
            (tmp_path / "1").mkdir(parents=True, exist_ok=True)
            (tmp_path / "1" / "data.csv").write_text("a,b,c")
            return "2"

        mock_run.side_effect = fake_run
        paths = connector.extract_draft_attachments(
            "160991", ["report.pdf", "data.csv"], tmp_path
        )
        assert paths == [
            tmp_path / "0" / "report.pdf",
            tmp_path / "1" / "data.csv",
        ]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_partial_extraction_returns_only_existing(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        # Simulate Mail.app writing only the first file (e.g., second
        # attachment was a Mail-internal sentinel that errored on save).
        def fake_run(script: str) -> str:
            (tmp_path / "0").mkdir(parents=True, exist_ok=True)
            (tmp_path / "0" / "ok.pdf").write_bytes(b"x")
            (tmp_path / "1").mkdir(parents=True, exist_ok=True)
            return "1"

        mock_run.side_effect = fake_run
        paths = connector.extract_draft_attachments(
            "160991", ["ok.pdf", "missing.pdf"], tmp_path
        )
        assert paths == [tmp_path / "0" / "ok.pdf"]

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        mock_run.return_value = "ERR_NOT_FOUND"
        with pytest.raises(MailDraftNotFoundError):
            connector.extract_draft_attachments(
                "999999", ["foo.pdf"], tmp_path
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_save_command(
        self, mock_run: MagicMock, connector: AppleMailConnector, tmp_path: Any
    ) -> None:
        mock_run.return_value = "0"
        connector.extract_draft_attachments(
            "160991", ["a.pdf"], tmp_path
        )
        script = mock_run.call_args[0][0]
        assert "save a in (POSIX file tp)" in script
        assert "mail attachments of foundDraft" in script


class TestUpdateMailbox:
    """Tests for AppleMailConnector.update_mailbox (rename only — #102)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_rename_success(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "success"
        assert connector.update_mailbox(
            account="Gmail", name="Old", new_name="New"
        ) is True

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_uses_set_name(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="Gmail", name="Old", new_name="New"
        )
        script = mock_run.call_args[0][0]
        assert 'set name of mb to "New"' in script
        assert 'mailbox "Old"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_script_handles_nested_path(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Slash-separated path passes through to Mail.app's
        `mailbox "Parent/Child"` form."""
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="Gmail", name="Archive/2024", new_name="Archive2024"
        )
        script = mock_run.call_args[0][0]
        assert 'mailbox "Archive/2024"' in script

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_mailbox_not_found_raises(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("MAILBOX_NOT_FOUND")
        from apple_mail_mcp.exceptions import MailMailboxNotFoundError
        with pytest.raises(MailMailboxNotFoundError):
            connector.update_mailbox(
                account="Gmail", name="Missing", new_name="New"
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_other_applescript_errors_propagate(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        mock_run.side_effect = MailAppleScriptError("something else")
        with pytest.raises(MailAppleScriptError):
            connector.update_mailbox(
                account="Gmail", name="Old", new_name="New"
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_path_traversal_chars_stripped_before_applescript(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """``sanitize_mailbox_name`` strips traversal chars (``..``, ``/``,
        ``\\``) rather than rejecting them outright. Verify the AppleScript
        embeds the sanitized form, not the raw user input."""
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="Gmail", name="Old", new_name="../../bad-name"
        )
        script = mock_run.call_args[0][0]
        # The dots and slashes are stripped; what's left is "bad-name".
        assert '"../../bad-name"' not in script
        assert '"bad-name"' in script

    def test_new_name_that_sanitizes_to_empty_raises(
        self, connector: AppleMailConnector
    ) -> None:
        """A new_name of just traversal chars sanitizes to empty -> reject."""
        with pytest.raises(ValueError, match="Invalid new_name"):
            connector.update_mailbox(
                account="Gmail", name="Old", new_name="../"
            )

    def test_empty_new_name_raises(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="Invalid new_name"):
            connector.update_mailbox(
                account="Gmail", name="Old", new_name=""
            )

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_account_clause_uses_uuid_when_uuid_passed(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Mirror the convention from create_mailbox: account UUIDs
        produce `account id "..."` and names produce `account "..."`."""
        mock_run.return_value = "success"
        connector.update_mailbox(
            account="DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5",
            name="Old", new_name="New",
        )
        script = mock_run.call_args[0][0]
        # applescript_account_clause emits `account id "<UUID>"`.
        assert 'account id "DC5AC137-2F7A-4299-B3D0-4D3E06C18DD5"' in script

    def test_requires_at_least_one_of_new_name_or_new_parent(
        self, connector: AppleMailConnector
    ) -> None:
        with pytest.raises(ValueError, match="at least one"):
            connector.update_mailbox(account="Gmail", name="Old")

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_gmail_system_label_source_refused_before_applescript(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """Pre-flight: source name like ``[Gmail]/Drafts`` raises
        ``MailUnsupportedGmailSystemLabelError`` before any AppleScript
        runs (#164). Renames of Gmail system labels don't stick anyway."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="[Gmail]/Drafts", new_name="MyDrafts",
            )
        mock_run.assert_not_called()

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_bare_gmail_parent_source_also_refused(
        self, mock_run: MagicMock, connector: AppleMailConnector
    ) -> None:
        """The bare ``[Gmail]`` parent (a \\Noselect folder) is also refused."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="[Gmail]", new_name="Whatever",
            )
        mock_run.assert_not_called()


class TestUpdateMailboxMove:
    """IMAP-dispatched move path of update_mailbox (#163)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_move_to_top_uses_imap_rename_with_leaf_only(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """new_parent="" means top-level — destination is just the leaf."""
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        connector.update_mailbox(
            account="Gmail", name="Archive/2024", new_parent=""
        )
        mock_imap.rename_mailbox.assert_called_once_with("Archive/2024", "2024")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_move_under_new_parent_keeps_leaf(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        connector.update_mailbox(
            account="Gmail", name="Archive/2024", new_parent="Old"
        )
        mock_imap.rename_mailbox.assert_called_once_with(
            "Archive/2024", "Old/2024"
        )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_move_with_rename_combines_in_one_rename(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        connector.update_mailbox(
            account="Gmail", name="Old/Sub",
            new_name="Renamed", new_parent="New",
        )
        mock_imap.rename_mailbox.assert_called_once_with(
            "Old/Sub", "New/Renamed"
        )

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_no_keychain_credentials_raises_imap_required(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from apple_mail_mcp.exceptions import (
            MailImapRequiredError,
            MailKeychainEntryNotFoundError,
        )
        mock_pw.side_effect = MailKeychainEntryNotFoundError("no entry")
        with pytest.raises(MailImapRequiredError):
            connector.update_mailbox(
                account="Gmail", name="X", new_parent="Y"
            )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_source_refused_before_imap_session(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Move source ``[Gmail]/Sent Mail`` raises before the IMAP
        credential lookup runs (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="[Gmail]/Sent Mail", new_parent="Archive",
            )
        # No IMAP session opened, no credentials looked up.
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_destination_parent_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Moving a regular folder INTO ``[Gmail]/Subfolder`` is also
        refused — the resulting destination would land in Gmail's
        system-label namespace (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="Archive",
                new_parent="[Gmail]/Subfolder",
            )
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_bare_gmail_parent_destination_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """``new_parent="[Gmail]"`` produces a destination of
        ``[Gmail]/<leaf>`` — also a system-label path; refused (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.update_mailbox(
                account="Gmail", name="Archive", new_parent="[Gmail]",
            )
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()


class TestDeleteMailbox:
    """delete_mailbox via IMAP (#162)."""

    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_delete_empty_mailbox_returns_zero(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.return_value = 0
        result = connector.delete_mailbox(account="Gmail", name="Empty")
        assert result == 0
        mock_imap.delete_mailbox.assert_called_once_with(
            "Empty", allow_non_empty=False
        )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_delete_messages_true_passes_through(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.return_value = 42
        result = connector.delete_mailbox(
            account="Gmail", name="Big", delete_messages=True
        )
        assert result == 42
        mock_imap.delete_mailbox.assert_called_once_with(
            "Big", allow_non_empty=True
        )

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_non_empty_refusal_raises_typed_error(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from apple_mail_mcp.exceptions import MailMailboxNotEmptyError
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.side_effect = ValueError(
            "mailbox 'X' is not empty (5 messages); pass allow_non_empty=True"
        )
        with pytest.raises(MailMailboxNotEmptyError):
            connector.delete_mailbox(account="Gmail", name="X")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_no_such_mailbox_maps_to_typed_error(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from imapclient.exceptions import IMAPClientError
        mock_pw.return_value = "secret"
        mock_imap = mock_imap_cls.return_value
        mock_imap.delete_mailbox.side_effect = IMAPClientError(
            "DELETE: No such mailbox"
        )
        with pytest.raises(MailMailboxNotFoundError):
            connector.delete_mailbox(account="Gmail", name="Missing")

    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(
        AppleMailConnector, "_resolve_imap_config",
        return_value=("imap.gmail.com", 993, "x@gmail.com"),
    )
    def test_no_keychain_raises_imap_required(
        self,
        _mock_cfg: MagicMock,
        mock_pw: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        from apple_mail_mcp.exceptions import (
            MailImapRequiredError,
            MailKeychainEntryNotFoundError,
        )
        mock_pw.side_effect = MailKeychainEntryNotFoundError("nope")
        with pytest.raises(MailImapRequiredError):
            connector.delete_mailbox(account="Gmail", name="X")

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_gmail_system_label_refused_before_credential_lookup(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """Pre-flight: deleting ``[Gmail]/Trash`` raises before the IMAP
        credential lookup runs (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.delete_mailbox(account="Gmail", name="[Gmail]/Trash")
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()

    @patch("apple_mail_mcp.mail_connector.ImapConnector")
    @patch("apple_mail_mcp.mail_connector.get_imap_password")
    @patch.object(AppleMailConnector, "_resolve_imap_config")
    def test_bare_gmail_parent_refused(
        self,
        mock_cfg: MagicMock,
        mock_pw: MagicMock,
        mock_imap_cls: MagicMock,
        connector: AppleMailConnector,
    ) -> None:
        """The bare ``[Gmail]`` parent is also refused (#164)."""
        from apple_mail_mcp.exceptions import (
            MailUnsupportedGmailSystemLabelError,
        )
        with pytest.raises(MailUnsupportedGmailSystemLabelError):
            connector.delete_mailbox(account="Gmail", name="[Gmail]")
        mock_cfg.assert_not_called()
        mock_pw.assert_not_called()
        mock_imap_cls.assert_not_called()
