#!/usr/bin/env python3
"""
omarchy-voice v2: a LIVE voice agent, not transcription.

Pipeline: mic (PipeWire, 24 kHz PCM) <-> realtime voice model over
WebSocket <-> speakers. The model talks, listens, handles interruptions,
and calls tools; this process executes the tools on the machine:

  delegate(task, harness)  run pi/claude/codex/opencode/... non-interactively
  run_shell(command)       bash on this Mac
  focus_window(query)      bring a desktop window/session to the front
  open_url(url)            browser
  fleet_status()           nodes up, running agents, load, battery

Provider: Grok Voice (xAI realtime) by default - the xAI credential already
in pi's auth store connects as-is. OpenAI Realtime is config-selectable
(same wire protocol). Config: ~/.config/omarchy/touchbar/voice-config.json
  {"provider": "grok"|"openai", "voice": "Ara"}
Keys resolve from env (XAI_API_KEY/OPENAI_API_KEY), then
~/.config/omarchy/touchbar/voice-secrets.env, then pi's auth store.
State -> ~/.local/state/omarchy-touchbar/voice.json (touchbar switch).
"""
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import websockets

BASE_DIR = os.path.expanduser("~/.config/omarchy/touchbar")
STATE = os.path.expanduser("~/.local/state/omarchy-touchbar/voice.json")
CONFIG_PATH = os.path.join(BASE_DIR, "voice-config.json")
SECRETS_ENV = os.path.join(BASE_DIR, "voice-secrets.env")
RATE = 24000

PROVIDERS = {
    "grok": {"url": "wss://api.x.ai/v1/realtime", "env": "XAI_API_KEY",
             "auth_provider": "xai", "voice": "Ara"},
    "openai": {"url": "wss://api.openai.com/v1/realtime?model=gpt-realtime",
               "env": "OPENAI_API_KEY", "auth_provider": "openai-codex",
               "voice": "alloy"},
}

INSTRUCTIONS = (
    "You are the live voice of this MacBook running Omarchy Linux. "
    "Greet the user briefly out loud as soon as the session starts. "
    "You are helpful, concise, and spoken-word friendly: short sentences, "
    "no lists or markup. You can act on this machine and fleet with tools: "
    "delegate tasks to coding harnesses (pi, claude, codex, opencode), run "
    "shell commands, focus desktop windows, open URLs, and report fleet "
    "status. For anything multi-step or open-ended, delegate it to the "
    "grok coordinator (delegate with harness grok), which plans and runs "
    "the work agentically, then summarize its result. This machine is "
    "Linux, not macOS: check fleet_status for battery and load before "
    "reaching for shell commands. When a tool result "
    "comes back, summarize the outcome in one or two sentences. If asked "
    "to do something consequential or irreversible, confirm briefly first."
)

TOOLS = [
    {"type": "function", "name": "delegate",
     "description": "Delegate a task to a coding agent harness on this machine. It runs with full tool access and returns its result. Use harness grok (the Grok coordinator) for multi-step or open-ended work.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "What to do, in detail"},
         "harness": {"type": "string", "enum": ["pi", "grok", "claude", "codex", "opencode"],
                     "description": "Which harness (default pi; grok is Grok Build)"}},
         "required": ["task"]}},
    {"type": "function", "name": "run_shell",
     "description": "Run a bash command on this Mac and return its output.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
    {"type": "function", "name": "focus_window",
     "description": "Bring a desktop window or session to the front by app name or title fragment (e.g. 'chrome', 'pi', 'terminal').",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "open_url",
     "description": "Open a URL in the default browser.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"}}, "required": ["url"]}},
    {"type": "function", "name": "fleet_status",
     "description": "Report fleet status: nodes online, running agent sessions, system load, battery.",
     "parameters": {"type": "object", "properties": {}}},
]


def set_state(live, status=""):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump({"live": live, "status": status, "ts": time.time()}, f)
    except OSError:
        pass


def load_config():
    cfg = {"provider": "grok", "voice": None}
    try:
        cfg.update(json.load(open(CONFIG_PATH)))
    except Exception:
        pass
    return cfg


PI_AUTH_PATH = os.path.expanduser("~/.pi/agent/auth.json")
# pi's public xAI OAuth client (device-code flow); used only to refresh
# an expired access token with the refresh token pi already stored.
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"


