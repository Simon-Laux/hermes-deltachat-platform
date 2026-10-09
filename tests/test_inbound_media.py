"""Incoming media: Hermes' inbound media size cap holds for every kind, and
videos reach Hermes as MessageType.VIDEO."""
from unittest.mock import AsyncMock

import pytest

import adapter as adapter_mod
from adapter import DeltaChatAdapter
from tests.conftest import (MockGatewayBase, MockMessageType, MockPlatform,
                            MockPlatformConfig)


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter_mod, "_get_or_create_chat_token", AsyncMock(return_value="tok"))

    def _make(view_type, size, mime, limit=0):
        monkeypatch.setattr(MockGatewayBase, "inbound_media_max_bytes", limit)
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"\0" * size)
        a = DeltaChatAdapter(MockPlatformConfig(name="deltachat-platform",
                                                platform=MockPlatform.DELTACHAT, extra={}))
        a.account_id = 1
        a.rpc = AsyncMock()
        a.rpc.get_contact.return_value = {"name": "u"}
        a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
        a.handle_message = AsyncMock()
        msg = {"id": 7, "from_id": 9, "text": "", "view_type": view_type,
               "file": str(blob), "file_mime": mime, "file_name": "clip.bin"}
        return a, msg
    return _make


async def _event(a, msg):
    await a._handle_non_text_message(msg, "5", "7")
    return a.handle_message.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("view_type,mime", [
    ("Image", "image/jpeg"), ("Voice", "audio/ogg"), ("File", "application/pdf"),
    ("Video", "video/mp4"),
])
async def test_media_over_the_cap_is_not_passed_on(make, view_type, mime):
    a, msg = make(view_type, size=101, mime=mime, limit=100)
    event = await _event(a, msg)
    assert event.media_urls == []
    assert "over the inbound media size limit" in event.text


@pytest.mark.asyncio
async def test_media_within_the_cap_is_passed_on(make):
    a, msg = make("File", size=100, mime="application/pdf", limit=100)
    event = await _event(a, msg)
    assert event.media_urls == [msg["file"]]
    assert "limit" not in event.text


@pytest.mark.asyncio
async def test_zero_cap_means_no_cap(make):
    a, msg = make("Image", size=10_000, mime="image/jpeg", limit=0)
    assert (await _event(a, msg)).media_urls


@pytest.mark.asyncio
async def test_video_is_a_video_not_a_document(make):
    a, msg = make("Video", size=10, mime="video/mp4")
    event = await _event(a, msg)
    assert event.message_type == MockMessageType.VIDEO
    assert event.text.startswith("[Video from u: clip.bin]")
    a, msg = make("File", size=10, mime="application/pdf")
    assert (await _event(a, msg)).message_type == MockMessageType.DOCUMENT
