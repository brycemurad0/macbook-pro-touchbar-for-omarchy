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
import itertools
import json
import os
import signal
import struct
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


HARNESS_CMDS = {
    "pi": ["pi", "--provider", "meta", "--model", "muse-wonder-1.0", "-p"],
    "grok": ["grok", "--always-approve", "-p"],
    "claude": ["claude", "-p", "--dangerously-skip-permissions"],
    "codex": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
    "opencode": ["opencode", "run"],
}


def tool_delegate(task, harness="pi"):
    cmd = HARNESS_CMDS.get(harness, HARNESS_CMDS["pi"]) + [task]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                         cwd=os.path.expanduser("~"))
    return _truncate((out.stdout or out.stderr or "done").strip())


# ------------------------------------------------------- background jobs
# Voice confirms a goal out loud first; only then does start_work launch
# a harness, permissionless, in the background. The voice layer never
# blocks on it: completion is announced back into the conversation.
JOBS_DIR = os.path.expanduser("~/.local/state/omarchy-touchbar/jobs")
_jobs = {}
_job_seq = itertools.count(1)
_jobs_announced = set()


def start_work_job(goal, harness="grok"):
    os.makedirs(JOBS_DIR, exist_ok=True)
    jid = f"job-{next(_job_seq)}"
    prompt = (f"Goal: {goal}\n\nWork autonomously and permissionless until "
              "this goal is complete. When done, end with a concise final "
              "report (at most 5 sentences): what was done and how it "
              "was verified.")
    log = open(os.path.join(JOBS_DIR, f"{jid}.log"), "wb")
    proc = subprocess.Popen(
        HARNESS_CMDS.get(harness, HARNESS_CMDS["grok"]) + [prompt],
        stdout=log, stderr=subprocess.STDOUT, cwd=os.path.expanduser("~"))
    _jobs[jid] = {"proc": proc, "goal": goal, "harness": harness,
                  "log": log.name, "started": time.time()}
    print(f"work started: {jid} on {harness}: {goal[:100]}", flush=True)
    return jid


def _job_outcome(job):
    try:
        with open(job["log"], errors="replace") as f:
            text = f.read().strip()
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return _truncate("\n".join(lines[-6:]), 700)


def job_states():
    out = {}
    for jid, job in _jobs.items():
        rc = job["proc"].poll()
        out[jid] = {"status": "running" if rc is None
                    else ("done" if rc == 0 else "failed"),
                    "goal": job["goal"], "harness": job["harness"]}
        if rc is not None:
            out[jid]["outcome"] = _job_outcome(job)
    return out


def pop_finished_jobs():
    fresh = []
    for jid, st in job_states().items():
        if st["status"] != "running" and jid not in _jobs_announced:
            _jobs_announced.add(jid)
            fresh.append((jid, st))
    return fresh


def tool_work_status():
    states = job_states()
    if not states:
        return "no background jobs this session"
    return "; ".join(
        f"{jid} ({s['harness']}, {s['status']}): {s['goal'][:80]}"
        for jid, s in states.items())


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


def tool_run_shell_quick(command):
    out = subprocess.run(["bash", "-lc", command], capture_output=True,
                         text=True, timeout=25, cwd=os.path.expanduser("~"))
    return _truncate((out.stdout + out.stderr).strip() or "(no output)", 1200)


def _start_work_tool(args):
    goal = (args.get("goal") or "").strip()
    if not goal:
        return "no goal given"
    jid = start_work_job(goal, args.get("harness", "grok"))
    return (f"{jid} started on {args.get('harness', 'grok')}. "
            "Tell the user it is running in one short line, then stop "
            "talking about it until it finishes.")


EL_TOOL_FUNCS = {
    "fleet_status": lambda a: tool_fleet_status(),
    "run_shell": lambda a: tool_run_shell_quick(a.get("command", "")),
    "focus_window": lambda a: tool_focus_window(a.get("query", "")),
    "open_url": lambda a: tool_open_url(a.get("url", "")),
    "start_work": _start_work_tool,
    "work_status": lambda a: tool_work_status(),
}


