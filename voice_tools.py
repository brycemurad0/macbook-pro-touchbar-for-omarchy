#!/usr/bin/env python3
"""Tools and helpers for the Pipecat voice agent (pipecat_voice.py).

Everything here is synchronous and local: fleet/session senses over
multiplexed ssh, Second Brain + Mnemos memory, desktop actions, and
the background-job machinery behind start_work. Keys resolve from
voice-secrets.env (never logged). The touchbar reads liveness from
the state file written by set_state().
"""
import itertools
import json
import os
import struct
import subprocess
import time
import urllib.error
import urllib.request

def speaker_target():
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


BASE_DIR = os.path.expanduser("~/.config/omarchy/touchbar")


STATE = os.path.expanduser("~/.local/state/omarchy-touchbar/voice.json")


SECRETS_ENV = os.path.join(BASE_DIR, "voice-secrets.env")


def set_state(live, status=""):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump({"live": live, "status": status, "ts": time.time()}, f)
    except OSError:
        pass


def _truncate(s, n=1600):
    return s[-n:] if len(s) > n else s


HARNESS_CMDS = {
    "pi": ["pi", "-p"],
    "grok": ["grok", "--always-approve", "-p"],
    "claude": ["claude", "-p", "--dangerously-skip-permissions"],
    "codex": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
    "opencode": ["opencode", "run"],
}


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


def tool_run_shell_quick(command):
    out = subprocess.run(["bash", "-lc", command], capture_output=True,
                         text=True, timeout=25, cwd=os.path.expanduser("~"))
    return _truncate((out.stdout + out.stderr).strip() or "(no output)", 1200)


# Confirm-before-work is enforced here, not just asked for in the
# prompt: a model (MiniMax did this live) can call start_work on the
# first turn and launch unconfirmed work. So the first call only
# STAGES the job; it launches on a later call, with the same goal,
# after at least one new user turn (note_user_turn) has arrived —
# i.e. the user has actually been asked and has answered.
_pending_work = {"goal": None, "harness": "grok", "turn": -1, "at": 0.0}
_user_turn = 0


def note_user_turn():
    global _user_turn
    _user_turn += 1


def _start_work_tool(args):
    goal = (args.get("goal") or "").strip()
    if not goal:
        return "no goal given"
    harness = args.get("harness", "grok")
    p = _pending_work
    staged = (p["goal"] or "").casefold() == goal.casefold()
    fresh = time.time() - p["at"] < 900
    if staged and fresh and p["turn"] < _user_turn:
        _pending_work["goal"] = None
        jid = start_work_job(goal, harness)
        return (f"{jid} started on {harness}. "
                "Tell the user it is running in one short line, then stop "
                "talking about it until it finishes.")
    _pending_work.update(goal=goal, harness=harness,
                         turn=_user_turn, at=time.time())
    return ("STAGED, not started — nothing is running yet. Now ask the "
            "user in one short line: restate this goal crisply, name the "
            f"harness ({harness}), and ask 'Want me to run that?' Then "
            "STOP and wait. Only call start_work again, with this exact "
            "goal, after the user says yes in a new turn.")


TOOL_FUNCS = {
    "fleet_status": lambda a: tool_fleet_status(),
    "run_shell": lambda a: tool_run_shell_quick(a.get("command", "")),
    "focus_window": lambda a: tool_focus_window(a.get("query", "")),
    "open_url": lambda a: tool_open_url(a.get("url", "")),
    "start_work": _start_work_tool,
    "work_status": lambda a: tool_work_status(),
}


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


def _voice_cfg():
    for name in ("voice.json", "voice.example.json"):
        try:
            with open(os.path.join(BASE_DIR, name)) as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
        except Exception:
            continue
    return {}


# fleet.json may override any harness launch line per machine
# (provider/model choices are personal, not universal defaults)
HARNESS_CMDS.update({k: list(v) for k, v in
                     (_fleet_cfg().get("harness_cmds") or {}).items()})


def _memory_cfg():
    return (_voice_cfg().get("memory") or {})


def _vault_path():
    v = str(_memory_cfg().get("vault_path") or "").strip()
    return os.path.expanduser(v) if v else None


def _mnemos_container():
    return str(_memory_cfg().get("mnemos_container") or "").strip()


