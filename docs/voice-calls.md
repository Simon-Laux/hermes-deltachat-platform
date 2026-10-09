# Voice Calls

The adapter can both **answer incoming** Delta Chat voice calls and **place
outgoing** ones, holding a spoken conversation either way: audio →
speech-to-text → Hermes AI → text-to-speech → spoken reply, all over the live
WebRTC call.

Requires `aiortc` (see [nixos-installation.md](nixos-installation.md) for the
NixOS setup). Incoming calls are auto-answered; hang up from your Delta Chat
client, or the bot hangs up after its goodbye — it ends the reply with `[[hangup]]`,
which the adapter strips before TTS. `dc_end_call` does the same as a tool.
If the other side drops off without hanging up (lost network, killed app), the
bot hangs up by itself once the WebRTC connection reports `closed` (or `failed`
if ICE never connected), about 30-35 s later. Calls from contacts Hermes
doesn't authorize, or whose authorization check fails, are declined without
being answered.

## Outgoing calls (the bot calls you)

> **Implementation notes (two things were needed to make outgoing work):**
> 1. **Dedicated event loop for WebRTC (the main one).** aiortc drives ICE STUN
>    checks *and* RTP send/recv as tasks on the asyncio loop — if that loop is
>    blocked, media stops. The bot places outgoing calls from inside an agent
>    turn, so the moment that turn resumed its heavy AI/TTS work it starved the
>    loop: first the ICE handshake stalled in `checking`, and even once a
>    short-lived park got it connected, RTP died the instant the park ended (a
>    real call carried audio for ~4 s — exactly the park window — then went
>    silent). Incoming calls escaped this only because the gateway loop happened
>    to be idle at accept time. Fix: every aiortc object (peer connection,
>    tracks, data channels, the incoming-audio receive loop) runs on a
>    **dedicated event loop in its own daemon thread** (`CallManager`), so call
>    media can never be starved by gateway/agent work. The only hop back to the
>    gateway loop is the Hermes AI pipeline (`handle_message`); RPC is
>    loop-agnostic. `dc_start_call` no longer needs to park — it returns as soon
>    as the call connects (or is declined/unanswered), confirming for ~2 s, then
>    finishes setup and plays the opening in the background.
> 2. **Audio-only offer.** The `iceTrickling`/`mutedState` data channels' SCTP
>    transport wedges the offerer's ICE against a `max-bundle` answerer (the DC
>    mobile); outgoing offers are audio-only. Incoming keeps the data channels
>    (works as answerer). Tradeoff: outgoing can't receive trickled candidates,
>    but our offer carries our TURN relay so the peer can reach us.
>
> Both were pinned down with `DELTACHAT_CALL_ICE_DEBUG=true` (raises aioice/
> aiortc to DEBUG) and the in-process loopback harness
> (`tests/test_call_webrtc_loopback.py`, `pytest -m slow`).

The bot can proactively call a contact with the **`dc_start_call`** tool — ideal
from a scheduled/cron task: a reminder, an alert, a check-in. It takes the
recipient's `chat_token` (from one of their messages) and a required `opening`:

- `dc_start_call(chat_token=…, opening="Hi Simon, quick reminder to take your
  meds.")` — the `opening` is the exact words to say. It's synthesized **while
  the phone is still ringing** and played the instant they pick up, so there's no
  startup delay and no rate-limit exposure on the first line. Write it as natural
  speech, not a topic label.

`opening` is required because the agent placing the call already knows what it
wants to say — pre-rendering it is what makes the call speak instantly on pickup
(a post-pickup AI greeting would add a multi-second silent gap).

How the `opening` stays coherent: nothing is written to the call's conversation
history until the call connects, so an unanswered/declined call leaves no trace.
When the user replies, the AI is told what it opened with via a context note on
that first turn, so the conversation continues naturally.

It rings up to ~40s; if unanswered (or declined) the tool returns an error. Once
connected the call behaves exactly like an incoming one (same STT/AI/TTS, barge-in,
sentence streaming, and `dc_end_call` to hang up). Outgoing and incoming calls
share the same WebRTC/audio setup, so they can't drift apart.

## How it works

```
DC mobile  ──WebRTC audio──▶  aiortc  ──▶  silence detection  ──▶  STT
                                                                     │
   DC mobile  ◀──WebRTC audio──  aiortc  ◀──  TTS  ◀──  Hermes AI  ◀──┘
```

- Incoming audio is buffered per utterance; a ~1s pause marks the end of a turn.
  Speech that runs on for 30s without a pause goes to STT in 30s pieces.
- The transcript is injected into the normal Hermes session pipeline, so the
  agent has full context, tools, and memory — same as a text chat.
- The AI's reply is intercepted before it would be sent as a chat message and
  is instead spoken into the call via TTS.

## Barge-in

You can interrupt the bot mid-reply: as soon as you start speaking, the bot
stops talking and listens. To avoid a click or background noise cutting the bot
off, an interrupt is only triggered after ~0.25s of sustained voiced audio
(RMS-gated). On the next turn the model is told what you did vs didn't hear
(estimated from how much of the reply had played), so it won't assume you heard
the part it was cut off on.

## Configuration (environment variables)

All optional. Set in `~/.hermes/.env`.

### Speech-to-text

| Variable | Default | Description |
|---|---|---|
| `DELTACHAT_CALL_STT_VOXTRAL` | off | When `true`, send call audio straight to **Mistral Voxtral** cloud STT (~1-2s, accurate). Requires `MISTRAL_API_KEY`. Uses `stt.mistral.model` from `config.yaml` if set, else `voxtral-mini-latest`. If Voxtral fails, the call falls back to the configured provider. |

```bash
DELTACHAT_CALL_STT_VOXTRAL=true
```

