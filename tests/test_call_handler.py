"""Unit tests for the voice-call handler's pure / unit-testable parts.

Covers sentence splitting, env-flag parsing, the barge-in frame→char mapping,
and the HermesAudioTrack queue/playback/flush accounting + TTS decode (no
padding). Networked pieces (STT/TTS/WebRTC signalling) are not exercised here.

conftest.py installs the gateway mocks; aiortc/av come from the nix dev shell.
Run via:  nix develop --command bash -c "cd tests && python3 -m pytest test_call_handler.py"
"""

import asyncio
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor"))

import call_handler as ch  # noqa: E402


# ---------------------------------------------------------------------------
# _split_sentences
# ---------------------------------------------------------------------------

class TestSplitSentences:
    def test_empty(self):
        assert ch._split_sentences("") == []
        assert ch._split_sentences("   ") == []

    def test_no_terminator_single_chunk(self):
        assert ch._split_sentences("just one line no period") == ["just one line no period"]

    def test_long_multi_sentence_splits_per_sentence(self):
        text = (
            "The weather today is sunny with a gentle breeze. "
            "Temperatures will reach about twenty degrees by noon. "
            "There is a small chance of rain in the evening."
        )
        chunks = ch._split_sentences(text)
        assert len(chunks) == 3
        assert chunks[0].startswith("The weather")
        assert chunks[1].startswith("Temperatures")
        assert chunks[2].startswith("There is")

    def test_tiny_fragments_are_merged(self):
        # Each fragment is below _MIN_TTS_SENTENCE_CHARS → merged into one chunk
        chunks = ch._split_sentences("Yes. No. Ok.")
        assert len(chunks) == 1

    def test_question_and_exclamation_terminators(self):
        text = "Are you absolutely sure about that? Yes I am completely certain!"
        chunks = ch._split_sentences(text)
        assert len(chunks) == 2

    def test_no_text_is_lost(self):
        text = "First long enough sentence here. Second long enough sentence here."
        chunks = ch._split_sentences(text)
        joined = " ".join(chunks)
        # every word survives splitting
        for word in text.replace(".", "").split():
            assert word in joined


# ---------------------------------------------------------------------------
# _env_flag
# ---------------------------------------------------------------------------

class TestEnvFlag:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
    def test_truthy(self, monkeypatch, val):
        monkeypatch.setenv("DC_TEST_FLAG", val)
        assert ch._env_flag("DC_TEST_FLAG") is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "nonsense"])
    def test_falsy(self, monkeypatch, val):
        monkeypatch.setenv("DC_TEST_FLAG", val)
        assert ch._env_flag("DC_TEST_FLAG") is False

    def test_unset(self, monkeypatch):
        monkeypatch.delenv("DC_TEST_FLAG", raising=False)
        assert ch._env_flag("DC_TEST_FLAG") is False


# ---------------------------------------------------------------------------
# CallManager._frames_to_chars (barge-in attribution)
# ---------------------------------------------------------------------------

class TestFramesToChars:
    CPS = [(10, 100), (25, 250), (40, 400)]  # (cum_chars, cum_frames) per sentence

    def test_no_checkpoints(self):
        assert ch.CallManager._frames_to_chars(123, [], 40) == 0

    def test_nothing_played(self):
        assert ch.CallManager._frames_to_chars(0, self.CPS, 40) == 0

    def test_exact_first_checkpoint(self):
        assert ch.CallManager._frames_to_chars(100, self.CPS, 40) == 10

    def test_interpolates_mid_sentence(self):
        # halfway through the 2nd sentence (100→250 frames, 10→25 chars)
        assert ch.CallManager._frames_to_chars(175, self.CPS, 40) == 17

    def test_played_all_caps_at_text_len(self):
        assert ch.CallManager._frames_to_chars(9999, self.CPS, 40) == 40

    def test_never_exceeds_text_len(self):
        # text_len smaller than checkpoint chars → clamp
        assert ch.CallManager._frames_to_chars(9999, self.CPS, 30) == 30