def _fleet_nodes():
    """[{label, display, ssh|None(local), online}] from tailscale."""
    cfg = _fleet_cfg()
    aliases = cfg.get("node_aliases") or {}
    ssh_aliases = cfg.get("ssh_aliases") or {}
    hide = {str(x).lower() for x in (cfg.get("hide_nodes") or [])}
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
        if not label or label in hide:
            continue  # unnamed, or a retired ghost (fleet.json hide_nodes)
        nodes.append({
            "label": label,
            "display": aliases.get(label, peer.get("HostName") or label),
            "ssh": None if is_self else (ssh_aliases.get(label) or label),
            "online": bool(peer.get("Online")) or is_self,
            "self": is_self})
    return nodes


def _resolve_node(query):
    q = (query or "").strip().lower()
    if not q or q in ("here", "local", "this", os.uname().nodename):
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


_fleet_sessions_cache = (0.0, "")


def tool_fleet_sessions():
    global _fleet_sessions_cache
    if time.time() - _fleet_sessions_cache[0] < 30:
        return _fleet_sessions_cache[1]
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
        result = ". ".join(ex.map(describe, nodes))
    _fleet_sessions_cache = (time.time(), result)
    return result


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


VAULT = _vault_path()  # voice.json memory.vault_path; None = unconfigured


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
    if not VAULT:
        return "no notes vault configured (memory.vault_path in voice.json)"
    bits = []
    truth = os.path.join(VAULT, "operations/CURRENT-TRUTH.md")
    try:
        with open(truth) as f:
            bits.append(f"Current truth [as of {_age_note(truth)}]:\n"
                        + f.read()[:1400])
    except OSError:
        pass
    try:
        import glob
        days = sorted(glob.glob(os.path.join(VAULT, "logs/daily/*.md")))
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
    if not VAULT or not os.path.isdir(VAULT):
        return "no notes vault configured (memory.vault_path in voice.json)"
    try:
        proc = subprocess.run(
            ["rg", "-i", "-n", "-m", "2", "--glob", "*.md",
             "--glob", "!**/_archive/**", "--glob", "!**/history/**",
             "--glob", "!**/openclaw-imports/**", "--glob", "!**/legacy*/**",
             query, VAULT], capture_output=True, text=True, timeout=12)
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


TOOL_FUNCS.update({
    "fleet_sessions": lambda a: tool_fleet_sessions(),
    "node_status": lambda a: tool_node_status(a.get("node", "")),
    "node_shell": lambda a: tool_node_shell(a.get("node", ""),
                                            a.get("command", "")),
    "daily_brief": lambda a: tool_daily_brief(),
    "brain_search": lambda a: tool_brain_search(a.get("query", "")),
})





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
    return (url or "http://127.0.0.1:8090").rstrip("/"), token


def tool_memory_search(query):
    if not _mnemos_container():
        return "mnemos not configured (memory.mnemos_container in voice.json)"
    url, token = _mnemos_url_token()
    if not token:
        return "mnemos token not available on this machine"
    body = json.dumps({"query": query,
                       "container_tag": _mnemos_container(),
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


TOOL_FUNCS["memory_search"] = lambda a: tool_memory_search(
    a.get("query", ""))


def tool_remember(fact):
    if not _mnemos_container():
        return "mnemos not configured (memory.mnemos_container in voice.json)"
    url, token = _mnemos_url_token()
    if not token:
        return "mnemos token not available on this machine"
    body = json.dumps({
        "raw_text": fact, "container_tag": _mnemos_container(),
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


TOOL_FUNCS.update({
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
    """Mic gain: the built-in mic arrives quiet (~-50 dBFS speech), low
    enough that server VAD ignored it entirely. Boost with clamping."""
    if gain == 1.0 or not raw:
        return raw
    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw[:n * 2])
    out = struct.pack(f"<{n}h", *(
        max(-32768, min(32767, int(s * gain))) for s in samples))
    return out + raw[n * 2:]


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

    def note_speech(self):
        """Called by the mic loop while real voice energy is present;
        latency is measured from actual speech end, not from when the
        transcript event happens to arrive (that read ~0.0s, useless)."""
        self._last_user_event = time.time()

    def user_spoke(self, transcript):
        self.user_turns += 1
        self.user_words += len((transcript or "").split())

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


def _gemini_key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    try:
        for line in open(SECRETS_ENV):
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


if __name__ == "__main__":
    main()


