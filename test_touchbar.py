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
