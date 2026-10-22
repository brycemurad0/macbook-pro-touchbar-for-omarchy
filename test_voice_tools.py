import json
import os
import struct
import subprocess
import time
import unittest

import voice_tools as vt

# captured before test_touchbar swaps subprocess.Popen for its fake
_REAL_POPEN = subprocess.Popen

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_voice_config():
    path = os.path.join(BASE, "voice.json")
    if not os.path.exists(path):  # fresh clone: only the example ships
        path = os.path.join(BASE, "voice.example.json")
    return json.load(open(path))


class GainTests(unittest.TestCase):
    def test_gain_boosts_and_clamps(self):
        raw = struct.pack("<4h", 100, -100, 30000, -30000)
        out = struct.unpack("<4h", vt._gain_pcm(raw, 2.2))
        self.assertEqual(out[0], 220)
        self.assertEqual(out[1], -220)
        self.assertEqual(out[2], 32767)
        self.assertEqual(out[3], -32768)

    def test_gain_duck_attenuates(self):
        raw = struct.pack("<2h", 1000, -1000)
        out = struct.unpack("<2h", vt._gain_pcm(raw, 0.12))
        self.assertEqual(out, (120, -120))

    def test_gain_passthrough_at_unity(self):
        raw = struct.pack("<2h", 5, -5)
        self.assertEqual(vt._gain_pcm(raw, 1.0), raw)


class JobTests(unittest.TestCase):
    def setUp(self):
        subprocess.Popen = _REAL_POPEN

    def test_job_lifecycle(self):
        vt.HARNESS_CMDS["t-ok"] = ["true"]
        vt.HARNESS_CMDS["t-bad"] = ["false"]
        ok = vt.start_work_job("test goal ok", "t-ok")
        bad = vt.start_work_job("test goal bad", "t-bad")
        deadline = time.time() + 10
        while time.time() < deadline:
            states = vt.job_states()
            if all(states[j]["status"] != "running" for j in (ok, bad)):
                break
            time.sleep(0.1)
        states = vt.job_states()
        self.assertEqual(states[ok]["status"], "done")
        self.assertEqual(states[bad]["status"], "failed")
        finished = dict(vt.pop_finished_jobs())
        self.assertIn(ok, finished)
        self.assertEqual(vt.pop_finished_jobs(), [])  # announced once

    def test_start_work_requires_goal(self):
        self.assertEqual(vt._start_work_tool({}), "no goal given")
        self.assertEqual(vt._start_work_tool({"goal": "  "}),
                         "no goal given")


class MetricsTests(unittest.TestCase):
    def test_summary_math(self):
        m = vt.ConvMetrics()
        m.note_speech()  # mic energy while the user was talking
        m.user_spoke("what is the battery status")
        m.first_audio()
        m.agent_replied("Eighty percent.")
        m.interruptions += 1
        s = m.summary()
        self.assertEqual(s["user_turns"], 1)
        self.assertEqual(s["user_words"], 5)
        self.assertEqual(s["agent_chars"], 15)
        self.assertEqual(s["interruptions"], 1)
        self.assertIsNotNone(s["latency_p50"])
        self.assertEqual(m.latencies, m.latencies[:1])
        m.first_audio()  # no pending user event: no double count
        self.assertEqual(len(m.latencies), 1)


class VoiceConfigTests(unittest.TestCase):
    """voice.json is the single voice config: brain chain, ElevenLabs
    voice, persona prompt and the tool surface are contract."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = _load_voice_config()

    def test_single_config_shape(self):
        self.assertTrue(self.cfg["brain_models"])
        self.assertTrue(all("flash" in m
                            for m in self.cfg["brain_models"]))
        self.assertTrue(self.cfg["tts"]["voice_id"])
        self.assertTrue(self.cfg["tts"]["model"])
        self.assertTrue(self.cfg["first_message"].strip())

    def test_persona_rules_survive(self):
        prompt = self.cfg["prompt"].lower()
        for rule in ("never monologue", "want me to run that",
                     "start_work", "one or two sentences",
                     "short", "dry", "wit"):
            self.assertIn(rule, prompt)
        for slot in ("{{node}}", "{{time}}", "{{fleet}}"):
            self.assertIn(slot, self.cfg["prompt"])

    def test_tool_surface_matches_impls(self):
        names = [t["name"] for t in self.cfg["tools"]]
        self.assertEqual(names, ["fleet_status", "run_shell",
                                 "focus_window", "open_url", "start_work",
                                 "work_status", "fleet_sessions",
                                 "node_status", "node_shell", "daily_brief",
                                 "brain_search", "memory_search", "remember",
                                 "clipboard", "notify", "remind", "media"])
        self.assertEqual(set(names), set(vt.TOOL_FUNCS))

    def test_tool_schemas_are_gemini_shaped(self):
        for t in self.cfg["tools"]:
            params = t.get("parameters") or {}
            for prop, spec in (params.get("properties") or {}).items():
                self.assertIn("type", spec, f"{t['name']}.{prop}")
            for req in params.get("required") or []:
                self.assertIn(req, params.get("properties") or {})

    def test_remember_posts_to_mnemos(self):
        import io
        import urllib.request as u
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig = u.urlopen

        def fake(req, timeout=0):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data)
            seen["auth"] = req.headers.get("Authorization", "")
            return Resp(b"{}")

        u.urlopen = fake
        try:
            self.assertEqual(vt.tool_remember("the sky test fact"),
                             "remembered")
        finally:
            u.urlopen = orig
        self.assertTrue(seen["url"].endswith("/store"))
        self.assertEqual(seen["body"]["raw_text"], "the sky test fact")
        self.assertTrue(seen["auth"].startswith("Bearer "))
        self.assertGreater(len(seen["auth"]), 12)

    def test_hidden_ghost_node_skipped(self):
        payload = {"Self": {"HostName": "fred",
                            "DNSName": "fred.tail.example.",
                            "OS": "linux", "Online": True},
                   "Peer": {
                       "a": {"HostName": "Fred's MacBook Pro",
                             "DNSName": "freds-macbook-pro.tail.example.",
                             "OS": "macOS", "Online": False},
                       "b": {"HostName": "bob",
                             "DNSName": "bob.tail.example.",
                             "OS": "linux", "Online": True}}}

        class R:
            stdout = json.dumps(payload)

        orig_run, orig_cfg = vt.subprocess.run, vt._fleet_cfg
        vt.subprocess.run = lambda *a, **k: R()
        vt._fleet_cfg = lambda: {"hide_nodes": ["freds-macbook-pro"]}
        try:
            labels = [n["label"] for n in vt._fleet_nodes()]
        finally:
            vt.subprocess.run, vt._fleet_cfg = orig_run, orig_cfg
        self.assertNotIn("freds-macbook-pro", labels)
        self.assertIn("bob", labels)

    def test_node_resolution(self):
        self.assertIsNone(vt._resolve_node("here"))
        self.assertIsNone(vt._resolve_node(""))
        self.assertEqual(vt._resolve_node("no-such-node-xyz"), "unknown")
        node = vt._resolve_node("bob")
        if node == "unknown":
            self.skipTest("fleet not enumerable in this environment")
        self.assertIn(node["display"].lower(), ("bob",))


if __name__ == "__main__":
    unittest.main()
