# Jarvis voice (apps/voice)

Real-time voice front for Hermes, built on [Pipecat](https://github.com/pipecat-ai/pipecat).

```
mic (WebRTC) → Silero VAD + smart-turn (max 1.2 s wait) → Whisper base.en (Speaches) → Gemma 4, thinking off (Ollama)
            → Kokoro "bm_daniel" at 1.1x (Speaches) → speaker
                              │
                              └─ ask_hermes(task) → Hermes /v1/chat/completions (background)
                                 → result spoken when done
```

## Shared brain with Hermes

- **One ongoing Hermes session for voice.** Every `ask_hermes` task goes into the same Hermes API
  session (`X-Hermes-Session-Id`), so Hermes remembers earlier voice requests, across reconnects.
  Its id is kept in `apps/voice/.state.json` (git-ignored); delete that file to start fresh.
- **Briefing on connect.** Jarvis's instructions include Hermes's long-term memory (`USER.md`,
  `MEMORY.md`, read from `data/instances/dev/memories/`) plus a recap of the previous call.
  Entries that look like credentials are left out. The briefing also has the current local date and time,
  and Jarvis is told to put the user's location and the date into every task it hands to Hermes
  (the ongoing Hermes session loaded its memory when it started, so it may not see newer entries).
- **Wrap-up on disconnect.** The call transcript goes to Hermes, which saves anything durable to its
  memory (the same memory Telegram uses) and writes the recap for next time.
- Not shared: the Telegram *chat history*. Hermes refuses API access to a conversation another
  channel owns, so memory is the bridge between voice and Telegram.

## Run

Needs the root `docker compose up -d` (Hermes + Speaches with the Whisper and Kokoro models) and Ollama.

```bash
uv run bot.py -t webrtc --host 127.0.0.1 --port 7860
```

Open http://127.0.0.1:7860/client in Chrome or Firefox and click Connect (allow the microphone).
You can also type in the Playground's message box.

## Phone line (Twilio)

`phone.py` puts the same Jarvis (same prompt, memory, Hermes session, voice) on a phone number.

```
caller ── Twilio number ──POST /twilio/voice──▶ Tailscale Funnel ──▶ phone.py (127.0.0.1:7861)
                     ◀── TwiML <Connect><Stream wss://…/twilio/ws/<token>>
        audio (8 kHz mu-law over WebSocket) ◀────────────▶ bot.run_bot (STT → Gemma → Kokoro)
```

Only `phone.py` is public. It checks Twilio's request signature, accepts calls only from
`PHONE_ALLOWED_NUMBERS`, puts a single-use 2-minute token in the media stream URL, and only dials
allowed numbers.

One-time setup:
1. Twilio: sign up (free trial credit), verify your own mobile number, take the free trial number.
   For outbound calls to South Africa, enable it under Voice → Settings → Geo permissions.
2. `cp .env.example .env` here and fill in the Twilio SID, auth token, number and your mobile number.
3. Public HTTPS: `sudo tailscale funnel --bg 7861` (the first time it prints a link to enable Funnel
   for your tailnet). Your host is `<machine>.<tailnet>.ts.net`; put it in `PHONE_PUBLIC_HOST`.
4. Twilio console → your number → Voice → "A call comes in": Webhook, HTTP POST,
   `https://<PHONE_PUBLIC_HOST>/twilio/voice`.

Run:

```bash
uv run phone.py serve                 # keep running
uv run phone.py call                  # Jarvis rings you (first allowed number)
uv run phone.py call +27... "Your weather report is ready"
```

Trial accounts play a short Twilio notice first (outbound: press a key to continue).
Calling a US trial number from a South African phone is an international call on your plan;
having Jarvis call you uses Twilio credit instead.

Tested locally with a simulated Twilio stream: bad signatures get 403, unknown callers are turned
away, and an 8 kHz question ("What city do I live in?") got the right spoken answer.

## Measured (RTX 3080, Ryzen 9 5900X)

| Step | Time |
|---|---|
| Greeting TTS, first audio | 0.56 s |
| Front LLM decides to call Hermes | 0.72 s |
| Spoken "I'm on it" | ~1.3 s after your message |
| Hermes weather task | 18.9 s, then spoken summary |

## Latency (warm, measured 2026-11-17)

| Stage | Time |
|---|---|
| End of speech detected | 0.2 s silence + smart-turn check (waits up to 1.2 s if you sound unfinished) |
| Whisper base.en, int8, 8 threads | ~0.5 s for a short phrase (small: ~1.4 s) |
| Front LLM first token | 0.3-0.8 s |
| Kokoro first audio | ~0.9-1.1 s |
| **Total, end of speech → Jarvis talking** | **~2-3 s** |

Things that make it slow:
- **Cold model.** Ollama unloads models after 5 idle minutes; reloading takes ~9-10 s. The bot warms the
  model up when you connect. To keep it loaded, set `OLLAMA_KEEP_ALIVE` (see root README).
- **Memory pressure.** llama.cpp's RAM prompt cache defaults to 8 GB and fills zram swap on a 16 GB machine.
  Cap it with `LLAMA_ARG_CACHE_RAM` (see root README).
- **Hermes busy.** Jarvis and Hermes share one Ollama model, so a reply can queue behind a Hermes task.

## Settings (env vars, optional `apps/voice/.env`)

`SPEACHES_URL`, `STT_MODEL`, `TTS_MODEL`, `TTS_VOICE` (default `bm_daniel`; British: `bm_george`, `bm_lewis`, `bm_fable`,
`af_heart`, ...), `TTS_SPEED` (default 1.1), `TURN_MAX_WAIT_SECS` (default 1.2), `OLLAMA_URL`, `FRONT_MODEL`, `HERMES_BASE`, `HERMES_TIMEOUT`.
`API_SERVER_KEY` is read from the project root `.env`.

## Known limits

- Front LLM and Hermes share one GPU model; Ollama may queue a front reply behind a Hermes call.
- Task status lives in memory; restarting the bot forgets running tasks (the Hermes session keeps the results).
- Memory is read from local files; on Fly this needs an API or a shared volume.