# --------------------------------------------- fleet senses (voice tools)
# The voice agent must understand the fleet, not just itself: what agent
# sessions are actually running where, what state each node is in, what
# the Second Brain says about projects, and what happened today.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=" + os.path.expanduser("~/.ssh/cm-%C"),
            "-o", "ControlPersist=180"]
HARNESS_WORDS = ("hermes", "grok", "claude", "codex", "opencode", "pi",
                 "cursor", "kimi", "antigravity", "mcode")


def _fleet_cfg():
    try:
        with open(os.path.join(BASE_DIR, "fleet.json")) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _fleet_nodes():
    """[{label, display, ssh|None(local), online}] from tailscale."""
    cfg = _fleet_cfg()
    aliases = cfg.get("node_aliases") or {}
    ssh_aliases = cfg.get("ssh_aliases") or {}
    nodes = []
    try:
        out = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=5).stdout
        ts = json.loads(out)
    except Exception:
        return nodes
    entries = [(ts.get("Self") or {}, True)]
    entries += [(p, False) for p in (ts.get("Peer") or {}).values()]
    for peer, is_self in entries:
        if (peer.get("OS", "").lower() in ("ios", "android")):
            continue
        dns = (peer.get("DNSName") or "").rstrip(".")
        label = dns.split(".")[0].lower() if dns else \
            (peer.get("HostName") or "").lower()
        if not label:
            continue
        nodes.append({
            "label": label,
            "display": aliases.get(label, peer.get("HostName") or label),
            "ssh": None if is_self else (ssh_aliases.get(label) or label),
            "online": bool(peer.get("Online")) or is_self,
            "self": is_self})
    return nodes


def _resolve_node(query):
    q = (query or "").strip().lower()
    if not q or q in ("here", "local", "this", "fred", os.uname().nodename):
        return None  # local
    for n in _fleet_nodes():
        if q in (n["label"], n["display"].lower(),
                 (n["ssh"] or "").lower()):
            return n
    for n in _fleet_nodes():
        if q in n["label"] or q in n["display"].lower():
            return n
    return "unknown"


def _run_on(node, command, timeout=10):
    """node None = local shell; else multiplexed ssh (shared with the
    touchbar's connections, so no fresh-login storms)."""
    if node is None:
        proc = subprocess.run(["bash", "-lc", command], capture_output=True,
                              text=True, timeout=timeout,
                              cwd=os.path.expanduser("~"))
    else:
        proc = subprocess.run(["ssh"] + SSH_OPTS + [node["ssh"], command],
                              capture_output=True, text=True, timeout=timeout)
    return (proc.stdout + proc.stderr).strip()


def _sessions_on(node):
    """[{'name': tmux session, 'kind': harness}] for one node."""
    out = _run_on(node,
                  "ps -eo pid=,ppid=,args=; echo __S__; "
                  "tmux list-sessions -F '#{session_name}' 2>/dev/null; "
                  "echo __P__; "
                  "tmux list-panes -a -F '#{pane_pid} #{session_name}' "
                  "2>/dev/null", timeout=8)
    # exact-line markers: the scan's own cmdline in ps output contains
    # the marker text but never as a whole line by itself
    lines = out.splitlines()
    try:
        i_s = lines.index("__S__")
        i_p = lines.index("__P__")
    except ValueError:
        return []
    ps_part = "\n".join(lines[:i_s])
    sess_part = "\n".join(lines[i_s + 1:i_p])
    pane_part = "\n".join(lines[i_p + 1:])
    sessions = [ln.strip() for ln in sess_part.splitlines() if ln.strip()]
    if not sessions:
        return []
    pane_sess = {}
    for ln in pane_part.splitlines():
        p = ln.split(None, 1)
        if len(p) == 2 and p[0].isdigit():
            pane_sess[int(p[0])] = p[1]
    procs, children = {}, {}
    for ln in ps_part.splitlines():
        p = ln.split(None, 2)
        if len(p) == 3 and p[0].isdigit():
            procs[int(p[0])] = p[2]
            children.setdefault(int(p[1]), []).append(int(p[0]))

    def kind_of(root):
        stack, seen = [root], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            args = procs.get(pid, "").lower()
            for w in HARNESS_WORDS:
                if w in args:
                    return w
            stack.extend(children.get(pid, []))
        return "shell"

    by_sess = {}
    for pid, sess in pane_sess.items():
        by_sess.setdefault(sess, kind_of(pid))
    return [{"name": s, "kind": by_sess.get(s, "shell")} for s in sessions]


