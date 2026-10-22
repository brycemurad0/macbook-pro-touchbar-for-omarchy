#!/usr/bin/env python3
"""Bake-off contender 2: Gemini Live via direct google-genai SDK.
Local side pumps mic/speaker and executes function calls."""
import asyncio, json, os, struct, subprocess, sys, time
sys.path.insert(0, os.path.expanduser("~/.config/omarchy/touchbar"))
import voice_tools as vt

BASE = os.path.expanduser("~/.config/omarchy/touchbar")
MODEL = None  # resolved from voice.json in main()

def rms(data):
    if len(data) < 2:
        return 0
    s = struct.unpack("<%dh" % (len(data) // 2), data[:len(data) // 2 * 2])
    return (sum(x * x for x in s) / max(len(s), 1)) ** 0.5

async def main():
    import datetime
    from google import genai
    from google.genai import types
    cfg = json.load(open(os.path.join(BASE, "voice.json")))
    gcfg = cfg.get("backends", {}).get("gemini", {})
    model = os.environ.get("OMARCHY_GEMINI_MODEL",
                           gcfg.get("model", "gemini-3.1-flash-live-preview"))
    gvoice = gcfg.get("voice", "Kore")
    system = (cfg["prompt"]
              .replace("{{node}}", os.uname().nodename)
              .replace("{{time}}", datetime.datetime.now().strftime("%A %H:%M"))
              .replace("{{fleet}}", vt.tool_fleet_status())
              .replace("{{brain}}", "Google's Gemini Live voice model"))
    tools = [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=t["name"], description=t["description"],
            parameters=t.get("parameters") or {}) for t in cfg["tools"]])]
    mic_target = os.environ.get("OMARCHY_VOICE_MIC")
    spk_target = os.environ.get("OMARCHY_VOICE_SPK") or vt.speaker_target()
    rec_args = ["pw-record", "--rate", "16000", "--channels", "1",
                "--format", "s16"]
    if mic_target:
        rec_args += ["--target", mic_target]
    rec_args.append("-")
    rec = subprocess.Popen(rec_args, stdout=subprocess.PIPE)
    # Gemini Live emits 24kHz PCM
    player = subprocess.Popen(
        ["pw-cat", "--playback", "--raw", "--rate", "24000",
         "--channels", "1", "--format", "s16",
         *(["--target", spk_target] if spk_target else []), "-"],
        stdin=subprocess.PIPE)
    print("player live (pw-cat rate=24000 target=%s)" % spk_target, flush=True)
    print("mic live (pw-record)", flush=True)
    client = genai.Client(api_key=vt._gemini_key())
    play_end = [0.0]
    async with client.aio.live.connect(
            model=model,
            config=types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=system,
                tools=tools,
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=gvoice))),
                input_audio_transcription={},
                output_audio_transcription={},
                context_window_compression=types.ContextWindowCompressionConfig(
                    sliding_window=types.SlidingWindow()),
            )) as session:
        print("pipecat voice up: GeminiLive direct variant", flush=True)
        vt.set_state(True, "listening")
        await session.send_client_content(
            turns={"role": "user", "parts": [{"text":
                "(The user just toggled voice on. Greet them briefly "
                "in character.)"}]}, turn_complete=True)

        async def pump_mic():
            loop = asyncio.get_running_loop()
            while True:
                data = await loop.run_in_executor(None, rec.stdout.read, 3200)
                if not data:
                    break
                # half-duplex guard: while she is speaking only very
                # loud input (a real shout-over) reaches the model
                # continuous stream (server VAD needs the silences
                # to close turns); only gate while SHE is speaking,
                # where echo would otherwise feed back
                speaking = time.monotonic() < play_end[0] + 0.5
                if speaking and rms(data) < 4000:
                    continue
                await session.send_realtime_input(
                    audio=types.Blob(data=data,
                                     mime_type="audio/pcm;rate=16000"))

        heard_buf, said_buf = [], []

        async def announcer():
            while True:
                await asyncio.sleep(5)
                for jid, st in vt.pop_finished_jobs():
                    await session.send_client_content(
                        turns={"role": "user", "parts": [{"text":
                            f"[Background job {jid} ({st['goal'][:80]}) "
                            f"{st['status']}. Result: "
                            f"{st.get('outcome', '')}] Report this outcome "
                            "now in one or two short sentences."}]},
                        turn_complete=True)

        async def pump_in():
          while True:
            async for msg in session.receive():
                if msg.data:
                    play_end[0] = max(time.monotonic(), play_end[0]) \
                        + len(msg.data) / 2 / 24000
                    player.stdin.write(msg.data)
                    player.stdin.flush()
                sc = msg.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    heard_buf.append(sc.input_transcription.text)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    said_buf.append(sc.output_transcription.text)
                if sc and sc.turn_complete:
                    heard = "".join(heard_buf).strip()
                    said = "".join(said_buf).strip()
                    heard_buf.clear(); said_buf.clear()
                    if heard:
                        print("heard:", heard[:200], flush=True)
                        vt.note_user_turn()
                    if said:
                        print("assistant:", said[:200], flush=True)
                        vt.note_said(said)
                if msg.tool_call:
                    for fc in msg.tool_call.function_calls:
                        args = dict(fc.args or {})
                        print(f"tool call: {fc.name} {str(args)[:120]}",
                              flush=True)
                        func = vt.TOOL_FUNCS.get(fc.name)
                        try:
                            result = await asyncio.to_thread(func, args) \
                                if func else "unknown tool"
                        except Exception as exc:
                            result = f"tool failed: {exc}"
                        await session.send_tool_response(
                            function_responses=[types.FunctionResponse(
                                id=fc.id, name=fc.name,
                                response={"result": str(result)[:4000]})])
        await asyncio.gather(pump_mic(), pump_in(), announcer())

asyncio.run(main())
