# Handoff: omarchy-touchbar (M1 MacBook Pro Touch Bar)

Goal: a living Touch Bar for Omarchy — ASCII pinwheel flow behind every view, functional
controls, tap-to-focus sessions (local + fleet), a DEV launcher (node × harness → tmux →
terminal), and a Grok live-voice toggle. Code: `~/.config/omarchy/touchbar/touchbar.py`
(stdlib + cairo only). Services: `omarchy-touchbar.service`, `omarchy-voice.service` (user units).
`restore.sh` returns the machine to stock tiny-dfr at any time. tiny-dfr is masked, never uninstalled.

## What is verified working (do not re-derive)

- **Display pipeline** (the hard part): panel is 60×2008 portrait, command-mode DSI. Mirror
  tiny-dfr exactly: 64-wide dumb buffer (pitch 256; a 60-wide/240 pitch shears into smudge),
  atomic modeset with plane SRC cropped to 60×2008, offscreen 64×2008 surface drawn with
  `translate(60,0) rotate(+90°)`, memcpy, then `DRM_IOCTL_MODE_DIRTYFB` full clip per frame.
  **No page flips** — the adp driver is `drm_gem_fb_create_with_dirty`. Orientation transform
  is proven (L/R/TOP pattern read correctly by the user). Frame pacing is lockstep with the
  ~33 ms DSI upload (`stats.json`: fps == upload_fps, target 24). A framebuffer *readback*
  (read dumb buffer, un-rotate, save PNG) verifies rendering without the user's eyes;
  `touchbar.py --snapshot DIR` renders all views offscreen.
- **Touch**: `/dev/input/event1`, BTN_TOUCH(330) + ABS_MT_POSITION_X/Y (23044×639 →
  2008×60). A tap = lift on the *same* target pressed (an old 14 px drift rule ate taps).
  The notification pill must be hit-tested **before** all content (it overlays every view).
- **Views**: FLOW (pinwheel field, centered info pill → fleet drill-down, quiet right folder
  with DISPLAY/VOLUME sliders + media, mic switch, centered chips), KEYS (Fn summons),
  CTRL (4 sliders incl. TOUCHBAR backlight), LIVE (sessions; dedupe agent-hosted windows;
  tap focuses), DEV (host → harness → confirm), FLEET + 3 detail views. Flow renders dimmed
  behind all views. Theme: colors live from `~/.local/state/omarchy/current/theme/colors.toml`,
  font from `fc-match monospace`. Nothing hardcoded.
- **Focus**: this Hyprland build speaks Lua at IPC. Queries: raw socket `j/clients` etc.
  (discover socket under `$XDG_RUNTIME_DIR/hypr/*/`). Dispatch ONLY via
  `hyprctl dispatch 'hl.dsp.focus({ window = "address:0x…" })'` with
  `HYPRLAND_INSTANCE_SIGNATURE` injected into env. Raw-socket `dispatch` is rejected.
- **SSH mesh: COMPLETE.** All 42 directed pairs BatchMode-verified. Fleet table lives in
  `~/.ssh/config` (sparky, pavilion=user bpm, nas, atlas=user admin, bob, jarvis, fred).
  `ssh-mesh-from-fred.sh` is the idempotent receipt. **Do not** re-run any password bootstrap:
  the earlier "password failures" were wrong usernames (`fred` is not the account everywhere).
  Remote DEV sessions run in tmux `tb-<harness>` / `tb-ssh`, launched through an ssh
  reconnect loop (drop → 2 s → reattach). Resume flags: pi `--continue`, claude `--continue`,
  codex `resume --last`, opencode `--continue`, grok `--always-approve --continue`.
- **Notifications**: only completions/failures (session ≥90 s old, two consecutive absences,
  poll-failure carries last-known state; starts are ambient, never flash). Events carry their
  target: local → focus window; remote (goal JSON may set `node`/`harness` in
  `~/.local/state/omarchy-touchbar/goals/*.json`) → ssh + tmux resume. Every flash is logged:
  `journalctl --user -u omarchy-touchbar | grep flash:`.
