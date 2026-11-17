#!/bin/sh
# Hermes command TTS provider: Kokoro voice from the local Speaches container, encoded as
# Ogg/Opus so Telegram shows it as a voice note. (Speaches can't output Opus itself, and
# Hermes 0.23.1's openai provider asks for Opus and fails.)
# Usage: kokoro_tts.sh INPUT_TXT OUTPUT_OGG [VOICE] [SPEED]
set -eu
in="$1"; out="$2"; voice="${3:-bm_daniel}"; speed="${4:-1.1}"
python3 -c 'import json, sys
print(json.dumps({"model": "speaches-ai/Kokoro-82M-v1.0-ONNX", "voice": sys.argv[2],
                  "speed": float(sys.argv[3]), "response_format": "wav",
                  "input": open(sys.argv[1], encoding="utf-8").read()}))' "$in" "$voice" "$speed" \
  | curl -sf --max-time 120 http://127.0.0.1:8000/v1/audio/speech \
      -H 'Content-Type: application/json' --data-binary @- \
  | ffmpeg -loglevel error -y -f wav -i - -c:a libopus -b:a 48k -ar 48000 -ac 1 "$out"
