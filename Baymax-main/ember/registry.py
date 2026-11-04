"""Ember's built-in features, in the order the version-8 runtime wires them.

This is the order the hooks end up in today, via import side effects:
gemini_tools imports audiobooks (speaker source + mic gate), then proactive
(frame observer + session task), then everyday (session task). Listing them
here makes that order visible and checked (`python -m ember --plugins`,
tests/test_ember_plugins.py). Add a new feature by adding a Plugin to the
right place in this list instead of editing the core or gemini_tools.

The single observer slots (runtime_monitor.event_observer / transcript_observer,
used by the eyes from run_mvp.py) are not list hooks; new features should
subscribe on ember.bus instead of taking a slot.
"""
from ember.plugins import Plugin, Registry, lazy


def _audio_config(config):
    from features.conversational_ability.processing import extend_config
    return extend_config(config)


def _toolkit_config(config):
    from features.toolkits.gemini_tools import extend_config
    return extend_config(config)


def _load_toolkit():
    import features.toolkits.gemini_tools  # noqa: F401  (registers audiobooks, proactive, everyday)


def builtin():
    return [
        Plugin("audio-cleanup", "Turn detection and interruption settings for the local WebRTC audio cleanup",
               base_config=_audio_config),
        Plugin("toolkit", "Persona, session resumption, eyes, tools, Google Search and discovery tools",
               load=_load_toolkit, config=_toolkit_config),
        Plugin("audiobooks", "LibriVox books mixed into the speaker; the mic is withheld while a book plays",
               output_source=lazy("features.toolkits.audiobooks:player.render"),
               input_gate=lazy("features.toolkits.audiobooks:player.gate")),
        Plugin("proactive", "Greets a person who comes into view, from the shared camera frames",
               frame_observer=lazy("features.toolkits.proactive:proactive.observe"),
               session_task=lazy("features.toolkits.proactive:proactive.session_task")),
        Plugin("everyday", "Announces due reminders and timers",
               session_task=lazy("features.toolkits.everyday.tools:session_task")),
    ]


def default():
    return Registry(builtin())