- **Voice**: Grok Voice (`wss://api.x.ai/v1/realtime`) connects with the xAI OAuth token from
  pi's auth store — no new secret. Verified: session, tool-call round trip, live service.
  Playback MUST be `pw-cat --playback --raw --target audio_effect.j293-convolver`
  (`pw-play` rejects raw stdin; the default sink is an empty headphone jack — this bug made
  Grok inaudible for hours). Service supervises itself (reconnect loop, mic respawn, 15 s
  heartbeat). Toggle truth = `systemctl --user is-active`, not `voice.json`.
  Tools executed locally: delegate (pi/grok/claude/codex/opencode CLIs), shell, focus, URL, fleet.

## Landmines (each cost real breakage once)

- ctypes: `drmModeRes` is count-then-pointer; `_Conn` is connector_id-then-encoder_id;
  `drmModePlane` starts with format list; **`drmModeAtomicAlloc.restype` must be `c_void_p`**
  (default int truncation segfaulted the daemon repeatedly).
- Never `c_char.from_buffer()` immutable `bytes` in the upload path; never bare `except: pass`
  around uploads/flashes — the bar sat black and "working" for an hour that way.
- Touchbar brightness: two different devices (`apple-panel-bl` = main, `228600000.dsi.0` = bar).
  Display slider hard-floored at 8% (a rotated build once drove the user's screen to black).
  Dimmer must key off *global* keyboard/trackpad activity and never read the dimmed sysfs
  value back as the user's level (ratchet-to-dark bug).
- Wi-Fi power-save must be off at the *connection profile* (`nmcli connection modify Maestro
  wifi.powersave 2`); it was dropping SSH on this Asahi radio.
- Pi session dir encoding is `--` + cwd with `/`→`-` + `--` (e.g. `--home-fred--`).
- Agent scans see transient processes (`pi --help`!): lifecycle events need lifespan filters.
  Test notifications OFFLINE (state-machine unit tests), and always disclose synthetic tests —
  a fake "Treadle complete" test goal and probe flashes already eroded the user's trust once.
- Don't stop tiny-dfr or the daemon without an immediate restore path; don't leave test
  windows, tmux sessions, goal files, or terminals behind after verifying.

## Known-open / polish

- In-daemon render measures ~15 ms vs ~3 ms standalone (cause unknown; harmless — panel
  lockstep caps fps anyway). CPU ≈ 37–50% of one core at 24 fps; acceptable, tunable.
- Gemini Live provider not implemented (needs a Gemini API key in
  `~/.config/omarchy/touchbar/voice-secrets.env`); OpenAI Realtime likely needs a platform
  key, not the Codex OAuth token. Config: `voice-config.json` (`provider`, `voice`).
- Voice TTS is the model's own audio now (espeak/whisper v1 pipeline is superseded); mic UX
  could still use a push-to-talk mode. Node-side goal reporters (so remote harnesses write
  completion JSONs to fred) are the Hermes agent's open thread.
- Flow smoothness is user-judged good at 24 fps lockstep; per-cell temporal smoothing is 0.18.

## Acceptance checks for "done"

All services active with 0 restarts over a 10 min soak; `stats.json` fps == upload_fps;
zero lines from `grep flash:` during idle; tap each tab, each slider (display never <8%),
each LIVE card from workspace 3 (must snap), DEV launch local + one remote tmux (verify with
`ssh <node> tmux ls`, then clean it up), fleet drill-downs reachable, voice toggle on → Grok
audibly greets and answers a fleet question → toggle off; `restore.sh` then `install.sh`
round-trip leaves the custom bar back. Log meaningful changes in the project README as you go.

## Addendum 2026-10-21 (acceptance run, Muse)

Acceptance passed end-to-end. Root causes found and fixed upstream:

- **Voice never talked back** (owner report trumped "verified"): the mic streamed live
  from connect; a 13 ms `pw-record` startup blip tripped server VAD at +0.23 s,
  barged-in and cancelled the greeting; speaker echo would re-trigger on answers.
  Fix (`voice_agent.py`): documented xAI session schema (`audio.*.format`, lowercase
  voice, `?model=grok-voice-latest`), greet only after `session.updated`, mic sends
  digital silence until the greeting completes and while the assistant speaks
  (half-duplex — there is no AEC on this mic path, so barge-in while speaking is
  traded for answers that finish), exact 200 ms mic frames, player death is logged
  (stderr to journal + `poll()` check), `heard:`/`assistant:`/`tool call:` journal
  lines are the live transcript receipt. Agentics: instructions route multi-step
  work to `delegate` harness=grok (Grok coordinator); fleet tool excludes phones
  and knows this is Linux. Verified live: greeting spoken, fleet question heard →
  `fleet_status` tool → spoken answer, agentic follow-up, clean toggle off.
- **SSH flakiness under polling**: remote scan opened a fresh sshd login per node
  every 20 s (~18/min fleet-wide; bob logged 2,457 sshd lines/hr from all pollers).
  Fix: `ControlMaster=auto` + `ControlPersist=180` multiplexing (scan 1.4 s → 0.1 s),
  parallel single-flight scan at 45 s, 120 s per-node failure backoff (backed-off
  nodes keep last-known sessions, never read as ended). DEV `_ssh_loop` now backs
  off exponentially (2 s → 60 s) when ssh exits fast instead of hammering sshd.
- **LIVE sessions are now sessions, not processes**: argv-pattern classification
  (interpreter-hosted `python …/hermes-agent/hermes`, `node …/grok`, shims
  agy/cursor-agent), outermost-per-tree dedupe, one card per hosting window,
  CPU-jiffy activity, Hermes cross-checked against
  `~/.hermes/runtime/active_sessions.json`. New kinds: hermes, grok, antigravity,
  cursor, kimi, mcode; DEV can launch hermes. Hermes previously rendered as a
  generic "terminal" window (argv[0] is `python`).
- **No duplicate terminals on tap**: DEV / LIVE-remote / notification launches are
  focus-or-launch keyed by (host, harness) — a second tap focuses the existing
  foot window (verified live: 2 taps → 1 window).
- `flash:` journal lines are only real flashes now (`agent_start` was logged but
  never flashed, polluting the idle check). DEV slide-transition hit rects now
  follow the slid drawing.
- Soak: touchbar PID stable 10 min, 0 restarts, fps == upload_fps (one 0.1
  rounding split in 10 samples), zero `flash:` lines. Voice greeted during the
  soak and was toggled off by the owner at the bar (clean stop, 0 restarts).
- Disclosure: cleanup closed the owner's `hermes@Sparky` foot viewer window;
  its `tb-hermes` tmux on sparky is intact and resumable from LIVE.
- Follow-up (same night): LIVE is two-tap — first tap arms a card (accent
  outline, "TAP AGAIN TO OPEN"), second tap on the same card opens it;
  selection expires in 6 s. Cards also require a terminal window or two
  consecutive scans, so transient delegate sub-calls never flicker in, and
  `hermes gateway run` (a systemd service) is not classified as a session.
- FLOW is water now: energy drives current pace/swell, never brightness
  (the old brightness swing was the in-and-out pulsing). Touch stirs it:
  taps/drag release expanding ripples and the finger carries a standing
  wave (`Renderer.add_ripple`/`set_touch`/`_touch_wave`, faster cell
  smoothing while touching). Rail tabs outrank all content hits
  (scrolled cards slide under the rail), closing a launched ssh terminal
  kills its remote tmux (`Actions.reap_closed`), offline DEV hosts are
  shown but not launchable, DEV/LIVE gained both scroll arrows, the
  two-tap outline fades with its 6 s window, the FLOW pod has a voice
  button, and CTRL slider labels can no longer collide with percentages.
  Regression suite: `test_touchbar.py` (8 tests, stdlib unittest).
- Owner style test chose `classic` as the permanent FLOW default: sparse
  tiny symbols, slow sideways current, lots of dead space. Its `levels`
  leverage means idle stays sparse while higher agent/CPU energy visibly
  adds density, current speed, and spiral strength. Remote LIVE cards
  now require a harness inside an actual tmux pane; loose harness-named
  daemons on fleet nodes no longer masquerade as closable sessions.
  Session identity is the exact tmux session discovered on the node.
  Ownership is a runtime record of what this daemon created — never a
  name pattern — so existing system sessions are only detached when
  their viewer closes, while a daemon-created session is closed with
  its viewer. The stale Sparky `tb-ssh` Hermes session was closed and
  verified absent from the next scan. Voice credentials now refresh
  pi's xAI OAuth token before expiry instead of retrying a dead token
  into HTTP 403. Public release: brycemurad0/omarchy-touchbar (MIT),
  with per-machine fleet naming moved to an uncommitted `fleet.json`.
- Addendum 2026-10-22 (voice replatform, Muse): the owner never got a
  spoken answer from Grok Voice despite the greeting working. Probe proof:
  with the mic ungated, 28 s of continuous owner speech (RMS 60-145) was
  streamed and xAI sent back only pings - no `speech_started`, ever. The
  J293 mic arrives quiet (~-50 dBFS at 0.70 source volume), plausibly
  under xAI's VAD. Voice moved to ElevenLabs Conversational AI (their
  ASR/turn-taking/LLM/TTS pipeline): `voice_agent.py` `ElevenLabsSession`
  streams `pw-record` 16 kHz with x2.2 gain (0.12 duck while the agent
  speaks; no AEC on this path), plays via the convolver sink, executes
  client tools locally, and reports touchbar state as before. Persona,
  voice and brain are a versioned profile (`agent-profile.json`, applied
  by `apply_agent.py`; brain = latest Gemini Flash, voice-layer only).
  Speech discipline is authored in the prompt: 1-2 sentences, never a
  monologue. Work protocol (owner-ruled): quick bounded checks answer by
  voice; real work is restated as a goal, confirmed out loud, then
  `start_work` launches a permissionless harness (grok coordinator
  default) in the background; completion is injected via
  `contextual_update` + `user_message` and reported once. Grok Voice
  remains a config fallback (`provider: grok`), including the xAI token
  refresh fix. Key lives only in `voice-secrets.env`
  (`ELEVENLABS_API_KEY`, user-placed); agent creation is pending that
  key, then a live owner-voice test (greeting, quick question, confirmed
  background job) closes this out.
