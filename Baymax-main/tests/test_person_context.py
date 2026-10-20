import ast
import asyncio
import time
from unittest.mock import AsyncMock
from test_public_core_integration import config_namespace, load_function
from pathlib import Path
from types import SimpleNamespace
import unittest

from features.person_recognition.context import IDENTITY_CONTEXT, extend_config

ROOT = Path(__file__).resolve().parents[1]


def function(path, name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class PersonContextTests(unittest.TestCase):
    def test_identity_context_is_independent_of_optional_tools(self):
        original = {"system_instruction": "base", "tools": [{"existing": True}]}
        result = extend_config(original)
        self.assertEqual(original["system_instruction"], "base")
        self.assertIs(result["tools"], original["tools"])
        self.assertIn("use no name", result["system_instruction"])
        self.assertIn("Honor a person's correction immediately", result["system_instruction"])
        self.assertIs(extend_config(result), result)

    def test_robot_config_does_not_make_old_memory_an_identity_instruction(self):
        for version in (9, 10):
            with self.subTest(version=version):
                store = SimpleNamespace(count=lambda: 1, get=lambda **kwargs: {"documents": ["James likes jazz"]})
                build = function(ROOT / f"realtime_gemini_{version}.py", "_build_config", {
                    "_BASE_CONFIG": {"speech_config": {"voice": "same"}}, "_BASE_SYSTEM_INSTRUCTION": "base",
                    "_memory_embedder": SimpleNamespace(_collection=store),
                    "public_toolkit": SimpleNamespace(extend_config=lambda value: value)})
                if version == 10:
                    build = load_function("_build_config", config_namespace({"speech_config": {"voice": "same"}}, store))
                result = build()
                prompt = result["system_instruction"]
                if version == 10:
                    self.assertNotIn("James likes jazz", prompt)
                else:
                    self.assertIn("James likes jazz", prompt)
                self.assertIn("They do not establish who is speaking now" if version == 9 else "use no name", prompt)
                self.assertNotIn("do NOT second-guess", prompt)
                self.assertIn(IDENTITY_CONTEXT, prompt)
                self.assertEqual(result["speech_config"], {"voice": "same"})

    def test_recalled_notes_do_not_claim_current_user_ownership(self):
        for version in (9,):
            with self.subTest(version=version):
                retrieve = function(ROOT / f"realtime_gemini_{version}.py", "_retrieve_memories", {
                    "_memory_embedder": SimpleNamespace(search=lambda *a, **k: [{"distance": 0.1, "document": "James likes jazz"}]),
                    "MEMORY_TOP_K": 3, "MEMORY_MAX_DISTANCE": 0.75})
                context = retrieve("music")
                self.assertIn("ownership and current relevance are not established", context)
                self.assertNotIn("about this user", context)

    def test_v10_recall_preserves_person_attribution(self):
        path = ROOT / "realtime_gemini_10.py"
        node = next(n for n in ast.parse(path.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.ClassDef) and n.name == "_MemoryInjector")
        ns = {"time": time, "asyncio": asyncio, "_face_id": None, "_current_memory_context": lambda: {"person_id":"james", "generation":0}}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
        session = SimpleNamespace(send_client_content=AsyncMock())
        recall = ns["_MemoryInjector"](session)
        recall.owner_generation = 0
        asyncio.run(recall._send([("jazz", "James likes jazz")]))
        text = session.send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("James likes jazz", text)
        self.assertNotIn("about this user", text)
        self.assertIn("jazz", recall.sent)

    def test_desktop_base_has_no_demo_location(self):
        tree = ast.parse((ROOT / "realtime_gemini_8.py").read_text(encoding="utf-8"))
        prompt = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "_BASE_SYSTEM_INSTRUCTION" for t in n.targets))
        self.assertNotIn("Temple University", prompt)
        self.assertIn("companion robot", prompt)


if __name__ == "__main__":
    unittest.main()
