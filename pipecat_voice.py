#!/usr/bin/env python3
"""Omarchy voice agent on Pipecat: the open-source framework owns the
hard part (VAD, end-of-turn, interruptions, streaming TTS), we own the
machine (PipeWire transport, fleet tools, persona, touchbar state).

Pipeline: pw-record -> Silero VAD -> Moonshine (local STT) -> Gemini
(brain, function calls into voice_tools.py) ->
ElevenLabs TTS (Sarah) -> pw-cat to the speaker convolver.

No inbound exposure: every connection is outbound. Echo control: the
mic is ducked (not muted) while the bot speaks, so a loud real voice
can still barge in but speaker echo cannot self-trigger.

Run with the project venv: .venv/bin/python pipecat_voice.py
All voice configuration lives in voice.json next to this file
(brain chain, ElevenLabs voice, persona prompt, tool schemas); keys
live in voice-secrets.env. Tools/state/metrics: voice_tools.py.
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
import voice_tools as vt  # tools, metrics, state, secrets

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    LLMContextAssistantTurnFrame,
    LLMRunFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FrameProcessed
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.services.openai.llm import OpenAILLMService
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
        args = ["pw-record", "--rate", str(self.sample_rate),
                "--channels", "1", "--format", "s16"]
        mic = os.environ.get("OMARCHY_VOICE_MIC")  # test hook: virtual mic
        if mic:
            args += ["--target", mic]
        args.append("-")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
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
                # near-mute, not a polite duck: at 0.15 the agent's
                # own voice still passed VAD and interrupted itself
                # mid-sentence (live 2026-10-22). Turns are short;
                # barge-in resumes 0.7s after she stops.
                data = vt._gain_pcm(data, 0.04)
            else:
                data = vt._gain_pcm(data, 2.0)
            await self.push_audio_frame(InputAudioRawFrame(
                audio=data, sample_rate=self.sample_rate, num_channels=1))

    async def cleanup(self):
        await super().cleanup()
        if self._task:
            self._task.cancel()
            self._task = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


class FillerGate(FrameProcessor):
    """If the brain hasn't started within ~1.6 s of the owner
    finishing a real utterance, say one short line so silence never
    reads as broken. Arms only after actual user speech (a stray
    stop frame after the greeting must not trigger it)."""

    def __init__(self):
        super().__init__()
        self._timer = None
        self._user_spoke = False

    def _disarm(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._user_spoke = True
        elif isinstance(frame, UserStoppedSpeakingFrame) \
                and self._user_spoke:
            self._user_spoke = False
            self._disarm()

            async def fire():
                await asyncio.sleep(1.6)
                print("filler: One sec.", flush=True)
                await self.push_frame(TTSSpeakFrame(text="One sec."),
                                      FrameDirection.DOWNSTREAM)

            self._timer = self.create_task(fire())
        elif isinstance(frame, (LLMTextFrame, BotStartedSpeakingFrame)):
            self._user_spoke = False
            self._disarm()
        await self.push_frame(frame, direction)


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
        target = os.environ.get("OMARCHY_VOICE_SPK") or vt.speaker_target()
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
class HeardObserver(FrameProcessor):
    """Sits between STT and the user aggregator: transcriptions are
    consumed by the aggregator, so receipts are logged here."""

    def __init__(self, metrics, health):
        super().__init__()
        self.metrics = metrics
        self.health = health

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            said = self.health.get("said_text", "")
            if time.time() - self.health.get("said_at", 0) < 20 \
                    and vt.is_echo(frame.text, said):
                print("echo dropped:", frame.text[:120], flush=True)
                return
            print("heard:", frame.text[:200], flush=True)
            vt.note_user_turn()
            self.metrics.note_speech()
            self.metrics.user_spoke(frame.text)
            self.health["awaiting"] = True
            vt.set_state(True, "thinking")
        await self.push_frame(frame, direction)


class RetryObserver(BaseObserver):
    """When a brain error lands while a reply is owed, schedule a
    patient revive-and-reask instead of going mute: Gemini capacity
    (503) clears in tens of seconds, and even daily quotas roll."""

    def __init__(self, health, kick):
        super().__init__()
        self.health = health
        self.kick = kick

    async def on_process_frame(self, data: FrameProcessed):
        if isinstance(data.frame, ErrorFrame) and self.health["awaiting"]:
            text = str(data.frame.error)
            if any(k in text for k in ("429", "RESOURCE_EXHAUSTED",
                                       "503", "UNAVAILABLE", "TIMEOUT",
                                       "DEADLINE")):
                self.kick()


class StateObserver(FrameProcessor):
    """Keeps the touchbar state file + journal receipts + metrics in
    the same shape the other voice backends produce. Assistant text
    arrives aggregated as LLMContextAssistantTurnFrame."""

    def __init__(self, metrics, health):
        super().__init__()
        self.metrics = metrics
        self.health = health
        self.on_quota = None  # set by amain: re-ask the dead turn once
        self._last_reask = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextAssistantTurnFrame) \
                and frame.text.strip():
            print("assistant:", frame.text[:200], flush=True)
            self.health["said_text"] = frame.text
            self.health["said_at"] = time.time()
            vt.note_said(frame.text)
            self.metrics.agent_replied(frame.text)
            self.health["awaiting"] = False
            self.health["retries"] = 0
            self.health["reask_pending"] = False
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.metrics.first_audio()
            vt.set_state(True, "speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # echo of this speech reaches the mic only now (speaker
            # + room latency), so the echo-guard clock starts here,
            # not when the text was generated
            self.health["said_at"] = time.time()
            vt.set_state(True, "listening")
        elif isinstance(frame, ErrorFrame):
            text = str(frame.error)
            if "429" in text or "QUOTA" in text.upper():
                vt.set_state(True, "brain quota out")
                now = time.monotonic()
                if self.on_quota and now - self._last_reask > 15:
                    self._last_reask = now
                    self.on_quota()
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
    for name, func in vt.TOOL_FUNCS.items():
        async def handler(params, _func=func, _name=name):
            print(f"tool call: {_name} {str(params.arguments)[:120]}",
                  flush=True)
            vt.set_state(True, f"tool: {_name}")
            try:
                result = await asyncio.to_thread(
                    _func, dict(params.arguments or {}))
            except Exception as exc:
                result = f"tool failed: {exc}"
            await params.result_callback({"result": str(result)[:4000]})
        llm.register_function(name, handler)


# -------------------------------------------------------------------- main
class _BenchOnError:
    """Marks the model unusable on quota/capacity/timeout failures so
    the failover switcher moves the conversation on; a bare 429 would
    otherwise read as a hiccup the service 'can carry on from' and
    the turn dies silently. Provider-agnostic."""

    async def push_error(self, error_msg, exception=None, fatal=False,
                         category=None, force_treat_as_permanent=False):
        text = f"{error_msg} {exception}"
        if any(k in text for k in ("429", "RESOURCE_EXHAUSTED", "503",
                                   "UNAVAILABLE", "TIMEOUT", "DEADLINE",
                                   "RATE_LIMIT", "OVERLOADED")):
            force_treat_as_permanent = True
        await super().push_error(
            error_msg, exception=exception, fatal=fatal, category=category,
            force_treat_as_permanent=force_treat_as_permanent)


class BrainGoogleLLM(_BenchOnError, GoogleLLMService):
    pass


class BrainAnthropicLLM(_BenchOnError, AnthropicLLMService):
    pass


class BrainOpenAILLM(_BenchOnError, OpenAILLMService):
    pass


def _secret(name):
    try:
        for line in open(vt.SECRETS_ENV):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"') or None
    except OSError:
        pass
    return os.environ.get(name)


def build_brain(cfg, system):
    """voice.json's brain_models is a failover chain. Entries are
    'provider:model' (bare names = google). Any provider whose API
    key is absent from voice-secrets.env is skipped, so dropping a
    key in is all it takes to add a brain; a rate-limited or down
    model benches itself and the conversation re-asks on the next."""
    llms, names = [], []
    for spec in cfg.get("brain_models") or []:
        provider, _, model = str(spec).partition(":")
        if not model:
            provider, model = "google", provider
        provider, model = provider.strip().lower(), model.strip()
        svc = None
        if provider == "google":
            key = vt._gemini_key()
            if key:
                svc = BrainGoogleLLM(
                    api_key=key, model=model,
                    params=GoogleLLMService.InputParams(temperature=0.7))
        elif provider == "anthropic":
            key = _secret("ANTHROPIC_API_KEY")
            if key:
                svc = BrainAnthropicLLM(
                    api_key=key, model=model,
                    params=AnthropicLLMService.InputParams(temperature=0.7))
        elif provider in ("openai", "openrouter", "minimax", "kimi"):
            key = _secret({"openai": "OPENAI_API_KEY",
                           "openrouter": "OPENROUTER_API_KEY",
                           "minimax": "MINIMAX_API_KEY",
                           "kimi": "KIMI_API_KEY"}[provider])
            if provider == "kimi" and not key:
                key = _secret("MOONSHOT_API_KEY")
            if key:
                kw = {"api_key": key, "model": model,
                      "params": OpenAILLMService.InputParams(
                          temperature=0.7)}
                if provider == "minimax":
                    # M3.1 is a reasoning model: at default effort it
                    # burns ~1-2s and hundreds of tokens thinking before
                    # the first spoken word (and sometimes returns the
                    # reasoning with empty content). Low effort: first
                    # content ~0.6s, no reasoning tokens. Probed live.
                    kw["params"] = OpenAILLMService.InputParams(
                        temperature=0.7,
                        extra={"extra_body": {"reasoning_effort": "low"}})
                if provider == "openrouter":
                    kw["base_url"] = "https://openrouter.ai/api/v1"
                elif provider == "minimax":
                    kw["base_url"] = "https://api.minimax.io/v1"
                elif provider == "kimi":
                    kw["base_url"] = "https://api.moonshot.ai/v1"
                svc = BrainOpenAILLM(**kw)
        if svc is None:
            print(f"brain skip {spec}: no key / unknown provider",
                  flush=True)
            continue
        register_tools(svc)
        llms.append(svc)
        names.append(spec)
    print(f"brain chain: {' -> '.join(names) or '(none)'}", flush=True)
    if not llms:
        return None, llms
    if len(llms) == 1:
        return llms[0], llms
    return LLMSwitcher(llms=llms,
                       strategy_type=ServiceSwitcherStrategyFailover), llms


async def amain():
    cfg = json.load(open(os.path.join(BASE, "voice.json")))
    el_key = vt.resolve_elevenlabs_key()
    if not el_key:
        vt.set_state(True, "sign-in needed")
        print("pipecat voice needs ELEVENLABS_API_KEY "
              "in voice-secrets.env", flush=True)
        while True:
            await asyncio.sleep(30)
    import datetime
    system = (cfg["prompt"]
              .replace("{{node}}", os.uname().nodename)
              .replace("{{time}}", datetime.datetime.now().strftime(
                  "%A %H:%M"))
              .replace("{{fleet}}", vt.tool_fleet_status())
              .replace("{{brain}}", "the configured voice brain "
                       f"({(cfg.get('brain_models') or ['local'])[0]})"))
    transport = PwTransport(TransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        audio_in_sample_rate=RATE, audio_out_sample_rate=RATE))
    stt = MoonshineSTTService()
    llm, brain_members = build_brain(cfg, system)
    if llm is None:
        vt.set_state(True, "no brain key")
        print("no brain provider key found in voice-secrets.env "
              "(GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / "
              "OPENROUTER_API_KEY)", flush=True)
        while True:
            await asyncio.sleep(30)
    tts = ElevenLabsTTSService(
        api_key=el_key,
        voice_id=cfg["tts"]["voice_id"],
        settings=ElevenLabsTTSService.Settings(model=cfg["tts"]["model"]),
        sample_rate=RATE)
    context = LLMContext(tools=build_tools(cfg))
    # persona rides in context so every provider gets it identically
    context.add_message({"role": "system", "content": system})
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.6, start_secs=0.25, stop_secs=0.55,
                min_volume=0.55)),
            user_turn_stop_timeout=1.2))
    metrics = vt.ConvMetrics()
    health = {"awaiting": False, "retries": 0,
              "reask_pending": False}  # greeting is local TTS now
    observer = StateObserver(metrics, health)
    pipeline = Pipeline([
        transport.input(), stt, HeardObserver(metrics, health),
        aggregators.user(),
        llm, FillerGate(), tts, transport.output(),
        aggregators.assistant(), observer])
    worker = PipelineWorker(pipeline, cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigint=False)

    _retry_task = None

    def kick():
        nonlocal _retry_task
        if _retry_task and not _retry_task.done():
            return

        async def retry():
            delay = min(20 * 2 ** health["retries"], 120)
            health["retries"] += 1
            print(f"brain retry {health['retries']} in {delay}s",
                  flush=True)
            await asyncio.sleep(delay)
            health["reask_pending"] = False
            if not health["awaiting"]:
                return
            if health["retries"] == 2:
                # say it once, honestly, instead of filling forever
                await worker.queue_frames([TTSSpeakFrame(
                    text="Quick heads-up: my brain is unreachable "
                         "right now. That is Google's quota, not your "
                         "mic. I will keep trying.")])
            for svc in brain_members:
                await svc.set_usable(True)
            await worker.queue_frames([LLMRunFrame()])

        _retry_task = asyncio.create_task(retry())

    worker.add_observer(RetryObserver(health, kick))

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
            for jid, st in vt.pop_finished_jobs():
                context.add_message({
                    "role": "user",
                    "content": f"[Background job {jid} ({st['goal'][:80]}) "
                               f"{st['status']}. Result: "
                               f"{st.get('outcome', '')}] Report this "
                               "outcome now in one or two short sentences."})
                await worker.queue_frames([LLMRunFrame()])

    await runner.add_workers(worker)
    vt.set_state(True, "listening")
    # the greeting is spoken locally (ElevenLabs TTS only) so it can
    # never be held hostage by brain quota; the model just finds it
    # already said when the conversation starts
    context.add_message({"role": "assistant",
                         "content": cfg["first_message"]})

    async def greet():
        await asyncio.sleep(3)  # let the pipeline reach steady state
        print(f"greeting (local tts): {cfg['first_message']}", flush=True)
        await worker.queue_frames(
            [TTSSpeakFrame(text=cfg["first_message"])])

    asyncio.create_task(greet())
    print("pipecat voice up: Silero + Moonshine + Gemini + ElevenLabs",
          flush=True)
    async def reask():
        # single-flight: the switch-event re-ask and the delayed
        # revive-retry must never both re-run a turn (ghost replies)
        if health["reask_pending"]:
            return
        health["reask_pending"] = True
        await asyncio.sleep(1.2)  # let the failover switch land
        if health["awaiting"]:
            await worker.queue_frames([LLMRunFrame()])

    observer.on_quota = lambda: asyncio.create_task(reask())
    if isinstance(llm, LLMSwitcher):
        @llm.strategy.event_handler("on_service_switched")
        async def _on_switched(strategy, service):
            print(f"brain switched -> {service}", flush=True)
            asyncio.create_task(reask())

    async def revive_brains():
        while not stop.is_set():
            await asyncio.sleep(900)
            for svc in brain_members:
                await svc.set_usable(True)  # quotas heal; retry primary

    hb = asyncio.create_task(heartbeat())
    ann = asyncio.create_task(announcer())
    rev = asyncio.create_task(revive_brains())
    run_task = asyncio.create_task(runner.run())
    stop_task = asyncio.create_task(stop.wait())
    await asyncio.wait([run_task, stop_task],
                       return_when=asyncio.FIRST_COMPLETED)
    hb.cancel()
    ann.cancel()
    rev.cancel()
    if stop.is_set():
        await worker.stop()  # graceful pipeline shutdown on SIGTERM
        try:
            await asyncio.wait_for(run_task, 4)
        except asyncio.TimeoutError:
            run_task.cancel()
    metrics.flush()
    vt.set_state(False, "")
    print("pipecat voice stopped", flush=True)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    asyncio.run(amain())
