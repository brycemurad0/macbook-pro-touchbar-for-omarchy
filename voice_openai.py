#!/usr/bin/env python3
"""Contender: OpenAI Realtime (gpt-realtime) via direct websocket.
Local side pumps mic/speaker and executes function calls."""
import asyncio, base64, json, os, struct, subprocess, sys, time
sys.path.insert(0, os.path.expanduser("~/.config/omarchy/touchbar"))
import voice_tools as vt
import websockets

BASE = os.path.expanduser("~/.config/omarchy/touchbar")
_cfg0 = json.load(open(os.path.join(BASE, "voice.json"))) \
    .get("backends", {}).get("openai", {})
MODEL = os.environ.get("OMARCHY_RT_MODEL", _cfg0.get("model", "gpt-realtime"))
VOICE = os.environ.get("OMARCHY_RT_VOICE", _cfg0.get("voice", "marin"))

def rms(data):
    if len(data) < 2:
        return 0
    s = struct.unpack("<%dh" % (len(data) // 2), data[:len(data) // 2 * 2])
    return (sum(x * x for x in s) / max(len(s), 1)) ** 0.5

def _secret(name):
    try:
        for line in open(vt.SECRETS_ENV):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"') or None
    except OSError:
        pass
    return os.environ.get(name)

async def main():
    import datetime
    key = _secret("OPENAI_API_KEY")
    if not key:
        print("pipecat voice up: OpenAI Realtime variant (NO KEY)", flush=True)
        print("OPENAI_API_KEY missing from voice-secrets.env", flush=True)
        while True:
            await asyncio.sleep(30)
    cfg = json.load(open(os.path.join(BASE, "voice.json")))
    system = (cfg["prompt"]
              .replace("{{node}}", os.uname().nodename)
              .replace("{{time}}", datetime.datetime.now().strftime("%A %H:%M"))
              .replace("{{fleet}}", "Fleet state is NOT preloaded: call fleet_status / fleet_sessions / node_status for live state, every time.")
              .replace("{{brain}}", f"OpenAI's {MODEL} live voice model"))
    tools = [{"type": "function", "name": t["name"],
              "description": t["description"],
              "parameters": t.get("parameters") or {"type": "object",
                                                    "properties": {}}}
             for t in cfg["tools"]]
    mic_target = os.environ.get("OMARCHY_VOICE_MIC")
    spk_target = os.environ.get("OMARCHY_VOICE_SPK") or vt.speaker_target()
    rec_args = ["pw-record", "--rate", "24000", "--channels", "1",
                "--format", "s16"]
    if mic_target:
        rec_args += ["--target", mic_target]
    rec_args.append("-")
    rec = subprocess.Popen(rec_args, stdout=subprocess.PIPE)
    player = subprocess.Popen(
        ["pw-cat", "--playback", "--raw", "--rate", "24000",
         "--channels", "1", "--format", "s16",
         *(["--target", spk_target] if spk_target else []), "-"],
        stdin=subprocess.PIPE)
    print("player live (pw-cat rate=24000 target=%s)" % spk_target, flush=True)
    print("mic live (pw-record)", flush=True)
    play_end = [0.0]
    url = f"wss://api.openai.com/v1/realtime?model={MODEL}"
    try:
        return await _run(url, key, cfg, system, tools, rec, player,
                          spk_target, play_end, vt)
    except Exception as exc:
        text = str(exc)
        if any(k in text for k in ("401", "403", "429", "insufficient",
                                   "quota", "billing")):
            print(f"openai unavailable ({text[:120]}) — exit 42", flush=True)
            return 42
        raise

async def _run(url, key, cfg, system, tools, rec, player, spk_target,
               play_end, vt):
    import websockets as _ws
    async with _ws.connect(
            url, additional_headers={"Authorization": f"Bearer {key}"},
            max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": MODEL, "instructions": system,
            "tools": tools, "tool_choice": "auto",
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000},
                          "transcription": {"model": "gpt-4o-mini-transcribe"},
                          "turn_detection": {"type": "server_vad",
                                             "create_response": True,
                                             "interrupt_response": True}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000},
                           "voice": VOICE}}}}))
        print("pipecat voice up: OpenAI Realtime variant", flush=True)
        vt.set_state(True, "listening")
        greeted = [False]

        async def greet():
            await asyncio.sleep(1)
            await ws.send(json.dumps({
                "type": "conversation.item.create", "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text":
                                 "(The user just toggled voice on. Greet "
                                 "them briefly, in character.)"}]}}))
            await ws.send(json.dumps({"type": "response.create"}))
            greeted[0] = True

        async def pump_mic():
            loop = asyncio.get_running_loop()
            while True:
                data = await loop.run_in_executor(None, rec.stdout.read, 4800)
                if not data:
                    break
                speaking = time.monotonic() < play_end[0] + 0.5
                if speaking and rms(data) < 4000:
                    continue
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(data).decode()}))

        async def announcer():
            while True:
                await asyncio.sleep(5)
                for jid, st in vt.pop_finished_jobs():
                    await ws.send(json.dumps({
                        "type": "conversation.item.create", "item": {
                            "type": "message", "role": "user",
                            "content": [{"type": "input_text", "text":
                                f"[Background job {jid} ({st['goal'][:80]}) "
                                f"{st['status']}. Result: "
                                f"{st.get('outcome', '')}] Report this "
                                "outcome now in one or two short "
                                "sentences."}]}}))
                    await ws.send(json.dumps({"type": "response.create"}))

        async def pump_in():
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "session.updated" and not greeted[0]:
                    asyncio.create_task(greet())
                elif t == "response.output_audio.delta":
                    data = base64.b64decode(msg.get("delta") or "")
                    play_end[0] = max(time.monotonic(), play_end[0]) \
                        + len(data) / 2 / 24000
                    player.stdin.write(data)
                    player.stdin.flush()
                elif t == "conversation.item.input_audio_transcription.completed":
                    text = (msg.get("transcript") or "").strip()
                    if text:
                        print("heard:", text[:200], flush=True)
                        vt.note_user_turn()
                elif t == "response.output_audio_transcript.done":
                    text = (msg.get("transcript") or "").strip()
                    if text:
                        print("assistant:", text[:200], flush=True)
                        vt.note_said(text)
                elif t == "response.function_call_arguments.done":
                    name = msg.get("name")
                    try:
                        args = json.loads(msg.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    print(f"tool call: {name} {str(args)[:120]}", flush=True)
                    func = vt.TOOL_FUNCS.get(name)
                    try:
                        result = await asyncio.to_thread(func, dict(args)) \
                            if func else "unknown tool"
                    except Exception as exc:
                        result = f"tool failed: {exc}"
                    await ws.send(json.dumps({
                        "type": "conversation.item.create", "item": {
                            "type": "function_call_output",
                            "call_id": msg.get("call_id"),
                            "output": str(result)[:4000]}}))
                    await ws.send(json.dumps({"type": "response.create"}))
                elif t == "error":
                    print("realtime error:", str(msg.get("error"))[:200],
                          flush=True)
                    err = str(msg.get("error")).lower()
                    if any(k in err for k in ("insufficient", "quota",
                                              "billing", "rate_limit")):
                        print("openai credits exhausted — exit 42",
                              flush=True)
                        sys.exit(42)
        await asyncio.gather(pump_mic(), pump_in(), announcer())

sys.exit(asyncio.run(main()))
