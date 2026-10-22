#!/usr/bin/env python3
"""Omarchy voice agent on Pipecat: the open-source framework owns the
hard part (VAD, end-of-turn, interruptions, streaming TTS), we own the
machine (PipeWire transport, fleet tools, persona, touchbar state).

Pipeline: pw-record -> Silero VAD -> Moonshine (local STT) -> Gemini
(brain, function calls to the same local tools as voice_agent.py) ->
ElevenLabs TTS (Sarah) -> pw-cat to the speaker convolver.

No inbound exposure: every connection is outbound. Echo control: the
mic is ducked (not muted) while the bot speaks, so a loud real voice
can still barge in but speaker echo cannot self-trigger.

Run with the project venv: .venv/bin/python pipecat_voice.py
State file and metrics are shared with voice_agent.py's format.
"""
import asyncio
import json
import os
import signal
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_agent as va  # tools, metrics, state, secrets helpers

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMRunFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

RATE = 16000
BASE = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------ pw transport
class DuckState:
    """Shared playback clock: mic ducks while bot audio is sounding."""

    def __init__(self):
        self.play_end = 0.0

    def note_output(self, nbytes, rate):
        self.play_end = max(time.monotonic(), self.play_end) \
            + nbytes / 2 / rate

    @property
    def ducking(self):
        return time.monotonic() < self.play_end + 0.7


class PwInputTransport(BaseInputTransport):
    def __init__(self, params, duck):
        super().__init__(params)
        self._duck = duck
        self._proc = None
        self._task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._proc = await asyncio.create_subprocess_exec(
            "pw-record", "--rate", str(self.sample_rate), "--channels", "1",
            "--format", "s16", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self._task = self.create_task(self._read_loop())
        await self.set_transport_ready(frame)
        print("mic live (pw-record)", flush=True)

    async def _read_loop(self):
        while True:
            data = await self._proc.stdout.read(640)  # 20 ms @16k
            if not data:
                break
            if self._duck.ducking:
                data = va._gain_pcm(data, 0.15)
            else:
                data = va._gain_pcm(data, 2.0)
            await self.push_audio_frame(InputAudioRawFrame(
                audio=data, sample_rate=self.sample_rate, num_channels=1))

    async def cleanup(self):
        await super().cleanup()
        if self._proc:
            self._proc.terminate()
            self._proc = None


class PwOutputTransport(BaseOutputTransport):
    def __init__(self, params, duck):
        super().__init__(params)
        self._duck = duck
        self._proc = None
        import concurrent.futures
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        args = ["pw-cat", "--playback", "--raw", "--rate",
                str(self.sample_rate), "--channels", "1", "--format", "s16"]
        target = va.VoiceSession._speaker_target()
        if target:
            args += ["--target", target]
        args.append("-")
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL)
        await self.set_transport_ready(frame)
        print(f"player live (pw-cat rate={self.sample_rate} "
              f"target={target})", flush=True)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if not self._proc or self._proc.poll() is not None:
            return False
        self._duck.note_output(len(frame.audio), self.sample_rate)
        await self.get_event_loop().run_in_executor(
            self._executor, self._proc.stdin.write, frame.audio)
        return True

    async def cleanup(self):
        await super().cleanup()
        if self._proc:
            self._proc.terminate()
            self._proc = None
        self._executor.shutdown(wait=False)


class PwTransport(BaseTransport):
    def __init__(self, params):
        super().__init__()
        self._params = params
        self._duck = DuckState()
        self._input = None
        self._output = None

    def input(self):
        if not self._input:
            self._input = PwInputTransport(self._params, self._duck)
        return self._input

    def output(self):
        if not self._output:
            self._output = PwOutputTransport(self._params, self._duck)
        return self._output


