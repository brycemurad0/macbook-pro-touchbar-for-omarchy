#!/usr/bin/env python3
"""Focused regression tests for omarchy-touchbar daemon behavior.
Run: python3 -m unittest test_touchbar -v (from this directory).
Covers: rail-tab hit priority over scrolled content, LIVE two-tap
arm/confirm, and ssh-terminal close reaping its remote session."""
import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import touchbar as tb

CALLS = []
class FakePopen:
    def __init__(self, argv, **kw):
        CALLS.append([str(a) for a in argv]); self.pid = 424242
class FakeUI:
    def __init__(self): self.keys = []
    def tap(self, code): self.keys.append(code)

def make_daemon():
    tb.subprocess.Popen = FakePopen
    d = tb.Daemon(display=None, snapshot_only=True)
    d.act.ui = FakeUI()
    return d

def render(d, view):
    d.view = view
    d.renderer.dev_state = (d.dev_step, d.dev_node, d.dev_harness)
    d.renderer.live_sel = d.live_sel
    hits, ms = d.renderer.render(d.logical, view, time.monotonic(), d.pressed, int(d.scroll))
    d.hits, d.max_scroll = hits, ms
    return hits

def find(hits, kind, data=None):
    for h in hits:
        if h[4] == kind and (data is None or h[5] == data): return h
def tap(d, h):
    x, y = h[0] + h[2] / 2, h[1] + h[3] / 2
    d._touch_down(x, y); d._touch_up(x, y)

class RailPriority(unittest.TestCase):
    def test_rail_wins_over_scrolled_live_card(self):
        d = make_daemon()
        d.st.agents = [{"kind": "pi", "glyph": "x", "label": "work", "detail": "pi - work",
                        "elapsed": 300, "active": True, "percent": None, "pid": 111,
                        "model": "m", "window": "0xW1"}]
        d.st.clients = []
        d.scroll = 100.0  # card slid left, partially underneath the rail
        hits = render(d, "live")
        self.assertTrue(any(h[4] == "agent" and h[0] < tb.RAIL_W for h in hits),
                        "test setup: a card must overlap the rail")
        flow = find(render(d, "live"), "view", "flow")
        # re-render scrolled (render resets nothing; hits rebuilt above)
        hits = render(d, "live")
        flow = find(hits, "view", "flow")
        tap(d, flow)
        self.assertEqual(d.view, "flow", "FLOW tap must switch view, not open the session behind the rail")

class TwoTap(unittest.TestCase):
    def test_first_tap_arms_second_confirms(self):
        d = make_daemon()
        d.st.clients = [{"address": "0xW2", "class": "foot", "title": "t", "pid": 42,
                         "workspace": {"id": 2}, "mapped": True}]
        d.st.agents = []
        CALLS.clear()
        tap(d, find(render(d, "live"), "win", "0xW2"))
        self.assertEqual([c for c in CALLS if "hyprctl" in c[0]], [], "first tap must not focus")
        self.assertEqual(d.live_sel, ("win", "0xW2"))
        tap(d, find(render(d, "live"), "win", "0xW2"))
        self.assertTrue(any("0xW2" in " ".join(c) for c in CALLS if "hyprctl" in c[0]),
                        "second tap on same card must focus it")