# ---------------------------------------------------------------------------
# HermesAudioTrack — queue / flush / played accounting
# ---------------------------------------------------------------------------

def _make_frames(n):
    """n silent 960-sample mono s16 frames."""
    import av
    frames = []
    for _ in range(n):
        f = av.AudioFrame(format="s16", layout="mono", samples=ch.HermesAudioTrack._FRAME_SAMPLES)
        for p in f.planes:
            p.update(bytes(p.buffer_size))
        f.sample_rate = ch._SAMPLE_RATE
        frames.append(f)
    return frames


class TestHermesAudioTrack:
    def test_is_speaking_and_flush(self):
        t = ch.HermesAudioTrack()
        assert t.is_speaking() is False
        t.enqueue_tts_frames(_make_frames(5))
        assert t.is_speaking() is True
        dropped = t.flush()
        assert dropped == 5
        assert t.is_speaking() is False

    def test_played_count_starts_zero(self):
        assert ch.HermesAudioTrack().played_count == 0

    @pytest.mark.asyncio
    async def test_recv_plays_queued_then_silence(self):
        t = ch.HermesAudioTrack()
        t.enqueue_tts_frames(_make_frames(2))
        f1 = await t.recv()
        f2 = await t.recv()
        assert f1.samples == ch.HermesAudioTrack._FRAME_SAMPLES
        assert t.played_count == 2          # both queued frames counted
        # queue now empty → silence frame, played_count unchanged
        f3 = await t.recv()
        assert f3 is not None
        assert t.played_count == 2

    @pytest.mark.asyncio
    async def test_flush_after_partial_play_reports_remaining(self):
        t = ch.HermesAudioTrack()
        t.enqueue_tts_frames(_make_frames(10))
        await t.recv()
        await t.recv()
        await t.recv()
        assert t.played_count == 3
        dropped = t.flush()
        assert dropped == 7                 # 10 enqueued - 3 played


# ---------------------------------------------------------------------------
# HermesAudioTrack.decode_tts — clean 960-sample frames, no padding
# ---------------------------------------------------------------------------

class TestBargeIn:
    """_handle_barge_in: only fires while speaking, cancels a pending hangup."""

    def _session(self, n_frames):
        from unittest.mock import MagicMock
        track = ch.HermesAudioTrack()
        track.enqueue_tts_frames(_make_frames(n_frames))
        return ch.CallSession(
            pc=MagicMock(), chat_id="12", msg_id=1, caller_id="11", caller_name="X",
            outgoing_track=track, audio_buffer=MagicMock(), ice_channel=MagicMock(),
            last_response_text="Hello there friend. How are you doing today?",
        )

    def _manager(self, session):
        from unittest.mock import MagicMock
        mgr = ch.CallManager(adapter=MagicMock())
        mgr._sessions[session.msg_id] = session
        mgr._chat_to_msg[session.chat_id] = session.msg_id
        return mgr

    @pytest.mark.asyncio
    async def test_no_interrupt_when_not_speaking(self):
        session = self._session(0)            # nothing queued
        session.is_responding = False
        mgr = self._manager(session)
        mgr._handle_barge_in(1)
        assert session.interrupted is False   # nothing to interrupt

    @pytest.mark.asyncio
    async def test_interrupt_flushes_and_stops_tts(self):
        session = self._session(10)
        session.is_responding = True
        mgr = self._manager(session)
        mgr._handle_barge_in(1)
        assert session.interrupted is True
        assert session.outgoing_track.is_speaking() is False   # queue flushed

    @pytest.mark.asyncio
    async def test_barge_in_cancels_pending_hangup(self):
        session = self._session(10)
        session.hangup_pending = True
        session.hanging_up = True             # goodbye drain in progress
        mgr = self._manager(session)
        mgr._handle_barge_in(1)
        assert session.hangup_pending is False
        assert session.hangup_cancelled is True   # _hangup_session will abort


