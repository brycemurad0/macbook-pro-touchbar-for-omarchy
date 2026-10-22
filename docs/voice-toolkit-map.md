# Voice control toolkit

Mapped 2026-10-22 from the live install at `~/.config/omarchy/touchbar/`
(the process `omarchy-voice.service` actually runs) and from the git
tree at `~/omarchy-touchbar/` (re-synced 2026-10-22, see Verification).

## Where a spoken request goes

`omarchy-voice.service` starts `voice_daemon.py`. With `"backend": "auto"`
it runs the first backend whose key is in `voice-secrets.env`, in order:
OpenAI Realtime, Gemini Live, ElevenLabs hosted, then the local Pipecat
cascade (`pipecat_voice.py`: PipeWire mic, Moonshine STT, the
`brain_models` chain, ElevenLabs TTS).

The brain sees the `tools` array in `voice.json`. Each call is dispatched
to `voice_tools.TOOL_FUNCS` and the string result is spoken. Schemas and
handlers are read once at process start. Editing the files does nothing
until the next voice start.

Live install and git tree, re-checked 2026-10-22 evening: **28 names,
schema and `TOOL_FUNCS` identical.** (The 15:27 check found 24 with the
git tree at 17; four tools and two honesty fixes landed after.)

Background work is a separate path. `start_work` only stages on the
first call. A second call with the same goal, after a new user turn (or
after the agent already asked "Want me to run that?"), launches a
harness on **this machine**. The voice announcer, while the service is
up, injects the last lines of the job log and speaks them once.

## What is wired today

| Tool | What it does | Bound it actually has |
|---|---|---|
| `fleet_status` | Online non-phone Tailscale nodes, local load, battery | Offline nodes are omitted. Battery path is the Asahi pack. |
| `fleet_sessions` | Tmux sessions on every node, harness kind guessed from the pane's process tree, 30s cache | A harness that is not inside tmux is invisible. This mapping job did not appear. |
| `node_status` | One node: uptime, disk, top 3 CPU, tmux names | |
| `node_shell` | One shell command on a node over multiplexed ssh | 25s, 1200 characters, exit code dropped. |
| `run_shell` | Same, on this machine | Bound to the 25s helper. A 180s helper exists and is not registered. |
| `daily_brief` | `~/brain` current-truth head plus the tail of the latest captain's log | About 1400 + 1600 characters, then cut at 3200. |
| `brain_search` | Ripgrep over the vault, newest notes first | Two hits per file, 22 lines. `history/` is excluded, so the decision archive is skipped. |
| `memory_search` | Mnemos `/recall` on `cortex/agent-context/second-brain` | Six hits. The agent-fabric transcript container is not queried. |
| `remember` | Mnemos `/store` as episodic, actor `omarchy-voice` | Writes only when called. No correct or forget. |
| `focus_window` | Focus the first Hyprland client whose class or title contains the query | No list, no "which one", no workspace move. |
| `close_window` | Closes via `hl.dsp.window.close` by address, verifies against `j/clients`, escalates to `kill` | Refuses ambiguous matches (exact class/title narrows); reports "still open" instead of a false success. The old pid-kill version claimed "closed foot" while the window lived. |
| `list_windows` | All Hyprland clients: class, title, workspace | First 25. Read-only. |
| `open_terminal` | Real foot window here (shell or harness) or ssh+tmux into a node | Verifies by *new client address* - bash retitles windows on its first prompt, so title checks see nothing. |
| `files` | list/find/read under $HOME (skips caches/.git/venvs) | Output truncated at 1800 chars. |
| `cancel_work` | Terminate a background job by id (live proc or disk registry pid) | SIGTERM only. |
| `open_app` | `gtk-launch` the first `.desktop` whose `Name=` contains the query, else a PATH binary | No "already open, just focus". File handles are left open. |
| `open_url` | `xdg-open` | |
| `clipboard` | `wl-copy` | Write only. `wl-paste` is on the machine and unused. |
| `notify` | `notify-send` | |
| `remind` | `systemd-run --user --on-active=` then `notify-send` | |
| `media` | `playerctl` status, play, pause, toggle, next, previous | Volume is `system_control`, not this tool. |
| `system_control` | Sink volume, ±5%, mute, panel brightness, `hyprlock` | Brightness floor is 5%. Default device is `apple-panel-bl` (checked). Keyboard and Touch Bar backlights are separate devices and are not covered. |
| `start_work` | Stage, then launch `grok` / `pi` / `claude` / `codex` / `opencode` here | Confirm gate is in code. Never a fleet node. |
| `work_status` | One line per job: id, harness, running/done/failed, goal | The outcome text is computed and then dropped. Jobs reloaded from disk after a restart are "done" whenever the pid is gone, including failed runs. |
| `read_session` | `tmux capture-pane` on a node | Session name is sanitized. Output is the last 25 non-empty lines. |
| `send_to_session` | `tmux send-keys` plus Enter | No confirm step in code. |
| `start_session` | Detached tmux `tb-<harness>-<n>` running the bare binary, optional goal typed after 3s | No `--always-approve` / `--continue`, no `fleet.json` launch line, no foot window. Harnesses: pi, claude, codex, grok, opencode. |
| `stop_session` | `tmux kill-session` | Any name, not only sessions this daemon created. No confirm step in code. |

