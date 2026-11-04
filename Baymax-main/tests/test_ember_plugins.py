"""Feature registry, event bus, profiles and core selection (offline)."""
import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import runtime_monitor as monitor
from ember import main as ember_main
from ember import profiles, registry
from ember.bus import Bus
from ember.plugins import Plugin, Registry, lazy


class RegistryTests(unittest.TestCase):
    def test_builtin_order_is_exactly_what_v8_imports_register(self):
        import features.toolkits.gemini_tools  # noqa: F401  (what v8's run() imports first)
        built = registry.default()
        self.assertEqual(built.problems(), [])
        from features.conversational_ability import processing
        from features.toolkits import audiobooks, fall_detection, proactive
        from features.toolkits.everyday import tools as everyday
        self.assertEqual(processing.input_gates[:1], [audiobooks.player.gate])
        self.assertEqual(fall_detection.session_tasks[:2], [proactive.proactive.session_task, everyday.session_task])

    def test_install_appends_in_declared_order_once_and_reports_strays(self):
        lists = {"input_gate": [], "output_source": [], "frame_observer": [], "session_task": []}
        loaded = []
        first, second, stray = (lambda s: False), (lambda s: True), (lambda s: False)
        plugins = Registry([Plugin("a", load=lambda: loaded.append("a"), input_gate=first),
                            Plugin("off", enabled=lambda: False, input_gate=stray),
                            Plugin("b", input_gate=second)], lists=lists, bus=Bus())
        plugins.install().install()
        self.assertEqual(loaded, ["a"])
        self.assertEqual(lists["input_gate"], [first, second])
        self.assertEqual(plugins.problems(), [])
        lists["input_gate"].insert(0, stray)
        lists["input_gate"].reverse()
        problems = plugins.problems()
        self.assertTrue(any("outside the registry" in p for p in problems))
        self.assertTrue(any("order" in p for p in problems))

    def test_config_steps_run_in_order_and_skip_disabled(self):
        plugins = Registry([Plugin("a", base_config=lambda c: {**c, "a": 1}, config=lambda c: {**c, "x": c.get("x", "") + "a"}),
                            Plugin("off", enabled=lambda: False, config=lambda c: {**c, "x": "never"}),
                            Plugin("b", config=lambda c: {**c, "x": c["x"] + "b"})], lists={}, bus=Bus())
        self.assertEqual(plugins.base_config({}), {"a": 1})
        self.assertEqual(plugins.session_config({}), {"x": "ab"})

    def test_lazy_hooks_resolve_after_load_and_names_must_be_unique(self):
        self.assertEqual(lazy("features.toolkits.audiobooks:player.gate").resolve(),
                      __import__("features.toolkits.audiobooks", fromlist=["player"]).player.gate)
        with self.assertRaises(ValueError):
            Registry([Plugin("a"), Plugin("a")])

    def test_bus_subscribers_from_plugins(self):
        bus, seen = Bus(), []
        Registry([Plugin("listener", subscribe={"transcript": lambda speaker, message: seen.append(speaker)})],
                 lists={}, bus=bus).install()
        bus.publish("transcript", "user", {"id": 1, "text": "hi"})
        self.assertEqual(seen, ["user"])


class BusTests(unittest.TestCase):
    def test_monitor_publishes_states_and_transcripts_to_every_subscriber(self):
        bus, states, transcripts, others = Bus(), [], [], []
        bus.subscribe("state", states.append)
        bus.subscribe("state", others.append)
        bus.subscribe("transcript", lambda speaker, message: transcripts.append((speaker, message["text"])))
        with patch.object(monitor, "_bus", bus), patch.object(monitor, "event_observer", None), \
                patch.object(monitor, "transcript_observer", None), patch.object(monitor, "waiting_audio", None):
            monitor.emit("thinking", "test")
            monitor.transcribe("user", "Hello")
            monitor.finish_transcripts()
        self.assertEqual(states[:2], ["thinking", "user_speech"])
        self.assertEqual(others, states)
        self.assertEqual(transcripts, [("user", "Hello")])

    def test_failing_subscriber_is_dropped_and_others_still_run(self):
        bus, seen = Bus(), []

        def broken(event):
            raise RuntimeError("boom")
        bus.subscribe("state", broken)
        bus.subscribe("state", seen.append)
        with contextlib.redirect_stdout(io.StringIO()) as log:
            bus.publish("state", "a")
            bus.publish("state", "b")
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(log.getvalue().count("disabled after an error"), 1)
        unsubscribe = bus.subscribe("state", seen.append)
        unsubscribe()
        with self.assertRaises(ValueError):
            bus.subscribe("nope", seen.append)

    def test_monitor_slots_still_work_alongside_the_bus(self):
        slot = []
        with patch.object(monitor, "event_observer", slot.append), patch.object(monitor, "waiting_audio", None):
            monitor.notify("speaking")
        self.assertEqual(slot, ["speaking"])