- Addendum 2026-10-22 (fleet senses, Muse): agent created
  (gemini-2.5-flash, voice "Roger", eleven_flash_v2 - agents reject
  flash_v2_5) and live-verified: owner spoke, it heard, tooled, and
  answered. Owner then required real fleet understanding, so the voice
  tools grew: `fleet_sessions` (live tmux+harness scan per node, same
  multiplexed ssh discipline; exact-line marker parsing - the scan's
  own cmdline contains the marker text), `node_status`, `node_shell`
  (bounded cross-node shell), `daily_brief` (CURRENT-TRUTH + latest
  captain's log) and `brain_search` (vault ripgrep). The persona names
  which tool answers which question class, so it checks before saying
  it cannot see something. Per-conversation metrics
  (`voice-metrics.jsonl`: seconds, turns, agent chars, latency,
  interruptions, tool calls) are recorded to size the planned wiring-C
  migration from real usage. 17 tests green; public repo at `a511185`.
- Addendum 2026-10-22 (completion pass, Muse): voice brain owner-ruled
  to `gemini-3.8-flash`; it IS on ElevenLabs' hosted menu (an earlier
  probe falsely rejected it - a round-tripped GET agent config carries
  both `tools` and `tool_ids` and PATCH rejects that; always apply from
  `agent-profile.json`). Wiring B is built: `llm_bridge.py` serves the
  same model from the owner's Gemini key (OpenAI-compatible, strips
  legacy sampling params), localhost-only, exposed via Tailscale
  Funnel behind a generated secret in `voice-secrets.env`;
  `apply_agent.py` switches the agent to the bridge only when it is up
  AND keyed (`brain: custom` in `voice-config.json`), else stays
  hosted. Flip to custom awaits the owner's `GEMINI_API_KEY`. If B
  proves flaky in live use, wiring C (own ASR/turn-taking, ElevenLabs
  TTS only) is the fallback; metrics keep sizing it (latency now
  measured from real mic speech-end, not transcript arrival).
  Voice kinks closed: echo gate tracks queued playback duration
  (`_play_end`) instead of timers - fixed tails leaked the greeting's
  last words through convolver drain; silent-room verified 45 s with
  zero self-transcripts. `turn_eagerness: patient` lives in the
  top-level `turn` block (a flat `conversation` block drops it).
  FLOW: compact centered status pill (CPU left, time center, battery
  icon green/amber/red + charging bolt + % right), equal sun/speaker
  in the quiet tab. The classic field math
  (`inten = _ia + lvl*0.24e + (_ib + lvl*0.36e)*height`, skip 0.03)
  is OWNER-APPROVED AND FROZEN - two retunes during this pass were
  reverted on owner verdict; do not retune without the owner watching.
  Field phases run on wall time with a first-frame snap so restarts
  rejoin the same river. Public repo is the config source of truth
  (fleet/voice/profile/secrets all config + examples, fresh-clone
  `unittest discover` green, 23 tests).
