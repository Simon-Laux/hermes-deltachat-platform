"""WebRTC loopback diagnostics for outgoing (offerer) calls.

The real failure is: as the WebRTC *offerer* (outgoing call), aiortc's ICE
never establishes a media path, while as the *answerer* (incoming) it works.
These tests negotiate two in-process aiortc RTCPeerConnections — a "bot"
offerer using our real CallManager._new_peer_connection setup (audio track +
the iceTrickling/mutedState negotiated data channels) against a mock
answerer — and check whether the offerer reaches connectionState=connected.

This answers ONE high-value question cheaply (no phone):
  * Does our offerer path connect against a cooperative aiortc answerer?
    - PASS  → the offerer code is fine; the real bug is specific to the
              browser/DC-mobile answerer (max-bundle + trickle + NAT).
    - FAIL  → the bug is in our offerer setup; iterate here.

CAVEAT: aiortc-vs-aiortc on loopback does NOT reproduce the browser's bundle/
trickle behavior or the NAT/TURN path, so a PASS here does not guarantee the
phone works — final confirmation still needs a live call. Treat as a fast
diagnostic + regression guard, not a workaround oracle.

Run: nix develop --command bash -c "cd tests && python3 -m pytest test_call_webrtc_loopback.py -q -s"
"""

import asyncio
import os
import sys

import pytest

# These run real aiortc ICE negotiation (with timeouts) — slow (~2 min total).
# Excluded from the default run; invoke with: pytest -m slow
pytestmark = pytest.mark.slow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor"))

import call_handler as ch  # noqa: E402
from aiortc import (  # noqa: E402
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    RTCBundlePolicy,
)
from aiortc.mediastreams import AudioStreamTrack  # noqa: E402


async def _gather(pc):
    """Wait until ICE gathering completes (mirrors CallManager._gather_ice)."""
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on():
        if pc.iceGatheringState == "complete":
            done.set()

    if pc.iceGatheringState == "complete":
        done.set()
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass


