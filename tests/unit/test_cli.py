"""Tests for the apple-mail-mcp CLI entry point and setup-imap subcommand."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from apple_mail_mcp.cli import run_setup_imap
from apple_mail_mcp.exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_accounts() -> list[dict[str, Any]]:
    return [
        {
            "id": "UUID-ICLOUD",
            "name": "iCloud",
            "email_addresses": ["alice@icloud.com"],
            "account_type": "imap",
            "enabled": True,
        },
        {
            "id": "UUID-GMAIL",
            "name": "Gmail",
            "email_addresses": ["alice@gmail.com"],
            "account_type": "imap",
            "enabled": True,
        },
    ]


@pytest.fixture
def mock_connector() -> MagicMock:
    """Stand-in for AppleMailConnector — list_accounts + _resolve_imap_config."""
    m = MagicMock()
    m.list_accounts.return_value = _make_accounts()
    m._resolve_imap_config.return_value = (
        "imap.mail.me.com", 993, "alice@icloud.com",
    )
    return m


@pytest.fixture
def mock_imap_client() -> MagicMock:
    """Stand-in for ImapConnector. Default: search_messages succeeds."""
    m = MagicMock()
    m.search_messages.return_value = []
    return m


# ---------------------------------------------------------------------------
# Account validation
# ---------------------------------------------------------------------------


class TestAccountValidation:
    def test_unknown_account_lists_available_and_returns_1(
        self,
        mock_connector: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = run_setup_imap(
            account_name="Gmial",  # typo
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "No Mail.app account named 'Gmial'" in captured.err
        # Existing accounts must be listed so the user can correct the typo.
        assert "iCloud" in captured.err
        assert "Gmail" in captured.err

    def test_account_with_no_email_addresses_returns_1(
        self,
        mock_connector: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Strip out email_addresses for one account to simulate a misconfigured
        # Mail.app entry.
        accs = _make_accounts()
        accs[0]["email_addresses"] = []
        mock_connector.list_accounts.return_value = accs

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "no email addresses" in captured.err
        assert "--email" in captured.err

    def test_cli_email_override_used_when_provided(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        set_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            cli_mod, "set_imap_password",
            lambda a, e, p: set_calls.append((a, e, p)),
        )

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email="alice+aliased@icloud.com",
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "secretpw",
            imap_factory=lambda h, p, e, pw: mock_imap_client,
        )
        assert rc == 0
        # The CLI-supplied email is what gets written, not the default.
        assert set_calls == [("iCloud", "alice+aliased@icloud.com", "secretpw")]


# ---------------------------------------------------------------------------
# Happy path — setup
# ---------------------------------------------------------------------------


class TestSetupHappyPath:
    def test_setup_success_writes_keychain_and_verifies(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        set_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            cli_mod, "set_imap_password",
            lambda a, e, p: set_calls.append((a, e, p)),
        )

        captured_imap_args: list[tuple[str, int, str, str]] = []

        def imap_factory(h: str, p: int, e: str, pw: str) -> MagicMock:
            captured_imap_args.append((h, p, e, pw))
            return mock_imap_client

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "appspecificpw",
            imap_factory=imap_factory,
        )
        assert rc == 0
        assert set_calls == [("iCloud", "alice@icloud.com", "appspecificpw")]
        # Verification used the resolved (Mail.app's) email, not necessarily
        # the same as the Keychain key — important for iCloud.
        assert captured_imap_args == [
            ("imap.mail.me.com", 993, "alice@icloud.com", "appspecificpw"),
        ]
        mock_imap_client.search_messages.assert_called_once()
        out = capsys.readouterr().out
        assert "Setup complete." in out
        assert "OK (connected to imap.mail.me.com:993)" in out

    def test_setup_uses_resolved_email_for_imap_login(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """For iCloud, --email may be the Apple ID alias but IMAP needs
        @icloud.com. The CLI must use _resolve_imap_config's email for LOGIN."""
        from apple_mail_mcp import cli as cli_mod

        monkeypatch.setattr(
            cli_mod, "set_imap_password", lambda a, e, p: None,
        )

        # User supplied an Apple-ID-style email; Mail.app resolves the IMAP
        # username to the actual @icloud.com address.
        mock_connector._resolve_imap_config.return_value = (
            "imap.mail.me.com", 993, "alice@icloud.com",
        )

        captured: list[tuple[str, int, str, str]] = []

        def factory(h: str, p: int, e: str, pw: str) -> MagicMock:
            captured.append((h, p, e, pw))
            return mock_imap_client

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email="alice@gmail.com",  # user's Apple ID is a Gmail
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "pw",
            imap_factory=factory,
        )
        assert rc == 0
        # IMAP LOGIN uses the Mail.app-resolved email, not the CLI override.
        assert captured == [
            ("imap.mail.me.com", 993, "alice@icloud.com", "pw"),
        ]


# ---------------------------------------------------------------------------
# Setup — failure paths
# ---------------------------------------------------------------------------


