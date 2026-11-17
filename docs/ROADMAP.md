# Roadmap

## North star: a "Jarvis" you can talk to and that can call

The defining feature is voice: talk to your agent live (browser/app/phone), and have it place and take phone calls.

### What Hermes gives us for voice (from its docs)
- Voice mode: CLI push-to-talk, voice replies on Telegram/Discord, live talk in Discord voice channels. Wake word ("hey hermes") on desktop/CLI.
- TTS with many providers (ElevenLabs, OpenAI, Gemini, xAI, local Piper/KittenTTS, ...). STT via Whisper.
- SMS gateway via Twilio (people text the agent's number).
- Optional `telephony` skill: buys a Twilio number, sends SMS, places **outbound** AI calls through Vapi or Bland.ai.
- Not covered: a real-time **inbound** phone line, or a live phone conversation where Hermes itself is the brain.
  In the telephony skill, Vapi/Bland run their own model during the call; Hermes just kicks it off.

### Why Hermes can't simply be the voice loop
Live voice needs a reply in under ~1 second. A Hermes turn that uses tools (email, browser, search) can take
10–60+ s. So split it in two:

```
 Phone (Twilio) / browser mic / app
            │  audio
            ▼
 ┌──────────────────────────┐   fast model, small prompt, low latency
 │ Voice front ("Jarvis")    │   STT → LLM → TTS, barge-in, filler ("on it...")
 │ Pipecat or LiveKit Agents │   tools: ask_hermes(task), get_status(run_id)
 │ or Vapi w/ custom LLM     │
 └────────────┬─────────────┘
              │  POST /v1/runs (async)  /  /v1/chat/completions (quick Qs)
              ▼
 ┌──────────────────────────┐   the "brain": memory, integrations, browser, long tasks
 │ Hermes instance           │   reports back by speaking it on the call, or later via
 └──────────────────────────┘   Telegram/SMS/a callback call
```

Options for the voice front, cheapest-to-build first:
1. **Vapi (or Retell) + Twilio number, "custom LLM" pointed at our endpoint.** Fastest to a working phone call.
   Paid per minute; least control. Good for a demo.
2. **Pipecat** (open-source Python) **+ Twilio Media Streams.** Runs as one more container next to Hermes,
   same on Fly. Pick any STT (Deepgram), LLM, TTS (ElevenLabs/Cartesia). Recommended for the real product.
3. **LiveKit Agents + LiveKit SIP.** Best if you also want a polished in-app/browser voice UI; more infra.
   Or a speech-to-speech model (OpenAI Realtime / Gemini Live) as the front, still calling Hermes as a tool.

### Voice milestones
- [x] V1: Hermes replies with voice on Telegram (Speaches Whisper STT + Edge TTS)
- [ ] V2: Outbound calls via the `telephony` skill (Twilio + Vapi) — "call the dentist and book Tuesday"
- [x] V3: `apps/voice` service (Pipecat): browser voice → Jarvis (Gemma, thinking off) → Hermes in background
- [~] V4: Phone line: Twilio number → Pipecat → Jarvis, in and out (`apps/voice/phone.py`); caller allowlist now, caller ID → user lookup later
- [ ] V5: Outbound calls through our own pipeline (replace Vapi), call transcripts saved to Hermes memory
- [ ] Safety: always confirm before dialing; never emergency numbers; per-plan minute limits; recording consent per region

Billing note: phone minutes + STT/TTS cost real money per minute. Meter minutes per tenant from day one.

## Target: what Azaris sells, and who provides it

Reference product: https://azaris.ai — "digital employees". One always-on agent per customer that lives in
their chat app, connects to their business tools, works overnight, and asks before big actions.
$97/mo self-serve (3-day trial, AI credits included or bring your own ChatGPT plan) + custom done-for-you tier.

| Azaris feature | Hermes already has it? | What we build |
|---|---|---|
| Lives in Telegram / Slack / Discord | Yes: messaging gateways | UI to paste bot tokens / run the Slack app install, then write them to the instance |
| iMessage | Partly: BlueBubbles / Photon gateways (needs a Mac relay) | Later, or skip at launch |
| Voice | Yes: TTS / voice mode | Later |
| "1,000+ tools" (Gmail, Calendar, QuickBooks, Meta Ads...) | MCP support + a bundled catalog of optional MCP servers | OAuth "Connect" buttons. Easiest: a managed-auth provider (e.g. Composio or Pipedream Connect) exposed to Hermes as one MCP server per tenant |
| Per-agent permissions ("what each agent may access") | Toolsets / per-profile config | Toggles in UI → instance `config.yaml` |
| Persistent memory, "learns your business" | Yes: memory, skills, context files | Onboarding step that runs an initial "read everything and summarize" job |
| Works overnight, morning report | Yes: cron, heartbeat, goals | Schedule picker in UI |
| Cloud browser | Yes: browser tool (Playwright in the image) | Nothing at first; resource limits per plan |
| Approval cards | Yes: approval prompts in chat gateways | Make sure risky tools require approval by default |
| Multiple named agents ("Milo") | Profiles / one instance per agent | Agent create/rename/personality (SOUL.md) in UI |
| AI included + top-ups | No (single provider key) | Route every instance through our own LLM proxy key, meter tokens per tenant, Stripe credits |
| Bring your own ChatGPT plan | Has a Codex/ChatGPT subscription runtime | Later |
| 3-day trial, $97/mo, affiliates | No | Stripe Checkout + trial; affiliates via Rewardful/Tolt later |

The takeaway: Hermes is ~70% of the product. The platform is onboarding, provisioning, the integrations
"Connect" screen, metering, and billing.

## Architecture

```
 Browser ──> Web UI (apps/web) ──> Control plane API (apps/api) ──> Postgres
                                          │  (users, orgs, agents, integrations, usage)
                                          │
                     provisions & proxies │  Docker API (local)  /  Fly Machines API (prod)
                                          ▼
                 ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
                 │ Hermes: acme │ │ Hermes: bob  │ │ Hermes: ...  │   one container + volume per agent
                 │ :8642 /v1    │ │ :8642 /v1    │ │              │   private network only
                 └──────────────┘ └──────────────┘ └──────────────┘
```

Rules that keep this simple:
- Hermes instances are never public. Only the control plane talks to them, with a per-instance `API_SERVER_KEY`.
- All per-agent state is in that instance's `/opt/data` volume. The control plane stores only metadata + secrets.
- One `InstanceDriver` interface with two implementations: `DockerDriver` (local) and `FlyDriver` (prod). Same code path otherwise.

## Phase 1 — Hermes running (now)
- [ ] Add your user to the `docker` group, fill in `.env`, `docker compose up -d`
- [ ] Chat with it via `/v1/chat/completions` and the built-in dashboard
- [ ] Learn its config surface: `config.yaml`, `.env`, skills, MCP servers, messaging gateways (Telegram/Slack/Discord) — these become your "integrations"
- [ ] Spin up 2–3 instances with `scripts/instance.sh` to confirm isolation
- [ ] Pin the image to a version tag

## Phase 2 — Control plane + UI
- Stack suggestion: Next.js (UI) + a TypeScript or Python API, Postgres, Auth (Clerk / Better Auth / Supabase Auth)
- Agents: create / start / stop / delete → driver calls
- Chat: UI → API → proxy streaming to the instance's `/v1/responses` or `/v1/chat/completions`
- Integrations: forms that write the instance's `.env` / `config.yaml` (API keys, MCP servers, Telegram/Slack tokens), then restart the instance
- Logs + health per agent

## Phase 3 — Fly.io
- `FlyDriver`: Fly Machines API, one Machine + Volume per agent, `auto_stop` to suspend idle agents
- Control plane on Fly too, reaching agents over 6PN (`*.internal`)
- Backups of volumes (snapshots)

## Phase 4 — Billing
- Stripe subscriptions (plan = number of agents / machine size)
- Usage metering: either route all LLM calls through your own key + proxy (OpenRouter / LiteLLM) and meter tokens, or BYO-key per customer (simpler to start)
- Suspend Machines on non-payment
