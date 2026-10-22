import json
import os
import struct
import subprocess
import time
import unittest

import voice_agent as va

# captured before test_touchbar swaps subprocess.Popen for its fake
_REAL_POPEN = subprocess.Popen

BASE = os.path.dirname(os.path.abspath(__file__))


class GainTests(unittest.TestCase):
    def test_gain_boosts_and_clamps(self):
        raw = struct.pack("<4h", 100, -100, 30000, -30000)
        out = struct.unpack("<4h", va._gain_pcm(raw, 2.2))
        self.assertEqual(out[0], 220)
        self.assertEqual(out[1], -220)
        self.assertEqual(out[2], 32767)
        self.assertEqual(out[3], -32768)

    def test_gain_duck_attenuates(self):
        raw = struct.pack("<2h", 1000, -1000)
        out = struct.unpack("<2h", va._gain_pcm(raw, 0.12))
        self.assertEqual(out, (120, -120))

    def test_gain_passthrough_at_unity(self):
        raw = struct.pack("<2h", 5, -5)
        self.assertEqual(va._gain_pcm(raw, 1.0), raw)


class JobTests(unittest.TestCase):
    def setUp(self):
        subprocess.Popen = _REAL_POPEN

    def test_job_lifecycle(self):
        va.HARNESS_CMDS["t-ok"] = ["true"]
        va.HARNESS_CMDS["t-bad"] = ["false"]
        ok = va.start_work_job("test goal ok", "t-ok")
        bad = va.start_work_job("test goal bad", "t-bad")
        deadline = time.time() + 10
        while time.time() < deadline:
            states = va.job_states()
            if all(states[j]["status"] != "running" for j in (ok, bad)):
                break
            time.sleep(0.1)
        states = va.job_states()
        self.assertEqual(states[ok]["status"], "done")
        self.assertEqual(states[bad]["status"], "failed")
        finished = dict(va.pop_finished_jobs())
        self.assertIn(ok, finished)
        self.assertEqual(va.pop_finished_jobs(), [])  # announced once

    def test_start_work_requires_goal(self):
        self.assertEqual(va._start_work_tool({}), "no goal given")
        self.assertEqual(va._start_work_tool({"goal": "  "}),
                         "no goal given")


class MetricsTests(unittest.TestCase):
    def test_summary_math(self):
        m = va.ConvMetrics()
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


class ProfileTests(unittest.TestCase):
    def test_profile_shape_and_persona_rules(self):
        path = os.path.join(BASE, "agent-profile.json")
        if not os.path.exists(path):  # fresh clone: only the example ships
            path = os.path.join(BASE, "agent-profile.example.json")
        profile = json.load(open(path))
        self.assertIn("flash", profile["llm"])
        # turn settings live in their own block (a flat "conversation"
        # block silently drops them server-side); patient eagerness is
        # what stops it grabbing the turn during a mid-sentence pause
        self.assertEqual(profile["turn"]["turn_eagerness"], "patient")
        self.assertNotIn("turn_timeout", profile["conversation"])
        prompt = profile["prompt"].lower()
        for rule in ("never monologue", "want me to run that",
                     "start_work", "one or two sentences"):
            self.assertIn(rule, prompt)
        names = [t["name"] for t in profile["tools"]]
        self.assertEqual(names, ["fleet_status", "run_shell",
                                 "focus_window", "open_url", "start_work",
                                 "work_status", "fleet_sessions",
                                 "node_status", "node_shell", "daily_brief",
                                 "brain_search", "memory_search", "remember",
                                 "clipboard", "notify", "remind", "media"])
        self.assertEqual(set(names), set(va.EL_TOOL_FUNCS))

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
            self.assertEqual(va.tool_remember("the sky test fact"),
                             "remembered")
        finally:
            u.urlopen = orig
        self.assertTrue(seen["url"].endswith("/store"))
        self.assertEqual(seen["body"]["raw_text"], "the sky test fact")
        self.assertTrue(seen["auth"].startswith("Bearer "))
        self.assertGreater(len(seen["auth"]), 12)

    def test_node_resolution(self):
        self.assertIsNone(va._resolve_node("here"))
        self.assertIsNone(va._resolve_node(""))
        self.assertEqual(va._resolve_node("no-such-node-xyz"), "unknown")
        node = va._resolve_node("bob")
        if node == "unknown":
            self.skipTest("fleet not enumerable in this environment")
        self.assertIn(node["display"].lower(), ("bob",))


if __name__ == "__main__":
    unittest.main()
