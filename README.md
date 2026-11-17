# agent-platform

A voice-first "Jarvis" agent platform: talk to it, it makes calls, it does the work. A hosted "digital employee" platform (in the vein of [Azaris](https://azaris.ai)) built on [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Each customer agent is one Hermes container with its own data volume. A control plane (to be built)
creates, configures and bills those containers.

## Run Hermes locally

Prereq: your user can talk to Docker (`docker ps` works without sudo).

```bash
cp .env.example .env        # add an LLM key, API_SERVER_KEY, dashboard password
docker compose up -d
curl localhost:8642/health  # {"status": "ok"}
```

Talk to it (OpenAI-compatible):

```bash
curl localhost:8642/v1/chat/completions \
  -H "Authorization: Bearer $API_SERVER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":"Hello"}]}'
```

Hermes's own dashboard: http://localhost:9119 (log in with the basic-auth values from `.env`).

Interactive setup wizard / CLI inside the container:

```bash
docker compose exec hermes-dev hermes setup
```

## More instances (one per tenant)

```bash
scripts/instance.sh create acme
scripts/instance.sh list
```

## Layout

| Path | What |
|---|---|
| `docker-compose.yml` | Local dev Hermes instance |
| `scripts/instance.sh` | Manual multi-instance tool (precursor to the control plane) |
| `deploy/fly/` | Fly.io template for one instance |
| `data/instances/<name>/` | Each instance's `/opt/data` (config, keys, sessions, memory). Git-ignored. |
| `apps/voice/` | Jarvis: real-time voice front (Pipecat) that hands tasks to Hermes |
| `apps/` | Future: control-plane API + web UI |
| `docs/ROADMAP.md` | Plan |

## Gotcha: API key ownership

On first boot without `API_SERVER_KEY`, Hermes generates its own key into the instance's `data/instances/<name>/.env`,
and that file wins over the container environment. The project `.env` / control plane should be the only source of the key:
always pass `API_SERVER_KEY` at first start, and if an instance has its own line, delete it and restart.

## Local model (Ollama, no subscriptions)

The dev instance uses Ollama on your machine (`data/instances/dev/config.yaml`: `provider: custom`,
`base_url: http://127.0.0.1:11434/v1`, `default: gemma4-64k`). Hermes needs a model with tool calling
and at least a 64k context window.

```bash
ollama pull gemma4:12b                     # ~8 GB, fits a 12 GB GPU
printf 'FROM gemma4:12b\nPARAMETER num_ctx 65536\n' > /tmp/Modelfile
ollama create gemma4-64k -f /tmp/Modelfile
docker compose restart
```

Gemma 4 needs a recent Ollama (0.24.0 is too old; tested with 0.40.16). Measured on an RTX 3080:
first reply ~110 s (model load + ~11.5k-token system prompt), warm tool-using replies ~10 s.

If the `ollama` systemd service crash-loops with `mkdir /usr/share/ollama: permission denied`:

```bash
sudo install -d -o ollama -g ollama /usr/share/ollama && sudo systemctl restart ollama
```

Recommended on a 16 GB machine (keeps the model loaded, caps llama.cpp's 8 GB RAM prompt cache): add to
`/etc/systemd/system/ollama.service.d/override.conf` under `[Service]`, then
`sudo systemctl daemon-reload && sudo systemctl restart ollama`:

```ini
Environment="OLLAMA_KEEP_ALIVE=2h"
Environment="LLAMA_ARG_CACHE_RAM=1024"
Environment="LLAMA_ARG_CTX_CHECKPOINTS=4"
``` The dev container uses host networking so it can
reach Ollama on 127.0.0.1; `scripts/instance.sh` instances use bridge networking and need a hosted
provider key (or Ollama reachable through the firewall).

## Voice (Telegram voice notes)

The official Hermes image has no speech-to-text engine, so `docker-compose.yml` runs **Speaches**
(OpenAI-compatible Whisper server, CPU) on `127.0.0.1:8000`. One-time model download:

```bash
curl -X POST localhost:8000/v1/models/Systran/faster-whisper-small
```

Download the Kokoro voice model once as well:

```bash
curl -X POST localhost:8000/v1/models/speaches-ai/Kokoro-82M-v1.0-ONNX
```

Instance settings (`data/instances/dev/`):
- `config.yaml`: `stt.provider: openai`, `stt.openai.model: Systran/faster-whisper-small`,
  `voice.auto_tts: true` + `auto_tts_mode: voice_only`, and `tts.provider: kokoro`, a command provider running
  `hermes/scripts/kokoro_tts.sh` (mounted at `/opt/platform/scripts`) (Speaches Kokoro `bm_daniel` 1.1x → ffmpeg → Ogg/Opus). Hermes 0.23.1's
  built-in `openai` provider asks Speaches for Opus, which Speaches can't produce, so Telegram voice replies fail with it
  with `command: "sh /opt/platform/scripts/kokoro_tts.sh {input_path} {output_path} bm_daniel 1.1"`,
  `output_format: ogg`, `voice_compatible: true`
- `.env`: `STT_OPENAI_BASE_URL=http://127.0.0.1:8000/v1`, `VOICE_TOOLS_OPENAI_KEY=<any non-empty value>`

In Telegram, `/voice tts` makes it speak every reply; `/voice on` goes back to voice-notes-only.
Hermes keeps running on the last valid `config.yaml` if you break it: check with
`docker exec -u hermes hermes-dev hermes config check`.