def _refresh_pi_xai(entry):
    refresh = entry.get("refresh")
    if not refresh:
        return None
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": XAI_CLIENT_ID,
        "refresh_token": refresh,
    }).encode()
    req = urllib.request.Request(
        XAI_TOKEN_URL, data=body,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc)
            detail = err.get("error_description") or err.get("error") or ""
        except Exception:
            detail = ""
        print(f"xAI token refresh failed (HTTP {exc.code}) {detail}".strip(),
              flush=True)
        return None
    except Exception as exc:
        print(f"xAI token refresh failed ({exc})", flush=True)
        return None
    access = data.get("access_token")
    if not access:
        print("xAI token refresh returned no access token", flush=True)
        return None
    refreshed = dict(entry)
    refreshed["access"] = access
    if data.get("refresh_token"):
        refreshed["refresh"] = data["refresh_token"]
    refreshed["expires"] = int(time.time() * 1000
                               + float(data.get("expires_in", 3600)) * 1000
                               - 5 * 60 * 1000)
    return refreshed


def resolve_key(provider):
    meta = PROVIDERS[provider]
    if os.environ.get(meta["env"]):
        return os.environ[meta["env"]]
    try:
        for line in open(SECRETS_ENV):
            if line.startswith(meta["env"] + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    try:
        auth = json.load(open(PI_AUTH_PATH))
        entry = auth.get(meta["auth_provider"]) or {}
        access = entry.get("access") or entry.get("key")
        if not access:
            return None
        expires = float(entry.get("expires") or 0)
        if not expires or expires > time.time() * 1000 + 60_000:
            return access
        # expired (or about to): the old code returned the dead token
        # forever and the provider answered 403 on every reconnect
        if provider == "grok":
            refreshed = _refresh_pi_xai(entry)
            if refreshed:
                auth[meta["auth_provider"]] = refreshed
                tmp = f"{PI_AUTH_PATH}.tmp-{os.getpid()}"
                with open(tmp, "w") as f:
                    json.dump(auth, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, PI_AUTH_PATH)
                print("xAI credential refreshed", flush=True)
                return refreshed["access"]
            # another process may have refreshed first; accept that
            try:
                entry = json.load(open(PI_AUTH_PATH)).get(
                    meta["auth_provider"]) or {}
                if float(entry.get("expires") or 0) > time.time() * 1000 + 60_000:
                    return entry.get("access")
            except Exception:
                pass
        print(f"credential for {provider} expired and could not refresh",
              flush=True)
        return None
    except Exception:
        return None


# ---------------------------------------------------------------- tools

def _truncate(s, n=1600):
    return s[-n:] if len(s) > n else s


def tool_delegate(task, harness="pi"):
    cmds = {
        "pi": ["pi", "--provider", "meta", "--model", "muse-wonder-1.0", "-p", task],
        "grok": ["grok", "--always-approve", "-p", task],
        "claude": ["claude", "-p", "--dangerously-skip-permissions", task],
        "codex": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", task],
        "opencode": ["opencode", "run", task],
    }
    cmd = cmds.get(harness, cmds["pi"])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                         cwd=os.path.expanduser("~"))
    return _truncate((out.stdout or out.stderr or "done").strip())


def tool_run_shell(command):
    out = subprocess.run(["bash", "-lc", command], capture_output=True,
                         text=True, timeout=180, cwd=os.path.expanduser("~"))
    return _truncate((out.stdout + out.stderr).strip() or "(no output)")


def _hypr(req):
    import glob as g
    import socket as s
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if sig:
        path = f"{runtime}/hypr/{sig}/.socket.sock"
    else:
        cands = sorted(g.glob(os.path.join(runtime, "hypr", "*", ".socket.sock")),
                       key=os.path.getmtime, reverse=True)
        path = cands[0] if cands else ""
    if not path:
        return ""
    sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    sock.settimeout(1.5)
    sock.connect(path)
    sock.sendall(req.encode())
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    return data.decode(errors="replace")


def tool_focus_window(query):
    try:
        clients = json.loads(_hypr("j/clients"))
    except Exception:
        return "could not read the window list"
    q = query.lower()
    for c in clients:
        if q in (c.get("class", "") + " " + c.get("title", "")).lower():
            path = _hypr_path()
            sig = os.path.basename(os.path.dirname(path)) if path else ""
            env = dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE=sig)
            subprocess.run(["hyprctl", "dispatch",
                            f'hl.dsp.focus({{ window = "address:{c["address"]}" }})'],
                           env=env, capture_output=True, timeout=5)
            return f"focused {c.get('class')} on workspace {c.get('workspace', {}).get('id')}"
    return f"no window matching '{query}'"


def _hypr_path():
    import glob as g
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if sig:
        return f"{runtime}/hypr/{sig}/.socket.sock"
    cands = sorted(g.glob(os.path.join(runtime, "hypr", "*", ".socket.sock")),
                   key=os.path.getmtime, reverse=True)
    return cands[0] if cands else ""


def tool_open_url(url):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return f"opened {url}"


