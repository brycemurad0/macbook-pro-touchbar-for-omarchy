#!/usr/bin/env python3
"""Voice service entrypoint: pick the backend, supervise it.

One config (voice.json) drives every backend. "backend": "auto"
means: run the first backend whose API key is present in
voice-secrets.env, in the order below. If a backend exits with
code 42 (key rejected / out of credits), fall through to the
next one — OpenAI Realtime primary, Gemini Live as the funded
fallback, then ElevenLabs hosted, then the local Pipecat cascade.
Set "backend" to a name or OMARCHY_VOICE_BACKEND to force one.
"""
import json, os, subprocess, sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import voice_tools as vt

BACKENDS = [
    ("openai", "OPENAI_API_KEY", "voice_openai.py"),
    ("gemini", "GEMINI_API_KEY", "voice_gemini.py"),
    ("elevenlabs", "ELEVENLABS_API_KEY", "voice_elevenlabs.py"),
    ("pipecat", None, "pipecat_voice.py"),
]
FALLBACK_EXIT = 42

def _has(name):
    try:
        for line in open(vt.SECRETS_ENV):
            if line.startswith(name + "="):
                return bool(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return bool(os.environ.get(name))

def main():
    cfg = json.load(open(os.path.join(BASE, "voice.json")))
    want = os.environ.get("OMARCHY_VOICE_BACKEND") or cfg.get("backend", "auto")
    chain = []
    for name, key_name, script in BACKENDS:
        if want not in ("auto", name):
            continue
        if key_name and not _has(key_name):
            continue
        chain.append((name, script))
        if want == name:
            break
    if not chain:
        chain = [("pipecat", "pipecat_voice.py")]
    for i, (name, script) in enumerate(chain):
        print(f"voice backend: {name} ({script})", flush=True)
        rc = subprocess.run(
            [sys.executable, os.path.join(BASE, script)]).returncode
        if rc == FALLBACK_EXIT and i < len(chain) - 1:
            print(f"voice backend {name} unavailable "
                  "(key/credits) — falling back", flush=True)
            continue
        return rc
    return 1

if __name__ == "__main__":
    sys.exit(main())