- Addendum 2026-10-22 (Pipecat voice, Muse): wirings B and C both
  failed the owner live test (ElevenLabs custom-LLM generations errored/
  timed out; Gemini free-tier 429/503 chains; hand-rolled VAD turned
  0.3 s blips into turns). Voice is now Pipecat 1.13
  (`pipecat_voice.py`, project `.venv`): custom PipeWire transports
  (`pw-record` in with duck-don't-mute echo control, `pw-cat` out to
  the convolver — no pyaudio), Silero VAD, Moonshine local STT, Gemini
  brain with all 17 fleet/local/job tools registered as function calls
  (`voice_agent.EL_TOOL_FUNCS`), ElevenLabs TTS voice Sarah
  (`EXAVITQu4vr4xnSDxMaL`, `eleven_flash_v2`, 16 kHz). Persona from
  `agent-profile.json`: short, sweet, dry wit, confirm-before-background.
  `omarchy-voice.service` ExecStart is the venv python. Debugging notes:
  Pipecat 1.13 universal aggregators emit `LLMContextAssistantTurnFrame`
  (not LLMTextFrame) after the assistant aggregator, and transcriptions
  are consumed by the user aggregator — receipts live in HeardObserver
  (post-STT) and StateObserver (post-aggregator); queue the greeting
  LLMRunFrame ~3 s after start or it is dropped. Hard quota truth:
  `gemini-3.8-flash` free tier is 20 requests/DAY — brain runs
  `gemini-3.5-flash` (`voice-config.json` `brain_model`) until the 3.8
  quota resets or billing is enabled; owner wants 3.8 back. Verified:
  greeting spoken ("Hey Bryce. What's on your mind?"), silent-room 0
  phantom transcripts, 30/30 tests both orders. Still open: owner's
  live spoken acceptance (pause-holding, barge-in, tool calls, one
  confirmed background job) — never test with a computer voice, never
  restart the voice service mid-conversation. ElevenLabs agent/bridge
  remain as fallbacks; Funnel exposure can be retired once Pipecat is
  owner-accepted.