def tool_fleet_status():
    bits = []
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=4).stdout
        ts = json.loads(out)
        up = [ts.get("Self", {}).get("HostName", "this Mac")]
        up += [p.get("HostName") for p in (ts.get("Peer") or {}).values()
               if p.get("Online") and p.get("OS", "").lower() not in ("ios", "android")]
        bits.append(f"{len(up)} nodes up: {', '.join(x for x in up if x)}")
    except Exception:
        pass
    try:
        load = open("/proc/loadavg").read().split()[0]
        batt = open("/sys/class/power_supply/macsmc-battery/capacity").read().strip()
        bits.append(f"load {load}, battery {batt}%")
    except OSError:
        pass
    return ". ".join(bits) or "status unavailable"


TOOL_FUNCS = {"delegate": lambda a: tool_delegate(a.get("task", ""), a.get("harness", "pi")),
              "run_shell": lambda a: tool_run_shell(a.get("command", "")),
              "focus_window": lambda a: tool_focus_window(a.get("query", "")),
              "open_url": lambda a: tool_open_url(a.get("url", "")),
              "fleet_status": lambda a: tool_fleet_status()}


# ---------------------------------------------------------------- realtime

class VoiceSession:
    def __init__(self, cfg):
        self.cfg = cfg
        self.provider = cfg.get("provider", "grok")
        self.meta = PROVIDERS[self.provider]
        self.ws = None
        self.player = None
        self.mic_proc = None
        self.status = "listening"
        self.closed = False
        # Half-duplex discipline (no AEC on this mic path): the mic sends
        # digital silence until the greeting completes, and again while
        # the assistant speaks. Streaming live mic from connect is what
        # killed every session: a 13 ms pw-record startup blip tripped
        # server VAD, barged-in and cancelled the greeting response, and
        # speaker echo re-triggered it on every later answer.
        self.mic_live = False
        self.speaking = False
        self._greeted = False
        self._resp_audio = 0
        self._greet_retried = False

    @staticmethod
    def _speaker_target():
        """The MacBook speakers live behind the Asahi convolver sink; the
        default sink is the (empty) headphone jack, which is why Grok
        was inaudible. Find the speaker sink by name."""
        try:
            objs = json.loads(subprocess.run(
                ["pw-dump"], capture_output=True, text=True, timeout=4).stdout)
            for o in objs:
                props = (o.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Sink":
                    name = props.get("node.name", "")
                    desc = props.get("node.description", "")
                    if "convolver" in name or "speaker" in desc.lower():
                        return name
        except Exception:
            pass
        return None

    def _st(self, status):
        self.status = status
        set_state(True, status)

    def _start_player(self):
        # pw-play cannot take raw PCM on stdin (sndfile rejects "-");
        # pw-cat --playback --raw is the correct PipeWire raw sink,
        # explicitly targeted at the speakers (not the headphone jack)
        args = ["pw-cat", "--playback", "--raw", "--rate", str(RATE),
                "--channels", "1", "--format", "s16"]
        target = getattr(self, "_spk", None) or self._speaker_target()
        self._spk = target
        if target:
            args += ["--target", target]
        args.append("-")
        # stderr stays attached to the journal: a player that dies
        # instantly must be visible, never a silent black hole again
        self.player = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        print(f"player started (target={target})", flush=True)

    def _stop_player(self):
        try:
            if self.player:
                self.player.terminate()
        except Exception:
            pass
        self.player = None

    async def mic_loop(self):
        # supervised: if the capture stream dies (PipeWire restart, source
        # suspend), respawn it - never let the session go deaf silently
        while not self.closed:
            proc = await asyncio.create_subprocess_exec(
                "pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.mic_proc = proc
            buf = b""
            try:
                while not self.closed:
                    raw = await proc.stdout.read(4096)
                    if not raw:
                        print("mic stream ended; respawning capture", flush=True)
                        break
                    # gated: silence until the greeting is done, and
                    # while the assistant is speaking (echo guard)
                    if not self.mic_live or self.speaking:
                        raw = b"\x00" * len(raw)
                    buf += raw
                    chunk = RATE // 5 * 2  # send exact 200 ms frames
                    while len(buf) >= chunk:
                        await self.ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(buf[:chunk]).decode()}))
                        buf = buf[chunk:]
            except (ConnectionError, Exception) as exc:
                print(f"mic loop interrupted: {exc}", flush=True)
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if not self.closed:
                await asyncio.sleep(0.8)

    def _session_payload(self):
        voice = (self.cfg.get("voice") or self.meta["voice"]).lower()
        if self.provider == "grok":
            # documented xAI schema (docs.x.ai speech-to-speech): the old
            # OpenAI-style modalities/*_audio_format fields are not it
            return {"instructions": INSTRUCTIONS, "voice": voice,
                    "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE}},
                              "output": {"format": {"type": "audio/pcm", "rate": RATE}}},
                    "turn_detection": {"type": "server_vad"}, "tools": TOOLS}
        return {"instructions": INSTRUCTIONS, "voice": voice,
                "modalities": ["text", "audio"],
                "input_audio_format": "pcm16", "output_audio_format": "pcm16",
                "turn_detection": {"type": "server_vad"}, "tools": TOOLS}

    async def event_loop(self):
        async for raw in self.ws:
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t == "session.created":
                await self.ws.send(json.dumps({
                    "type": "session.update",
                    "session": self._session_payload()}))
            elif t == "session.updated":
                # greet only once the config is acknowledged, not raced
                if not self._greeted:
                    self._greeted = True
                    await self.ws.send(json.dumps({"type": "response.create"}))
                self._st("listening")
            elif t in ("response.audio.delta", "response.output_audio.delta"):
                self.speaking = True
                self._resp_audio += len(ev.get("delta", ""))
                if self.player is None or self.player.poll() is not None:
                    if self.player is not None:
                        print("player had exited; restarting", flush=True)
                    self._start_player()
                try:
                    self.player.stdin.write(base64.b64decode(ev["delta"]))
                    self.player.stdin.flush()
                except Exception as exc:
                    print(f"player write failed: {exc}", flush=True)
                    self._start_player()
                self._st("speaking")
            elif t == "response.output_audio_transcript.done":
                print("assistant:", (ev.get("transcript") or "")[:200], flush=True)
            elif t == "conversation.item.input_audio_transcription.completed":
                print("heard:", (ev.get("transcript") or "")[:200], flush=True)
            elif t == "input_audio_buffer.speech_started":
                # real barge-in only counts once the mic is live; while
                # speaking the mic sends silence, so this is the user
                if self.mic_live and not self.speaking:
                    self._stop_player()
                    self._st("listening")
            elif t == "response.function_call_arguments.done":
                await self._run_tool(ev)
            elif t == "response.done":
                was_speaking = self.speaking
                self.speaking = False
                status = (ev.get("response") or {}).get("status", "")
                first = not self.mic_live
                if first:
                    self.mic_live = True
                    print("mic live (greeting done)", flush=True)
                if first and status == "cancelled" and not self._greet_retried:
                    # greeting eaten despite gating: ask once more
                    self._greet_retried = True
                    print("greeting cancelled; re-requesting", flush=True)
                    await self.ws.send(json.dumps({"type": "response.create"}))
                self._resp_audio = 0
                set_state(True, "listening")
            elif t == "error":
                print("provider error:", str(ev)[:300], flush=True)
                self._st("provider error")

    async def _run_tool(self, ev):
        name = ev.get("name", "")
        call_id = ev.get("call_id", "")
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except ValueError:
            args = {}
        self._st(f"tool: {name}")
        print(f"tool call: {name} {str(args)[:120]}", flush=True)
        func = TOOL_FUNCS.get(name)
        try:
            result = await asyncio.to_thread(func, args) if func else "unknown tool"
        except Exception as exc:
            result = f"tool failed: {exc}"
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id,
                     "output": str(result)[:4000]}}))
        await self.ws.send(json.dumps({"type": "response.create"}))

    async def _heartbeat(self):
        while not self.closed:
            set_state(True, getattr(self, "status", "listening"))
            await asyncio.sleep(15)

    async def run(self):
        # supervised reconnect: a dropped provider socket or a dead mic
        # must never flap the service (or the user's mic indicator).
        # The credential is re-resolved (and refreshed when expired) on
        # every attempt; an expired token must not retry in a hot loop.
        delay = 3
        while not self.closed:
            key = resolve_key(self.provider)
            if not key:
                self._st("sign-in needed")
                print(f"no usable credential for {self.provider}; "
                      "waiting before retry", flush=True)
                await asyncio.sleep(30)
                continue
            try:
                self._st("connecting")
                # per-connection state: every (re)connect re-greets with
                # the mic gated, exactly like a fresh start
                self.mic_live = False
                self.speaking = False
                self._greeted = False
                self._resp_audio = 0
                url = self.meta["url"]
                if self.provider == "grok" and "model=" not in url:
                    url += "?model=grok-voice-latest"
                async with websockets.connect(
                        url,
                        additional_headers={"Authorization": f"Bearer {key}"},
                        open_timeout=15) as ws:
                    self.ws = ws
                    delay = 3
                    print(f"connected to {self.provider} voice", flush=True)
                    await asyncio.gather(self.mic_loop(), self.event_loop(),
                                         self._heartbeat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"voice session dropped ({exc}); reconnecting", flush=True)
                self._stop_player()
                if not self.closed:
                    self._st("reconnecting")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)


async def _amain():
    cfg = load_config()
    session = VoiceSession(cfg)
    print("speaker target:", session._speaker_target(), flush=True)
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(session.run())
    # SIGTERM (systemctl stop) must cancel cleanly so state settles truthfully
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        session.closed = True
        session._stop_player()


def main():
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    finally:
        set_state(False, "")


if __name__ == "__main__":
    main()
