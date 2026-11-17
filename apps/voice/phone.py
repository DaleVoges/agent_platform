#
# Jarvis on the phone: a Twilio Media Streams server around the same voice bot (bot.run_bot).
#
#   uv run phone.py serve                     # webhook + media stream server on 127.0.0.1:7861
#   uv run phone.py call                      # Jarvis rings your phone (first allowed number)
#   uv run phone.py call +27... "why"         # ... a specific allowed number, with a reason
#
# Only this server is exposed to the internet (e.g. Tailscale Funnel); the browser playground,
# Hermes, Ollama and Speaches stay on 127.0.0.1.
#
# Security:
#   - POST /twilio/voice must carry a valid X-Twilio-Signature (signed with your auth token).
#   - Inbound calls are accepted only from PHONE_ALLOWED_NUMBERS; everyone else hears a polite no.
#   - The media WebSocket URL holds a short-lived, single-use HMAC token, so nobody can open a
#     Jarvis session by connecting to /twilio/ws directly.
#   - Outbound calls go only to PHONE_ALLOWED_NUMBERS.
#
import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from xml.sax.saxutils import quoteattr

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from loguru import logger

import bot  # loads .env files, Speaches/Ollama/Hermes settings and run_bot
from pipecat.runner.types import WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")  # your Twilio number, E.164 (+1...)
PUBLIC_HOST = os.getenv("PHONE_PUBLIC_HOST", "")  # e.g. your-machine.your-tailnet.ts.net
ALLOWED = [n.strip() for n in os.getenv("PHONE_ALLOWED_NUMBERS", "").split(",") if n.strip()]
PORT = int(os.getenv("PHONE_PORT", "7861"))
TOKEN_TTL = 120  # seconds between the webhook answering and Twilio opening the stream