async def _wait_connected(pcs, timeout=15.0):
    """Wait until all pcs reach connectionState 'connected' (or fail/timeout)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        states = [pc.connectionState for pc in pcs]
        if all(s == "connected" for s in states):
            return True, states
        if any(s in ("failed", "closed") for s in states):
            return False, states
        await asyncio.sleep(0.1)
    return False, [pc.connectionState for pc in pcs]


def _make_answerer(bundle: bool):
    """A mock 'mobile' answerer: audio + the two negotiated data channels."""
    config = (RTCConfiguration(bundlePolicy=RTCBundlePolicy.MAX_BUNDLE)
              if bundle else RTCConfiguration())
    pc = RTCPeerConnection(config)
    pc.createDataChannel("iceTrickling", negotiated=True, id=1)
    pc.createDataChannel("mutedState", negotiated=True, id=3)
    pc.addTrack(AudioStreamTrack())
    return pc


def _make_offerer(bundle: bool, data_channels: bool):
    """A configurable offerer to A/B workarounds (no dependency on reverted code)."""
    config = (RTCConfiguration(bundlePolicy=RTCBundlePolicy.MAX_BUNDLE)
              if bundle else RTCConfiguration())
    pc = RTCPeerConnection(config)
    if data_channels:
        pc.createDataChannel("iceTrickling", negotiated=True, id=1)
        pc.createDataChannel("mutedState", negotiated=True, id=3)
    pc.addTrack(AudioStreamTrack())
    return pc


async def _negotiate(offerer, answerer):
    """Drive offer→answer→connect; return (ok, states)."""
    try:
        await offerer.setLocalDescription(await offerer.createOffer())
        await _gather(offerer)
        await answerer.setRemoteDescription(offerer.localDescription)
        await answerer.setLocalDescription(await answerer.createAnswer())
        await _gather(answerer)
        await offerer.setRemoteDescription(answerer.localDescription)
        return await _wait_connected([offerer, answerer])
    finally:
        await offerer.close()
        await answerer.close()


async def _run_real_offerer_vs_answerer(answerer_bundle: bool):
    """Negotiate the REAL CallManager offerer setup (as start_call builds it:
    _new_peer_connection(with_data_channels=False) + HermesAudioTrack) against
    an aiortc answerer. Ties the test to production code. Returns (ok, states)."""
    from unittest.mock import MagicMock, AsyncMock
    adapter = MagicMock()
    adapter.rpc.ice_servers = AsyncMock(return_value="[]")  # loopback: no TURN needed
    mgr = ch.CallManager(adapter=adapter)

    off, _off_ice = await mgr._new_peer_connection(with_data_channels=False)  # as start_call does
    off.addTrack(ch.HermesAudioTrack())
    answer_pc = _make_answerer(bundle=answerer_bundle)
    try:
        await off.setLocalDescription(await off.createOffer())
        await _gather(off)
        await answer_pc.setRemoteDescription(off.localDescription)
        await answer_pc.setLocalDescription(await answer_pc.createAnswer())
        await _gather(answer_pc)
        await off.setRemoteDescription(answer_pc.localDescription)
        return await _wait_connected([off, answer_pc])
    finally:
        await off.close()
        await answer_pc.close()


@pytest.mark.asyncio
async def test_real_callmanager_offerer_connects_vs_maxbundle_answerer():
    """The actual CallManager outgoing setup connects against a max-bundle
    answerer (the DC mobile). Exercises production code, not just a mock offer."""
    ok, states = await _run_real_offerer_vs_answerer(answerer_bundle=True)
    print(f"\n[real CallManager offerer vs max-bundle answerer] connected={ok} states={states}")
    assert ok, f"real offerer setup did not connect (states={states})"


@pytest.mark.asyncio
async def test_audio_only_offerer_connects_vs_maxbundle_answerer():
    """Regression guard: an AUDIO-ONLY offer (no data channels) must connect
    against a max-bundle answerer (the DC mobile). This is the outgoing-call fix
    — adding the iceTrickling/mutedState data channels breaks it (see matrix)."""
    offerer = _make_offerer(bundle=False, data_channels=False)
    answerer = _make_answerer(bundle=True)
    ok, states = await _negotiate(offerer, answerer)
    print(f"\n[audio-only offerer vs max-bundle answerer] connected={ok} states={states}")
    assert ok, f"audio-only offerer did not connect (states={states})"


@pytest.mark.asyncio
async def test_data_channel_offerer_fails_vs_maxbundle_answerer():
    """Documents the bug: offer WITH data channels does NOT connect against a
    max-bundle answerer. If this ever starts passing, aiortc fixed the offerer
    bundle issue and we could restore the data channels (and trickle)."""
    offerer = _make_offerer(bundle=False, data_channels=True)
    answerer = _make_answerer(bundle=True)
    ok, states = await _negotiate(offerer, answerer)
    print(f"\n[data-channel offerer vs max-bundle answerer] connected={ok} states={states}")
    assert not ok, "data-channel offerer now connects — aiortc may be fixed; revisit"


@pytest.mark.asyncio
async def test_workaround_matrix_vs_maxbundle_answerer():
    """A/B every offerer config against a max-bundle answerer (the real peer).

    Prints a matrix so we can pick a workaround that actually connects.
    Not an assertion test — it reports.
    """
    print("\n=== offerer config vs MAX-BUNDLE answerer (mimics DC mobile) ===")
    results = {}
    for off_bundle in (False, True):
        for data_ch in (True, False):
            label = f"offerer(bundle={off_bundle}, data_channels={data_ch})"
            offerer = _make_offerer(bundle=off_bundle, data_channels=data_ch)
            answerer = _make_answerer(bundle=True)
            try:
                ok, states = await _negotiate(offerer, answerer)
            except Exception as e:
                ok, states = False, [f"error: {e}"]
            results[label] = (ok, states)
            print(f"  {label:48s} -> connected={ok} {states}")
    print("=== end matrix ===")
    # at least the control (default-vs-default elsewhere) sanity; here just report
    assert results, "no results"


def _drop_all_datagrams(pc):
    """Make pc's ICE go silent both ways, like a peer whose network vanished."""
    async def _send_data(data, addr):
        pass

    for transceiver in pc.getTransceivers():
        ice = transceiver.receiver.transport.transport
        for protocol in ice._connection._protocols:
            protocol.datagram_received = lambda data, addr: None
            protocol.send_stun = lambda message, addr: None
            protocol.send_data = _send_data


@pytest.mark.asyncio
async def test_vanished_peer_ends_the_call():
    """A connected call whose peer goes silent is torn down by the bot.

    aioice's consent checks expire after ~30 s and aiortc closes the pc: the
    state goes connected -> closed, never "failed". Takes ~35 s.
    """
    from unittest.mock import MagicMock, AsyncMock
    adapter = MagicMock()
    adapter.rpc.ice_servers = AsyncMock(return_value="[]")
    adapter.rpc.end_call = AsyncMock()
    mgr = ch.CallManager(adapter=adapter)
    mgr._note_call_ended = AsyncMock()

    bot, bot_ice = await mgr._new_peer_connection(with_data_channels=False)
    out_track = ch.HermesAudioTrack()
    bot.addTrack(out_track)
    peer = _make_answerer(bundle=True)
    states = []
    bot.on("connectionstatechange", lambda: states.append(bot.connectionState))
    try:
        await bot.setLocalDescription(await bot.createOffer())
        await _gather(bot)
        await peer.setRemoteDescription(bot.localDescription)
        await peer.setLocalDescription(await peer.createAnswer())
        await _gather(peer)
        await bot.setRemoteDescription(peer.localDescription)
        ok, st = await _wait_connected([bot, peer])
        assert ok, f"loopback did not connect (states={st})"

        mgr._register_session(bot, bot_ice, out_track, MagicMock(), 5, "12", "10", "Bob")
        _drop_all_datagrams(peer)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + 90
        while 5 in mgr._sessions and loop.time() < deadline:
            await asyncio.sleep(0.5)
        print(f"\n[vanished peer] bot states={states}")
        assert 5 not in mgr._sessions, f"dead call not torn down (states={states})"
        adapter.rpc.end_call.assert_awaited_once_with(adapter.account_id, 5)
        mgr._note_call_ended.assert_called_once()
    finally:
        await bot.close()
        await peer.close()