- Addendum 2026-10-22 (config cleanup, Muse): voice config is now
  ONE file, `voice.json` {brain_models chain, tts{voice_id, model},
  first_message, prompt, tools[17]}; `pipecat_voice.py` reads only
  it. Tools/helpers extracted to `voice_tools.py` (`TOOL_FUNCS`,
  jobs, ConvMetrics, set_state, key loaders, `speaker_target()`);
  `voice_agent.py` and its session classes (Grok VoiceSession,
  ElevenLabsSession, LocalVoiceSession/GeminiBrain/VadSegmenter) are
  deleted, as are `llm_bridge.py`, `apply_agent.py`,
  `agent-profile.json`, `voice-config.json`, the
  `omarchy-llm-bridge.service` unit, the Tailscale Funnel that
  exposed the bridge, and `LLM_BRIDGE_SECRET`. Pipecat brain is a
  failover chain (BrainLLM marks itself unusable on 429/503;
  switch re-asks; RetryObserver revives+re-asks single-flight;
  15-min revive). FLOW pod gained a visible far-right chevron
  (`podclose`) that folds the pod - the corner previously closed it
  invisibly. Test hooks: `OMARCHY_VOICE_MIC`/`OMARCHY_VOICE_SPK`
  override the pw-record/pw-cat targets for virtual-mic tests.
