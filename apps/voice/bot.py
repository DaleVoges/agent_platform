"""Jarvis: real-time voice front for Hermes.

Browser mic (WebRTC) -> Silero VAD -> Whisper (Speaches) -> fast LLM (Ollama, no thinking)
-> Kokoro TTS (Speaches) -> speaker.

The fast LLM keeps the conversation snappy. Anything that needs real work (email, web,
files, memory, scheduling...) goes to Hermes through the `ask_hermes` tool, which runs in
the background. Jarvis keeps talking, and speaks the result when Hermes is done.

Shared brain:
- All voice tasks go into ONE ongoing Hermes session (id kept in .state.json), so Hermes
  remembers earlier voice requests, across reconnects.
- On connect, Jarvis is briefed with Hermes's long-term memory (USER.md / MEMORY.md) and
  a recap of the previous call.
- On disconnect, the call transcript goes to Hermes, which saves anything worth keeping to
  its memory (shared with Telegram) and returns a short recap for next time.

Run:  uv run bot.py -t webrtc --host 127.0.0.1 --port 7860   then open /client
"""

import asyncio
import json
import os
import re
import time
import uuid

import httpx
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

# Voice settings live in apps/voice/.env; the Hermes API key comes from the project .env.
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, "..", "..", ".env"))
load_dotenv(os.path.join(HERE, ".env"), override=True)

SPEACHES_URL = os.getenv("SPEACHES_URL", "http://127.0.0.1:8000/v1")
# base.en: ~0.5 s per phrase on CPU vs ~1.4 s for small (int8, 8 threads), same results on commands.
STT_MODEL = os.getenv("STT_MODEL", "Systran/faster-whisper-base.en")
# Longest silence to wait when the smart-turn model thinks you haven't finished (Pipecat default 3 s).
TURN_MAX_WAIT_SECS = float(os.getenv("TURN_MAX_WAIT_SECS", "1.2"))
TTS_MODEL = os.getenv("TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX")
TTS_VOICE = os.getenv("TTS_VOICE", "bm_daniel")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.1"))  # a touch brisk
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/v1")
FRONT_MODEL = os.getenv("FRONT_MODEL", "gemma4-64k")
HERMES_BASE = os.getenv("HERMES_BASE", "http://127.0.0.1:8642")
HERMES_KEY = os.environ.get("API_SERVER_KEY", "")
HERMES_TIMEOUT = float(os.getenv("HERMES_TIMEOUT", "900"))
# Hermes memory files. Local dev reads the instance's data dir directly.
MEMORY_DIR = os.getenv(
    "HERMES_MEMORY_DIR", os.path.join(HERE, "..", "..", "data", "instances", "dev", "memories")
)
STATE_FILE = os.path.join(HERE, ".state.json")

# Pipecat's OpenAI TTS only accepts OpenAI's voice names. Speaches serves the same API with
# Kokoro voices (bm_george, af_heart, ...), so register ours as valid.
from pipecat.services.openai import tts as _openai_tts  # noqa: E402

_openai_tts.VALID_VOICES[TTS_VOICE] = TTS_VOICE

BASE_PROMPT = """You are Jarvis, a calm, witty British AI assistant talking out loud with your user.
Everything you say is spoken, so: one to three short sentences, no lists, no markdown, no emojis.

You have a back-office agent called Hermes that can actually do things: browse the web, check
weather and news, read and write files, run commands, remember facts, set reminders and schedules,
and use the user's connected tools. Hermes remembers your earlier requests in this voice channel.
- For small talk and things you clearly know, answer directly.
- For anything that needs real-world information or action, call ask_hermes with a clear,
  self-contained task, then tell the user you are on it. Do not make up results.
- Always put relevant facts you know into the task, especially the user's location and the
  current date, e.g. "weather in Cape Town tomorrow", never just "the weather".
- If the user asks you to remember something, call ask_hermes to save it to memory.
- When a Hermes result arrives, tell the user the gist in one or two spoken sentences.
- If the user asks about progress, call task_status."""


# ---------------------------------------------------------------- state & memory

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


_SECRETISH = re.compile(
    r"(token|api[_ -]?key|secret|password|passwd|bearer)"  # lines that talk about credentials
    r"|\b\d{6,12}:[A-Za-z0-9_-]{25,}\b"  # Telegram bot tokens
    r"|\b(sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{12,}"  # common API key prefixes
    r"|[A-Za-z0-9_+/=-]{40,}",  # long opaque strings
    re.IGNORECASE,
)