def tool_fleet_sessions():
    from concurrent.futures import ThreadPoolExecutor
    nodes = _fleet_nodes()
    if not nodes:
        return "could not read the fleet (tailscale status failed)"

    def describe(node):
        if not node["online"]:
            return f"{node['display']}: offline"
        try:
            sess = _sessions_on(None if node["self"] else node)
        except Exception:
            return f"{node['display']}: unreachable"
        if not sess:
            return f"{node['display']}: no agent sessions"
        inner = ", ".join(f"{s['name']} ({s['kind']})" for s in sess)
        return f"{node['display']}: {inner}"

    with ThreadPoolExecutor(max_workers=6) as ex:
        return ". ".join(ex.map(describe, nodes))


def tool_node_status(node_query):
    node = _resolve_node(node_query)
    if node == "unknown":
        return f"no fleet node matching '{node_query}'"
    try:
        out = _run_on(node,
                      "uptime | sed 's/^ *//'; df -h / | tail -1 | "
                      "awk '{print \"disk \" $5 \" used\"}'; "
                      "ps -eo pcpu=,comm= | sort -rn | head -3 | "
                      "awk '{printf \"%s %s%% cpu; \", $2, $1}'; echo; "
                      "tmux list-sessions -F '#{session_name}' 2>/dev/null "
                      "| paste -sd, -", timeout=10)
    except Exception as exc:
        return f"could not reach {node_query}: {exc}"
    name = "this Mac" if node is None else node["display"]
    return _truncate(f"{name}: {out}", 900)


def tool_node_shell(node_query, command):
    node = _resolve_node(node_query)
    if node == "unknown":
        return f"no fleet node matching '{node_query}'"
    try:
        out = _run_on(node, command, timeout=25)
    except subprocess.TimeoutExpired:
        return "command timed out after 25 seconds (use start_work for longer work)"
    except Exception as exc:
        return f"command failed to run: {exc}"
    return _truncate(out or "(no output)", 1200)


BRAIN = os.path.expanduser("~/brain")


def _age_note(path):
    try:
        import datetime
        mtime = datetime.date.fromtimestamp(os.path.getmtime(path))
        days = (datetime.date.today() - mtime).days
        when = "today" if days == 0 else \
            "yesterday" if days == 1 else f"{days} days old"
        return f"{mtime.isoformat()} ({when})"
    except OSError:
        return "unknown date"


def tool_daily_brief():
    bits = []
    truth = os.path.join(BRAIN, "operations/CURRENT-TRUTH.md")
    try:
        with open(truth) as f:
            bits.append(f"Current truth [as of {_age_note(truth)}]:\n"
                        + f.read()[:1400])
    except OSError:
        pass
    try:
        import glob
        days = sorted(glob.glob(os.path.join(BRAIN, "logs/daily/*.md")))
        if days:
            with open(days[-1]) as f:
                bits.append(
                    f"Latest daily log ({os.path.basename(days[-1])}, "
                    f"{_age_note(days[-1])}):\n" + f.read()[-1600:])
    except OSError:
        pass
    if not bits:
        return "no Second Brain vault found on this machine"
    return _truncate("\n\n".join(bits), 3200)


def tool_brain_search(query):
    if not os.path.isdir(BRAIN):
        return "no Second Brain vault found on this machine"
    try:
        proc = subprocess.run(
            ["rg", "-i", "-n", "-m", "2", "--glob", "*.md",
             "--glob", "!**/_archive/**", "--glob", "!**/history/**",
             "--glob", "!**/openclaw-imports/**", "--glob", "!**/legacy*/**",
             query, BRAIN], capture_output=True, text=True, timeout=12)
    except Exception as exc:
        return f"search failed: {exc}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return f"nothing current in the vault about '{query}'"

    def freshness(ln):
        path = ln.split(":", 1)[0]
        try:
            return -os.path.getmtime(path)
        except OSError:
            return 0

    lines.sort(key=freshness)  # newest notes first, legacy sinks
    dated = []
    for ln in lines[:22]:
        path = ln.split(":", 1)[0]
        dated.append(f"[{_age_note(path)}] {ln}")
    return _truncate("\n".join(dated), 2200)