class TestHangupMarker:
    """A reply ending in [[hangup]] is spoken without the marker, then hangs up."""

    def _manager(self, monkeypatch):
        import json
        import types
        from unittest.mock import AsyncMock, MagicMock

        spoken = []
        fake_tts = types.ModuleType("tools.tts_tool")
        # TTS "fails" so no audio decode is needed; we only check what was sent.
        fake_tts.text_to_speech_tool = lambda s: spoken.append(s) or json.dumps({"success": False})
        monkeypatch.setitem(sys.modules, "tools.tts_tool", fake_tts)

        session = ch.CallSession(
            pc=MagicMock(), chat_id="12", msg_id=1, caller_id="11", caller_name="X",
            outgoing_track=ch.HermesAudioTrack(), audio_buffer=MagicMock(),
            ice_channel=MagicMock(),
        )
        mgr = ch.CallManager(adapter=MagicMock())
        mgr._sessions[1] = session
        mgr._chat_to_msg["12"] = 1
        mgr._hangup_session = AsyncMock()
        return mgr, session, spoken

    @pytest.mark.asyncio
    async def test_marker_is_stripped_and_hangs_up(self, monkeypatch):
        mgr, session, spoken = self._manager(monkeypatch)
        await mgr._play_response("12", "Tschüss, bis bald! [[hangup]]")
        assert spoken == ["Tschüss, bis bald!"]
        mgr._hangup_session.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_marker_spelling_is_lenient(self, monkeypatch):
        mgr, _, spoken = self._manager(monkeypatch)
        await mgr._play_response("12", "Bye! [[ Hang-Up ]]")
        assert spoken == ["Bye!"]
        mgr._hangup_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_marker_alone_hangs_up_without_speaking(self, monkeypatch):
        mgr, _, spoken = self._manager(monkeypatch)
        await mgr._play_response("12", "[[hangup]]")
        assert spoken == []
        mgr._hangup_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_marker_keeps_the_call(self, monkeypatch):
        mgr, _, spoken = self._manager(monkeypatch)
        await mgr._play_response("12", "Sure, here is a joke.")
        assert spoken == ["Sure, here is a joke."]
        mgr._hangup_session.assert_not_awaited()


class TestIncomingCallAuthorization:
    """Calls from contacts Hermes wouldn't talk to are declined, not answered."""

    def _manager(self, verdict):
        from unittest.mock import AsyncMock, MagicMock
        adapter = MagicMock()
        adapter.rpc.get_message = AsyncMock(return_value={"from_id": 10})
        adapter.rpc.get_contact = AsyncMock(return_value={"name": "Eve"})
        adapter.rpc.end_call = AsyncMock()
        adapter._is_sender_authorized = MagicMock(return_value=verdict)
        mgr = ch.CallManager(adapter=adapter)
        mgr._answer_call = AsyncMock()
        mgr._warmup_stt = AsyncMock()
        return mgr, adapter

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", [False, None])
    async def test_unauthorized_or_unknown_caller_is_declined(self, verdict):
        # None: the gateway's check raised or returned a non-bool -- fail closed
        mgr, adapter = self._manager(verdict)
        await mgr._handle_incoming_call({"msg_id": 5, "chat_id": 12, "place_call_info": "sdp"})
        adapter._is_sender_authorized.assert_called_once_with("10", "dm", "12")
        adapter.rpc.end_call.assert_awaited_once()
        mgr._answer_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorized_caller_is_answered(self):
        mgr, adapter = self._manager(True)
        await mgr._handle_incoming_call({"msg_id": 5, "chat_id": 12, "place_call_info": "sdp"})
        mgr._answer_call.assert_awaited_once()
        adapter.rpc.end_call.assert_not_awaited()