class ReapClosed(unittest.TestCase):
    def test_closed_owned_terminal_kills_exact_session(self):
        d = make_daemon()
        d.st._remote_agents = [{"kind": "pi", "node": "node-b", "node_name": "Node B",
                                "detail": "pi - Bob", "elapsed": 60, "active": False,
                                "percent": None, "pid": 0, "remote": True,
                                "window": None, "tmux": "work-session"}]
        # dead pid -> viewer closed; ownership/session from runtime record
        d.act._launched[("node-b", "work-session")] = {
            "pid": 424242, "owned": True, "session": "work-session"}
        CALLS.clear()
        d.act.reap_closed()
        self.assertNotIn(("node-b", "work-session"), d.act._launched)
        self.assertTrue(any("kill-session" in " ".join(c) and "work-session" in " ".join(c)
                            for c in CALLS if c[0] == "ssh"), f"expected ssh tmux kill, got {CALLS}")
        self.assertEqual(d.st._remote_agents, [], "card source must be removed at once")

    def test_closed_owned_terminal_leaves_quick_resume(self):
        # Owner 2026-10-22: closing an ssh viewer is fine, but FLOW
        # must offer a one-tap resume for it afterwards.
        d = make_daemon()
        d.act._launched[("node-b", "pi")] = {
            "pid": 424242, "owned": True, "session": "tb-pi",
            "node_name": "Node B"}
        d.act.reap_closed()
        self.assertEqual(len(d.st.closed_resumes), 1)
        e = d.st.closed_resumes[0]
        self.assertEqual((e["alias"], e["harness"], e["node_name"]),
                         ("node-b", "pi", "Node B"))

    def test_closed_existing_session_is_never_killed(self):
        d = make_daemon()
        d.act._launched[("node-b", "work-session")] = {
            "pid": 424242, "owned": False, "session": "work-session"}
        CALLS.clear()
        d.act.reap_closed()
        self.assertFalse(any(c[0] == "ssh" for c in CALLS), CALLS)

