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
import subprocess
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


def bridge_url():
    if os.environ.get("LLM_BRIDGE_URL"):
        return os.environ["LLM_BRIDGE_URL"].rstrip("/")
    try:
        out = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=6).stdout
        dns = json.loads(out)["Self"]["DNSName"].rstrip(".")
        return f"https://{dns}/v1/chat/completions"
    except Exception:
        return None


def bridge_ready():
    """(url, secret) if the local bridge is up AND has a Gemini key."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8911/health",
                                    timeout=4) as resp:
            health = json.load(resp)
    except Exception:
        return None, None
    if not health.get("key"):
        return None, None
    sec = {}
    try:
        for line in open(SECRETS):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                sec[k] = v
    except OSError:
        pass
    if not sec.get("LLM_BRIDGE_SECRET"):
        return None, None
    return bridge_url(), sec["LLM_BRIDGE_SECRET"]


def workspace_secret(key, name, value):
    """Create or reuse an ElevenLabs workspace secret; return its id."""
    try:
        existing = call(key, "GET", "/workspace/secrets")
        items = existing if isinstance(existing, list) \
            else existing.get("secrets", [])
        for s in items:
            if s.get("name") == name:
                return s.get("secret_id")
    except SystemExit:
        pass
    created = call(key, "POST", "/workspace/secrets",
                   {"type": "new", "name": name, "value": value})
    return created.get("secret_id")


def main():
    key = api_key()
    if not key:
        raise SystemExit("no ELEVENLABS_API_KEY (env or voice-secrets.env)")
    profile = json.load(open(PROFILE))
    voices = call(key, "GET", "/voices").get("voices", [])
    target = profile["voice_name"].lower()
    voice = next((v for v in voices
                  if v.get("name", "").split(" - ")[0].strip().lower()
                  == target), None) \
        or (voices[0] if voices else None)
    if not voice:
        raise SystemExit("no voices on this ElevenLabs account")
    print(f"voice: {voice.get('name')} ({voice.get('voice_id')})")
    tools = [{"type": "client", "name": t["name"],
              "description": t["description"],
              "parameters": t["parameters"], "expects_response": True}
             for t in profile["tools"]]
    cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    # wiring B: if config asks for the custom brain and the local bridge
    # is actually ready (up + Gemini key), serve gemini-3.8-flash from
    # our own endpoint; otherwise stay on the hosted model - never
    # strand the agent on a dead endpoint
    custom = None
    if cfg.get("brain") == "custom":
        url, secret = bridge_ready()
        if url:
            sid = workspace_secret(key, "OMARCHY_LLM_BRIDGE", secret)
            if sid:
                custom = {"url": url, "model_id": "gemini-3.8-flash",
                          "api_key": {"secret_id": sid}}
                print(f"brain: custom bridge {url}")
        if not custom:
            print("brain: custom requested but bridge not ready "
                  "(needs GEMINI_API_KEY); staying hosted", file=sys.stderr)
    llm_candidates = (["custom-llm"] if custom else []) + [
        profile["llm"], "gemini-3.8-flash", "gemini-3.7-flash",
        "gemini-3.5-flash", "gemini-2.5-flash", "gpt-4.1-mini"]
    agent_id = cfg.get("agent_id")
    last_err = None
    for llm in dict.fromkeys(llm_candidates):
        prompt_cfg = {"prompt": profile["prompt"], "llm": llm,
                      "temperature": profile["temperature"],
                      "tools": tools}
        if llm == "custom-llm":
            prompt_cfg["custom_llm"] = custom
        body = {
            "name": profile["name"],
            "conversation_config": {
                "agent": {
                    "first_message": profile["first_message"],
                    "language": profile["language"],
                    "prompt": prompt_cfg},
                "asr": {"user_input_audio_format":
                        profile["audio"]["input_format"]},
                "tts": {"model_id": profile["tts_model"],
                        "voice_id": voice["voice_id"],
                        "agent_output_audio_format":
                            profile["audio"]["output_format"],
                        "voice_settings": profile["voice_settings"]},
                "turn": profile.get("turn", {}),
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
