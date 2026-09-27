"""Voice call handler for the Delta Chat Hermes adapter.

Handles WebRTC signalling for incoming DC calls using aiortc.
Signalling transport: DC messages carry raw SDP offer/answer strings.
Audio pipeline: aiortc AudioFrame → silence detect → STT → Hermes AI → TTS → outgoing track.

Architecture note: only the DC-specific WebRTC signalling lives here.
STT (transcription_tools), TTS (tts_tool), and the Hermes AI session pipeline
are all reused unchanged — we bridge into them via fake MessageEvents and
by intercepting send() in the adapter.
"""

import asyncio
import contextlib
import fractions
import json
import logging
import os
import sys
import threading
import time
import wave
import re as _re
from collections import Counter
from dataclasses import dataclass, field

# On NixOS the gateway process does not inherit PYTHONPATH from ~/.hermes/.env
# (that only applies to agent subprocesses).  If the user created the aiortc
# GC-root symlink at ~/.hermes/aiortc-env (see docs/nixos-installation.md),
# add it to sys.path so the import works without requiring environment setup.
# On non-NixOS systems aiortc is installed normally and this block is a no-op.
_AIORTC_ENV = os.path.expanduser("~/.hermes/aiortc-env")
if os.path.isdir(_AIORTC_ENV):
    import glob as _glob

    for _site in _glob.glob(
        os.path.join(_AIORTC_ENV, "lib", "python3.*", "site-packages")
    ):
        if _site not in sys.path:
            sys.path.insert(0, _site)
            break
from pathlib import Path  # noqa: E402
from typing import Any, Callable, Dict, Optional  # noqa: E402

import av  # noqa: E402
from aiortc import (  # noqa: E402
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import AudioStreamTrack  # noqa: E402

logger = logging.getLogger("hermes_plugins.deltachat.calls")

# Silence detection thresholds (mirroring Discord VoiceReceiver)
_SILENCE_THRESHOLD_S = (
    1.0  # seconds of silence → end of utterance (shorter = more responsive)
)
_MIN_SPEECH_S = 0.5  # minimum utterance duration to process
_RMS_THRESHOLD = 200  # below this → silence (int16 range 0-32767)
_MIN_VOICED_S = 0.3  # require this much actual voiced audio to process
_BARGE_IN_MIN_VOICED_S = 0.25  # sustained speech before counting as a barge-in
_SAMPLE_RATE = 48000  # aiortc delivers audio at 48 kHz
_CHANNELS = 2  # stereo
_BYTES_PER_SAMPLE = 2  # int16
_BYTES_PER_SEC = _SAMPLE_RATE * _CHANNELS * _BYTES_PER_SAMPLE  # 192000
# Buffer is stored at 16 kHz mono (after resampling for STT)
_STT_RATE = 16000
_STT_BYTES_PER_SEC = _STT_RATE * 1 * _BYTES_PER_SAMPLE  # 32000
# Safety ceiling for a single utterance (forces a flush so the buffer can never
# grow without bound during continuous speech).
_MAX_UTTERANCE_S = 60.0
_MAX_SPEECH_BUF_BYTES = int(_MAX_UTTERANCE_S * _STT_BYTES_PER_SEC)  # ~1.9 MB

# ICE gathering timeout before accepting the call anyway
_ICE_GATHER_TIMEOUT_S = 10.0

# How long an outgoing call rings before we give up waiting for an answer
_OUTGOING_CALL_TIMEOUT_S = 40.0

# Minimum characters before a sentence is flushed to TTS on its own. Short
# leading fragments ("Yes." "OK.") are merged with the next so TTS isn't choppy.
_MIN_TTS_SENTENCE_CHARS = 25

_SENTENCE_SPLIT_RE = _re.compile(r"(?<=[.!?…])\s+")


def _split_sentences(text: str) -> list:
    """Split text into sentence-ish chunks for incremental TTS.

    Greedily merges fragments until each chunk is at least
    _MIN_TTS_SENTENCE_CHARS so we don't synthesize tiny choppy clips. The first
    chunk is intentionally kept small so playback can start as soon as possible.
    """
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text.strip()) if p]
    if not parts:
        return []
    chunks, buf = [], ""
    for part in parts:
        buf = f"{buf} {part}".strip() if buf else part
        if len(buf) >= _MIN_TTS_SENTENCE_CHARS:
            chunks.append(buf)
            buf = ""
    if buf:
        # merge a tiny trailing remainder into the previous chunk
        if chunks and len(buf) < _MIN_TTS_SENTENCE_CHARS:
            chunks[-1] = f"{chunks[-1]} {buf}"
        else:
            chunks.append(buf)
    return chunks


# Delta Chat's reserved contact id for the account itself.
_DC_CONTACT_ID_SELF = 1


def _contact_name(contact: dict, fallback: str) -> str:
    """Best display name from a ``get_contact`` snapshot."""
    return (
        contact.get("name")
        or contact.get("display_name")
        or contact.get("name_and_addr")
        or fallback
    )


def _take_one(counter: Counter, key: str) -> bool:
    """Decrement *counter[key]* if positive; return whether it was."""
    if counter[key] <= 0:
        return False
    counter[key] -= 1
    if not counter[key]:
        del counter[key]
    return True


# ---------------------------------------------------------------------------
# Configuration (all via environment variables — see docs/voice-calls.md)
# ---------------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# Opt-in deep WebRTC/ICE debugging — turns aioice + aiortc to DEBUG so the log
# shows every STUN connectivity check, candidate-pair transition, and TURN op.
# Use to diagnose why a call's ICE doesn't connect. Very verbose.
if _env_flag("DELTACHAT_CALL_ICE_DEBUG"):
    for _name in ("aioice", "aioice.ice", "aioice.turn", "aioice.stun", "aiortc"):
        logging.getLogger(_name).setLevel(logging.DEBUG)
    logger.info("ICE debug logging enabled (aioice + aiortc at DEBUG)")

# Opt-in: route call audio to Mistral Voxtral cloud STT (fast, ~1-2s, accurate).
# Default off → use the locally configured STT provider (e.g. faster-whisper),
# which is much slower on CPU. Requires MISTRAL_API_KEY.
_CALL_STT_VOXTRAL = _env_flag("DELTACHAT_CALL_STT_VOXTRAL")

# Per-call system prompt — keeps spoken replies short. Applied via the
# MessageEvent.channel_prompt field (ephemeral, never persisted to history).
# Override the text with DELTACHAT_CALL_PROMPT.
_DEFAULT_CALL_PROMPT = (
    "You are speaking with the user on a live voice phone call. "
    "Your reply will be read aloud by text-to-speech, so keep it to 1-2 short, "
    "natural spoken sentences. Do not use markdown, lists, code blocks, emojis, "
    "or URLs. Be conversational and concise. "
    "For anything complex, long-running, or research-heavy, delegate to a "
    "subagent (it runs on the more capable default model) and then give a brief "
    "spoken summary of the result — do not attempt heavy work inline, especially "
    "since this call may be running on a smaller, faster model. "
    "When the user says goodbye or asks to end the call, end it gracefully: "
    "say goodbye, then call dc_end_call to hang up. The tool waits for your "
    "goodbye to finish playing before disconnecting."
)
_CALL_PROMPT = os.getenv("DELTACHAT_CALL_PROMPT", _DEFAULT_CALL_PROMPT).strip()

# Isolate the call conversation in its own session so spoken turns don't mix
# into the text DM history. A DC call happens inside the contact's chat, so
# without this the call shares the text DM's session key. Setting a distinct
# thread_id ("call") gives it a separate session. Opt into shared history
# (continuity between call and text, at the cost of mixing messy transcripts
# into the text chat) with DELTACHAT_CALL_SHARED_HISTORY=true.
_CALL_SHARED_HISTORY = _env_flag("DELTACHAT_CALL_SHARED_HISTORY")
_CALL_THREAD_ID = None if _CALL_SHARED_HISTORY else "call"

# Optional per-call LLM override (off by default — see docs). When set, calls
# use this model instead of the chat's normal model, restored on hangup.
_CALL_MODEL = os.getenv("DELTACHAT_CALL_MODEL", "").strip()
_CALL_MODEL_PROVIDER = os.getenv("DELTACHAT_CALL_MODEL_PROVIDER", "").strip()
_CALL_MODEL_API_KEY = os.getenv("DELTACHAT_CALL_MODEL_API_KEY", "").strip()
_CALL_MODEL_BASE_URL = os.getenv("DELTACHAT_CALL_MODEL_BASE_URL", "").strip()


# ---------------------------------------------------------------------------
# Outgoing audio track
# ---------------------------------------------------------------------------