class FlowResumeAndNotify(unittest.TestCase):
    def _entry(self):
        return {"alias": "node-b", "node_name": "Node B", "harness": "pi",
                "tmux": "tb-pi", "closed_at": time.time()}

    def test_resume_pill_docked_left_and_tap_relaunches(self):
        d = make_daemon()
        d.st.closed_resumes = [self._entry()]
        hits = render(d, "flow")
        h = find(hits, "resume")
        self.assertIsNotNone(h, "closed ssh session must offer a resume pill")
        self.assertLess(h[0], tb.W // 3, "resume pill docks left, never mid-field")
        CALLS.clear()
        tap(d, h)
        self.assertEqual(d.st.closed_resumes, [], "resume consumes its entry")
        self.assertTrue(any(c[0] == "foot" for c in CALLS),
                        f"tap must relaunch a terminal, got {CALLS}")

    def test_notification_docks_left_and_chips_stay(self):
        # Owner 2026-10-22: no notification overlay in the middle of
        # FLOW; the center status chips stay on screen.
        d = make_daemon()
        d.st.closed_resumes = [self._entry()]
        d.renderer.add_flash("bright_green", "pi \u00b7 work done",
                             node="node-b", harness="pi")
        hits = render(d, "flow")
        h = find(hits, "notify")
        self.assertIsNotNone(h)
        self.assertLess(h[0], tb.W // 3, "FLOW notification must dock left")
        self.assertIsNotNone(find(hits, "view", "fleet"),
                             "center status chips must stay while notifying")
        self.assertIsNone(find(hits, "resume"),
                          "notification takes the dock while it is live")

class FlowTouch(unittest.TestCase):
    def test_ripple_answers_then_expires(self):
        d = make_daemon()
        r = d.renderer
        t0 = time.monotonic()
        self.assertEqual(r._touch_wave(1000, 30, t0), 0.0)
        r.add_ripple(1000, 30)
        self.assertNotEqual(r._touch_wave(1090, 30, t0 + 0.3), 0.0,
                            "expanding ring must move water at its radius")
        self.assertEqual(r._touch_wave(1090, 30, t0 + 3.0), 0.0,
                         "ripple must expire")
        r.set_touch(500, 30)
        self.assertNotEqual(r._touch_wave(560, 30, time.monotonic()), 0.0,
                            "finger must raise a standing wave")

class DevOffline(unittest.TestCase):
    def test_offline_node_shown_but_not_launchable(self):
        d = make_daemon()
        d.st.nodes_detail = [{"name": "Atlas", "host": "atlas", "ssh": "atlas",
                              "online": False, "os": "macOS", "ip": ""},
                             {"name": "Node B", "host": "node-b", "ssh": "node-b",
                              "online": True, "os": "linux", "ip": ""}]
        hits = render(d, "dev")
        datas = [h[5].get("name") for h in hits if h[4] == "node"]
        self.assertIn("Node B", datas)
        self.assertNotIn("Atlas", datas, "offline node must not be a launch target")

    def test_available_hosts_read_left_to_right_offline_last(self):
        # Owner rule 2026-10-22: selectable hosts in fleet order from
        # the left; offline hosts pinned right, never interleaved.
        d = make_daemon()
        d.st.nodes_detail = [
            {"name": "Off A", "host": "a", "ssh": "a", "online": False, "os": "", "ip": ""},
            {"name": "Node B", "host": "b", "ssh": "b", "online": True, "os": "", "ip": ""},
            {"name": "Off C", "host": "c", "ssh": "c", "online": False, "os": "", "ip": ""},
            {"name": "Node D", "host": "d", "ssh": "d", "online": True, "os": "", "ip": ""}]
        hits = render(d, "dev")
        nodes = sorted((h for h in hits if h[4] == "node"), key=lambda h: h[0])
        self.assertEqual([h[5].get("name") for h in nodes],
                         ["LOCAL", "Node B", "Node D"])
        online, offline = tb.Renderer._order_hosts(d.st.nodes_detail)
        self.assertEqual([e["name"] for e in online], ["Node B", "Node D"])
        self.assertEqual([e["name"] for e in offline], ["Off A", "Off C"])

class FleetOnlineTruth(unittest.TestCase):
    def test_ssh_proof_overrides_tailscale_offline_flag(self):
        # tailscale said Jarvis was offline while it answered ssh
        # (2026-10-22): a fresh ssh proof must win over the flag.
        d = make_daemon()
        now = time.monotonic()
        self.assertFalse(d.st._fleet_online("jarvis", False))
        self.assertTrue(d.st._fleet_online("jarvis", True))
        d.st._ssh_ok_until["jarvis"] = now + 150
        self.assertTrue(d.st._fleet_online("jarvis", False))
        d.st._ssh_ok_until["jarvis"] = now - 1  # stale proof expires
        self.assertFalse(d.st._fleet_online("jarvis", False))

    def test_remote_scan_probes_offline_flagged_nodes(self):
        # the scan is the probe: an offline-flagged node must still be
        # scanned (with the usual backoff), or it can never recover
        d = make_daemon()
        d.st.nodes_detail = [{"name": "Jarvis", "host": "jarvis",
                              "ssh": "jarvis", "online": False,
                              "os": "macOS", "ip": ""}]
        seen = []
        d.st._scan_one_node = lambda e: (seen.append(e["ssh"]) or (e, "", False))
        d.st._scan_remote_agents()
        self.assertEqual(seen, ["jarvis"])
        self.assertTrue(d.st._fleet_online("jarvis", False),
                        "a successful scan is reachability proof")

class GhostNode(unittest.TestCase):
    def test_hidden_ghost_peer_never_listed(self):
        # a retired tailnet entry for this same hardware (its
        # pre-Linux macOS node) must not show up as a dead machine
        import json as _json
        d = make_daemon()
        payload = {"Self": {"HostName": "thismac",
                            "TailscaleIPs": ["100.64.0.1"]},
                   "Peer": {
                       "a": {"HostName": "Old MacBook Pro",
                             "DNSName": "old-macbook-pro.tail.example.",
                             "OS": "macOS", "Online": False,
                             "TailscaleIPs": ["100.64.0.9"]},
                       "b": {"HostName": "bob",
                             "DNSName": "bob.tail.example.",
                             "OS": "linux", "Online": True,
                             "TailscaleIPs": ["100.64.0.2"]}}}

        class R:
            stdout = _json.dumps(payload)

        orig_run, orig_hide = tb.subprocess.run, tb.HIDE_NODES
        tb.subprocess.run = lambda *a, **k: R()
        tb.HIDE_NODES = {"old-macbook-pro"}
        try:
            d.st._scan_fleet()
        finally:
            tb.subprocess.run, tb.HIDE_NODES = orig_run, orig_hide
        names = [e["name"].lower() for e in d.st.nodes_detail]
        self.assertNotIn("old macbook pro", names)
        self.assertIn("bob", names)


class PodClose(unittest.TestCase):
    def test_pod_back_button_far_right_folds_pod(self):
        d = make_daemon()
        d.renderer.pod_open = True
        hits = render(d, "flow")
        h = find(hits, "podclose")
        self.assertIsNotNone(h, "open pod must draw a back button at the far right")
        self.assertGreaterEqual(h[0] + h[2], tb.W - 16,
                                "back button must sit at the pod's far right edge")
        self.assertGreater(h[0], tb.W - 120)
        d.pod_open_until = time.monotonic() + 60
        tap(d, h)
        self.assertLessEqual(d.pod_open_until, time.monotonic() + 1,
                             "back button must fold the pod away")


class PodDictation(unittest.TestCase):
    def test_pod_mic_is_dictation_not_agent_toggle(self):
        # Owner 2026-10-22: the control-pod mic is push-to-talk
        # dictation into the focused field (voxtype), never a second
        # realtime-voice-agent toggle (that keeps its FLOW switch).
        d = make_daemon()
        d.renderer.pod_open = True
        hits = render(d, "flow")
        h = find(hits, "media", ("dictate", None))
        self.assertIsNotNone(h, "open pod must offer the dictation toggle")
        self.assertIsNone(find(hits, "media", ("voice", None)))
        CALLS.clear()
        tap(d, h)
        vox = [c for c in CALLS if c[:3] == ["voxtype", "record", "toggle"]]
        self.assertTrue(vox, f"mic tap must toggle voxtype, got {CALLS}")
        self.assertFalse(any("omarchy-voice.service" in c for c in CALLS),
                         "pod mic must not touch the realtime agent")

    def test_dictate_button_brightness_follows_voice(self):
        # Owner 2026-10-22: the toggle lights up while he is speaking
        # (mic level -> button brightness), not a static recording lamp.
        d = make_daemon()
        d.renderer.pod_open = True
        d.st.dictation = "recording"
        def mean_brightness(lvl):
            d.st.mic_level = lvl
            hits = render(d, "flow")
            h = find(hits, "media", ("dictate", None))
            self.assertIsNotNone(h)
            data = d.logical.get_data(); stride = d.logical.get_stride()
            tot = cnt = 0
            for yy in range(13, 48, 3):
                row = yy * stride
                for xx in range(int(h[0]) + 6, int(h[0] + h[2]) - 6, 3):
                    o = row + xx * 4  # ARGB32 little-endian: B,G,R,A
                    tot += data[o] + data[o + 1] + data[o + 2]; cnt += 3
            return tot / max(cnt, 1)
        self.assertGreater(mean_brightness(1.0), mean_brightness(0.0) + 10,
                           "speaking loudly must visibly flare the toggle")

class NotificationDismiss(unittest.TestCase):
    def test_tap_on_field_around_notification_dismisses_it(self):
        d = make_daemon()
        d.view = "flow"
        d.renderer.add_flash("bright_green", "pi - work done", window="0xWIN9", pid=None)
        render(d, "flow")
        self.assertIsNotNone(d.renderer._notif_rect)
        # well left of the pill, on the ASCII field itself
        x, y = tb.RAIL_W + 30, 30
        d._touch_down(x, y)
        self.assertIsNotNone(d.renderer.notification, "press alone must not dismiss")
        d._touch_up(x, y)
        self.assertIsNone(d.renderer.notification)
        self.assertIsNone(d.renderer._notif_rect)

class FlowChips(unittest.TestCase):
    def test_batt_color_thresholds(self):
        bc = tb.Renderer.batt_color
        self.assertEqual(bc(12, True), (0.35, 0.85, 0.45))   # charging wins
        self.assertEqual(bc(20, False), (0.95, 0.36, 0.33))  # red <= 20
        self.assertEqual(bc(21, False), (0.98, 0.76, 0.28))  # amber
        self.assertEqual(bc(50, False), (0.98, 0.76, 0.28))  # amber at 50
        self.assertEqual(bc(51, False), (0.35, 0.85, 0.45))  # green above

    def test_flow_renders_with_and_without_battery(self):
        d = make_daemon()
        for batt, chg in ((86, False), (12, True), (-1, False)):
            d.st.battery, d.st.charging = batt, chg
            hits = render(d, "flow")
            pill = find(hits, "view", "fleet")
            self.assertIsNotNone(pill)
            # compact and centered: well inside the open field, and
            # far narrower than it (the ASCII art must show around it)
            zone = tb.W - 140 - (tb.RAIL_W + 12)
            self.assertLess(pill[2], zone * 0.45)
            self.assertGreater(pill[0], tb.RAIL_W + 100)

    def test_flow_handle_hit_present(self):
        d = make_daemon()
        hits = render(d, "flow")
        self.assertIsNotNone(find(hits, "podopen"))

    def test_flow_field_continuous_across_restart(self):
        # two fresh renderers (a restart) must produce the same field:
        # phases are wall-time based and the first frame snaps, so the
        # owner-approved river does not reset to a bare dot grid
        import cairo
        grids = []
        for _ in range(2):
            r = tb.Renderer(tb.Theme(), tb.State())
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, tb.W, tb.H)
            ctx = cairo.Context(surf)
            r.view_flow(ctx, [], time.monotonic(), {})
            grids.append(list(r._smooth))
        diffs = [abs(a - b) for a, b in zip(*grids)]
        self.assertTrue(any(v > 0.03 for v in grids[0]),
                        "field must be developed on the first frame")
        self.assertLess(max(diffs), 0.05)

    def test_flow_weightings_vary_and_scale(self):
        # the owner-approved field (retuned 2026-10-22 at the owner's
        # direction): mostly dead space at rest — only wave crests
        # render — and as energy (fleet agent work) rises the waves
        # fill in across the glyph ramp. Retune only with the owner
        # watching.
        import cairo

        def probe(energy):
            st = tb.State()
            st.energy = energy
            r = tb.Renderer(tb.Theme(), st)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, tb.W, tb.H)
            r.view_flow(cairo.Context(surf), [], time.monotonic(), {})
            lit = [v for v in r._smooth if v >= 0.11]
            levels = {min(int(v * 12), 11) for v in lit}
            return lit, levels, max(r._smooth)

        idle_lit, _, idle_max = probe(0.0)
        self.assertLess(len(idle_lit) / len(tb.Renderer(tb.Theme(),
                        tb.State())._smooth), 0.35,
                        "idle field must be mostly dead space")
        full_lit, full_levels, full_max = probe(1.0)
        self.assertGreater(len(full_lit), len(idle_lit) * 1.6,
                           "coverage must grow with energy")
        self.assertGreaterEqual(len(full_levels), 4,
                                "busy field must shade across glyph weights")
        self.assertGreater(full_max, idle_max * 1.8)


