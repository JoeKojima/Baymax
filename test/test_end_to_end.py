"""
End-to-end scenarios on realtime_gemini_10 with real components (only Gemini
and the robot's camera/mic/speaker are faked). Each runs in its own process so
its module setup can't affect other tests.

  identity   — always runs (needs google-genai, numpy, opencv)
  session    — always runs: Gemini session compression/resumption/GoAway handover
               against a fake Live server
  presence   — real MediaPipe pose model; needs mediapipe + EMBER_FACE_TEST_DATA
  memory     — the whole people-awareness + memory process with the real face
               models, the robot's SemanticEmbedder (ONNX all-MiniLM-L6-v2) and
               ChromaDB; needs EMBER_FACE_TEST_DATA and
               Baymax-main/all-MiniLM-L6-v2-onnx (python3 convert_model.py)

EMBER_FACE_TEST_DATA is a folder of face photos with one sub-folder per person,
e.g. the public LFW benchmark (http://vis-www.cs.umass.edu/lfw/).

    EMBER_FACE_TEST_DATA=/path/to/lfw_funneled python3 -m unittest test/test_end_to_end.py -v
"""
import importlib.util
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("EMBER_FACE_TEST_DATA")
EMBEDDING_MODEL = os.path.join(HERE, "..", "Baymax-main", "all-MiniLM-L6-v2-onnx", "model.onnx")


def _have(*modules):
    return all(importlib.util.find_spec(m) is not None for m in modules)


class EndToEnd(unittest.TestCase):
    def run_scenario(self, name):
        run = subprocess.run([sys.executable, os.path.join(HERE, name)],
                             capture_output=True, text=True, timeout=900)
        steps = [ln for ln in run.stdout.splitlines()
                 if ln[:3].strip().isdigit() and " " in ln[:3]]
        if steps:
            print("\n" + "\n".join(steps))
        self.assertEqual(run.returncode, 0, run.stdout[-4000:] + run.stderr[-4000:])
        self.assertIn("PASS", run.stdout)

    @unittest.skipUnless(_have("google.genai", "numpy", "cv2", "dotenv"), "missing core packages")
    def test_identity(self):
        self.run_scenario("e2e_identity_scenario.py")

    @unittest.skipUnless(_have("google.genai", "numpy", "cv2", "dotenv"), "missing core packages")
    def test_gemini_session_continuity(self):
        self.run_scenario("e2e_session_scenario.py")

    @unittest.skipUnless(_have("google.genai", "numpy", "cv2", "dotenv"), "missing core packages")
    def test_speed_changes_keep_behaviour(self):
        self.run_scenario("e2e_efficiency_scenario.py")

    @unittest.skipUnless(DATA and _have("mediapipe", "cv2"), "needs mediapipe and EMBER_FACE_TEST_DATA")
    def test_presence_with_real_pose_model(self):
        self.run_scenario("e2e_presence_scenario.py")

    @unittest.skipUnless(DATA and os.path.exists(EMBEDDING_MODEL) and _have("chromadb", "optimum"),
                         "needs EMBER_FACE_TEST_DATA and the ONNX embedding model")
    def test_people_awareness_and_memory(self):
        self.run_scenario("e2e_memory_scenario.py")


if __name__ == "__main__":
    unittest.main()