`voice.json` memory is configured: vault `~/brain`, Mnemos container
`cortex/agent-context/second-brain`.

The Touch Bar already does four things voice still cannot: open a foot
viewer for a session, resume with the harness continue flag, launch
cursor / antigravity / mcode / kimi / hermes / grokbot / plain ssh, and
close only a session this daemon created. It also drives keyboard
backlight and Touch Bar brightness, and it refuses to dim the panel
below 8%.

## What is still missing

**Apps.** Launch by desktop name is in. Still absent: a list of apps,
focus-if-already-open, and quit that closes one window through Hyprland
instead of signaling the whole pid.

**Windows.** Focus and close-by-kill are in. Still absent: list the
windows (four were on screen during the probe: pi, foot, Chrome, an
Omarchy dev terminal), pick among matches, move or switch workspace,
float or fullscreen.

**Sessions.** List, read, type, start, and kill tmux sessions are in.
Still absent: resume the harness's last conversation, open the viewer
on this screen, the Touch Bar's wider harness set, and any session that
is not tmux. `start_work` jobs are in that last bucket, so "what's
running here" and "what jobs are running" are different questions with
different tools.

**Shell.** Short local and remote commands are in. Still absent: a
working directory, the exit code, and the 180s helper that was never
registered. Destructive commands run on the first call.

**Files.** No file tool. There is no list, read, search, or open-in-editor
outside the vault search. A short `run_shell` is the only stand-in, and
it cuts the output at 1200 characters.

**Fleet status.** Up/down, load, battery, per-node disk and CPU, and
tmux names are in. Still absent: a service probe (Treadle, Hermes,
Mnemos, vLLM, and the rest of the current-truth port list) and an
offline rollup inside `fleet_status`. During the probe Pavilion was
offline and appeared only in `fleet_sessions`.

**Background jobs.** Stage, launch, list, and (while voice is up) speak
the finish are in. Still absent: cancel, read the log on demand, steer
a `start_work` job, and a true failed state for jobs whose process has
already exited. The registry currently holds this mapping job plus
several already-announced unit-test jobs (`test goal ok` / `test goal
bad`); `work_status` reports the failed test harness as done.

**Memory.** Vault brief, vault ripgrep, Mnemos recall, and Mnemos
remember are in. Still absent: the `cortex/agent-fabric` transcript
container, `operations/owner-decisions.md` as its own read, the
`history/decisions` archive (`brain_search` skips `history/`), and any
way to correct a stored fact.

**Desk controls.** Volume, mute, panel brightness, lock, media
transport, notifications, and reminders are in. Still absent: clipboard
read, keyboard backlight, Touch Bar brightness, screenshot, and typing
into the focused window (tmux panes only).

## Confirm gate

The persona says state-changing acts wait for a spoken yes. The code
enforces that for `start_work` only. These run on the first call:
`run_shell`, `node_shell`, `open_app`, `open_url`, `close_window`,
`send_to_session`, `start_session`, `stop_session`, `system_control`,
`clipboard`, `notify`, `remind`, `media`, `remember`.

## Verification

- Imported the live module: 28 tools, same names and order as live
  `voice.json`. Imported the git module after re-sync: 28 tools.
- 2026-10-22 evening (Pi): `close_window` and `open_terminal` were
  live-proven end to end (sacrificial windows opened and closed,
  absence verified in `j/clients`); ambiguous close queries refuse
  after one such test closed a live shell. `python3 -m unittest
  discover` green in both the live tree and this repo.
- Called `fleet_status` (6 nodes up, load 2.31, battery 63%),
  `fleet_sessions` (Pavilion offline; this machine reported no tmux
  agent session while this job was running), `node_status("here")`,
  `work_status` (outcome text absent), `media("status")`,
  `focus_window` on a name that matches nothing, `daily_brief` (3102
  characters, truth and daily log both present), `brain_search("Treadle")`
  (7 lines), and `memory_search` (5 hits, 2400 characters). No memory
  text was copied into this note.
- `brightnessctl info` with no device selected `apple-panel-bl`.
  `hyprlock`, `gtk-launch`, `wl-paste`, and `playerctl` are installed.
- Unit tests from the live tree, excluding `JobTests` so the live job
  registry was not written again: 25 passed. That run included the
  confirm gate, the schema/implementation match, a throwaway tmux
  send/read/stop (`vt-test`, removed afterward), and `open_app` with a
  name that matches nothing.
- `omarchy-voice.service` was inactive at check time. The process that
  stopped at 15:26:35 had started before `voice_tools.py` was updated
  at 15:25:36, so that conversation was still on the previous tool
  list. The next start loads these 28.