- Addendum 2026-10-22 (cleanup + proof, Muse): the config cleanup
  above is DONE locally and in the public repo - renamed to
  brycemurad0/macbook-pro-touchbar-for-omarchy (PUBLIC). Deleted:
  host ElevenLabs agent (API 204, verified 404), Funnel, bridge unit
  + secret. Personal specifics moved to config: harness launch lines
  + local pi line (fleet.json harness_cmds/local_pi_cmd), vault path
  + Mnemos container (voice.json memory), ghost tailnet nodes
  (fleet.json hide_nodes). Tests 29/29 both orders and from a fresh
  clone. Voice PROVEN end-to-end with synthetic owner speech (the
  owner authorized self-tests; rig: null-sink + virtual-source,
  OMARCHY_VOICE_MIC): greeting, no self-talk, fleet answer, banana
  context recall, EXACT confirm-before-work ("Want me to run that?"),
  start_work on pi, spoken job report, 0.9s pause held into one
  battery answer, reply audio measured on the sink (peak 20328).
  Persona hardened: memory is a tool, not a promise. Gemma floor
  REJECTED (streams chat, tool turns die on completion timeout).
  Remaining risk is substrate, not code: Gemini free tier (3.8 =
  20 req/day) 429s and 3.1-lite 503-flaps for minutes at a time; the
  chain benches bad models (incl. timeouts) and retries patiently,
  so answers can land late at night. Enabling billing on the Gemini
  project is the durable fix (owner call). Owner spoken acceptance
  is still the finish line; service left STOPPED for their toggle.
