#!/usr/bin/env python3
"""Bake-off contender: ElevenLabs hosted Conversational AI agent.
Local side only pumps mic/speaker and executes client tool calls."""
import asyncio, base64, json, os, subprocess, sys
sys.path.insert(0, os.path.expanduser("~/.config/omarchy/touchbar"))
import voice_tools as vt
import websockets

BASE = os.path.expanduser("~/.config/omarchy/touchbar")
AGENT_FILE = os.path.join(BASE, "el-agent.json")

def ensure_agent(key, cfg):
    """The hosted agent is a rendered artifact of voice.json: create
    it once, then PATCH it from the current config on every start so
    persona/tools edits apply without dashboard work."""
    import urllib.request, urllib.error
    ecfg = cfg.get("backends", {}).get("elevenlabs", {})
    tools = [{"type": "client", "name": t["name"],
              "description": t["description"],
              "parameters": t.get("parameters") or {"type": "object",
                                                    "properties": {}},
              "expects_response": True} for t in cfg["tools"]]
    import datetime as _dt
    prompt = (cfg["prompt"]
              .replace("{{node}}", os.uname().nodename)
              .replace("{{time}}", _dt.datetime.now().strftime("%A %H:%M"))
              .replace("{{fleet}}", vt.tool_fleet_status())
              .replace("{{brain}}", "ElevenLabs' hosted voice agent"))
    body = {"name": "Omarchy Voice",
            "conversation_config": {
                "agent": {"first_message": cfg["first_message"],
                          "language": "en",
                          "prompt": {"prompt": prompt,
                                     "llm": ecfg.get("llm",
                                                     "gemini-2.5-flash"),
                                     "temperature": 0.7, "tools": tools}},
                "tts": {"voice_id": cfg["tts"]["voice_id"],
                        "model_id": cfg["tts"]["model"]},
                "asr": {"quality": "high"}}}
    agent_id = None
    try:
        agent_id = json.load(open(AGENT_FILE)).get("agent_id")
    except Exception:
        pass
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    if agent_id:
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}",
            data=json.dumps(body).encode(), method="PATCH",
            headers=headers)
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return agent_id
        except urllib.error.HTTPError:
            agent_id = None  # deleted upstream: recreate below
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/convai/agents/create",
        data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        agent_id = json.load(r)["agent_id"]
    json.dump({"agent_id": agent_id}, open(AGENT_FILE, "w"))
    return agent_id

async def main():
    key = vt.resolve_elevenlabs_key()
    cfg = json.load(open(os.path.join(BASE, "voice.json")))
    agent_id = ensure_agent(key, cfg)
    print(f"elevenlabs agent: {agent_id}", flush=True)
    url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={agent_id}"
    mic_target = os.environ.get("OMARCHY_VOICE_MIC")
    spk_target = os.environ.get("OMARCHY_VOICE_SPK") or vt.speaker_target()
    rec_args = ["pw-record", "--rate", "16000", "--channels", "1",
                "--format", "s16"]
    if mic_target:
        rec_args += ["--target", mic_target]
    rec_args.append("-")
    rec = subprocess.Popen(rec_args, stdout=subprocess.PIPE)
    play_args = ["pw-cat", "--playback", "--raw", "--rate", "16000",
                 "--channels", "1", "--format", "s16"]
    if spk_target:
        play_args += ["--target", spk_target]
    play_args.append("-")
    player = subprocess.Popen(play_args, stdin=subprocess.PIPE)
    print("player live (pw-cat rate=16000 target=%s)" % spk_target, flush=True)
    print("mic live (pw-record)", flush=True)
    async with websockets.connect(url, additional_headers={"xi-api-key": key},
                                  max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "conversation_initiation_client_data",
            "conversation_config_override": {},
            "custom_llm_extra_body": {}}))
        print("pipecat voice up: ElevenLabs hosted variant", flush=True)

        async def pump_mic():
            loop = asyncio.get_running_loop()
            while True:
                data = await loop.run_in_executor(
                    None, rec.stdout.read, 3200)
                if not data:
                    break
                await ws.send(json.dumps({
                    "type": "user_audio_chunk",
                    "user_audio_chunk": base64.b64encode(data).decode()}))

        async def pump_ws():
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "audio":
                    b64 = (msg.get("audio_event") or {}).get("audio_base_64")
                    if b64:
                        player.stdin.write(base64.b64decode(b64))
                        player.stdin.flush()
                elif t == "agent_response":
                    text = (msg.get("agent_response_event") or {}).get(
                        "agent_response", "")
                    if text.strip():
                        print("assistant:", text[:200], flush=True)
                        vt.note_said(text)
                elif t == "user_transcript":
                    text = (msg.get("user_transcription_event") or {}).get(
                        "user_transcript", "")
                    if text.strip():
                        print("heard:", text[:200], flush=True)
                        vt.note_user_turn()
                elif t == "client_tool_call":
                    call = msg.get("client_tool_call") or {}
                    name = call.get("tool_name")
                    args = call.get("parameters") or {}
                    print(f"tool call: {name} {str(args)[:120]}", flush=True)
                    func = vt.TOOL_FUNCS.get(name)
                    try:
                        result = await asyncio.to_thread(
                            func, dict(args)) if func else "unknown tool"
                    except Exception as exc:
                        result = f"tool failed: {exc}"
                    await ws.send(json.dumps({
                        "type": "client_tool_result",
                        "tool_call_id": call.get("tool_call_id"),
                        "result": str(result)[:4000],
                        "is_error": False}))
                elif t == "ping":
                    ev = msg.get("ping_event") or {}
                    await ws.send(json.dumps({
                        "type": "pong",
                        "event_id": ev.get("event_id")}))
        await asyncio.gather(pump_mic(), pump_ws())

asyncio.run(main())
