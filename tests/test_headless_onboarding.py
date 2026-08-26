"""Tests for headless (env-var driven) account onboarding."""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import adapter as adapter_mod
from adapter import (
    ANCHOR_RELAY,
    DeltaChatAdapter,
    _auto_relays,
    _headless_onboarding,
    _parse_relay_list,
)


ONBOARDING_VARS = (
    "DELTACHAT_EMAIL",
    "DELTACHAT_PASSWORD",
    "DELTACHAT_CHATMAIL_SERVERS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Onboarding is opt-in via env; start every test from a blank slate."""
    for var in ONBOARDING_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def adapter(platform_config):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = MagicMock()
    return a


class TestParseRelayList:
    def test_splits_and_strips(self):
        assert _parse_relay_list(" a.org , b.org ") == ["a.org", "b.org"]

    def test_strips_scheme_and_trailing_slash(self):
        assert _parse_relay_list("https://a.org/,b.org") == ["a.org", "b.org"]

    def test_dedups_and_drops_empties(self):
        assert _parse_relay_list("a.org,,a.org,b.org") == ["a.org", "b.org"]

    def test_empty_string(self):
        assert _parse_relay_list("") == []


def _relays(*hosts):
    return patch.object(adapter_mod, "_get_relay_servers", return_value=list(hosts))


class TestSetupModuleLoading:
    def test_loads_our_setup_not_whatever_is_on_syspath(self):
        """`setup` is a collision-prone name; we load ours by explicit path."""
        module = adapter_mod._load_setup_module()
        assert module.__file__ == os.path.join(adapter_mod._plugin_dir, "setup.py")
        assert hasattr(module, "get_relay_servers")

    def test_is_cached(self):
        assert adapter_mod._load_setup_module() is adapter_mod._load_setup_module()


class TestAutoRelays:
    def test_anchor_first_then_random_others(self):
        with _relays(ANCHOR_RELAY, "b.org", "c.org", "d.org"):
            relays = _auto_relays(count=3)
        assert relays[0] == ANCHOR_RELAY
        assert len(relays) == 3
        assert set(relays[1:]) <= {"b.org", "c.org", "d.org"}

    def test_no_duplicate_anchor(self):
        with _relays(ANCHOR_RELAY, "b.org"):
            relays = _auto_relays(count=3)
        assert relays.count(ANCHOR_RELAY) == 1

    def test_short_list_is_not_padded(self):
        with _relays(ANCHOR_RELAY):
            assert _auto_relays(count=3) == [ANCHOR_RELAY]

    def test_scrape_failure_falls_back_to_anchor(self):
        with patch.object(
            adapter_mod, "_get_relay_servers", side_effect=OSError("no network")
        ):
            assert _auto_relays(count=3) == [ANCHOR_RELAY]

    def test_count_of_one_is_anchor_only(self):
        with _relays(ANCHOR_RELAY, "b.org"):
            assert _auto_relays(count=1) == [ANCHOR_RELAY]


class TestHeadlessOptIn:
    def test_disabled_when_no_env(self):
        assert _headless_onboarding() is None

    def test_email_auto_selects_chatmail(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_EMAIL", "auto")
        assert _headless_onboarding() == {"mode": "chatmail", "relays": []}

    def test_email_auto_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_EMAIL", "AUTO")
        assert _headless_onboarding()["mode"] == "chatmail"

    def test_real_address_selects_email_mode(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_EMAIL", "bot@example.com")
        assert _headless_onboarding() == {
            "mode": "email",
            "email": "bot@example.com",
        }

    def test_servers_alone_opts_in(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_CHATMAIL_SERVERS", "a.org,b.org")
        assert _headless_onboarding() == {
            "mode": "chatmail",
            "relays": ["a.org", "b.org"],
        }

    def test_whitespace_only_does_not_opt_in(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_EMAIL", "   ")
        assert _headless_onboarding() is None

    def test_address_wins_over_servers(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_EMAIL", "bot@example.com")
        monkeypatch.setenv("DELTACHAT_CHATMAIL_SERVERS", "a.org")
        assert _headless_onboarding()["mode"] == "email"


class TestEmailTransport:
    @pytest.mark.asyncio
    async def test_configures_and_clears_password(self, adapter, monkeypatch):
        monkeypatch.setenv("DELTACHAT_PASSWORD", "hunter2")
        adapter.rpc.add_or_update_transport = AsyncMock()

        assert await adapter._configure_email_transport("bot@example.com") is True

        adapter.rpc.add_or_update_transport.assert_awaited_once_with(
            1, {"addr": "bot@example.com", "password": "hunter2"}
        )
        assert "DELTACHAT_PASSWORD" not in os.environ

    @pytest.mark.asyncio
    async def test_missing_password_fails_without_calling_rpc(self, adapter):
        adapter.rpc.add_or_update_transport = AsyncMock()
        assert await adapter._configure_email_transport("bot@example.com") is False
        adapter.rpc.add_or_update_transport.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_password_survives_a_failed_configure(self, adapter, monkeypatch):
        """A reconnect must be able to retry, so only clear on success."""
        monkeypatch.setenv("DELTACHAT_PASSWORD", "hunter2")
        adapter.rpc.add_or_update_transport = AsyncMock(side_effect=OSError("nope"))

        assert await adapter._configure_email_transport("bot@example.com") is False
        assert os.environ["DELTACHAT_PASSWORD"] == "hunter2"


class TestChatmailTransports:
    @pytest.mark.asyncio
    async def test_registers_on_every_relay(self, adapter):
        adapter.rpc.add_transport_from_qr = AsyncMock()

        assert await adapter._configure_chatmail_transports(["a.org", "b.org"]) is True

        assert [c.args for c in adapter.rpc.add_transport_from_qr.await_args_list] == [
            (1, "dcaccount:a.org"),
            (1, "dcaccount:b.org"),
        ]

    @pytest.mark.asyncio
    async def test_one_working_relay_is_enough(self, adapter):
        adapter.rpc.add_transport_from_qr = AsyncMock(
            side_effect=[OSError("down"), None]
        )
        assert await adapter._configure_chatmail_transports(["a.org", "b.org"]) is True

    @pytest.mark.asyncio
    async def test_all_relays_failing_is_an_error(self, adapter):
        adapter.rpc.add_transport_from_qr = AsyncMock(side_effect=OSError("down"))
        assert await adapter._configure_chatmail_transports(["a.org", "b.org"]) is False

    @pytest.mark.asyncio
    async def test_empty_list_falls_back_to_auto_selection(self, adapter):
        adapter.rpc.add_transport_from_qr = AsyncMock()
        with patch.object(adapter_mod, "_auto_relays", return_value=["picked.org"]):
            assert await adapter._configure_chatmail_transports([]) is True
        adapter.rpc.add_transport_from_qr.assert_awaited_once_with(
            1, "dcaccount:picked.org"
        )


LINK = "OPENPGP4FPR:ABC#a=bot@x.org"


class TestInviteLink:
    @pytest.mark.asyncio
    async def test_writes_0600_file_and_logs_only_its_path(
        self, adapter, tmp_path, caplog
    ):
        adapter._dc_config_dir = str(tmp_path)
        adapter.rpc.get_chat_securejoin_qr_code = AsyncMock(return_value=LINK)

        with caplog.at_level("INFO"):
            await adapter._publish_invite_link()

        invite = tmp_path / "invite.txt"
        assert invite.read_text().strip() == LINK
        assert invite.stat().st_mode & 0o777 == 0o600
        assert adapter._invite_link == LINK
        # gateway.log is world-readable; the link must not land there at INFO.
        assert LINK not in caplog.text
        assert str(invite) in caplog.text

    @pytest.mark.asyncio
    async def test_link_is_available_at_debug(self, adapter, tmp_path, caplog):
        adapter._dc_config_dir = str(tmp_path)
        adapter.rpc.get_chat_securejoin_qr_code = AsyncMock(return_value=LINK)

        with caplog.at_level("DEBUG"):
            await adapter._publish_invite_link()

        assert LINK in caplog.text

    @pytest.mark.asyncio
    async def test_tightens_mode_on_a_preexisting_file(self, adapter, tmp_path):
        """O_CREAT's mode is ignored when the file already exists."""
        adapter._dc_config_dir = str(tmp_path)
        stale = tmp_path / "invite.txt"
        stale.write_text("old\n")
        stale.chmod(0o644)
        adapter.rpc.get_chat_securejoin_qr_code = AsyncMock(return_value=LINK)

        await adapter._publish_invite_link()

        assert stale.read_text().strip() == LINK
        assert stale.stat().st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    async def test_rpc_failure_is_not_fatal(self, adapter, tmp_path):
        adapter._dc_config_dir = str(tmp_path)
        adapter.rpc.get_chat_securejoin_qr_code = AsyncMock(side_effect=OSError("x"))

        await adapter._publish_invite_link()

        assert adapter._invite_link is None
        assert not (tmp_path / "invite.txt").exists()

    @pytest.mark.asyncio
    async def test_unwritable_dir_falls_back_to_logging_the_link(
        self, adapter, tmp_path, caplog
    ):
        adapter._dc_config_dir = str(tmp_path / "does-not-exist")
        adapter.rpc.get_chat_securejoin_qr_code = AsyncMock(return_value=LINK)

        with caplog.at_level("WARNING"):
            await adapter._publish_invite_link()

        # No file to point at, so the link itself is the only way to pair.
        assert LINK in caplog.text
        assert adapter._invite_link == LINK