> Note: enabling this sends call audio to Mistral's API. For fully local
> speech processing, leave it off and use `stt.provider: local` (expect high
> latency).

With the flag off, calls transcribe exactly like voice messages: Hermes picks
the provider from `stt.provider` and the model from that provider's section
(`stt.local.model`, `stt.mistral.model`, ...). The flag is only a shortcut
to Voxtral. It is not needed when `stt.provider: mistral` is already set.

For `stt.provider: local`, the local Whisper model is pre-loaded when a call
is accepted, so the first utterance doesn't wait for the model to load. Pick
the size with `stt.local.model`. Larger sizes such as `medium` are far slower
than realtime on CPU (≈30s for a 2s clip) and are unusable for live calls. Use
a cloud provider or a small model.

### Spoken-reply style

| Variable | Default | Description |
|---|---|---|
| `DELTACHAT_CALL_PROMPT` | (built-in) | Ephemeral per-call system prompt that keeps replies short and TTS-friendly. Applied only during calls, never persisted to chat history. Override to change the calling persona/brevity — keep the `[[hangup]]` instruction, or the bot can only hang up via `dc_end_call`. |

The built-in prompt instructs the agent to reply in 1-2 short spoken sentences
with no markdown/lists/emojis/URLs (cuts both AI and TTS latency, which scale
with response length), and to **delegate complex/long-running work to a subagent**
(which runs on the more capable default model) rather than doing heavy work
inline — especially important when `DELTACHAT_CALL_MODEL` points calls at a
smaller, faster model.

### Session isolation

A Delta Chat call happens inside the contact's chat, so by default it would
share the **same Hermes session/history as your text DM** with the bot. To keep
spoken turns (and their rough transcripts) out of the text conversation, calls
run in a **separate session** by default (a distinct `thread_id`).
When a call ends, the text-chat session gets a short note naming the call's
session id, so the bot can read the transcript with Hermes' `session_search`
tool when you ask about the call later.

| Variable | Default | Description |
|---|---|---|
| `DELTACHAT_CALL_SHARED_HISTORY` | off | When `true`, calls share one session with the text DM — the bot remembers across call↔text, at the cost of mixing spoken transcripts into the text history. When off (default), every call gets its own fresh session (`call-<msg_id>`), so old calls never pile up in the context. |

Note: the `channel_prompt` itself is **never** persisted to history in either
mode — only the transcript and the AI's spoken reply are stored (in whichever
session applies).

### Per-call LLM model override (opt-in)

By default a call uses the same model as the chat. You can override the model
**only for calls** (e.g. a faster model for snappier turn-taking) without
affecting text chats. The override is installed on the call's session when the
call starts and removed automatically on hangup.

| Variable | Default | Description |
|---|---|---|
| `DELTACHAT_CALL_MODEL` | unset | Model name to use during calls (e.g. `mistral-small-latest`). Unset = no override. |
| `DELTACHAT_CALL_MODEL_PROVIDER` | unset | Optional provider for the override model. |
| `DELTACHAT_CALL_MODEL_API_KEY` | unset | Optional API key. If omitted, the gateway resolves the key from env/config as usual and applies the model/provider on top. |
| `DELTACHAT_CALL_MODEL_BASE_URL` | unset | Optional base URL. |

```bash
# Example: use a faster model for calls only
DELTACHAT_CALL_MODEL=mistral-small-latest
```

> This feature is implemented and tested but **not enabled by default** — leave
> `DELTACHAT_CALL_MODEL` unset to use your normal model for calls.

**Reasoning settings follow the model, not the call.** Hermes sends a global
`agent.reasoning_effort` with every model, including the call model, and
`hermes model` sets that global value. If the call model can't take it, every
call turn fails with HTTP 400 and the call stays silent. For example,
`ministral-14b-2512` answers "reasoning_effort is not enabled for this model".
Hermes 0.21.5 doesn't recognise that wording, so it doesn't retry without the
field. Set the effort per model instead, and mark the call model as
non-reasoning so Hermes leaves the field out. A plain `none` doesn't work for
this: it is sent as `reasoning_effort: "none"`.

```yaml
agent:
  reasoning_effort: ''                # no global effort
  reasoning_overrides:
    mistral-medium-3.5: high          # your main model
model_overrides:                      # top-level key
  custom:                             # the provider name Hermes uses
    ministral-14b-2512:
      supports_reasoning: false
```

## Performance

Typical per-turn latency once warmed up (with Voxtral STT enabled):

| Stage | Time |
|---|---|
| turn-end silence detection | ~1.0s (fixed) |
| STT (Voxtral) | ~0.7-1.5s |
| AI (LLM) | ~3-12s, scales with response length & model |
| TTS | ~1.5-6s, scales with response length |

**Sentence-streaming TTS:** the reply is synthesized sentence-by-sentence and
the first chunk starts playing while the rest are still being synthesized. For
long replies this drops *time-to-first-audio* from the whole-response TTS time
(e.g. ~7s for 500 chars) to just the first sentence (~1.5s). The `perf` log line
reports `first_audio=` for this. Short replies are a single chunk (no change).

The concise-reply prompt is the main lever for total AI+TTS time. For lower AI
latency, consider the per-call model override above.

> Note: LLM-token streaming (overlapping AI generation itself with TTS) was
> evaluated and deferred — see `docs/plans/streaming-tts.md`. Sentence-streaming
> above gives most of the perceived-latency benefit without that complexity.

## Limitations

- Audio only; incoming video tracks are ignored.
- Whisper on AMD GPU (ROCm) is not yet supported — `ctranslate2` (faster-whisper's
  backend) is built for CUDA. Use Voxtral cloud STT instead.