EL_TOOL_FUNCS.update({
    "fleet_sessions": lambda a: tool_fleet_sessions(),
    "node_status": lambda a: tool_node_status(a.get("node", "")),
    "node_shell": lambda a: tool_node_shell(a.get("node", ""),
                                            a.get("command", "")),
    "daily_brief": lambda a: tool_daily_brief(),
    "brain_search": lambda a: tool_brain_search(a.get("query", "")),
})


# ------------------------------------------------------ mnemos (memory)
# Curated, ranked memory beats raw vault grep: the vault search surfaces
# legacy notes as if current. Mnemos recall over the synced Second
# Brain collection is the primary knowledge source; every result is
# returned with its source so the model can judge freshness.
MNEMOS_CONTAINER = "cortex/agent-context/second-brain"


def _mnemos_url_token():
    url = os.environ.get("MNEMOS_URL")
    token = os.environ.get("MNEMOS_API_TOKEN")
    try:
        for line in open(SECRETS_ENV):
            if line.startswith("MNEMOS_API_TOKEN="):
                token = token or line.split("=", 1)[1].strip()
            elif line.startswith("MNEMOS_URL="):
                url = url or line.split("=", 1)[1].strip()
    except OSError:
        pass
    if not token:
        try:
            with open(os.path.expanduser("~/.mnemos-harness-token")) as f:
                token = f.read().strip() or None
        except OSError:
            pass
    if not (url and token):
        # the harness-sync unit carries the URL; reuse in-process
        try:
            import re
            unit = open(os.path.expanduser(
                "~/.config/systemd/user/mnemos-harness-sync.service")).read()
            if not url:
                m = re.search(r"MNEMOS_URL=(\S+)", unit)
                url = m.group(1) if m else url
            if not token:
                m = re.search(r"MNEMOS_API_TOKEN=([^\s\"'$]+)", unit)
                token = m.group(1) if m else token
        except OSError:
            pass
    return (url or "http://100.127.212.28:8090").rstrip("/"), token


