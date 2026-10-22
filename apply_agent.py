#!/usr/bin/env python3
"""Create or update the ElevenLabs agent from agent-profile.json.

The profile is the source of truth (persona, voice, brain, tools); this
script resolves the voice by name, applies the profile to the account,
and stores the resulting agent_id in voice-config.json. The API key is
read from ELEVENLAB_API_KEY env or voice-secrets.env and never printed.

Usage: python3 apply_agent.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.expanduser("~/.config/omarchy/touchbar")
PROFILE = os.path.join(BASE, "agent-profile.json")
CONFIG = os.path.join(BASE, "voice-config.json")
SECRETS = os.path.join(BASE, "voice-secrets.env")
API = "https://api.elevenlabs.io/v1"


def api_key():
    if os.environ.get("ELEVENLABS_API_KEY"):
        return os.environ["ELEVENLABS_API_KEY"]
    try:
        for line in open(SECRETS):
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def call(key, method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {detail}")


def main():
    key = api_key()
    if not key:
        raise SystemExit("no ELEVENLABS_API_KEY (env or voice-secrets.env)")
    profile = json.load(open(PROFILE))
    voices = call(key, "GET", "/voices").get("voices", [])
    voice = next((v for v in voices
                  if v.get("name", "").lower()
                  == profile["voice_name"].lower()), None) \
        or (voices[0] if voices else None)
    if not voice:
        raise SystemExit("no voices on this ElevenLabs account")
    print(f"voice: {voice.get('name')} ({voice.get('voice_id')})")
    tools = [{"type": "client", "name": t["name"],
              "description": t["description"],
              "parameters": t["parameters"], "expects_response": True}
             for t in profile["tools"]]
    cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    llm_candidates = [profile["llm"], "gemini-2.5-flash",
                      "gemini-2.0-flash", "gpt-4.1-mini"]
    agent_id = cfg.get("agent_id")
    last_err = None
    for llm in dict.fromkeys(llm_candidates):
        body = {
            "name": profile["name"],
            "conversation_config": {
                "agent": {
                    "first_message": profile["first_message"],
                    "language": profile["language"],
                    "prompt": {"prompt": profile["prompt"], "llm": llm,
                               "temperature": profile["temperature"],
                               "tools": tools}},
                "asr": {"user_input_audio_format":
                        profile["audio"]["input_format"]},
                "tts": {"model_id": profile["tts_model"],
                        "voice_id": voice["voice_id"],
                        "agent_output_audio_format":
                            profile["audio"]["output_format"],
                        "voice_settings": profile["voice_settings"]},
                "conversation": profile["conversation"]}}
        try:
            if agent_id:
                call(key, "PATCH", f"/convai/agents/{agent_id}", body)
            else:
                agent_id = call(key, "POST", "/convai/agents/create",
                                body)["agent_id"]
                cfg["agent_id"] = agent_id
            print(f"llm: {llm}")
            break
        except SystemExit as exc:
            last_err = exc
            if "llm" not in str(exc).lower() and "model" not in str(exc).lower():
                raise
            print(f"llm {llm} rejected, trying next", file=sys.stderr)
    else:
        raise last_err or SystemExit("no llm accepted")
    cfg["provider"] = "elevenlabs"
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"agent_id: {agent_id} (saved to voice-config.json)")


if __name__ == "__main__":
    main()
