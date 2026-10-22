import json
import os
import struct
import time
import unittest

import voice_agent as va

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


class ProfileTests(unittest.TestCase):
    def test_profile_shape_and_persona_rules(self):
        profile = json.load(open(os.path.join(BASE, "agent-profile.json")))
        self.assertIn("flash", profile["llm"])
        prompt = profile["prompt"].lower()
        for rule in ("never monologue", "want me to run that",
                     "start_work", "one or two sentences"):
            self.assertIn(rule, prompt)
        names = [t["name"] for t in profile["tools"]]
        self.assertEqual(names, ["fleet_status", "run_shell",
                                 "focus_window", "open_url", "start_work",
                                 "work_status"])
        self.assertEqual(set(names), set(va.EL_TOOL_FUNCS))


if __name__ == "__main__":
    unittest.main()