class HermesAudioTrack(AudioStreamTrack):
    """aiortc AudioStreamTrack that plays queued TTS audio, silence otherwise.

    TTS audio files (mp3/ogg) are enqueued via enqueue_tts() which decodes
    them with av and resamples to 48 kHz stereo int16 (aiortc's format).
    Between queued frames recv() returns silence so the connection stays alive.
    """

    kind = "audio"
    _FRAME_SAMPLES = 960  # 20 ms at 48 kHz

    def __init__(self):
        super().__init__()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._played_count = (
            0  # monotonic count of real TTS frames sent (for barge-in accounting)
        )

    @property
    def played_count(self) -> int:
        return self._played_count

    def is_speaking(self) -> bool:
        """True if there are queued TTS frames still to play."""
        return not self._queue.empty()

    def flush(self) -> int:
        """Drop all queued TTS frames (barge-in). Return number of frames dropped."""
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    async def recv(self) -> av.AudioFrame:
        # Mirror AudioStreamTrack base pacing (next_timestamp() is VideoStreamTrack-only).
        from aiortc.mediastreams import MediaStreamError

        if self.readyState != "live":
            raise MediaStreamError

        if hasattr(self, "_timestamp"):
            self._timestamp += self._FRAME_SAMPLES
            wait = self._start + (self._timestamp / _SAMPLE_RATE) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            self._start = time.time()
            self._timestamp = 0

        try:
            frame = self._queue.get_nowait()
            self._played_count += 1
        except asyncio.QueueEmpty:
            # Zero-fill the silence frame — uninitialized memory sounds like noise.
            frame = av.AudioFrame(
                format="s16", layout="mono", samples=self._FRAME_SAMPLES
            )
            for p in frame.planes:
                p.update(bytes(p.buffer_size))
            frame.sample_rate = _SAMPLE_RATE

        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, _SAMPLE_RATE)
        return frame

    def enqueue_tts_frames(self, frames: list) -> None:
        """Enqueue pre-decoded 960-sample frames for playback.

        Frames come from _decode_tts() (run in a thread).  We enqueue the frame
        objects directly — never reconstruct from bytes(plane), which would
        include the plane's alignment padding (a classic PyAV gotcha that injects
        garbage samples and causes audible clicking every ~25 ms).
        """
        for frame in frames:
            self._queue.put_nowait(frame)
        logger.debug(
            "Enqueued %d TTS frames → %d queued", len(frames), self._queue.qsize()
        )

    @staticmethod
    def decode_tts(file_path: str) -> list:
        """Decode + resample a TTS file to 48 kHz mono 960-sample frames.

        Pure CPU work (no queue access) — safe to run via asyncio.to_thread.
        Uses the resampler's frame_size so it emits exactly-sized, internally
        continuous frames whose .samples reflect real data (no padding).
        """
        container = av.open(file_path)
        resampler = av.AudioResampler(
            format="s16",
            layout="mono",
            rate=_SAMPLE_RATE,
            frame_size=HermesAudioTrack._FRAME_SAMPLES,
        )
        frames = []
        for packet in container.demux(audio=0):
            for decoded in packet.decode():
                frames.extend(resampler.resample(decoded))
        frames.extend(resampler.resample(None))  # flush tail
        container.close()
        return frames


# ---------------------------------------------------------------------------
# Incoming audio buffer + silence detection
# ---------------------------------------------------------------------------