PHONE_NOTES = (
    "You are on a phone call (8 kHz audio, so speech recognition may mishear names and numbers: "
    "confirm important details back). Keep replies even shorter than usual. When the user says "
    "goodbye, say a brief goodbye."
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_used_tokens: dict[str, float] = {}


# ------------------------------------------------------------------ helpers

def _check_config(outbound: bool = False) -> None:
    missing = [
        k
        for k, v in {
            "TWILIO_ACCOUNT_SID": ACCOUNT_SID,
            "TWILIO_AUTH_TOKEN": AUTH_TOKEN,
            "PHONE_PUBLIC_HOST": PUBLIC_HOST,
            "PHONE_ALLOWED_NUMBERS": ALLOWED,
            **({"TWILIO_FROM_NUMBER": FROM_NUMBER} if outbound else {}),
        }.items()
        if not v
    ]
    if missing:
        sys.exit(f"Missing settings in apps/voice/.env: {', '.join(missing)}")


def _twilio_signature_ok(url: str, params: dict, signature: str) -> bool:
    """Twilio's request signing: base64(HMAC-SHA1(auth_token, url + sorted key/value pairs))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def _make_stream_token() -> str:
    """Short-lived token, verifiable by any process that knows the auth token."""
    payload = f"{int(time.time()) + TOKEN_TTL}.{secrets.token_hex(8)}"
    sig = hmac.new(AUTH_TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _consume_stream_token(token: str) -> bool:
    try:
        expiry, nonce, sig = token.split(".")
        good = hmac.new(AUTH_TOKEN.encode(), f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    except ValueError:
        return False
    now = time.time()
    for t, exp in list(_used_tokens.items()):
        if exp < now:
            del _used_tokens[t]
    if not hmac.compare_digest(good, sig) or int(expiry) < now or token in _used_tokens:
        return False
    _used_tokens[token] = int(expiry)
    return True


def _stream_twiml(params: dict[str, str]) -> str:
    """TwiML that connects the call audio to our media WebSocket."""
    url = f"wss://{PUBLIC_HOST}/twilio/ws/{_make_stream_token()}"
    extra = "".join(
        f"\n      <Parameter name={quoteattr(k)} value={quoteattr(v)} />" for k, v in params.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n  <Connect>\n'
        f"    <Stream url={quoteattr(url)}>{extra}\n    </Stream>\n  </Connect>\n</Response>"
    )


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


# ------------------------------------------------------------------ routes

@app.get("/twilio/health")
async def health():
    return {"ok": True}


@app.post("/twilio/voice")
async def incoming_call(request: Request):
    """Twilio calls this when someone rings your Twilio number."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = f"https://{PUBLIC_HOST}/twilio/voice"
    if not _twilio_signature_ok(url, form, request.headers.get("X-Twilio-Signature", "")):
        logger.warning("Rejected /twilio/voice: bad Twilio signature")
        return Response(status_code=403)
    caller = form.get("From", "")
    if caller not in ALLOWED:
        logger.warning(f"Rejected inbound call from {caller}")
        return _xml(
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response><Say>Sorry, this line is private.'
            "</Say><Hangup/></Response>"
        )
    logger.info(f"Inbound call from {caller} ({form.get('CallSid')})")
    return _xml(_stream_twiml({"direction": "inbound", "from_number": caller}))


@app.websocket("/twilio/ws/{token}")
async def media_stream(websocket: WebSocket, token: str):
    if not _consume_stream_token(token):
        logger.warning("Rejected media stream: invalid, expired or reused token")
        await websocket.close(code=4003)
        return
    await websocket.accept()
    runner_args = WebSocketRunnerArguments(websocket=websocket)
    transport = await create_transport(
        runner_args,
        {"twilio": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)},
    )
    if runner_args.transport_type != "twilio":
        logger.warning(f"Unexpected media stream type {runner_args.transport_type}")
        await websocket.close()
        return
    params = (runner_args.call_data or {}).get("body", {}) or {}
    reason = params.get("reason", "")
    if params.get("direction") == "outbound":
        greeting = f"Hello sir, Jarvis here. {reason}" if reason else "Hello sir, Jarvis here."
        context = PHONE_NOTES + " You placed this call to the user." + (
            f" Reason for calling: {reason}" if reason else ""
        )
    else:
        greeting = "Jarvis here. How can I help, sir?"
        context = PHONE_NOTES + " The user called you."
    await bot.run_bot(transport, runner_args, greeting=greeting, call_context=context)


# ------------------------------------------------------------------ outbound

async def place_call(to: str, reason: str) -> str:
    _check_config(outbound=True)
    if to not in ALLOWED:
        sys.exit(f"{to} is not in PHONE_ALLOWED_NUMBERS; refusing to call it.")
    twiml = _stream_twiml({"direction": "outbound", "reason": reason})
    async with httpx.AsyncClient(timeout=20, auth=(ACCOUNT_SID, AUTH_TOKEN)) as client:
        r = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls.json",
            data={"To": to, "From": FROM_NUMBER, "Twiml": twiml},
        )
    if r.status_code >= 300:
        sys.exit(f"Twilio refused the call ({r.status_code}): {r.text}")
    return r.json()["sid"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Jarvis phone line (Twilio)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the webhook + media stream server")
    c = sub.add_parser("call", help="have Jarvis call an allowed number")
    c.add_argument("to", nargs="?", help="E.164 number, e.g. +27821234567 (default: first allowed)")
    c.add_argument("reason", nargs="?", default="", help="what the call is about")
    a = ap.parse_args()

    if a.cmd == "serve":
        _check_config()
        logger.info(f"Phone server on 127.0.0.1:{PORT}; Twilio webhook: https://{PUBLIC_HOST}/twilio/voice")
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    else:
        to = a.to or (ALLOWED[0] if ALLOWED else "")
        sid = asyncio.run(place_call(to, a.reason))
        print(f"Calling {to} (call {sid}). The phone server must be running to connect the audio.")


if __name__ == "__main__":
    main()