class TestOutgoingCall:
    """Answer-future resolution for outgoing calls."""

    def _manager(self):
        from unittest.mock import MagicMock
        return ch.CallManager(adapter=MagicMock())

    @pytest.mark.asyncio
    async def test_accepted_resolves_answer_future(self):
        mgr = self._manager()
        fut = asyncio.get_running_loop().create_future()
        mgr._pending_answers[42] = fut
        await mgr.handle_outgoing_call_accepted(
            {"msg_id": 42, "accept_call_info": "v=0 ...sdp..."}
        )
        assert fut.done() and fut.result() == "v=0 ...sdp..."
        assert 42 not in mgr._pending_answers   # consumed

    @pytest.mark.asyncio
    async def test_accepted_without_sdp_sets_exception(self):
        mgr = self._manager()
        fut = asyncio.get_running_loop().create_future()
        mgr._pending_answers[7] = fut
        await mgr.handle_outgoing_call_accepted({"msg_id": 7, "accept_call_info": ""})
        assert fut.done()
        with pytest.raises(RuntimeError):
            fut.result()

    @pytest.mark.asyncio
    async def test_accepted_unknown_call_is_noop(self):
        mgr = self._manager()
        # no future registered → should not raise
        await mgr.handle_outgoing_call_accepted({"msg_id": 999, "accept_call_info": "x"})

    @pytest.mark.asyncio
    async def test_call_ended_wakes_pending_waiter(self):
        mgr = self._manager()
        fut = asyncio.get_running_loop().create_future()
        mgr._pending_answers[5] = fut
        await mgr.handle_call_ended({"msg_id": 5})
        assert fut.done()
        with pytest.raises(RuntimeError):
            fut.result()

    def test_call_end_reply_is_recognised_by_its_anchor(self):
        # Both injected notes (call thread + main thread) share the prefix.
        assert ch.CallManager.is_call_end_reply("callend-35422583") is True
        assert ch.CallManager.is_call_end_reply("callend-main-35422590") is True
        # Real DC message ids and missing anchors go through.
        assert ch.CallManager.is_call_end_reply("1756") is False
        assert ch.CallManager.is_call_end_reply(None) is False
        assert ch.CallManager.is_call_end_reply("") is False


class TestDeadCall:
    """A peer that vanishes never sends CallEnded; the failed pc ends the call."""

    class _FakePc:
        def __init__(self):
            self.connectionState = "connected"
            self._handlers = {}

        def on(self, event):
            def deco(fn):
                self._handlers.setdefault(event, []).append(fn)
                return fn
            return deco

        def set_state(self, state):
            self.connectionState = state
            for fn in self._handlers.get("connectionstatechange", []):
                fn()

        async def close(self):
            pass

    def _manager(self):
        from unittest.mock import AsyncMock, MagicMock
        adapter = MagicMock()
        adapter.rpc.end_call = AsyncMock()
        mgr = ch.CallManager(adapter=adapter)
        mgr._note_call_ended = AsyncMock()
        return mgr, adapter

    def _register(self, mgr, msg_id, chat_id="12"):
        from unittest.mock import MagicMock
        pc = self._FakePc()
        mgr._register_session(pc, None, ch.HermesAudioTrack(), MagicMock(),
                              msg_id, chat_id, "10", "Bob")
        return pc

    @pytest.mark.asyncio
    async def test_failed_connection_hangs_up_and_tears_down(self):
        mgr, adapter = self._manager()
        pc = self._register(mgr, 5)
        pc.set_state("failed")
        for _ in range(5):
            await asyncio.sleep(0)
        adapter.rpc.end_call.assert_awaited_once_with(adapter.account_id, 5)
        assert 5 not in mgr._sessions
        assert not mgr.has_active_call("12")

    @pytest.mark.asyncio
    async def test_other_states_keep_the_call(self):
        mgr, adapter = self._manager()
        pc = self._register(mgr, 5)
        pc.set_state("connecting")
        await asyncio.sleep(0)
        adapter.rpc.end_call.assert_not_awaited()
        assert mgr.has_active_call("12")

    @pytest.mark.asyncio
    async def test_old_call_teardown_keeps_redialled_call(self):
        mgr, _ = self._manager()
        self._register(mgr, 5)
        self._register(mgr, 6)   # same chat, redial before 5 was noticed dead
        await mgr._teardown_session(5)
        assert mgr._chat_to_msg["12"] == 6

    @pytest.mark.asyncio
    async def test_closed_connection_hangs_up(self):
        # real aiortc: consent expiry closes a connected pc, it never says "failed"
        mgr, adapter = self._manager()
        pc = self._register(mgr, 5)
        pc.set_state("closed")
        for _ in range(5):
            await asyncio.sleep(0)
        adapter.rpc.end_call.assert_awaited_once_with(adapter.account_id, 5)
        assert 5 not in mgr._sessions

    @pytest.mark.asyncio
    async def test_own_teardown_close_does_not_end_call_again(self):
        mgr, adapter = self._manager()
        pc = self._register(mgr, 5)

        async def close():
            pc.set_state("closed")
        pc.close = close
        await mgr._teardown_session(5)
        for _ in range(5):
            await asyncio.sleep(0)
        adapter.rpc.end_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pc_dead_before_registration_is_torn_down(self):
        from unittest.mock import MagicMock
        mgr, adapter = self._manager()
        pc = self._FakePc()
        pc.connectionState = "failed"   # no state change left to fire
        mgr._register_session(pc, None, ch.HermesAudioTrack(), MagicMock(),
                              5, "12", "10", "Bob")
        for _ in range(5):
            await asyncio.sleep(0)
        adapter.rpc.end_call.assert_awaited_once_with(adapter.account_id, 5)
        assert 5 not in mgr._sessions

    @pytest.mark.asyncio
    async def test_outgoing_call_dead_before_opening_sends_no_greeting(self):
        from unittest.mock import AsyncMock, MagicMock
        mgr, _ = self._manager()
        mgr._play_greeting = AsyncMock()
        mgr._log_media_stats = AsyncMock()
        pc = self._register(mgr, 5)
        pc.set_state("closed")   # died while _finalize_outgoing_call waited
        for _ in range(5):
            await asyncio.sleep(0)
        await mgr._finalize_outgoing_call(pc, 5, "12", "10", "Bob",
                                          ch.HermesAudioTrack(), MagicMock(), "", None)
        await asyncio.sleep(0)
        mgr._play_greeting.assert_not_called()