class TestSetupFailurePaths:
    def test_empty_password_returns_1_without_writing(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        set_calls: list[Any] = []
        monkeypatch.setattr(
            cli_mod, "set_imap_password",
            lambda *a, **k: set_calls.append(a),
        )

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "",
            imap_factory=lambda *a, **k: mock_imap_client,
        )
        assert rc == 1
        assert set_calls == []
        assert "empty password" in capsys.readouterr().err

    def test_keyboard_interrupt_during_password_prompt(
        self,
        mock_connector: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def raise_interrupt(prompt: str) -> str:
            raise KeyboardInterrupt()

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=raise_interrupt,
        )
        assert rc == 1
        assert "Cancelled" in capsys.readouterr().err

    def test_login_error_rolls_back_keychain_entry(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        set_calls: list[tuple[str, str, str]] = []
        delete_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cli_mod, "set_imap_password",
            lambda a, e, p: set_calls.append((a, e, p)),
        )
        monkeypatch.setattr(
            cli_mod, "delete_imap_password",
            lambda a, e: delete_calls.append((a, e)),
        )

        mock_imap_client.search_messages.side_effect = LoginError(
            "AUTHENTICATIONFAILED"
        )

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "wrongpw",
            imap_factory=lambda *a, **k: mock_imap_client,
        )
        assert rc == 1
        # We wrote, then rolled back on the LoginError.
        assert set_calls == [("iCloud", "alice@icloud.com", "wrongpw")]
        assert delete_calls == [("iCloud", "alice@icloud.com")]
        err = capsys.readouterr().err
        assert "IMAP login was rejected" in err
        assert "removed" in err

    def test_network_error_keeps_entry_with_warning(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        delete_calls: list[Any] = []
        monkeypatch.setattr(
            cli_mod, "set_imap_password", lambda a, e, p: None,
        )
        monkeypatch.setattr(
            cli_mod, "delete_imap_password",
            lambda *a, **k: delete_calls.append(a),
        )

        mock_imap_client.search_messages.side_effect = OSError("network down")

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "pw",
            imap_factory=lambda *a, **k: mock_imap_client,
        )
        # Network errors → keep the entry, return 0 (set up may still be valid)
        assert rc == 0
        assert delete_calls == []
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "could not complete" in err

    def test_imap_protocol_error_keeps_entry_with_warning(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        monkeypatch.setattr(
            cli_mod, "set_imap_password", lambda a, e, p: None,
        )
        monkeypatch.setattr(
            cli_mod, "delete_imap_password", lambda a, e: None,
        )

        mock_imap_client.search_messages.side_effect = IMAPClientError(
            "BAD command"
        )

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "pw",
            imap_factory=lambda *a, **k: mock_imap_client,
        )
        assert rc == 0
        assert "WARNING" in capsys.readouterr().err

    def test_keychain_access_denied_during_write(
        self,
        mock_connector: MagicMock,
        mock_imap_client: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        def raise_denied(*a: Any, **k: Any) -> None:
            raise MailKeychainAccessDeniedError("user canceled")

        monkeypatch.setattr(cli_mod, "set_imap_password", raise_denied)

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=False,
            connector_factory=lambda: mock_connector,
            getpass_fn=lambda prompt: "pw",
            imap_factory=lambda *a, **k: mock_imap_client,
        )
        assert rc == 1
        # IMAP factory should NOT be called when the write fails.
        mock_imap_client.search_messages.assert_not_called()
        assert "ERROR" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Uninstall path
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_uninstall_deletes_entry(
        self,
        mock_connector: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        delete_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cli_mod, "delete_imap_password",
            lambda a, e: delete_calls.append((a, e)),
        )

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=True,
            connector_factory=lambda: mock_connector,
        )
        assert rc == 0
        assert delete_calls == [("iCloud", "alice@icloud.com")]
        assert "Removed Keychain entry" in capsys.readouterr().out

    def test_uninstall_when_no_entry_exists_returns_1(
        self,
        mock_connector: MagicMock,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        def raise_not_found(*a: Any, **k: Any) -> None:
            raise MailKeychainEntryNotFoundError("missing")

        monkeypatch.setattr(
            cli_mod, "delete_imap_password", raise_not_found,
        )

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=True,
            connector_factory=lambda: mock_connector,
        )
        assert rc == 1
        assert "No Keychain entry to remove" in capsys.readouterr().err

    def test_uninstall_does_not_prompt_for_password(
        self,
        mock_connector: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apple_mail_mcp import cli as cli_mod

        monkeypatch.setattr(
            cli_mod, "delete_imap_password", lambda a, e: None,
        )

        def fail_if_called(prompt: str) -> str:
            raise AssertionError("getpass must not be called for --uninstall")

        rc = run_setup_imap(
            account_name="iCloud",
            cli_email=None,
            uninstall=True,
            connector_factory=lambda: mock_connector,
            getpass_fn=fail_if_called,
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# argparse dispatch in server.main()
# ---------------------------------------------------------------------------


class TestServerMainDispatch:
    def test_no_args_starts_mcp_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apple_mail_mcp import server as server_mod

        run_calls: list[Any] = []
        monkeypatch.setattr(
            server_mod.mcp, "run", lambda: run_calls.append(True),
        )
        rc = server_mod.main([])
        assert rc == 0
        assert run_calls == [True]

    def test_setup_imap_subcommand_does_not_start_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apple_mail_mcp import server as server_mod

        # mcp.run() must NOT be called when a subcommand was given.
        def fail_if_called() -> None:
            raise AssertionError("mcp.run() called during subcommand dispatch")

        monkeypatch.setattr(server_mod.mcp, "run", fail_if_called)
        # cli.run_setup_imap is imported lazily inside main(); patch at that
        # spot via the cli module attribute.
        import apple_mail_mcp.cli as cli_mod

        captured: dict[str, Any] = {}

        def fake_run(**kwargs: Any) -> int:
            captured.update(kwargs)
            return 7

        monkeypatch.setattr(cli_mod, "run_setup_imap", fake_run)

        rc = server_mod.main(
            ["setup-imap", "--account", "iCloud", "--uninstall"]
        )
        assert rc == 7
        assert captured == {
            "account_name": "iCloud",
            "cli_email": None,
            "uninstall": True,
        }

    def test_setup_imap_requires_account(self) -> None:
        from apple_mail_mcp import server as server_mod

        with pytest.raises(SystemExit):
            server_mod.main(["setup-imap"])