class ProfileTests(unittest.TestCase):
    def test_profiles_and_defaults(self):
        self.assertEqual(profiles.get("mvp").defaults, profiles.MVP_DEFAULTS)
        self.assertEqual(profiles.get("v10").core, "legacy")
        with self.assertRaises(SystemExit):
            profiles.get("v11")
        environ = {"BAYMAX_VOICE_ANALYSIS": "1"}
        ember_main.apply_defaults(profiles.get("mvp"), environ)
        self.assertEqual(environ["BAYMAX_VOICE_ANALYSIS"], "1")      # setdefault, like run_mvp.py
        self.assertEqual(environ["BAYMAX_INTERRUPTION_ENABLED"], "0")

    def test_mvp_defaults_match_run_mvp(self):
        import ast
        from pathlib import Path
        source = (Path(ember_main.ROOT) / "run_mvp.py").read_text(encoding="utf-8")
        found = {node.args[0].value: node.args[1].value for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setdefault"
                 and ast.unparse(node.func.value) == "os.environ"}
        self.assertEqual(found, profiles.MVP_DEFAULTS)

    def test_legacy_profiles_run_their_file_and_ember_profiles_build_the_runtime(self):
        with patch.object(ember_main.runpy, "run_path") as run_path, patch.dict(os.environ, {}), \
                contextlib.redirect_stdout(io.StringIO()):
            ember_main.run("v10")
        self.assertTrue(run_path.call_args.args[0].endswith("realtime_gemini_10.py"))
        runtime = Mock()
        with patch.object(ember_main, "build", return_value=runtime) as build, patch("dotenv.load_dotenv"), \
                patch.dict(os.environ, {}), contextlib.redirect_stdout(io.StringIO()):
            ember_main.run("mvp")
            self.assertEqual(os.environ["BAYMAX_WAIT_CUE_ENABLED"], "0")
        build.assert_called_once()
        runtime.main.assert_called_once()

    def test_cli_lists_without_connecting(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ember_main.main(["--list"]), 0)
            self.assertEqual(ember_main.main(["--plugins", "--profile", "mvp"]), 0)
            self.assertEqual(ember_main.main(["--plugins", "--profile", "v9"]), 0)
        text = out.getvalue()
        self.assertIn("audiobooks", text)
        self.assertIn("realtime_gemini_9.py unchanged", text)


class CoreSelectionTests(unittest.TestCase):
    def start(self, core):
        import run_pc
        output = io.StringIO()
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "k" * 39, "BAYMAX_CORE": core}, clear=True), \
                patch.object(run_pc, "load_dotenv"), patch.object(run_pc.os, "chdir"), \
                patch.object(run_pc, "validate_key") as validate, patch.object(run_pc.runpy, "run_path") as legacy, \
                patch("ember.main.run", return_value=0) as ember, contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(output):
            result = run_pc.main()
        return result, validate, legacy, ember, output.getvalue()

    def test_legacy_core_runs_the_unchanged_file(self):
        result, validate, legacy, ember, _ = self.start("legacy")
        self.assertEqual(result, 0)
        self.assertTrue(str(validate.call_args.args[1]).endswith("realtime_gemini_8.py"))
        legacy.assert_called_once()
        ember.assert_not_called()

    def test_ember_core_validates_its_own_models_then_runs_profile_v8(self):
        result, validate, legacy, ember, output = self.start("ember")
        self.assertEqual(result, 0)
        self.assertTrue(str(validate.call_args.args[1]).endswith("ember/session/config.py"))
        ember.assert_called_once_with("v8", core="ember")
        legacy.assert_not_called()
        self.assertIn("(ember core)", output)

    def test_default_core_is_ember_after_the_cutover(self):
        import run_pc
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run_pc.core(), "ember")

    def test_unknown_core_stops_before_contacting_google(self):
        result, validate, legacy, ember, output = self.start("v11")
        self.assertEqual(result, 1)
        validate.assert_not_called()
        self.assertIn("BAYMAX_CORE", output)


if __name__ == "__main__":
    unittest.main()
