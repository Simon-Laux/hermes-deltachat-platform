"""Tests for pairing the Delta Chat database with Hermes' state (_check_db_id)."""

import logging
import sys
import types
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import adapter as adapter_mod
from adapter import DeltaChatAdapter, _DB_ID_KEY


@pytest.fixture
def adapter(platform_config, tmp_path):
    a = DeltaChatAdapter(platform_config)
    a._dc_config_dir = str(tmp_path / "deltachat-platform")
    a.account_id = 1
    a.dc_config = {}
    a.rpc = MagicMock()
    a.rpc.get_config = AsyncMock(side_effect=lambda acc, key: a.dc_config.get(key))
    a.rpc.set_config = AsyncMock(
        side_effect=lambda acc, key, value: a.dc_config.__setitem__(key, value))
    return a


@pytest.fixture
def marker(tmp_path):
    return tmp_path / ".deltachat-db-id"


@pytest.mark.asyncio
async def test_fresh_install_or_upgrade_creates_both(adapter, marker):
    assert await adapter._check_db_id()
    db_id = adapter.dc_config[_DB_ID_KEY]
    uuid.UUID(db_id)
    assert marker.read_text().strip() == db_id


@pytest.mark.asyncio
async def test_matching_ids_start(adapter, marker):
    adapter.dc_config[_DB_ID_KEY] = "abc"
    marker.write_text("abc\n")
    assert await adapter._check_db_id()
    assert not adapter.has_fatal_error


@pytest.mark.asyncio
async def test_recreated_dc_database_refuses(adapter, marker):
    """The disaster case: Hermes remembers a DB that is gone."""
    marker.write_text("abc\n")
    assert not await adapter._check_db_id()
    assert adapter.fatal_error_code == "deltachat_db_mismatch"
    assert not adapter.fatal_error_retryable
    assert str(marker) in adapter.fatal_error_message
    # must not paper over it by writing a new ID into the fresh DB
    assert _DB_ID_KEY not in adapter.dc_config


@pytest.mark.asyncio
async def test_different_database_refuses(adapter, marker):
    adapter.dc_config[_DB_ID_KEY] = "other"
    marker.write_text("abc\n")
    assert not await adapter._check_db_id()
    assert marker.read_text().strip() == "abc"


@pytest.mark.asyncio
async def test_wiped_hermes_state_adopts_dc_id(adapter, marker):
    adapter.dc_config[_DB_ID_KEY] = "abc"
    assert await adapter._check_db_id()
    assert marker.read_text().strip() == "abc"


@pytest.mark.asyncio
async def test_no_account_yet_without_marker_passes_and_writes_nothing(adapter, marker):
    adapter.account_id = None
    assert await adapter._check_db_id()
    assert not marker.exists()
    adapter.rpc.get_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_account_but_marker_refuses(adapter, marker):
    """A lost database is refused before headless onboarding makes a new one."""
    adapter.account_id = None
    marker.write_text("abc\n")
    assert not await adapter._check_db_id()
    assert adapter.fatal_error_code == "deltachat_db_mismatch"


@pytest.mark.asyncio
async def test_unwritable_marker_is_a_fatal_error(adapter, marker):
    with patch("builtins.open", side_effect=PermissionError("denied")):
        assert not await adapter._check_db_id()
    assert adapter.fatal_error_code == "deltachat_db_marker_io"
    assert not adapter.fatal_error_retryable


@pytest.mark.asyncio
async def test_upgrade_with_existing_approvals_warns(adapter, monkeypatch, caplog):
    store = MagicMock()
    store.return_value.list_approved.return_value = [{"user_id": "10"}]
    monkeypatch.setitem(sys.modules, "gateway.pairing", types.SimpleNamespace(PairingStore=store))
    with caplog.at_level(logging.WARNING):
        assert await adapter._check_db_id()
    store.return_value.list_approved.assert_called_once_with("deltachat-platform")
    assert "1 pairing approval" in caplog.text


@pytest.mark.asyncio
async def test_connect_stops_before_start_io_on_mismatch(adapter, marker, monkeypatch):
    """No message may be handled on a database that isn't ours."""
    marker.write_text("abc\n")
    adapter.dc_config[_DB_ID_KEY] = "other"
    rpc = adapter.rpc
    rpc.get_all_accounts = AsyncMock(return_value=[{"id": 1}])
    rpc.is_configured = AsyncMock(return_value=True)
    rpc.start_io = AsyncMock()
    adapter.account_id = None
    transport = types.ModuleType("deltachat2.transport")
    transport.IOTransport = MagicMock()
    monkeypatch.setitem(sys.modules, "deltachat2.transport", transport)
    monkeypatch.setitem(sys.modules, "deltachat2", types.SimpleNamespace(Rpc=MagicMock()))
    monkeypatch.setattr(adapter_mod, "_check_dc2_available", lambda: True)
    monkeypatch.setattr(adapter_mod, "_check_dc_version", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter_mod, "_AsyncRpc", lambda _: rpc)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", AsyncMock())

    assert not await adapter.connect()
    rpc.start_io.assert_not_awaited()
    assert adapter.fatal_error_code == "deltachat_db_mismatch"
    assert adapter.rpc is None  # cleaned up
