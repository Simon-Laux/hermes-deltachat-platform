"""Tests for pairing the Delta Chat database with Hermes' state (_check_db_id)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

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
    return tmp_path / "deltachat-platform.db-id"


@pytest.mark.asyncio
async def test_fresh_install_or_upgrade_creates_both(adapter, marker):
    assert await adapter._check_db_id()
    db_id = adapter.dc_config[_DB_ID_KEY]
    assert db_id
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