class TestDecodeTts:
    def _write_wav(self, path, seconds=0.4, rate=22050):
        # mono s16 sine-ish (just nonzero) to mimic a TTS mp3's mono low rate
        import struct
        nframes = int(seconds * rate)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(b"".join(struct.pack("<h", (i % 100) * 100 - 5000) for i in range(nframes)))

    def test_decode_yields_960_sample_mono_48k_frames(self, tmp_path):
        wav = tmp_path / "tts.wav"
        self._write_wav(wav)
        frames = ch.HermesAudioTrack.decode_tts(str(wav))
        assert len(frames) > 1
        f0 = frames[0]
        assert f0.format.name == "s16"
        assert f0.layout.name == "mono"
        assert f0.sample_rate == ch._SAMPLE_RATE
        # all but possibly the last frame are exactly one Opus frame
        assert all(f.samples == ch.HermesAudioTrack._FRAME_SAMPLES for f in frames[:-1])

    def test_decode_duration_matches_input(self, tmp_path):
        # Total decoded samples ≈ input duration resampled to 48 kHz. This
        # catches the old padding bug (which inflated the sample stream) without
        # needing numpy: we sum frame.samples, the real (un-padded) counts.
        seconds, rate = 0.4, 22050
        wav = tmp_path / "tts.wav"
        self._write_wav(wav, seconds=seconds, rate=rate)
        frames = ch.HermesAudioTrack.decode_tts(str(wav))
        total = sum(f.samples for f in frames)
        expected = seconds * ch._SAMPLE_RATE        # 48 kHz target
        assert abs(total - expected) < ch.HermesAudioTrack._FRAME_SAMPLES * 2


