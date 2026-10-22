# omarchy-touchbar

A living Touch Bar for [Omarchy](https://omarchy.org/) on Apple Silicon
MacBooks: an animated system field, real function keys, desktop controls,
live agent sessions, a fleet/ tmux launcher, and an optional realtime
voice agent — all rendered directly to the Touch Bar framebuffer with
the stock `tiny-dfr` kept one command away as the rollback.

![FLOW](docs/flow.png)

## Views

Tap the left rail (or hold **Fn** for KEYS). Non-FLOW views return to
FLOW after 8 idle seconds.

| View | What it does |
|---|---|
| **FLOW** | A calm ASCII current. Agent/CPU energy visibly levels it up — density, pace, and spiral strength — while idle stays sparse. Taps ripple the water; status chips show clock, battery, CPU, and agents. |
| **KEYS** | ESC + F1–F12 injected over `/dev/uinput`. Summoned physically by holding **Fn**; releasing Fn returns. |
| **CTRL** | Display / volume / keyboard-backlight / Touch Bar brightness sliders (display never below 8%), plus media keys. FLOW's centre pod carries the same sliders and media cluster. |
| **LIVE** | Agent sessions and windows across this machine and a Tailscale fleet. Cards are two-tap: first tap arms, second opens. Remote cards are real tmux-hosted sessions discovered from the node, never name-matched daemons. |
| **DEV** | Pick a host, pick a harness (`ssh`, `pi`, `claude`, `codex`, `opencode`, `cursor`, `antigravity`, `mcode`, `kimi`, `grok`, `hermes`, a desktop coordinator), confirm, launch. Remote launches live in tmux and resume with the harness's continue flag. |
| **Fleet** | From FLOW's chips: nodes, today's sessions, running sessions, and system detail. |

Notifications only fire for completions and failures (a session must
live at least 90 seconds and be gone for two scans). Tapping the pill
jumps to its session; tapping the ASCII field around it dismisses it.

## Voice (optional)

`voice_agent.py` is a live voice agent client. ElevenLabs
Conversational AI provides ears, turn-taking, and mouth; the persona,
voice, and brain are versioned in `agent-profile.example.json` and
applied with `apply_agent.py`; the daemon keeps PipeWire mic/speaker
I/O and executes the agent's client tools locally — quick checks
(fleet sessions/status, Mnemos memory recall, shell here or on any
node, window focus, clipboard, reminders, media) answer by voice,
while real work is restated as a goal, confirmed out loud, then run
permissionless in the background by a coding harness (`start_work`)
and reported once when done. Toggle it from FLOW.

The brain has two wirings, chosen by `brain` in `voice-config.json`:

- `hosted` — ElevenLabs serves the model from their menu
  (`gemini-3.8-flash` by default). Zero local moving parts.
- `custom` (wiring B) — `llm_bridge.py` serves `gemini-3.8-flash`
  from your own Gemini API key in OpenAI-compatible form, exposed to
  ElevenLabs over Tailscale Funnel behind a generated shared secret;
  ElevenLabs calls it as a custom LLM. `apply_agent.py` only switches
  the agent to the bridge when the bridge is up *and* has a key, so
  the agent can never be stranded on a dead endpoint.

Setup: put `ELEVENLABS_API_KEY` (and `GEMINI_API_KEY` for the custom
brain) in `~/.config/omarchy/touchbar/voice-secrets.env` (mode 600,
never committed), copy the two `*.example.json` config files into
place, and run `python3 apply_agent.py`. xAI Grok Voice (native
speech-to-speech) remains available as `provider: grok`. Every
conversation appends one numeric metrics row (length, turns, agent
characters, answer latency, interruptions, tool calls) to
`~/.local/state/omarchy-touchbar/voice-metrics.jsonl` — the data that
will justify (or kill) a future fully-local pipeline.

## Install

Requirements: Omarchy on an Apple Silicon MacBook (Touch Bar driven
by tiny-dfr), Python 3 with `cairo` and `websockets`, `foot`, `tmux`,
and a Tailscale mesh if you want fleet features.

```bash
git clone https://github.com/brycemurad0/omarchy-touchbar
cd omarchy-touchbar
./install.sh
```

`install.sh` copies the daemon to `~/.config/omarchy/touchbar/`,
installs both systemd user units, adds the `/dev/uinput` udev rule,
masks `tiny-dfr`, and starts the bar. Roll back any time:

```bash
~/.config/omarchy/touchbar/restore.sh
```

tiny-dfr is never uninstalled; masking is reversed by `restore.sh`.

State and diagnostics live in `~/.local/state/omarchy-touchbar/`
(`stats.json` frame timings, `preview.png` periodic render). At 20 fps
the daemon should show `fps == upload_fps`.

## Configuration

Everything machine-specific or preferential is config, never code:

| File (in `~/.config/omarchy/touchbar/`) | Controls |
|---|---|
| `fleet.json` | Display names + ssh aliases per Tailscale node (see `fleet.example.json`) |
| `voice-config.json` | Voice provider (`elevenlabs`/`grok`), `agent_id`, brain wiring (`hosted`/`custom`) |
| `agent-profile.json` | Persona prompt, voice name + settings, brain model, tool list, turn patience (applied by `apply_agent.py`) |
| `voice-secrets.env` | API keys only (`ELEVENLABS_API_KEY`, `GEMINI_API_KEY`); generated bridge secret lands here too. Mode 600, never committed |
| env `TOUCHBAR_FLOW` | FLOW field preset (default `classic`, the owner-approved field) |
| env `TOUCHBAR_ROTATE` / `TOUCHBAR_FLIP_X` | Panel orientation / touch mirroring |
| Theme files | Colors and font come from the active Omarchy theme |

## Fleet naming (optional)

Display names and ssh aliases that differ from Tailscale DNS labels
live outside the code in `~/.config/omarchy/touchbar/fleet.json`
(see `fleet.example.json`). Remote sessions are discovered with
`ps` ancestry + `tmux list-panes`, so any tmux-hosted harness counts
and no session is ever closed because of its name: only a session
this daemon created is closed when its viewer closes.

## Development

```bash
python3 -m unittest discover -v   # bar + voice regression suites
python3 touchbar.py --snapshot /tmp/tb-snap   # render every view offscreen
```

Key sources: `touchbar.py` (daemon, renderer, session discovery),
`voice_agent.py` (voice client + local tools), `llm_bridge.py`
(custom brain endpoint), `apply_agent.py` (profile → ElevenLabs
agent), `test_touchbar.py` / `test_voice_agent.py` (regressions:
tap matrix, rail priority, session ownership, FLOW field shape,
voice gain/jobs/metrics/profile consistency).

## License

MIT — see [LICENSE](LICENSE).