class IncomingAudioBuffer:
    """Buffers incoming AudioFrames, detects utterance boundaries, fires STT.

    Silence detection mirrors Discord's VoiceReceiver:
    - _SILENCE_THRESHOLD_S of silence after enough voiced audio → utterance complete
    - PCM resampled 48 kHz stereo → 16 kHz mono WAV via av before STT
    """

    def __init__(
        self,
        hermes_home: str,
        on_utterance: Callable[[str, str], None],
        on_speech_confirmed: Optional[Callable[[], None]] = None,
    ) -> None:
        # on_utterance(transcript, wav_path); on_speech_confirmed() fires once the
        # caller has produced sustained voiced audio (~0.25s) — used for barge-in,
        # gated to avoid clicks/noise triggering a false interrupt.
        self._on_utterance = on_utterance
        self._on_speech_confirmed = on_speech_confirmed
        self._audio_cache = Path(hermes_home) / "audio_cache"
        self._audio_cache.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stt_lock = asyncio.Semaphore(
            1
        )  # serialize STT — whisper isn't parallel-safe

    def start(self, track) -> None:
        self._running = True
        self._task = asyncio.ensure_future(self._receive_loop(track))

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _receive_loop(self, track) -> None:
        """Capture utterances from the incoming track and fire STT on each.

        RMS energy detects utterance *boundaries* only.  Once an utterance
        starts, EVERY frame is buffered (loud and quiet) until sustained
        silence — dropping quiet frames mid-word (consonants, soft syllables)
        mangles the audio and makes Whisper mishear.  Samples are read via
        to_ndarray() to avoid the plane alignment padding that bytes(plane)
        would include.
        """
        import numpy as np

        resampler = av.AudioResampler(format="s16", layout="mono", rate=_STT_RATE)
        speech_buf = bytearray()  # all frames between utterance start and end
        last_speech_time = 0.0
        voiced_s = 0.0  # accumulated voiced time (excludes silence)
        capturing = False
        barge_signaled = False  # barge-in already confirmed for this utterance
        buf_overflow_warned = False  # only log the max-utterance warning once per call
        frame_count = 0

        def end_utterance():
            """Hand the buffered utterance to STT (if long enough) and reset."""
            nonlocal speech_buf, capturing, voiced_s, barge_signaled
            if voiced_s >= _MIN_VOICED_S:
                logger.info(
                    "Utterance end: %.1f s voiced, %d KB",
                    voiced_s,
                    len(speech_buf) // 1024,
                )
                asyncio.ensure_future(self._process_utterance(bytes(speech_buf)))
            speech_buf, capturing, voiced_s, barge_signaled = (
                bytearray(),
                False,
                0.0,
                False,
            )

        logger.info("Audio receive loop started")
        while self._running:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                if capturing:
                    end_utterance()
                continue
            except (asyncio.CancelledError, Exception) as e:
                # CancelledError is expected on stop(); other errors end the loop.
                if isinstance(e, asyncio.CancelledError):
                    logger.debug("Audio receive loop cancelled")
                else:
                    logger.warning("Audio receive loop ended: %s", e)
                break

            frame_count += 1
            if frame_count == 1:
                logger.debug(
                    "First audio frame: format=%s layout=%s rate=%s samples=%s",
                    frame.format.name,
                    frame.layout.name,
                    frame.sample_rate,
                    frame.samples,
                )

            # RMS on clean samples (to_ndarray respects real sample count, no padding)
            try:
                arr = frame.to_ndarray()
                rms = int(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            except (AttributeError, ValueError, OSError):
                rms = 0

            now = time.monotonic()
            is_speech = rms >= _RMS_THRESHOLD
            frame_dur = frame.samples / frame.sample_rate

            if is_speech and not capturing:
                capturing = True
                logger.debug("Speech started (rms=%d)", rms)

            if not capturing:
                continue

            # Buffer every frame while capturing — clean samples via to_ndarray
            for out_frame in resampler.resample(frame):
                speech_buf.extend(out_frame.to_ndarray().tobytes())
            # Safety cap: if the caller speaks continuously for a very long
            # time, force a flush so the buffer cannot grow without bound.
            if len(speech_buf) >= _MAX_SPEECH_BUF_BYTES:
                if not buf_overflow_warned:
                    logger.warning(
                        "Utterance exceeded %s s ceiling — forcing a split",
                        _MAX_UTTERANCE_S,
                    )
                    buf_overflow_warned = True
                end_utterance()
                continue
            if is_speech:
                last_speech_time = now
                voiced_s += frame_dur
                # Barge-in only after sustained voiced audio — confirms real
                # speech, not a click/transient that briefly crosses the RMS gate.
                if (
                    not barge_signaled
                    and voiced_s >= _BARGE_IN_MIN_VOICED_S
                    and self._on_speech_confirmed is not None
                ):
                    barge_signaled = True
                    try:
                        self._on_speech_confirmed()
                    except Exception as e:
                        logger.debug("on_speech_confirmed error: %s", e)
            elif (now - last_speech_time) >= _SILENCE_THRESHOLD_S:
                end_utterance()

        # Flush remaining speech when call ends
        logger.info("Receive loop done: %d frames", frame_count)
        if capturing:
            end_utterance()

    async def _process_utterance(self, pcm: bytes) -> None:
        wav_path = await asyncio.to_thread(self._pcm_to_wav, pcm)
        if not wav_path:
            return
        t0 = time.monotonic()
        async with self._stt_lock:  # one at a time — avoids concurrent CPU contention
            try:
                result = await asyncio.to_thread(self._transcribe, wav_path)
                transcript = (
                    result.get("transcript", "").strip()
                    if result.get("success")
                    else ""
                )
            except (OSError, ValueError) as e:
                logger.error("STT failed: %s", e)
                return
        stt_s = time.monotonic() - t0
        if transcript:
            logger.info(
                "perf STT=%.1fs (%s) → %r",
                stt_s,
                result.get("provider", "?"),
                transcript[:120],
            )
            self._on_utterance(transcript, wav_path)
        else:
            logger.debug("perf STT=%.1fs → (empty)", stt_s)

    @staticmethod
    def _transcribe(wav_path: str) -> dict:
        """Transcribe a WAV. Runs in a worker thread.

        With DELTACHAT_CALL_STT_VOXTRAL enabled (and MISTRAL_API_KEY set) we use
        Voxtral cloud Transcribe (~1-2s, accurate) — local Whisper medium on CPU
        is ~15-30x slower than realtime (30s for a 2s clip), unusable for a live
        call. On any Voxtral failure we fall back to the configured provider.
        When the flag is off, the locally configured STT provider is used.
        """
        from tools import transcription_tools as tt

        if (
            _CALL_STT_VOXTRAL
            and os.getenv("MISTRAL_API_KEY")
            and hasattr(tt, "_transcribe_mistral")
        ):
            try:
                result = tt._transcribe_mistral(wav_path, "voxtral-mini-latest")
                if result.get("success"):
                    return result
                logger.warning(
                    "Voxtral STT failed (%s) — falling back", result.get("error")
                )
            except Exception as e:
                logger.warning("Voxtral STT error (%s) — falling back", e)

        return tt.transcribe_audio(wav_path, "medium")

    def _pcm_to_wav(self, pcm: bytes) -> Optional[str]:
        """Write 16 kHz mono s16le PCM buffer to a WAV file for STT."""
        try:
            out_path = str(self._audio_cache / f"call_{int(time.time() * 1000)}.wav")
            with wave.open(out_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(_STT_RATE)
                wf.writeframes(pcm)
            logger.debug("Wrote utterance WAV: %s (%d bytes)", out_path, len(pcm))
            return out_path
        except Exception as e:
            logger.error("PCM→WAV conversion failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Per-call state
# ---------------------------------------------------------------------------


@dataclass
class CallSession:
    pc: RTCPeerConnection
    chat_id: str
    msg_id: int
    caller_id: str
    caller_name: str
    outgoing_track: HermesAudioTrack
    audio_buffer: IncomingAudioBuffer
    ice_channel: Any  # RTCDataChannel
    inject_ts: float = 0.0  # when the last utterance was injected (for AI-latency perf)
    model_override_key: Optional[str] = (
        None  # gateway session key if a call model override is active
    )
    last_response_text: str = (
        ""  # text of the reply currently being spoken (for barge-in)
    )
    pending_interrupt_note: Optional[str] = (
        None  # note to prepend next turn after an interruption
    )
    is_responding: bool = False  # True while play_response is speaking (incl. TTS gaps)
    interrupted: bool = False  # set by barge-in to stop TTS of remaining sentences
    resp_start_frames: int = 0  # track.played_count at start of current response
    tts_checkpoints: list = field(
        default_factory=list
    )  # [(cum_chars, cum_frames)] per spoken sentence
    hangup_pending: bool = False  # dc_end_call was requested — hang up after TTS drain
    hanging_up: bool = False  # _hangup_session in progress (idempotency guard)
    hangup_cancelled: bool = False  # barge-in during a pending hangup cancels it
    opening_line: str = (
        ""  # outgoing call: line we spoke on connect (context for 1st reply)
    )


# ---------------------------------------------------------------------------
# Call manager
# ---------------------------------------------------------------------------


class CallManager:
    """Manages active DC voice calls.

    Instantiated by DeltaChatAdapter.connect() and torn down on disconnect().
    handle_incoming_call() is called from _handle_dc_event() for IncomingCall events.
    The adapter's send() override calls play_response() to route TTS into the call.

    Threading model — WHY aiortc runs on its own loop
    --------------------------------------------------
    aiortc drives ICE STUN checks AND RTP send/recv as tasks on the asyncio event
    loop.  If that loop is blocked, media stops dead.  For *outgoing* calls the
    bot places the call from inside an agent turn, and the moment that turn
    resumes its heavy work the gateway loop is starved — proven on real calls:
    audio flowed only while `dc_start_call` parked the turn, and cut off the
    instant the park ended (the @8s RTP sample never fired).  Incoming calls
    escaped this only because the gateway loop happened to be idle at accept time.

    Fix: every aiortc object (RTCPeerConnection, tracks, data channels, the
    incoming-audio receive loop) is created and driven on a DEDICATED event loop
    running in its own daemon thread.  Nothing the gateway/agent loop does can
    starve call media.  The boundary rules:

      * All public entry points (handle_*, start_call, play_response,
        request_hangup, teardown) are called from the gateway loop and marshal
        their real work onto the call loop via `_on_call_loop`.
      * The ONLY thing that must hop back to the gateway loop is the Hermes AI
        pipeline (`handle_message`) — done via `_to_hermes` — because that work
        belongs on the gateway loop (and must NOT run on, and starve, the call
        loop).  RPC calls are loop-agnostic (_AsyncRpc uses run_in_executor), so
        they run fine on the call loop.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self._sessions: Dict[int, CallSession] = {}  # msg_id → session
        self._chat_to_msg: Dict[str, int] = {}  # chat_id → msg_id
        self._pending_answers: Dict[int, asyncio.Future] = (
            {}
        )  # msg_id → answer-SDP future (outgoing)
        # chat_id → number of send() replies to suppress
        self._drop_next_response: Counter = Counter()
        # chat_id → suppress the agent's post-dc_start_call line
        self._drop_call_ack: Counter = Counter()

        # _sessions, _chat_to_msg, _pending_answers and the drop counters are
        # accessed from both the gateway loop (adapter.send(), tool handlers) and
        # the dedicated call loop.  A threading lock protects the dicts without
        # blocking event loops.
        self._state_lock = threading.Lock()

        # The gateway/agent loop (where we were constructed — connect() is async).
        # handle_message must run here; aiortc must NOT.
        self._gateway_loop = asyncio.get_running_loop()
        # The dedicated WebRTC loop, isolated from gateway/agent work.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_call_loop, name="dc-call-loop", daemon=True
        )
        self._thread.start()

    def _run_call_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _on_call_loop(self, coro):
        """Run *coro* on the dedicated call loop, awaiting the result from the
        gateway loop without blocking it."""
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        )

    async def _to_hermes(self, event) -> None:
        """Run the Hermes AI pipeline (handle_message) on the GATEWAY loop from
        call-loop code, awaiting it without blocking the call loop's media tasks."""
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(
                self._adapter.handle_message(event), self._gateway_loop
            )
        )

    # ------------------------------------------------------------------ #
    # Public entry points — called from the gateway loop, run on call loop #
    # ------------------------------------------------------------------ #

    async def handle_incoming_call(self, event: Dict[str, Any]) -> None:
        await self._on_call_loop(self._handle_incoming_call(event))

    async def handle_call_ended(self, event: Dict[str, Any]) -> None:
        await self._on_call_loop(self._handle_call_ended(event))

    async def handle_outgoing_call_accepted(self, event: Dict[str, Any]) -> None:
        await self._on_call_loop(self._handle_outgoing_call_accepted(event))

    async def start_call(self, chat_id: str, opening: str = "") -> int:
        msg_id = await self._on_call_loop(self._start_call(chat_id, opening))
        # The agent turn that placed the call usually emits a final line after
        # the tool returns ("Call connected, I'm speaking now"). In separate-
        # thread mode (default) that line comes from the chat thread and send()
        # routes it out as a normal text message — correct, nothing to suppress.
        # Only in shared-history mode does it land on the call session (same
        # thread as the spoken reply); arm a one-shot drop for that case. Cleared
        # on the first real user utterance so genuine replies are never dropped.
        if _CALL_THREAD_ID is None:
            with self._state_lock:
                self._drop_call_ack[chat_id] += 1
        return msg_id

    async def play_response(self, chat_id: str, text: str) -> None:
        await self._on_call_loop(self._play_response(chat_id, text))

    async def request_hangup(self, chat_id: str) -> bool:
        return await self._on_call_loop(self._request_hangup(chat_id))

    async def teardown(self) -> None:
        """Tear down all active calls then stop the call loop — from disconnect()."""
        with contextlib.suppress(Exception):
            await self._on_call_loop(self._teardown())
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------ #
    # Event handlers (run on the call loop)                               #
    # ------------------------------------------------------------------ #

    async def _handle_incoming_call(self, event: Dict[str, Any]) -> None:
        msg_id = int(event["msg_id"])
        chat_id = str(event["chat_id"])
        sdp_offer = event["place_call_info"]  # raw SDP text

        # Get the caller's real from_id so Hermes recognises them as the
        # same already-authenticated user (not a new unknown "caller").
        caller_id = "caller"
        caller_name = "Caller"
        try:
            msg = await self._adapter.rpc.get_message(self._adapter.account_id, msg_id)
            from_id = msg.get("from_id") or msg.get("fromId")
            if from_id:
                caller_id = str(from_id)
                contact = await self._adapter.rpc.get_contact(
                    self._adapter.account_id, int(from_id)
                )
                caller_name = _contact_name(contact, caller_name)
        except Exception as e:
            logger.debug("Could not fetch caller info: %s", e)

        logger.info(
            "Incoming call: msg_id=%s chat_id=%s caller=%s has_video=%s",
            msg_id,
            chat_id,
            caller_id,
            event.get("has_video"),
        )

        # Start warming up Whisper NOW — before ICE gathering and SDP exchange
        # which take ~5-10 s, giving the model time to load into memory.
        asyncio.ensure_future(self._warmup_stt())

        try:
            await self._answer_call(msg_id, chat_id, sdp_offer, caller_id, caller_name)
        except Exception as e:
            logger.error("Failed to answer call %s: %s", msg_id, e, exc_info=True)
            # Try to decline gracefully
            with contextlib.suppress(Exception):
                await self._adapter.rpc.end_call(self._adapter.account_id, msg_id)

    async def _handle_call_ended(self, event: Dict[str, Any]) -> None:
        msg_id = int(event["msg_id"])
        logger.info("Call ended: msg_id=%s", msg_id)
        # If an outgoing call was declined/cancelled before being answered,
        # wake the waiter so start_call stops blocking.
        with self._state_lock:
            fut = self._pending_answers.pop(msg_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(RuntimeError("call ended before it was answered"))
        await self._teardown_session(msg_id)

    async def _handle_outgoing_call_accepted(self, event: Dict[str, Any]) -> None:
        """Other party answered our outgoing call — hand the answer SDP to start_call.

        Resolving via a Future keeps setRemoteDescription on the same event loop
        that created the RTCPeerConnection (aiortc requirement).
        """
        msg_id = int(event["msg_id"])
        sdp_answer = event.get("accept_call_info", "")
        with self._state_lock:
            fut = self._pending_answers.pop(msg_id, None)
        if fut is None or fut.done():
            logger.debug("OutgoingCallAccepted for unknown/done call %s", msg_id)
            return
        if not sdp_answer:
            fut.set_exception(RuntimeError("OutgoingCallAccepted had no answer SDP"))
            return
        fut.set_result(sdp_answer)
        logger.info("Outgoing call answered: msg_id=%s", msg_id)

    # ------------------------------------------------------------------ #
    # Shared WebRTC setup (used by both incoming and outgoing calls)       #
    # ------------------------------------------------------------------ #

    async def _build_ice_config(self) -> RTCConfiguration:
        """Fetch DC ICE servers and build an aiortc RTCConfiguration.

        aiortc can't parse IPv6 TURN URIs (e.g. turn:[::1]:3478) — drop those
        URLs and keep the IPv4 ones DC provides alongside them.

        NOTE: do NOT set bundlePolicy=max-bundle. Tested both roles: on the
        answerer it yields an answer with zero candidates, and on the offerer
        it makes aiortc skip ICE connectivity checks entirely. Default policy
        is the only one that gathers + checks correctly.
        """
        ice_json = await self._adapter.rpc.ice_servers(self._adapter.account_id)
        ice_servers = []
        for s in json.loads(ice_json) or []:
            urls = s.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            ipv4_urls = [u for u in urls if "[" not in u]
            if ipv4_urls:
                ice_servers.append(RTCIceServer(**{**s, "urls": ipv4_urls}))
        logger.info("Using %d ICE server(s)", len(ice_servers))
        return RTCConfiguration(iceServers=ice_servers)

    @staticmethod
    def _sdp_candidates(sdp: str) -> str:
        """Summarize candidate types in an SDP for diagnostics, e.g. 'host:3 relay:1'."""
        c = Counter(_re.findall(r"a=candidate:.*? typ (\w+)", sdp or ""))
        return " ".join(f"{t}:{n}" for t, n in c.items()) or "none"

    @classmethod
    def _log_sdp(cls, label: str, sdp: str) -> None:
        """Log an SDP's candidate and media summaries."""
        logger.info("%s candidates: %s", label, cls._sdp_candidates(sdp))
        logger.info("%s media: %s", label, cls._sdp_media(sdp))

    @staticmethod
    def _sdp_media(sdp: str) -> str:
        """Summarize each m-line: kind, direction, msid presence — e.g.
        'audio:sendrecv+msid'. A missing msid on our offer means the browser's
        ontrack gets e.streams[0]=undefined and won't play our audio."""
        out, cur = [], None
        for ln in (sdp or "").splitlines():
            ln = ln.strip()
            if ln.startswith("m="):
                if cur:
                    out.append(cur)
                cur = {"kind": ln[2:].split()[0], "dir": "?", "msid": False}
            elif cur is not None:
                if ln in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
                    cur["dir"] = ln[2:]
                elif ln.startswith("a=msid") or ln.startswith("a=ssrc"):
                    cur["msid"] = True
        if cur:
            out.append(cur)
        return (
            " | ".join(
                f"{m['kind']}:{m['dir']}{'+msid' if m['msid'] else ''}" for m in out
            )
            or "none"
        )

    async def _log_media_stats(
        self, pc, msg_id: int, label: str, samples=(4.0, 8.0, 12.0)
    ) -> None:
        """Periodically dump RTP packet/byte counters per direction.

        connectionState=='connected' only means ICE+DTLS are up — it does NOT
        imply RTP is flowing. Sampling several times shows whether the counters
        actually GROW (media flowing) or stay flat at 0 (silent), and isolates a
        silent call to outbound, inbound, or both. Logged unconditionally so the
        line always appears even if the call ends early or never connects.
        """
        prev = 0.0
        for at in samples:
            await asyncio.sleep(max(0.0, at - prev))
            prev = at
            state = pc.connectionState
            if state in ("closed", "failed"):
                logger.info(
                    "Media stats [%s] %s @%.0fs: pc state=%s — stopping",
                    label,
                    msg_id,
                    at,
                    state,
                )
                return
            try:
                stats = await pc.getStats()
            except Exception as e:
                logger.warning(
                    "Media stats [%s] %s @%.0fs: getStats failed: %s",
                    label,
                    msg_id,
                    at,
                    e,
                )
                continue
            inb = outb = None
            for s in stats.values():
                t = getattr(s, "type", "")
                if t == "inbound-rtp":
                    inb = s
                elif t == "outbound-rtp":
                    outb = s
            logger.info(
                "Media stats [%s] %s @%.0fs state=%s: OUT packetsSent=%s bytesSent=%s | "
                "IN packetsReceived=%s packetsLost=%s",
                label,
                msg_id,
                at,
                state,
                getattr(outb, "packetsSent", "n/a"),
                getattr(outb, "bytesSent", "n/a"),
                getattr(inb, "packetsReceived", "n/a"),
                getattr(inb, "packetsLost", "n/a"),
            )

    async def _new_peer_connection(self, with_data_channels: bool = True):
        """Create an RTCPeerConnection (with DC ICE servers), optionally with the
        two negotiated data channels (iceTrickling/mutedState) + trickle handler.

        with_data_channels=False is used for OUTGOING calls: the data channels'
        SCTP transport wedges the offerer's ICE against a max-bundle answerer
        (the DC mobile) — confirmed by tests/test_call_webrtc_loopback.py. An
        audio-only offer connects. We lose trickle-receive, but our offer carries
        our TURN relay so the peer can still reach us.
        Returns (pc, ice_channel) — ice_channel is None when channels are off.
        """
        config = await self._build_ice_config()
        pc = RTCPeerConnection(config)

        @pc.on("connectionstatechange")
        def _on_conn():
            logger.info("PC connectionState=%s", pc.connectionState)

        @pc.on("iceconnectionstatechange")
        def _on_iceconn():
            logger.info("PC iceConnectionState=%s", pc.iceConnectionState)

        @pc.on("icegatheringstatechange")
        def _on_gather():
            logger.info("PC iceGatheringState=%s", pc.iceGatheringState)

        if not with_data_channels:
            return pc, None

        ice_channel = pc.createDataChannel("iceTrickling", negotiated=True, id=1)
        pc.createDataChannel("mutedState", negotiated=True, id=3)  # created, ignored

        @ice_channel.on("open")
        def _on_ice_open():
            logger.info("iceTrickling data channel OPEN")

        @ice_channel.on("message")
        async def on_ice_message(msg):
            # The peer (calls-webapp) trickles candidates as browser JSON:
            #   {"candidate": "candidate:... typ host ...", "sdpMid": "0", "sdpMLineIndex": 0}
            # or `null` for end-of-candidates. aiortc's RTCIceCandidate(**data) does
            # NOT accept that shape — we must parse the SDP string ourselves.
            try:
                data = json.loads(msg)
            except Exception as e:
                logger.debug("ICE trickle: bad JSON: %s", e)
                return
            if not data or not data.get("candidate"):
                with contextlib.suppress(Exception):
                    await pc.addIceCandidate(None)  # end-of-candidates
                return
            try:
                from aiortc.sdp import candidate_from_sdp

                cand_str = data["candidate"]
                if cand_str.startswith("candidate:"):
                    cand_str = cand_str[len("candidate:") :]
                cand = candidate_from_sdp(cand_str)
                cand.sdpMid = data.get("sdpMid")
                cand.sdpMLineIndex = data.get("sdpMLineIndex")
                await pc.addIceCandidate(cand)
                logger.info("ICE trickle: added remote %s candidate", cand.type)
            except Exception as e:
                logger.warning("ICE trickle: failed to add candidate %r: %s", data, e)

        return pc, ice_channel

    def _make_audio_buffer(
        self, msg_id: int, chat_id: str, caller_id: str, caller_name: str
    ) -> "IncomingAudioBuffer":
        """Build the incoming-audio buffer wired to STT + barge-in for a call."""
        return IncomingAudioBuffer(
            hermes_home=self._get_hermes_home(),
            on_utterance=lambda transcript, wav: asyncio.ensure_future(
                self._on_utterance(msg_id, chat_id, transcript, caller_id, caller_name)
            ),
            on_speech_confirmed=lambda: self._handle_barge_in(msg_id),
        )

    @staticmethod
    async def _gather_ice(pc) -> None:
        """Wait until ICE gathering completes (or the timeout) before sending SDP."""
        ice_done = asyncio.Event()

        def _on_state():
            if pc.iceGatheringState == "complete":
                ice_done.set()

        pc.on("icegatheringstatechange", _on_state)
        if pc.iceGatheringState == "complete":
            ice_done.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ice_done.wait(), timeout=_ICE_GATHER_TIMEOUT_S)
        logger.debug("ICE gathering state: %s", pc.iceGatheringState)

    @staticmethod
    async def _wait_for_connection(pc, timeout_s: float) -> None:
        """Poll until the connection settles (connected/failed/closed) or timeout."""
        for _ in range(round(timeout_s / 0.1)):
            if pc.connectionState in ("connected", "failed", "closed"):
                return
            await asyncio.sleep(0.1)

    def _register_session(
        self,
        pc,
        ice_channel,
        out_track,
        audio_buf,
        msg_id: int,
        chat_id: str,
        caller_id: str,
        caller_name: str,
    ) -> "CallSession":
        session = CallSession(
            pc=pc,
            chat_id=chat_id,
            msg_id=msg_id,
            caller_id=caller_id,
            caller_name=caller_name,
            outgoing_track=out_track,
            audio_buffer=audio_buf,
            ice_channel=ice_channel,
        )
        with self._state_lock:
            self._sessions[msg_id] = session
            self._chat_to_msg[chat_id] = msg_id
        return session

    # ------------------------------------------------------------------ #
    # Internal — incoming call setup                                       #
    # ------------------------------------------------------------------ #

    async def _answer_call(
        self,
        msg_id: int,
        chat_id: str,
        sdp_offer: str,
        caller_id: str = "caller",
        caller_name: str = "Caller",
    ) -> None:
        pc, ice_channel = await self._new_peer_connection()
        out_track = HermesAudioTrack()
        audio_buf = self._make_audio_buffer(msg_id, chat_id, caller_id, caller_name)

        @pc.on("track")
        def on_track(track):
            logger.info("Received remote track: kind=%s", track.kind)
            if track.kind == "audio":
                audio_buf.start(track)

        # SDP exchange: remote offer in, our answer out
        await pc.setRemoteDescription(
            RTCSessionDescription(type="offer", sdp=sdp_offer)
        )
        self._log_sdp("Incoming offer", sdp_offer)
        pc.addTrack(out_track)
        await pc.setLocalDescription(await pc.createAnswer())
        await self._gather_ice(pc)
        self._log_sdp("Our answer", pc.localDescription.sdp)

        await self._adapter.rpc.accept_incoming_call(
            self._adapter.account_id,
            msg_id,
            pc.localDescription.sdp,
        )
        logger.info("Accepted call msg_id=%s chat_id=%s", msg_id, chat_id)

        self._register_session(
            pc,
            ice_channel,
            out_track,
            audio_buf,
            msg_id,
            chat_id,
            caller_id,
            caller_name,
        )

        # Diagnostic baseline: a working (incoming) call's RTP counters, to diff
        # against the silent outgoing call.
        asyncio.ensure_future(self._log_media_stats(pc, msg_id, "incoming"))

        # Greet the caller — inject a system event so the AI generates the
        # first greeting based on conversation history.
        asyncio.ensure_future(
            self._play_greeting(msg_id, chat_id, caller_id, caller_name)
        )

    # ------------------------------------------------------------------ #
    # Internal — outgoing call setup                                       #
    # ------------------------------------------------------------------ #

    async def _resolve_chat_contact(self, chat_id: str):
        """Best-effort (contact_id, name) for the 1:1 partner of a chat.

        Used so an outgoing call's transcripts are attributed to the real
        contact (same as incoming), avoiding "unauthorized user" handling.
        """
        try:
            ids = await self._adapter.rpc.get_chat_contacts(
                self._adapter.account_id, int(chat_id)
            )
            for cid in ids or []:
                if int(cid) == _DC_CONTACT_ID_SELF:
                    continue
                contact = await self._adapter.rpc.get_contact(
                    self._adapter.account_id, int(cid)
                )
                return str(cid), _contact_name(contact, f"Contact {cid}")
        except Exception as e:
            logger.debug("Could not resolve chat contact: %s", e)
        return "user", "User"

    @staticmethod
    def _render_tts(text: str) -> list:
        """TTS *text* and decode to playable frames. Pure CPU/IO — run in a thread."""
        try:
            from tools.tts_tool import text_to_speech_tool

            data = json.loads(text_to_speech_tool(text))
            if data.get("success") and data.get("file_path"):
                return HermesAudioTrack.decode_tts(data["file_path"])
        except Exception as e:
            logger.error("opening TTS render failed: %s", e)
        return []

    async def _start_call(self, chat_id: str, opening: str = "") -> int:
        """Place an outgoing voice call to *chat_id*; return the call msg_id.

        Blocks until the other party answers (or raises on timeout/decline).
        If *opening* is given, that exact line is synthesized **during ringing**
        and played the instant the call connects (no post-pickup AI roundtrip);
        the AI is told what it opened with on the user's first reply. Without
        *opening* the AI generates a greeting after pickup. Reuses the same
        WebRTC/audio setup as incoming calls.
        """
        caller_id, caller_name = await self._resolve_chat_contact(chat_id)
        opening = (opening or "").strip()

        # Audio-only offer: the data channels' SCTP transport breaks the offerer's
        # ICE against a max-bundle answerer (the DC mobile). See the loopback test.
        pc, ice_channel = await self._new_peer_connection(with_data_channels=False)
        out_track = HermesAudioTrack()
        pc.addTrack(out_track)

        # Our offer out (ICE gathered) → place the call to get the msg_id.
        await pc.setLocalDescription(await pc.createOffer())
        await self._gather_ice(pc)
        self._log_sdp("Our offer", pc.localDescription.sdp)
        msg_id = int(
            await self._adapter.rpc.place_outgoing_call(
                self._adapter.account_id,
                int(chat_id),
                pc.localDescription.sdp,
                False,
            )
        )
        logger.info(
            "Placed outgoing call: msg_id=%s chat_id=%s caller=%s",
            msg_id,
            chat_id,
            caller_id,
        )

        # Now msg_id is known: wire audio + register the session so the
        # CallEnded/OutgoingCallAccepted handlers can find it.
        audio_buf = self._make_audio_buffer(msg_id, chat_id, caller_id, caller_name)

        @pc.on("track")
        def on_track(track):
            logger.info("Outgoing call remote track: kind=%s", track.kind)
            if track.kind == "audio" and not audio_buf._running:
                audio_buf.start(track)

        self._register_session(
            pc,
            ice_channel,
            out_track,
            audio_buf,
            msg_id,
            chat_id,
            caller_id,
            caller_name,
        )
        asyncio.ensure_future(self._warmup_stt())

        # Pre-render the opening line in parallel with ringing — by pickup the
        # audio is ready, so there's zero post-pickup latency and no AI call.
        opening_task = (
            asyncio.ensure_future(asyncio.to_thread(self._render_tts, opening))
            if opening
            else None
        )

        # Wait for the answer SDP (resolved by handle_outgoing_call_accepted).
        fut = asyncio.get_running_loop().create_future()
        with self._state_lock:
            self._pending_answers[msg_id] = fut
        try:
            sdp_answer = await asyncio.wait_for(fut, timeout=_OUTGOING_CALL_TIMEOUT_S)
        except Exception as e:
            with self._state_lock:
                self._pending_answers.pop(msg_id, None)
            if opening_task:
                opening_task.cancel()
            with contextlib.suppress(Exception):
                await self._adapter.rpc.end_call(self._adapter.account_id, msg_id)
            await self._teardown_session(msg_id)
            if isinstance(e, asyncio.TimeoutError):
                logger.info(
                    "Outgoing call %s not answered within %ds",
                    msg_id,
                    _OUTGOING_CALL_TIMEOUT_S,
                )
            raise

        self._log_sdp("Remote answer", sdp_answer)
        await pc.setRemoteDescription(
            RTCSessionDescription(type="answer", sdp=sdp_answer)
        )

        # Briefly confirm the media path comes up before returning, so the tool
        # reports a genuinely live call and surfaces an immediate failure as an
        # error (not false success). Capped short (~2 s) so the agent isn't held
        # for the full ICE timeout — DC answers already carry candidates, so
        # 'connected' is normally reached in well under a second. The opening +
        # remaining setup then run in the background on the call loop.
        await self._wait_for_connection(pc, 2.0)
        if pc.connectionState in ("failed", "closed"):
            await self._teardown_session(msg_id)
            raise RuntimeError(f"call failed to connect (state={pc.connectionState})")
        logger.info(
            "Outgoing call %s connection state at return: %s",
            msg_id,
            pc.connectionState,
        )

        asyncio.ensure_future(
            self._finalize_outgoing_call(
                pc,
                msg_id,
                chat_id,
                caller_id,
                caller_name,
                out_track,
                audio_buf,
                opening,
                opening_task,
            )
        )
        return msg_id

    async def _finalize_outgoing_call(
        self,
        pc,
        msg_id: int,
        chat_id: str,
        caller_id: str,
        caller_name: str,
        out_track: "HermesAudioTrack",
        audio_buf: "IncomingAudioBuffer",
        opening: str,
        opening_task,
    ) -> None:
        """Call-loop tail of a connected outgoing call: attach the remote track
        and play the pre-rendered opening.

        Split out of _start_call so the dc_start_call tool returns right after
        the call connects (the 2 s confirm) instead of holding the agent's turn
        through the opening playback. Runs entirely on the dedicated call loop.
        _start_call has already applied the answer and confirmed the connection
        is not failed; we still tolerate a connectionState that is briefly
        'connecting' here.
        """
        # In the rare case the connection was still 'connecting' at the 2 s
        # cutoff, give it the rest of the ICE budget before speaking.
        await self._wait_for_connection(pc, 13.0)
        logger.info(
            "Outgoing call %s connection state after ICE wait: %s",
            msg_id,
            pc.connectionState,
        )

        # Fallback: if on_track didn't fire, attach the remote audio track
        # from the negotiated transceiver.
        if not audio_buf._running:
            logger.warning(
                "Outgoing call %s: on_track did not fire — using transceiver fallback",
                msg_id,
            )
            for t in pc.getTransceivers():
                if t.kind == "audio" and t.receiver and t.receiver.track:
                    audio_buf.start(t.receiver.track)
                    break
        logger.info(
            "Outgoing call %s: incoming audio capture running=%s",
            msg_id,
            audio_buf._running,
        )

        # Diagnostic: is RTP actually flowing each way? (connected != media)
        asyncio.ensure_future(self._log_media_stats(pc, msg_id, "outgoing"))

        # Connected. Play the pre-rendered opening immediately, or fall back to
        # an AI greeting if no opening was given (or its TTS failed).
        with self._state_lock:
            session = self._sessions.get(msg_id)
        frames = []
        if opening_task:
            with contextlib.suppress(Exception):
                frames = await opening_task
        if frames and session is not None:
            # Minimal barge-in accounting so an interruption attributes correctly.
            session.last_response_text = opening
            session.resp_start_frames = out_track.played_count
            session.tts_checkpoints = [(len(opening), len(frames))]
            session.opening_line = opening  # context for the first user reply
            out_track.enqueue_tts_frames(frames)
            logger.info(
                "Outgoing call %s: played pre-rendered opening (%d frames)",
                msg_id,
                len(frames),
            )
        else:
            asyncio.ensure_future(
                self._play_greeting(msg_id, chat_id, caller_id, caller_name)
            )

    # ------------------------------------------------------------------ #
    # Greeting (AI says hello when call connects)                           #
    # ------------------------------------------------------------------ #

    async def _play_greeting(
        self, msg_id: int, chat_id: str, caller_id: str, caller_name: str
    ) -> None:
        """Inject a 'call started' MessageEvent so the AI greets the caller.

        The AI sees the event text in the call-thread history and responds
        naturally — it can personalise greetings based on past calls or
        remember user requests ("remind me to buy milk").
        The response is routed through send() → play_response() → TTS.
        """
        from gateway.platforms.base import MessageEvent, MessageType

        source = self._call_source(chat_id, caller_id, caller_name)

        event = MessageEvent(
            text="[Call started]",
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(msg_id),
            channel_prompt=_CALL_PROMPT or None,
        )
        logger.info("Injecting call-start greeting for msg_id=%s", msg_id)
        try:
            await self._to_hermes(event)
        except Exception as e:
            # ensure_future would otherwise swallow this silently
            logger.error("Greeting failed for msg_id=%s: %s", msg_id, e, exc_info=True)

    # ------------------------------------------------------------------ #
    # Barge-in (user interrupts the bot mid-reply)                         #
    # ------------------------------------------------------------------ #

    def _handle_barge_in(self, msg_id: int) -> None:
        """Caller started speaking — stop the bot and record what was missed.

        Flushes any queued audio, stops TTS of remaining sentences, then maps
        the number of frames actually played back to a character offset (via the
        per-sentence checkpoints recorded in play_response) to tell the model
        next turn what the user did not hear.
        """
        session = self._sessions.get(msg_id)
        if session is None:
            return
        # "Responding" covers the gaps between sentence chunks; "hanging_up"
        # covers a goodbye that's draining before a pending hangup.
        if not (
            session.is_responding
            or session.outgoing_track.is_speaking()
            or session.hanging_up
        ):
            return  # bot wasn't talking — nothing to interrupt
        session.interrupted = True  # stop TTS of remaining sentences

        # The user spoke up — they want to keep going. Cancel any pending
        # hangup so the goodbye-drain doesn't end the call out from under them.
        if session.hangup_pending or session.hanging_up:
            session.hangup_pending = False
            session.hangup_cancelled = True
            logger.info("Barge-in: cancelled pending hangup")

        session.outgoing_track.flush()  # drop queued audio now

        text = session.last_response_text or ""
        session.last_response_text = ""
        if not text:
            logger.info("Barge-in: bot interrupted (no text to attribute)")
            return

        played = max(0, session.outgoing_track.played_count - session.resp_start_frames)
        cut = self._frames_to_chars(played, session.tts_checkpoints, len(text))
        heard, unheard = text[:cut].strip(), text[cut:].strip()
        logger.info(
            "Barge-in: user interrupted at char %d/%d (%d frames played)",
            cut,
            len(text),
            played,
        )
        if unheard:
            session.pending_interrupt_note = (
                "[The user interrupted your previous reply. They heard only: "
                f'"{heard}" — they did NOT hear: "{unheard}". '
                "Take this into account; don't assume they know the part they missed.]"
            )

    @staticmethod
    def _frames_to_chars(played: int, checkpoints: list, text_len: int) -> int:
        """Map played frame count → character offset using per-sentence checkpoints.

        checkpoints is [(cum_chars, cum_frames), ...] in order. We find the last
        checkpoint fully played, then linearly interpolate into the next one.
        """
        if not checkpoints:
            return 0
        prev_chars, prev_frames = 0, 0
        for cum_chars, cum_frames in checkpoints:
            if played >= cum_frames:
                prev_chars, prev_frames = cum_chars, cum_frames
                continue
            # Partway through this sentence — interpolate.
            seg_frames = cum_frames - prev_frames
            seg_chars = cum_chars - prev_chars
            if seg_frames > 0:
                frac = (played - prev_frames) / seg_frames
                return min(text_len, prev_chars + int(seg_chars * frac))
            return min(text_len, prev_chars)
        return min(text_len, prev_chars)

    # ------------------------------------------------------------------ #
    # Internal — audio pipeline                                           #
    # ------------------------------------------------------------------ #

    async def _warmup_stt(self) -> None:
        """Pre-load the Whisper model right after call acceptance.

        Without this, the first utterance waits ~10 s for model load before
        transcription even starts. Running a silent dummy clip now means the
        model is hot in memory by the time the caller finishes their first sentence.

        Skipped entirely when cloud STT (Voxtral) is enabled — there's no local
        model to warm, and loading whisper would waste CPU and time.
        """
        if _CALL_STT_VOXTRAL:
            return
        try:
            from tools.transcription_tools import transcribe_audio

            tmp_path = os.path.join(
                self._get_hermes_home(), "audio_cache", "_warmup.wav"
            )
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(_BYTES_PER_SAMPLE)
                wf.setframerate(_STT_RATE)
                wf.writeframes(b"\x00" * (_STT_BYTES_PER_SEC // 2))  # 0.5 s of silence
            logger.info("Pre-warming Whisper medium model...")
            await asyncio.to_thread(transcribe_audio, tmp_path, "medium")
            logger.info("Whisper model ready")
        except Exception as e:
            logger.debug("STT warmup failed (non-fatal): %s", e)

    # ------------------------------------------------------------------ #
    # Per-call LLM model override (opt-in via DELTACHAT_CALL_MODEL)        #
    # ------------------------------------------------------------------ #

    def _gateway(self):
        """Return the GatewayRunner instance, or None.

        The adapter's message handler is the gateway's bound _handle_message,
        so its __self__ is the GatewayRunner that owns _session_model_overrides.
        """
        handler = getattr(self._adapter, "_message_handler", None)
        return getattr(handler, "__self__", None)

    def _install_model_override(self, session, source) -> None:
        """Install a per-call LLM override on the gateway session (idempotent)."""
        if not _CALL_MODEL or session.model_override_key is not None:
            return
        gw = self._gateway()
        if gw is None or not hasattr(gw, "_session_model_overrides"):
            logger.debug("Model override requested but gateway not reachable")
            return
        try:
            key = gw._session_key_for_source(source)
            gw._session_model_overrides[key] = {
                "model": _CALL_MODEL,
                "provider": _CALL_MODEL_PROVIDER or None,
                "api_key": _CALL_MODEL_API_KEY or None,
                "base_url": _CALL_MODEL_BASE_URL or None,
            }
            session.model_override_key = key
            logger.info(
                "Installed per-call model override: %s (session=%s)", _CALL_MODEL, key
            )
        except Exception as e:
            logger.warning("Failed to install call model override: %s", e)

    def _clear_model_override(self, session) -> None:
        if not session.model_override_key:
            return
        gw = self._gateway()
        try:
            if gw is not None:
                gw._session_model_overrides.pop(session.model_override_key, None)
                logger.debug(
                    "Cleared per-call model override (session=%s)",
                    session.model_override_key,
                )
        except Exception as e:
            logger.debug("Failed clearing model override: %s", e)
        session.model_override_key = None

    async def _on_utterance(
        self,
        msg_id: int,
        chat_id: str,
        transcript: str,
        caller_id: str = "caller",
        caller_name: str = "Caller",
    ) -> None:
        """Inject transcribed speech as a MessageEvent → Hermes AI → send() intercept → TTS."""
        try:
            from tools.voice_mode import is_whisper_hallucination

            if is_whisper_hallucination(transcript):
                logger.debug("Discarding Whisper hallucination: %r", transcript[:60])
                return
        except ImportError:
            pass

        # First real user turn — the post-dc_start_call ack window is over, so a
        # never-consumed suppression can't eat a genuine spoken reply.
        with self._state_lock:
            self._drop_call_ack.pop(chat_id, None)

        from gateway.platforms.base import MessageEvent, MessageType

        source = self._call_source(chat_id, caller_id, caller_name)
        # MessageType.TEXT since we already did STT — Hermes won't re-transcribe.
        # channel_prompt is an ephemeral per-message system prompt (applied at
        # API-call time, never persisted) — used to keep spoken replies short.
        # The send() override uses _chat_to_msg to detect active calls.
        # Prepend context notes the model needs but that aren't part of the
        # spoken transcript: an outgoing call's opening line (so it knows what
        # it just said), and/or a barge-in note (what the user didn't hear).
        text = transcript
        session = self._sessions.get(msg_id)
        notes = []
        if session is not None and session.opening_line:
            notes.append(
                f'[You placed this call and opened by saying: "{session.opening_line}". '
                f"The user is now responding.]"
            )
            session.opening_line = ""
        if session is not None and session.pending_interrupt_note:
            notes.append(session.pending_interrupt_note)
            session.pending_interrupt_note = None
        if notes:
            text = "\n\n".join(notes + [transcript])

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(msg_id),
            channel_prompt=_CALL_PROMPT or None,
        )
        logger.debug("Injecting utterance into Hermes: %r", transcript[:80])
        if session is not None:
            self._install_model_override(session, source)
            session.inject_ts = time.monotonic()
        await self._to_hermes(event)

    async def _play_response(self, chat_id: str, text: str) -> None:
        """Called from adapter.send() when an active call intercepts a response.

        TTS is done sentence-by-sentence: the first chunk is synthesized and
        enqueued so playback can START while later chunks are still being
        synthesized. This drops time-to-first-audio for long replies from the
        whole-response TTS time to just the first sentence's.
        """
        with self._state_lock:
            msg_id = self._chat_to_msg.get(chat_id)
            session = self._sessions.get(msg_id) if msg_id is not None else None
        if msg_id is None:
            logger.warning("play_response: no active call for chat_id=%s", chat_id)
            return
        if session is None:
            return

        from tools.tts_tool import text_to_speech_tool

        # AI latency: time from injecting the transcript to receiving this response
        ai_s = (time.monotonic() - session.inject_ts) if session.inject_ts else 0.0

        # Reset per-response barge-in accounting.
        track = session.outgoing_track
        session.last_response_text = text
        session.interrupted = False
        session.hangup_cancelled = False
        session.is_responding = True
        session.resp_start_frames = track.played_count
        session.tts_checkpoints = []

        sentences = _split_sentences(text) or [text]
        t0 = time.monotonic()
        first_audio_s = None
        text_cursor = 0  # real offset into `text`, tracked for barge-in accounting
        cum_frames = 0
        try:
            for i, sentence in enumerate(sentences):
                if session.interrupted:
                    logger.info(
                        "play_response: stopped TTS after interrupt (%d/%d sentences)",
                        i,
                        len(sentences),
                    )
                    break
                result_str = await asyncio.to_thread(text_to_speech_tool, sentence)
                tts_data = json.loads(result_str)
                if not (tts_data.get("success") and tts_data.get("file_path")):
                    logger.warning("TTS failed for sentence: %s", tts_data.get("error"))
                    continue
                frames = await asyncio.to_thread(
                    HermesAudioTrack.decode_tts, tts_data["file_path"]
                )
                if session.interrupted:
                    break
                track.enqueue_tts_frames(frames)
                if first_audio_s is None:
                    first_audio_s = time.monotonic() - t0
                # Checkpoint: real end-offset of this chunk in `text` + cumulative
                # frames, so barge-in can map played frames back to a character
                # offset. Locate the chunk with text.find rather than summing
                # len(sentence)+1 — sentence splitting collapses inter-sentence
                # whitespace, so the naive sum drifts short and under-reports how
                # much of the reply the user actually heard.
                idx = text.find(sentence, text_cursor)
                if idx >= 0:
                    text_cursor = idx + len(sentence)
                else:  # merged/normalised chunk not found verbatim — best effort
                    text_cursor = min(len(text), text_cursor + len(sentence) + 1)
                cum_frames += len(frames)
                session.tts_checkpoints.append((text_cursor, cum_frames))
            logger.info(
                "perf AI=%.1fs first_audio=%.1fs total_tts=%.1fs (%d chars, %d chunks)",
                ai_s,
                first_audio_s or 0.0,
                time.monotonic() - t0,
                len(text),
                len(sentences),
            )
        except Exception as e:
            logger.error("play_response failed: %s", e)
        finally:
            session.is_responding = False
            if session.hangup_pending:
                try:
                    await self._hangup_session(session)
                except Exception as e:
                    logger.error("hangup after play_response failed: %s", e)
                    await self._teardown_session(session.msg_id)

    # ------------------------------------------------------------------ #
    # Cleanup                                                             #
    # ------------------------------------------------------------------ #

    async def _hangup_session(self, session: CallSession) -> None:
        """Wait for the whole TTS queue to play out, then end and teardown.

        The queue drains in real time (one frame per 20 ms via recv), so a
        multi-sentence goodbye needs as long as its audio duration — a fixed
        short cap would cut it off after the first sentence. We instead drain
        while playback keeps *progressing* (played_count advances) and only bail
        if it stalls for ~1 s (peer gone) or hits an absolute safety ceiling.
        """
        if session.hanging_up:
            return  # already hanging up (avoid double drain/end_call race)
        session.hanging_up = True
        track = session.outgoing_track
        last_played = track.played_count
        stall_ticks = 0
        max_ticks = int(120 / 0.1)  # absolute ceiling: 120 s
        for _ in range(max_ticks):
            if session.hangup_cancelled:
                break
            if not track.is_speaking():
                break
            await asyncio.sleep(0.1)
            if track.played_count != last_played:
                last_played = track.played_count
                stall_ticks = 0
            else:
                stall_ticks += 1
                if stall_ticks >= 10:  # ~1 s with no playback progress
                    logger.warning("hangup drain: playback stalled, ending call anyway")
                    break
        # Barge-in during the drain means the user wants to keep talking —
        # abort the hangup and leave the call running.
        if session.hangup_cancelled:
            logger.info("hangup aborted — barge-in during goodbye drain")
            session.hanging_up = False
            session.hangup_cancelled = False
            return
        # let the final frames reach the peer before tearing down
        await asyncio.sleep(0.1)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._adapter.rpc.end_call(self._adapter.account_id, session.msg_id),
                timeout=5.0,
            )
        await self._teardown_session(session.msg_id)

    async def _request_hangup(self, chat_id: str) -> bool:
        """Request to end a call after current TTS finishes.

        Called from the dc_end_call tool handler.  If a response is being
        spoken it waits for play_response to complete, then drains the
        audio track before hanging up.  Returns True if a call was ended.
        """
        with self._state_lock:
            msg_id = self._chat_to_msg.get(chat_id)
            session = self._sessions.get(msg_id) if msg_id is not None else None
        if msg_id is None or session is None:
            return False

        session.hangup_pending = True

        # The goodbye reply is spoken by a play_response task scheduled from
        # send(). Give it a moment to start so we don't hang up before it does.
        for _ in range(10):  # 10 * 0.05 = 0.5 s
            if session.is_responding:
                break
            await asyncio.sleep(0.05)

        # If a response is being spoken, play_response's finally block owns the
        # drain + hangup (_hangup_session waits for the full audio to play out).
        # Return now rather than blocking the tool call for the whole goodbye.
        if session.is_responding:
            logger.info("request_hangup: deferring to play_response drain")
            return True

        # No response in progress — drain any leftover audio and hang up now.
        await self._hangup_session(session)
        return True

    async def _teardown_session(self, msg_id: int, notify_ai: bool = True) -> None:
        with self._state_lock:
            session = self._sessions.pop(msg_id, None)
            if session is None:
                return
            chat_id, caller_id, caller_name = (
                session.chat_id,
                session.caller_id,
                session.caller_name,
            )
            self._chat_to_msg.pop(chat_id, None)
        self._clear_model_override(session)
        session.audio_buffer.stop()
        # pc.close() can hang if ICE is in a bad state — don't let it block shutdown
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.pc.close(), timeout=3.0)
        logger.info("Call session %s torn down", msg_id)
        # Tell the AI the call is over so it doesn't think it's still connected.
        if notify_ai:
            asyncio.ensure_future(
                self._note_call_ended(chat_id, caller_id, caller_name)
            )

    async def _note_call_ended(
        self, chat_id: str, caller_id: str, caller_name: str
    ) -> None:
        """Inject a 'call ended' turn into the call session so the AI knows the
        voice call is over. The AI's reply to this note is suppressed in send()
        (we don't want to text the user) — it's only to update conversation state.
        Also notifies the main text thread when running in isolated-call mode so
        the text-chat AI knows a call just ended.
        """
        from gateway.platforms.base import MessageEvent, MessageType

        caller_id, caller_name = caller_id or "user", caller_name or "User"
        with self._state_lock:
            self._drop_next_response[chat_id] += 1
        source = self._call_source(chat_id, caller_id, caller_name)
        event = MessageEvent(
            text="[The voice call has ended — you are no longer connected to the user "
            "by voice. Acknowledge to yourself; do not produce a spoken reply.]",
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"callend-{int(time.monotonic() * 1000)}",
            channel_prompt=_CALL_PROMPT or None,
        )
        logger.info("Notifying AI that call ended (chat=%s)", chat_id)
        with contextlib.suppress(Exception):
            await self._to_hermes(event)

        # When calls run in their own isolated thread, the main text-chat session
        # never saw the call-start, so it won't know the call ended either.
        # Inject a brief context note so the text-chat AI is aware. SHARED_HISTORY
        # mode already uses thread_id=None above, so skip the duplicate.
        if _CALL_THREAD_ID is not None:
            with self._state_lock:
                self._drop_next_response[chat_id] += 1
            main_source = self._call_source(
                chat_id, caller_id, caller_name, thread_id=None
            )
            main_event = MessageEvent(
                text="[A voice call with the user has just ended. Do not call back right now.]",
                message_type=MessageType.TEXT,
                source=main_source,
                message_id=f"callend-main-{int(time.monotonic() * 1000)}",
            )
            logger.info("Notifying main thread that call ended (chat=%s)", chat_id)
            with contextlib.suppress(Exception):
                await self._to_hermes(main_event)

    async def _teardown(self) -> None:
        """Tear down all active calls (runs on the call loop)."""
        for msg_id in list(self._sessions):
            with contextlib.suppress(Exception):
                # notify_ai=False: gateway is shutting down, don't start AI turns
                await asyncio.wait_for(
                    self._teardown_session(msg_id, notify_ai=False), timeout=5.0
                )

    def has_active_call(self, chat_id: str) -> bool:
        with self._state_lock:
            return chat_id in self._chat_to_msg

    def first_active_chat_id(self) -> Optional[str]:
        """Return an arbitrary active chat_id, or None.  Used by dc_end_call."""
        with self._state_lock:
            for chat_id in self._chat_to_msg:
                return chat_id
            return None

    @staticmethod
    def is_call_thread(thread_id) -> bool:
        """Whether a reply with this thread_id belongs to the call conversation
        (and should be spoken into the call).

        Separate-thread mode (default): only the dedicated call thread matches;
        replies from the chat/text thread are delivered as normal messages.
        Shared-history mode (_CALL_THREAD_ID is None): the call shares the DM
        session, so every reply for the chat counts as the call conversation."""
        if _CALL_THREAD_ID is None:
            return True
        return str(thread_id or "") == str(_CALL_THREAD_ID)

    def consume_call_ack(self, chat_id: str) -> bool:
        """True if the next spoken reply for chat_id should be dropped — the
        agent's acknowledgment right after dc_start_call. The pre-rendered
        opening already covers the first thing said, so this avoids speaking a
        meta "call connected" line over it. Shared-history mode only (separate
        mode routes that line out as text). One-shot per placed call."""
        with self._state_lock:
            return _take_one(self._drop_call_ack, chat_id)

    def consume_drop_response(self, chat_id: str) -> bool:
        """True if the next send() to chat_id should be suppressed (call-ended note reply).

        Each call-end injects up to two AI notes (call thread + main thread), so
        the counter may be 2. Each send() call decrements once.
        """
        with self._state_lock:
            return _take_one(self._drop_next_response, chat_id)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _call_source(
        self, chat_id: str, caller_id: str, caller_name: str, thread_id=_CALL_THREAD_ID
    ):
        """Hermes source for a call turn.

        The default thread_id isolates the call session from the text DM
        (unless DELTACHAT_CALL_SHARED_HISTORY is set); pass None to address the
        main text-chat session.
        """
        return self._adapter.build_source(
            chat_id=chat_id,
            chat_name=f"Call {chat_id}",
            chat_type="dm",
            user_id=caller_id,
            user_name=caller_name,
            thread_id=thread_id,
        )

    def _get_hermes_home(self) -> str:
        try:
            from gateway.config import get_hermes_home

            return str(get_hermes_home())
        except Exception:
            return os.path.expanduser("~/.hermes")