class TestPerCallSession:
    """Each call gets its own session thread, so old calls never pile up."""

    def test_thread_id_is_per_call(self, monkeypatch):
        monkeypatch.setattr(ch, "_CALL_THREAD_ID", "call")
        assert ch._call_thread_id(1780) == "call-1780"
        assert ch._call_thread_id(1781) != ch._call_thread_id(1780)

    def test_replies_from_any_call_thread_are_spoken(self, monkeypatch):
        monkeypatch.setattr(ch, "_CALL_THREAD_ID", "call")
        assert ch.CallManager.is_call_thread("call-1780") is True
        assert ch.CallManager.is_call_thread(None) is False      # text chat
        assert ch.CallManager.is_call_thread("") is False

    def test_shared_history_mode_has_no_call_thread(self, monkeypatch):
        monkeypatch.setattr(ch, "_CALL_THREAD_ID", None)
        assert ch._call_thread_id(1780) is None
        assert ch.CallManager.is_call_thread(None) is True


class TestCallModelOverride:
    """DELTACHAT_CALL_MODEL reaches the gateway session even when the message
    handler is a closure (Hermes ≥ 0.21.5) rather than a bound method."""

    class _Runner:
        def __init__(self):
            self._session_model_overrides = {}

        def _session_key_for_source(self, source):
            return "agent:main:deltachat-platform:dm:12:call"

        async def _handle_message(self, event):  # pre-0.21.5 bound handler
            return None

    def _setup(self, monkeypatch, *, runner_ref, handler):
        import types
        from unittest.mock import MagicMock
        monkeypatch.setattr(ch, "_CALL_MODEL", "ministral-14b-2512")
        fake_run = types.ModuleType("gateway.run")
        fake_run._gateway_runner_ref = runner_ref
        monkeypatch.setitem(sys.modules, "gateway.run", fake_run)
        adapter = MagicMock()
        adapter._message_handler = handler
        mgr = ch.CallManager(adapter=adapter)
        return mgr, MagicMock(model_override_key=None)

    @pytest.mark.asyncio
    async def test_closure_handler_uses_the_runner_weakref(self, monkeypatch):
        runner = self._Runner()

        async def closure(*args):  # what _standalone_scoped installs
            return None

        mgr, session = self._setup(monkeypatch, runner_ref=lambda: runner, handler=closure)
        mgr._install_model_override(session, source=object())

        key = "agent:main:deltachat-platform:dm:12:call"
        assert runner._session_model_overrides[key]["model"] == "ministral-14b-2512"
        assert session.model_override_key == key

    @pytest.mark.asyncio
    async def test_bound_handler_still_works_without_the_weakref(self, monkeypatch):
        runner = self._Runner()
        mgr, session = self._setup(
            monkeypatch, runner_ref=lambda: None, handler=runner._handle_message
        )
        mgr._install_model_override(session, source=object())
        assert runner._session_model_overrides  # found via __self__

    @pytest.mark.asyncio
    async def test_greeting_turn_already_uses_the_call_model(self, monkeypatch):
        """The greeting is the first turn — seen live: a call hung up before the
        first sentence ran entirely on the default model."""
        from unittest.mock import AsyncMock
        runner = self._Runner()

        async def closure(*args):
            return None

        mgr, session = self._setup(monkeypatch, runner_ref=lambda: runner, handler=closure)
        mgr._sessions[1] = session
        mgr._to_hermes = AsyncMock()
        # conftest's MockMessageEvent predates channel_prompt; any kwargs will do here.
        import types
        monkeypatch.setattr(sys.modules["gateway.platforms.base"], "MessageEvent",
                            lambda **kw: types.SimpleNamespace(**kw))

        await mgr._play_greeting(1, "12", "11", "X")

        assert runner._session_model_overrides  # installed before the greeting turn
        mgr._to_hermes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unreachable_runner_warns(self, monkeypatch, caplog):
        async def closure(*args):
            return None

        mgr, session = self._setup(monkeypatch, runner_ref=lambda: None, handler=closure)
        with caplog.at_level("WARNING"):
            mgr._install_model_override(session, source=object())
        assert "DELTACHAT_CALL_MODEL" in caplog.text
        assert session.model_override_key is None