def read_memory() -> str:
    """Hermes's USER.md and MEMORY.md, minus anything that looks like a credential."""
    parts = []
    for name, label in (("USER.md", "About the user"), ("MEMORY.md", "Hermes's notes")):
        try:
            with open(os.path.join(MEMORY_DIR, name)) as f:
                raw = f.read()
        except OSError:
            continue
        # Hermes separates memory entries with lines containing only "§".
        entries = [" ".join(e.split()) for e in re.split(r"^\s*§\s*$", raw, flags=re.M)]
        entries = [e for e in entries if e]
        kept = [e for e in entries if not _SECRETISH.search(e)]
        if len(kept) < len(entries):
            logger.warning(f"Left {len(entries) - len(kept)} credential-like entr(y/ies) of {name} out of the briefing")
        if kept:
            parts.append(f"{label}:\n" + "\n".join(f"- {e.lstrip('-* ')}" for e in kept))
    return "\n\n".join(parts)


def build_prompt(state: dict) -> str:
    prompt = BASE_PROMPT
    prompt += "\n\nIt is now " + time.strftime("%A %d %B %Y, %H:%M %Z") + "."
    memory = read_memory()
    if memory:
        prompt += "\n\n# What you know (from Hermes's long-term memory)\n" + memory
    if state.get("last_call_recap"):
        prompt += f"\n\n# Last voice call ({state.get('last_call_at', 'earlier')})\n{state['last_call_recap']}"
    return prompt


# ---------------------------------------------------------------- Hermes API

def _headers() -> dict:
    return {"Authorization": f"Bearer {HERMES_KEY}"}


async def ensure_session(state: dict) -> str:
    """Return the ongoing voice session id, creating it if missing or deleted."""
    sid = state.get("hermes_session_id")
    async with httpx.AsyncClient(timeout=15) as client:
        if sid:
            r = await client.get(f"{HERMES_BASE}/api/sessions/{sid}", headers=_headers())
            if r.status_code == 200:
                return sid
            logger.warning(f"Voice session {sid} not usable ({r.status_code}); creating a new one")
        r = await client.post(
            f"{HERMES_BASE}/api/sessions", headers=_headers(), json={"title": "Jarvis voice"}
        )
        r.raise_for_status()
        sid = r.json()["session"]["id"]
    state["hermes_session_id"] = sid
    save_state(state)
    logger.info(f"Created Hermes voice session {sid}")
    return sid


async def call_hermes(session_id: str, text: str) -> str:
    """One turn in the ongoing voice session; Hermes loads the session's history itself."""
    async with httpx.AsyncClient(timeout=HERMES_TIMEOUT) as client:
        r = await client.post(
            f"{HERMES_BASE}/v1/chat/completions",
            headers={**_headers(), "X-Hermes-Session-Id": session_id},
            json={"model": "hermes-agent", "messages": [{"role": "user", "content": text}]},
        )
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "error":
            raise RuntimeError(data.get("hermes", {}).get("error") or choice["message"]["content"])
        return choice["message"]["content"]


VOICE_NOTE = "\n\n(Your answer will be read aloud by a voice assistant. Keep it short and plain, no markdown.)"


async def wrap_up_call(session_id: str, transcript: str, state: dict) -> None:
    """After a call: let Hermes save durable facts to memory and give a recap for next time."""
    prompt = (
        "The voice call with the user just ended. Transcript (Jarvis is the voice assistant):\n\n"
        f"{transcript}\n\n"
        "1. Use your memory tool to save anything durable and worth remembering about the user "
        "or their requests (preferences, facts, commitments). Never save credentials. Skip it if "
        "there is nothing new.\n"
        "2. Then reply with ONLY a recap of the call in at most two sentences, for the voice "
        "assistant to read before the next call."
    )
    try:
        recap = (await call_hermes(session_id, prompt)).strip()
    except Exception:  # noqa: BLE001
        logger.exception("Call wrap-up failed")
        return
    state["last_call_recap"] = recap
    state["last_call_at"] = time.strftime("%Y-%m-%d %H:%M")
    save_state(state)
    logger.info(f"Call wrap-up saved. Recap: {recap}")


# ---------------------------------------------------------------- bot

# Background Hermes tasks: id -> {"task", "started", "status", "result"}
TASKS: dict[str, dict] = {}
# Keep references so wrap-up tasks are not garbage-collected after the call ends.
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