# ------------------------------------------------------------- state/metrics
class StateObserver(FrameProcessor):
    """Keeps the touchbar state file + journal receipts + metrics in
    the same shape the other voice backends produce."""

    def __init__(self, metrics):
        super().__init__()
        self.metrics = metrics
        self._assistant_text = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            print("heard:", frame.text[:200], flush=True)
            self.metrics.note_speech()
            self.metrics.user_spoke(frame.text)
            va.set_state(True, "thinking")
        elif isinstance(frame, LLMTextFrame):
            self._assistant_text.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._assistant_text).strip()
            self._assistant_text = []
            if text:
                print("assistant:", text[:200], flush=True)
                self.metrics.agent_replied(text)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.metrics.first_audio()
            va.set_state(True, "speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            va.set_state(True, "listening")
        await self.push_frame(frame, direction)


# ------------------------------------------------------------------- tools
def build_tools(profile):
    schemas = []
    for t in profile.get("tools", []):
        params = t.get("parameters") or {}
        schemas.append(FunctionSchema(
            name=t["name"], description=t["description"],
            properties=params.get("properties") or {},
            required=params.get("required") or []))
    return ToolsSchema(standard_tools=schemas)


def register_tools(llm):
    for name, func in va.EL_TOOL_FUNCS.items():
        async def handler(params, _func=func, _name=name):
            print(f"tool call: {_name} {str(params.arguments)[:120]}",
                  flush=True)
            va.set_state(True, f"tool: {_name}")
            try:
                result = await asyncio.to_thread(
                    _func, dict(params.arguments or {}))
            except Exception as exc:
                result = f"tool failed: {exc}"
            await params.result_callback({"result": str(result)[:4000]})
        llm.register_function(name, handler)


# -------------------------------------------------------------------- main
async def amain():
    cfg = va.load_config()
    profile = json.load(open(os.path.join(BASE, "agent-profile.json")))
    gkey = va._gemini_key()
    el_key = va.resolve_elevenlabs_key()
    if not gkey or not el_key:
        va.set_state(True, "sign-in needed")
        print("pipecat voice needs GEMINI_API_KEY and ELEVENLABS_API_KEY "
              "in voice-secrets.env", flush=True)
        while True:
            await asyncio.sleep(30)

    import datetime
    system = (profile["prompt"]
              .replace("{{node}}", os.uname().nodename)
              .replace("{{time}}", datetime.datetime.now().strftime(
                  "%A %H:%M"))
              .replace("{{fleet}}", va.tool_fleet_status()))
    transport = PwTransport(TransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        audio_in_sample_rate=RATE, audio_out_sample_rate=RATE))
    stt = MoonshineSTTService()
    llm = GoogleLLMService(
        api_key=gkey, model=cfg.get("brain_model", "gemini-3.8-flash"),
        system_instruction=system,
        params=GoogleLLMService.InputParams(temperature=0.7))
    register_tools(llm)
    tts = ElevenLabsTTSService(
        api_key=el_key,
        voice_id=cfg.get("tts_voice_id", "EXAVITQu4vr4xnSDxMaL"),
        settings=ElevenLabsTTSService.Settings(
            model=profile.get("tts_model", "eleven_flash_v2")),
        sample_rate=RATE)
    context = LLMContext(tools=build_tools(profile))
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.6, start_secs=0.25, stop_secs=0.55,
                min_volume=0.55)),
            user_turn_stop_timeout=1.2))
    metrics = va.ConvMetrics()
    observer = StateObserver(metrics)
    pipeline = Pipeline([
        transport.input(), stt, aggregators.user(), llm, tts,
        transport.output(), aggregators.assistant(), observer])
    worker = PipelineWorker(pipeline, cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigint=False)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def heartbeat():
        while not stop.is_set():
            await asyncio.sleep(15)
            metrics.flush()

    async def announcer():
        while not stop.is_set():
            await asyncio.sleep(5)
            for jid, st in va.pop_finished_jobs():
                context.add_message({
                    "role": "user",
                    "content": f"[Background job {jid} ({st['goal'][:80]}) "
                               f"{st['status']}. Result: "
                               f"{st.get('outcome', '')}] Report this "
                               "outcome now in one or two short sentences."})
                await worker.queue_frames([LLMRunFrame()])

    await runner.add_workers(worker)
    va.set_state(True, "listening")
    context.add_message({
        "role": "user",
        "content": "[The owner just toggled voice on. Open with your "
                   "greeting line, short and in character.]"})
    await worker.queue_frames([LLMRunFrame()])
    print("pipecat voice up: Silero + Moonshine + Gemini + ElevenLabs",
          flush=True)
    hb = asyncio.create_task(heartbeat())
    ann = asyncio.create_task(announcer())
    run_task = asyncio.create_task(runner.run())
    stop_task = asyncio.create_task(stop.wait())
    await asyncio.wait([run_task, stop_task],
                       return_when=asyncio.FIRST_COMPLETED)
    hb.cancel()
    ann.cancel()
    metrics.flush()
    va.set_state(False, "")
    print("pipecat voice stopped", flush=True)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    asyncio.run(amain())