class RemoteSessions(unittest.TestCase):
    def test_only_tmux_hosted_harnesses_are_sessions(self):
        e = {"name": "Node A", "ssh": "node-a"}
        out = "\n".join([
            "100 1 5000 bash",
            "101 100 4900 python /home/x/.hermes/hermes-agent/hermes",
            "200 1 9000 python /home/x/.hermes/hermes-agent/hermes",
            "__TBSESS__",
            "tb-pi 1",
            "__TBPANES__",
            "100 tb-pi",
        ])
        agents, attached = tb.State._parse_remote(e, out)
        self.assertEqual(len(agents), 1, agents)
        self.assertEqual(agents[0]["tmux"], "tb-pi")
        self.assertEqual(attached, {"tb-pi": 1})

if __name__ == "__main__":
    unittest.main()


class FlowFrozen(unittest.TestCase):
    def test_flow_owner_frozen(self):
        # Owner froze this look 2026-10-22 after repeated retunes:
        # slow streaming waves, dead space at rest, density follows
        # fleet agent work. Changing these numbers requires the
        # owner watching — update this pin in the same commit as an
        # owner-approved retune, never silently.
        self.assertEqual(tb.FLOW_PRESETS["classic"], {
            "ramp": " ·:∙~+=×*#%@", "fsize": 11, "spiral_w": 0.0,
            "spiral_spd": (0.05, 0.08), "inten": (-0.06, 0.95),
            "smooth": 0.08, "current": 0.16, "levels": 1.0})