transport_params = {
    "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


DEFAULT_GREETING = "Jarvis online. How can I help?"


async def run_bot(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    greeting: str = DEFAULT_GREETING,
    call_context: str = "",
):
    """Run one voice conversation. ``call_context`` adds channel notes (e.g. a phone call) to the prompt."""
    state = load_state()
    session_id = await ensure_session(state)
    prompt = build_prompt(state)
    if call_context:
        prompt += "\n\n# This conversation\n" + call_context
    logger.info(f"Voice session {session_id}; briefing is {len(prompt)} chars")

    stt = OpenAISTTService(
        api_key="local",
        base_url=SPEACHES_URL,
        settings=OpenAISTTService.Settings(model=STT_MODEL),
    )
    llm = OLLamaLLMService(
        base_url=OLLAMA_URL,
        settings=OLLamaLLMService.Settings(
            model=FRONT_MODEL,
            system_instruction=prompt,
            # Gemma 4 "thinks" by default (~2 s). Off: ~0.3 s to answer.
            extra={"reasoning_effort": "none"},
        ),
    )
    tts = OpenAITTSService(
        api_key="local",
        base_url=SPEACHES_URL,
        settings=OpenAITTSService.Settings(model=TTS_MODEL, voice=TTS_VOICE, speed=TTS_SPEED),
    )

    worker: PipelineWorker | None = None
    context: LLMContext | None = None
    call_open = True

    async def run_task(task_id: str):
        t = TASKS[task_id]
        try:
            t["result"] = await call_hermes(session_id, t["task"] + VOICE_NOTE)
            t["status"] = "done"
        except Exception as e:  # noqa: BLE001 - report any failure back to the user
            logger.exception("Hermes task failed")
            t["result"] = f"failed: {e}"
            t["status"] = "failed"
        took = time.monotonic() - t["started"]
        logger.info(f"Hermes task {task_id} {t['status']} in {took:.1f}s")
        if call_open and worker and context:
            context.add_message(
                {
                    "role": "system",
                    "content": f"Hermes finished task {task_id} ({t['task']!r}) with status "
                    f"{t['status']}. Result:\n{t['result']}\n\nTell the user now, briefly.",
                }
            )
            await worker.queue_frames([LLMRunFrame()])

    async def ask_hermes(params: FunctionCallParams, task: str):
        """Hand a real-world task to Hermes, the back-office agent. Runs in the background.

        Args:
            task: A clear, self-contained description of what to do or find out, including
                any details the user gave (places, names, dates).
        """
        task_id = uuid.uuid4().hex[:6]
        TASKS[task_id] = {"task": task, "started": time.monotonic(), "status": "running", "result": None}
        _spawn(run_task(task_id))
        logger.info(f"Hermes task {task_id} started: {task}")
        await params.result_callback(
            {"task_id": task_id, "status": "started", "note": "Tell the user you're on it."}
        )

    async def task_status(params: FunctionCallParams):
        """Check on tasks previously given to Hermes."""
        now = time.monotonic()
        await params.result_callback(
            [
                {
                    "task_id": k,
                    "task": v["task"],
                    "status": v["status"],
                    "seconds": round(now - v["started"]),
                }
                for k, v in TASKS.items()
            ]
            or "No tasks yet."
        )

    context = LLMContext(tools=[ask_hermes, task_status])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3(
                            cpu_count=2,
                            params=SmartTurnParams(stop_secs=TURN_MAX_WAIT_SECS),
                        )
                    )
                ]
            ),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    def transcript() -> str:
        lines = []
        for m in context.messages:
            role, content = m.get("role"), m.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            lines.append(f"{'User' if role == 'user' else 'Jarvis'}: {content.strip()}")
        return "\n".join(lines)

    async def warm_up_model():
        """Load the front model now (if Ollama unloaded it) so the first reply isn't a cold start."""
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                await client.post(
                    OLLAMA_URL.removesuffix("/v1") + "/api/generate",
                    json={"model": FRONT_MODEL, "prompt": "", "keep_alive": "2h"},
                )
            logger.info(f"Front model ready in {time.monotonic() - t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Model warm-up failed: {e}")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        _spawn(warm_up_model())
        await worker.queue_frames([TTSSpeakFrame(greeting)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal call_open
        call_open = False
        logger.info("Client disconnected")
        text = transcript()
        if "User:" in text:
            _spawn(wrap_up_call(session_id, text, state))
        await runner.cancel()

    await runner.run()


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
