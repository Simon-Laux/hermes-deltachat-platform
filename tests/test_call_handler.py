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

    @pytest.mark.asyncio
    async def test_consume_drop_response_is_one_shot(self):
        mgr = self._manager()
        # _drop_next_response is a chat_id → count map: each call-end can inject
        # two AI notes (call thread + main thread), so one send() must not clear
        # a pending second drop.
        mgr._drop_next_response["12"] = 1
        assert mgr.consume_drop_response("12") is True   # call-ended note's reply → drop
        assert mgr.consume_drop_response("12") is False  # subsequent replies go through
        assert mgr.consume_drop_response("99") is False

        mgr._drop_next_response["12"] = 2
        assert mgr.consume_drop_response("12") is True
        assert mgr.consume_drop_response("12") is True   # second note also dropped
        assert mgr.consume_drop_response("12") is False


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