def tool_memory_search(query):
    url, token = _mnemos_url_token()
    if not token:
        return "mnemos token not available on this machine"
    body = json.dumps({"query": query, "container_tag": MNEMOS_CONTAINER,
                       "limit": 6, "rerank": True}).encode()
    req = urllib.request.Request(
        f"{url}/recall", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.load(resp)
    except Exception as exc:
        return f"mnemos recall failed: {exc}"
    items = data if isinstance(data, list) else next(
        (data[k] for k in ("results", "memories", "items", "data")
         if isinstance(data.get(k), list)), [])
    out = []
    for it in items[:6]:
        if not isinstance(it, dict):
            out.append(str(it)[:300])
            continue
        text = next((str(it[k]) for k in
                     ("raw_text", "text", "content", "chunk", "memory",
                      "summary") if it.get(k)), "")
        meta = it.get("metadata") or {}
        src = (meta.get("relative_path") or meta.get("source")
               or meta.get("title") or it.get("source") or "")
        stamp = str(it.get("updated_at") or it.get("created_at")
                    or "")[:10]
        head = f"[{src} {stamp}]".strip()
        out.append(f"{head} {_truncate(' '.join(text.split()), 480)}")
    if not out:
        return f"mnemos has nothing on '{query}'"
    return _truncate("\n".join(out), 2400)


EL_TOOL_FUNCS["memory_search"] = lambda a: tool_memory_search(
    a.get("query", ""))


def tool_remember(fact):
    url, token = _mnemos_url_token()
    if not token:
        return "mnemos token not available on this machine"
    body = json.dumps({
        "raw_text": fact, "container_tag": MNEMOS_CONTAINER,
        "memory_type": "episodic", "actor_id": "omarchy-voice",
        "source_provider": "omarchy-voice"}).encode()
    req = urllib.request.Request(
        f"{url}/store", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            json.load(resp)
    except Exception as exc:
        return f"could not store memory: {exc}"
    return "remembered"


def tool_clipboard(text):
    proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
    proc.communicate(text.encode(), timeout=5)
    return f"copied {len(text)} characters to the clipboard"


def tool_notify(text):
    subprocess.run(["notify-send", "Omarchy Voice", text], timeout=5)
    return "notification shown"


def tool_remind(minutes, text):
    try:
        mins = max(1, int(minutes))
    except (TypeError, ValueError):
        return "minutes must be a number"
    subprocess.run(["systemd-run", "--user", f"--on-active={mins}m",
                    "--", "notify-send", "Reminder", text],
                   capture_output=True, timeout=8, check=True)
    return f"reminder set for {mins} minutes from now"


def tool_media(action):
    cmds = {"play": ["play"], "pause": ["pause"], "toggle": ["play-pause"],
            "next": ["next"], "previous": ["previous"]}
    if action == "status":
        out = subprocess.run(["playerctl", "metadata", "--format",
                              "{{artist}} - {{title}} ({{status}})"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "nothing playing"
    if action not in cmds:
        return f"unknown media action '{action}'"
    subprocess.run(["playerctl"] + cmds[action], capture_output=True,
                   timeout=5)
    return f"media {action}"


EL_TOOL_FUNCS.update({
    "remember": lambda a: tool_remember(a.get("fact", "")),
    "clipboard": lambda a: tool_clipboard(a.get("text", "")),
    "notify": lambda a: tool_notify(a.get("text", "")),
    "remind": lambda a: tool_remind(a.get("minutes", 0), a.get("text", "")),
    "media": lambda a: tool_media(a.get("action", "status")),
})


def resolve_elevenlabs_key():
    if os.environ.get("ELEVENLABS_API_KEY"):
        return os.environ["ELEVENLABS_API_KEY"]
    try:
        for line in open(SECRETS_ENV):
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _gain_pcm(raw, gain):
    """Mic gain: the J293 mic arrives quiet (~-50 dBFS speech), low
    enough that server VAD ignored it entirely. Boost with clamping."""
    if gain == 1.0 or not raw:
        return raw
    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw[:n * 2])
    out = struct.pack(f"<{n}h", *(
        max(-32768, min(32767, int(s * gain))) for s in samples))
    return out + raw[n * 2:]


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
                self.status = "listening"
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


# ------------------------------------------------------------- elevenlabs
EL_RATE = 16000
METRICS_PATH = os.path.expanduser(
    "~/.local/state/omarchy-touchbar/voice-metrics.jsonl")


class ConvMetrics:
    """Per-conversation numbers that size a future self-hosted pipeline:
    how much the agent actually talks (TTS chars -> wiring-C cost), how
    fast it answers after the user stops, how often the user barges in
    (the turn-taking bar a local VAD must match), and how long sessions
    run (minutes/month -> wiring-B cost). Numeric only, local only."""

    def __init__(self):
        self.started = time.time()
        self.user_turns = 0
        self.user_words = 0
        self.agent_chars = 0
        self.agent_turns = 0
        self.interruptions = 0
        self.tool_calls = 0
        self.latencies = []
        self._last_user_event = 0.0

    def user_spoke(self, transcript):
        self.user_turns += 1
        self.user_words += len((transcript or "").split())
        self._last_user_event = time.time()

    def first_audio(self):
        if self._last_user_event:
            self.latencies.append(time.time() - self._last_user_event)
            self._last_user_event = 0.0

    def agent_replied(self, text):
        self.agent_turns += 1
        self.agent_chars += len(text or "")

    def summary(self):
        lat = sorted(self.latencies)
        return {
            "ts": self.started,
            "seconds": round(time.time() - self.started),
            "user_turns": self.user_turns,
            "user_words": self.user_words,
            "agent_turns": self.agent_turns,
            "agent_chars": self.agent_chars,
            "interruptions": self.interruptions,
            "tool_calls": self.tool_calls,
            "latency_p50": round(lat[len(lat) // 2], 2) if lat else None,
            "latency_max": round(lat[-1], 2) if lat else None,
        }

    def flush(self):
        try:
            os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
            with open(METRICS_PATH, "a") as f:
                f.write(json.dumps(self.summary()) + "\n")
        except OSError:
            pass


class ElevenLabsSession:
    """ElevenLabs Conversational AI client: their ASR/turn-taking/LLM/TTS
    pipeline over one WebSocket; this process owns PipeWire I/O, the
    touchbar state file, and local client-tool execution. Persona, voice
    and brain live in the agent profile (agent-profile.json, applied by
    apply_agent.py); live context goes in as dynamic variables."""

    _speaker_target = staticmethod(VoiceSession._speaker_target)

    def __init__(self, cfg):
        self.cfg = cfg
        self.ws = None
        self.player = None
        self.mic_proc = None
        self.status = "listening"
        self.closed = False
        self._last_audio = 0.0
        self._out_rate = EL_RATE
        self._announcer_live = False
        self.metrics = ConvMetrics()

    def _st(self, status):
        self.status = status
        set_state(True, status)

    @property
    def speaking(self):
        return time.time() - self._last_audio < 0.45

    def _start_player(self):
        args = ["pw-cat", "--playback", "--raw", "--rate", str(self._out_rate),
                "--channels", "1", "--format", "s16"]
        target = VoiceSession._speaker_target()
        if target:
            args += ["--target", target]
        args.append("-")
        self.player = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        print(f"player started (rate={self._out_rate} target={target})",
              flush=True)

    def _stop_player(self):
        try:
            if self.player:
                self.player.terminate()
        except Exception:
            pass
        self.player = None

    def _flush_player(self):
        # user barged in: drop everything queued for the speakers
        self._stop_player()
        self._last_audio = 0.0

    async def mic_loop(self):
        while not self.closed:
            proc = await asyncio.create_subprocess_exec(
                "pw-record", "--rate", str(EL_RATE), "--channels", "1",
                "--format", "s16", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.mic_proc = proc
            try:
                while not self.closed:
                    raw = await proc.stdout.read(4096)
                    if not raw:
                        print("mic stream ended; respawning capture", flush=True)
                        break
                    # duck hard while the agent speaks: no echo canceller
                    # on this path, full-level speaker echo would make it
                    # interrupt itself; a loud real voice still gets in
                    gain = 0.12 if self.speaking else 2.2
                    await self.ws.send(json.dumps({
                        "user_audio_chunk":
                        base64.b64encode(_gain_pcm(raw, gain)).decode()}))
            except Exception as exc:
                print(f"mic loop interrupted: {exc}", flush=True)
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if not self.closed:
                await asyncio.sleep(0.8)

    async def _run_tool(self, call):
        name = call.get("tool_name", "")
        call_id = call.get("tool_call_id", "")
        args = call.get("parameters") or {}
        self.metrics.tool_calls += 1
        print(f"tool call: {name} {str(args)[:120]}", flush=True)
        self._st(f"tool: {name}")
        func = EL_TOOL_FUNCS.get(name)
        try:
            result = await asyncio.to_thread(func, args) if func \
                else "unknown tool"
            is_error = False
        except Exception as exc:
            result, is_error = f"tool failed: {exc}", True
        if call.get("expects_response", True):
            await self.ws.send(json.dumps({
                "type": "client_tool_result", "tool_call_id": call_id,
                "result": str(result)[:4000], "is_error": is_error}))
        self._st("listening")

    async def event_loop(self):
        async for raw in self.ws:
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t == "conversation_initiation_metadata":
                meta = ev.get("conversation_initiation_metadata_event") or {}
                fmt = meta.get("agent_output_audio_format", "")
                if fmt.startswith("pcm_"):
                    self._out_rate = int(fmt.split("_")[1])
                print(f"elevenlabs conversation {meta.get('conversation_id')} "
                      f"(out={fmt})", flush=True)
                self._announcer_live = True
                self._st("listening")
            elif t == "audio":
                audio = (ev.get("audio_event") or {}).get("audio_base_64", "")
                if not audio:
                    continue
                self.metrics.first_audio()
                self._last_audio = time.time()
                if self.player is None or self.player.poll() is not None:
                    self._start_player()
                try:
                    self.player.stdin.write(base64.b64decode(audio))
                    self.player.stdin.flush()
                except Exception as exc:
                    print(f"player write failed: {exc}", flush=True)
                    self._start_player()
            elif t == "agent_response":
                text = ((ev.get("agent_response_event") or {})
                        .get("agent_response") or "")
                self.metrics.agent_replied(text)
                print("assistant:", text[:200], flush=True)
            elif t == "user_transcript":
                text = ((ev.get("user_transcription_event") or {})
                        .get("user_transcript") or "")
                self.metrics.user_spoke(text)
                print("heard:", text[:200], flush=True)
            elif t == "interruption":
                self.metrics.interruptions += 1
                self._flush_player()
            elif t == "ping":
                event_id = (ev.get("ping_event") or {}).get("event_id")
                await self.ws.send(json.dumps(
                    {"type": "pong", "event_id": event_id}))
            elif t == "client_tool_call":
                await self._run_tool(ev.get("client_tool_call") or {})
            elif t in ("agent_response_correction", "vad_score"):
                pass
            elif t == "error":
                print("elevenlabs error:", str(ev)[:300], flush=True)
                self._st("provider error")

    async def _announcer(self):
        # finished background work is reported once, in character:
        # silent context first, then a system-flavoured user message so
        # the agent speaks the outcome without being asked
        while not self.closed:
            await asyncio.sleep(4)
            if not self._announcer_live or self.ws is None:
                continue
            for jid, st in pop_finished_jobs():
                text = (f"Background job {jid} ({st['goal'][:80]}) "
                        f"{st['status']}. Result: {st.get('outcome', '')}")
                try:
                    await self.ws.send(json.dumps(
                        {"type": "contextual_update", "text": text}))
                    await self.ws.send(json.dumps({
                        "type": "user_message",
                        "text": f"[{text}] Report this outcome to me now "
                                "in one or two short sentences."}))
                    print(f"announced {jid}: {st['status']}", flush=True)
                except Exception as exc:
                    print(f"announce failed: {exc}", flush=True)

    async def _heartbeat(self):
        while not self.closed:
            set_state(True, "speaking" if self.speaking else self.status
                      if self.status.startswith("tool") else "listening")
            await asyncio.sleep(3)

    def _signed_url(self, key, agent_id):
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/convai/conversation/"
            f"get-signed-url?agent_id={urllib.parse.quote(agent_id)}",
            headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)["signed_url"]

    async def run(self):
        delay = 3
        while not self.closed:
            self.cfg = load_config()
            key = resolve_elevenlabs_key()
            agent_id = self.cfg.get("agent_id")
            if not key or not agent_id:
                self._st("sign-in needed")
                print("elevenlabs needs ELEVENLABS_API_KEY in "
                      "voice-secrets.env and agent_id in voice-config.json "
                      "(run apply_agent.py); waiting", flush=True)
                await asyncio.sleep(30)
                continue
            try:
                self._st("connecting")
                url = await asyncio.to_thread(self._signed_url, key, agent_id)
                async with websockets.connect(url, open_timeout=15) as ws:
                    self.ws = ws
                    self.metrics = ConvMetrics()
                    delay = 3
                    print("connected to elevenlabs voice", flush=True)
                    import datetime
                    await ws.send(json.dumps({
                        "type": "conversation_initiation_client_data",
                        "dynamic_variables": {
                            "node": os.uname().nodename,
                            "time": datetime.datetime.now().strftime(
                                "%A %H:%M"),
                            "fleet": tool_fleet_status()}}))
                    await asyncio.gather(self.mic_loop(), self.event_loop(),
                                         self._announcer(), self._heartbeat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"elevenlabs session dropped ({exc}); reconnecting",
                      flush=True)
                self.metrics.flush()
                self._announcer_live = False
                self._stop_player()
                if not self.closed:
                    self._st("reconnecting")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)


async def _amain():
    cfg = load_config()
    session = ElevenLabsSession(cfg) \
        if cfg.get("provider") == "elevenlabs" else VoiceSession(cfg)
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
        if hasattr(session, "metrics"):
            session.metrics.flush()


def main():
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    finally:
        set_state(False, "")


if __name__ == "__main__":
    main()