- Addendum 2026-10-22 (MiniMax brain + confirm gate, Muse): the
  flaky Gemini-free-tier lead is replaced. Owner-provided MiniMax
  subscription key lives only in voice-secrets.env (600); chain is
  now minimax:MiniMax-M3.1-Flash -> gemini-3.8-flash ->
  gemini-3.5-flash -> gemini-3.1-flash-lite. Key probed on the
  OpenAI-compatible route (api.minimax.io/v1): auth OK and correct
  function calling with the full 17-tool schema. Full synthetic
  conversation 12/12 (greeting, fleet tool, remember + banana
  recall, spoken confirm question, launch after yes only, spoken
  job report, 0.9s pause held, reply audio on speaker). BUT:
  MiniMax ignored the prompt-level confirm protocol and launched
  start_work unconfirmed, so the protocol is now enforced in code
  (voice_tools._start_work_tool): first call only STAGES; launch
  needs the same goal called again after a new user turn
  (pipecat_voice HeardObserver -> vt.note_user_turn). 4 gate unit
  tests; suite 34/34 both orders + fresh discover. Systemd start
  verified (brain chain log + greeting), stopped clean for the
  owner's toggle. Owner spoken acceptance still the finish line.
  Machine notes: gnome-keyring-daemon crashed exactly once
  (04:44 SIGABRT, duplicate-daemon race, "Secret Service already
  initialized"); the current daemon has run 7h+ since - the hourly
  "already registered" lines are warnings, not crashes, and no
  touchbar/voice code touches Secret Service (keys are the env
  file). The actual recurring crasher was voxtype.service: binary
  /usr/bin/voxtype missing, Restart=on-failure every 5s, 11,856
  restarts - disabled --now; reinstall voxtype to revive it.
- Addendum 2026-10-22 (stack bake-off + echo/latency, Muse): owner
  live test exposed three real faults in the cascade: (1) the mic
  transcribed the agent's OWN speaker output ("heard: One sec. Yeah,
  I'm here") and it answered itself; (2) MiniMax M3.1 at default
  reasoning effort spends 1-2s / ~700 tokens thinking before the
  first word, and sometimes returns reasoning with empty content;
  (3) after tool calls it could go silent. Fixes: reasoning_effort
  "low" for minimax in build_brain (probed: first content 1.9s ->
  0.6s, zero reasoning tokens; thinking=disabled is REJECTED by the
  model - adaptive thinking is mandatory); text echo guard
  (voice_tools.is_echo + HeardObserver drops transcriptions that
  near-verbatim match what the agent said in the last 10s); work
  gate also launches when the agent already asked verbally
  (note_said markers) and the user answered in a new turn.
  Bake-off on the same synthetic rig: Pipecat+MiniMax-low 12/12
  (winner, wired); ElevenLabs hosted agent (created for the test,
  deleted after) 11/12 - fast and natural, Sarah native, natural
  verbal confirm, BUT deadlocked on the old gate (fixed since),
  transcribed mic silence as "..." and nagged "Still there?",
  per-minute pricing, brain is EL-side Gemini 2.5 Flash; Gemini
  Live direct SDK calls tools in 0.7s but via Pipecat the session
  dies (1008 aborted) - not viable today; Gemini cascade quota
  429 all day; OpenAI Realtime / xAI voice have no keys here;
  local speech-to-speech (Moshi/Ultravox on the DGX) untested -
  heavy, and loses the ElevenLabs Sarah voice. Suite 39/39.
- Addendum 2026-10-22 (dialogue purity, config-side; Muse): owner
  live test heard tool preambles ("Let me check the fleet..."),
  filler, and cut-offs mixed with real answers. A DialogueGate
  frame processor (buffer LLM text, drop pre-tool text) was built
  and passed 12/12 but never fired on the rig, and the owner
  explicitly preferred config over injected dialogue machinery -
  REMOVED. Shipped instead: (1) voice.json hard rule "Never speak
  before or between tool calls... your first spoken words are the
  result" - rig run shows zero narration in every assistant frame,
  12/12; (2) thinking is config, not code: MiniMax reasoning_effort
  "low" (reasoning_content is a separate API field and is never
  voiced by the adapter anyway); (3) audio-side only: mic duck
  0.15 -> 0.04 while she speaks (her own voice was passing VAD and
  cutting herself off) and the echo-guard clock now starts at
  BotStoppedSpeaking with a 20s window (speaker/room latency meant
  echo arrived ~12s after the text frame). If preambles return in
  live use, the gate is the fallback - it is in git history
  (built and removed 2026-10-22, never committed; rebuild from
  this description).
- Addendum 2026-10-22 (voice settled, owner live tour; Muse):
  owner talked to every stack live and SETTLED: OpenAI Realtime
  (gpt-realtime) primary, voice "cedar" (auditioned marin, coral,
  sage, verse, ash, ballad, alloy, echo, shimmer; cedar/ash got
  the long sessions, cedar picked). Gemini Live is the funded
  fallback (billing enabled): voice_daemon.py supervises backends
  and falls through on exit 42 (key/credits), order openai ->
  gemini -> elevenlabs -> pipecat cascade; "backend": "auto" in
  voice.json, per-backend model/voice under "backends". Persona
  got {{brain}} so each backend names itself truthfully (Realtime
  claimed MiniMax live), memory only when explicitly asked
  (Gemini saved unasked), opening media is a quick action never a
  job. Jobs: start_work_job was spawning locally all along (the
  agent hallucinated "dgx-spark") and my voice-swap restarts
  KILLED both owner jobs; jobs are now detached + disk-registered
  (jobs.json, announced persisted) and survive restarts. The
  toolkit-map job was relaunched detached after the fix.
  elevenlabs agent is re-rendered from voice.json each start
  (el-agent.json). Remaining known gaps: fleet_sessions misses
  some local sessions (owner caught it twice), Spruce/Glean are
  ChatGPT-app voices - the Realtime API rejects them.
