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
        d.st._remote_agents = [{"kind": "pi", "node": "bob", "node_name": "Bob",
                                "detail": "pi - Bob", "elapsed": 60, "active": False,
                                "percent": None, "pid": 0, "remote": True,
                                "window": None, "tmux": "work-session"}]
        # dead pid -> viewer closed; ownership/session from runtime record
        d.act._launched[("bob", "work-session")] = {
            "pid": 424242, "owned": True, "session": "work-session"}
        CALLS.clear()
        d.act.reap_closed()
        self.assertNotIn(("bob", "work-session"), d.act._launched)
        self.assertTrue(any("kill-session" in " ".join(c) and "work-session" in " ".join(c)
                            for c in CALLS if c[0] == "ssh"), f"expected ssh tmux kill, got {CALLS}")
        self.assertEqual(d.st._remote_agents, [], "card source must be removed at once")

    def test_closed_existing_session_is_never_killed(self):
        d = make_daemon()
        d.act._launched[("bob", "work-session")] = {
            "pid": 424242, "owned": False, "session": "work-session"}
        CALLS.clear()
        d.act.reap_closed()
        self.assertFalse(any(c[0] == "ssh" for c in CALLS), CALLS)

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
                             {"name": "Bob", "host": "bob", "ssh": "bob",
                              "online": True, "os": "linux", "ip": ""}]
        hits = render(d, "dev")
        datas = [h[5].get("name") for h in hits if h[4] == "node"]
        self.assertIn("Bob", datas)
        self.assertNotIn("Atlas", datas, "offline node must not be a launch target")

class GhostNode(unittest.TestCase):
    def test_hidden_ghost_peer_never_listed(self):
        # a retired tailnet entry for this same hardware (its
        # pre-Linux macOS node) must not show up as a dead machine
        import json as _json
        d = make_daemon()
        payload = {"Self": {"HostName": "fred",
                            "TailscaleIPs": ["100.64.0.1"]},
                   "Peer": {
                       "a": {"HostName": "Fred's MacBook Pro",
                             "DNSName": "freds-macbook-pro.tail.example.",
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
        tb.HIDE_NODES = {"freds-macbook-pro"}
        try:
            d.st._scan_fleet()
        finally:
            tb.subprocess.run, tb.HIDE_NODES = orig_run, orig_hide
        names = [e["name"].lower() for e in d.st.nodes_detail]
        self.assertNotIn("fred's macbook pro", names)
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


class PodVoice(unittest.TestCase):
    def test_pod_has_voice_button_that_toggles(self):
        d = make_daemon()
        d.renderer.pod_open = True
        hits = render(d, "flow")
        h = find(hits, "media", ("voice", None))
        self.assertIsNotNone(h, "open pod must still offer the voice toggle")
        CALLS.clear(); d.st.voice_live = False
        tap(d, h)
        svc = [c for c in CALLS if "omarchy-voice.service" in c]
        self.assertTrue(svc and "start" in svc[0], f"voice button must toggle service, got {svc}")

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
        # the owner-approved field: smooth gradations across the glyph
        # ramp (not a binary on/off field), and weighting that scales
        # with energy. This pins the approved look; retune only with
        # the owner watching.
        import cairo

        def probe(energy):
            st = tb.State()
            st.energy = energy
            r = tb.Renderer(tb.Theme(), st)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, tb.W, tb.H)
            r.view_flow(cairo.Context(surf), [], time.monotonic(), {})
            levels = {min(int(v * 12), 11) for v in r._smooth if v >= 0.03}
            return levels, max(r._smooth)

        levels, idle_max = probe(0.0)
        self.assertGreaterEqual(len(levels), 4,
                                "field must shade across glyph weights")
        _, full_max = probe(1.0)
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
