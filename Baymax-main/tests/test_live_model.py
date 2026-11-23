"""The Gemini models are a reviewed choice: pinned defaults, env rollback, 2.5-only thinking budget."""
import ast
from pathlib import Path
import unittest

from gemini_preflight import configured_models

ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = [ROOT / name for name in ("realtime_gemini_8.py", "realtime_gemini_9.py", "realtime_gemini_10.py")]
LIVE_DEFAULT = "gemini-3.8-live"
SUMMARY_DEFAULT = "gemini-3.6-flash"
ROLLBACK = "gemini-2.5-flash-native-audio-preview-12-2025"  # The upstream model.


def base_config(runtime, model):
    """_BASE_CONFIG as the runtime builds it for `model`, without importing its devices."""
    import os
    from google.genai import types
    tree = ast.parse(runtime.read_text(encoding="utf-8"))
    body = [node for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)  # The literal, not the audio_config(...) wrapper.
            and any(isinstance(t, ast.Name) and t.id == "_BASE_CONFIG" for t in node.targets)]
    namespace = {"types": types, "os": os, "MODEL": model}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(runtime), "exec"), namespace)  # noqa: S102
    return namespace["_BASE_CONFIG"]


class LiveModelTests(unittest.TestCase):
    def test_defaults_are_the_reviewed_models_in_every_runtime(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime.name):
                self.assertEqual(configured_models(runtime, environ={}), (LIVE_DEFAULT, SUMMARY_DEFAULT))

    def test_env_overrides_roll_back_without_a_code_change(self):
        environ = {"EMBER_LIVE_MODEL": ROLLBACK, "EMBER_SUMMARY_MODEL": "gemini-2.5-flash"}
        for runtime in RUNTIMES:
            with self.subTest(runtime.name):
                self.assertEqual(configured_models(runtime, environ=environ), (ROLLBACK, "gemini-2.5-flash"))

    def test_rollback_model_is_upstream(self):
        tree = ast.parse((ROOT / "upstream/realtime_gemini_8.py").read_text(encoding="utf-8"))
        models = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name) and t.id == "MODEL"]
        self.assertEqual(models, [ROLLBACK])

    def test_thinking_budget_only_for_gemini_2_5(self):
        # Gemini 3.x Live rejects thinking_budget (close code 1007); 2.5 keeps it at 0.
        for runtime in RUNTIMES:
            with self.subTest(runtime.name):
                self.assertNotIn("thinking_config", base_config(runtime, LIVE_DEFAULT))
                self.assertEqual(base_config(runtime, ROLLBACK)["thinking_config"], {"thinking_budget": 0})

    def test_voice_and_modalities_unchanged(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime.name):
                config = base_config(runtime, LIVE_DEFAULT)
                self.assertEqual(config["response_modalities"], ["AUDIO"])
                self.assertEqual(config["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"],
                                 "Fenrir")


if __name__ == "__main__":
    unittest.main()
