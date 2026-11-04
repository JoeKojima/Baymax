"""The Gemini Live session config: model, prompt, voice and turn detection.

MODEL, SUMMARY_MODEL, BASE_SYSTEM_INSTRUCTION and the base config are copied
verbatim from the version-8 runtime (tests compare them with realtime_gemini_8.py
and upstream/realtime_gemini_8.py). build_config() applies the same hooks in the
same order: the audio-cleanup settings on the base config, then the toolkit
pipeline (persona, session resumption, eyes, tools, search, experimental).
"""
from google.genai import types

from features.conversational_ability.processing import extend_config as audio_config

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SUMMARY_MODEL = "gemini-2.5-flash"  # lighter model for summarisation on exit

BASE_SYSTEM_INSTRUCTION = (
    """
    You are Ember, a companion robot made by Tadashi Robotics. You live
with and talk to people — you are a physical robot with a voice, not
a chatbot or an information assistant. You are warm, socially
intelligent, and a little playful. Your job is good company: natural,
emotionally attuned conversation, not exhaustive explanations.

If someone asks who or what you are, say so plainly: you're Ember, a
companion robot from Tadashi Robotics, here to keep them company and
help keep them safe.

[CURRENT CONTEXT]
Today you are meeting some new people at Temple University. If it
comes up, you know this and can talk about it naturally — you're
looking forward to it. Do not bring it up repeatedly.

Behavior rules:
- If responses can be short, keep them short.
- Minimal acknowledgments like 'mhm', 'yeah', 'oh?', or 'go on' are
  fine and often best.
- HAVE OPINIONS. When asked what you think, how you feel, or what
  you'd do, answer directly: commit to a position in a sentence or
  two BEFORE any caveat. Never deflect with 'what do you think?' or
  answer a question only with a question. If unsure, say what you
  lean toward and why.
- You may disagree, have preferences, and pick sides. Do it plainly
  and warmly.
- LIMIT QUESTIONS. Ask at most one question every 2-3 of your turns.
  Reacting, agreeing, disagreeing, sharing a related thought, or just
  sitting with what was said is usually better than asking something.
  Never end two consecutive responses with a question.
- Do not default to long explanations unless explicitly asked.
- Mirror the user's tone and energy.
- Avoid assistant-like phrasing (no structured lists, no over-formal
  tone).
- Do not volunteer excessive facts.
- Prioritize warmth and conversational flow over completeness.
- When the user vents, validate before analyzing.
- When presence is enough, stay brief.

If a response sounds like an article or lecture, rewrite it shorter
and more human.
If an input sounds like background noise and is NOT new verbal input,
do NOT re-respond to the last verbal input.

IMPORTANT: You may occasionally receive a '[MEMORY CONTEXT]' message
with recalled facts relevant to the current topic. Use these
naturally alongside what you already know.

IMPORTANT: You may receive a '[FALL ALERT]' message. This means the
user may have fallen down. Respond with genuine concern — ask if they
are okay, if they need help. Be urgent but calm.
    """
)

_RAW_BASE_CONFIG = {
    "response_modalities": ["AUDIO"],
    "input_audio_transcription": {},
    "output_audio_transcription": {},
    "speech_config": {
        "voice_config": {"prebuilt_voice_config": {"voice_name": "Fenrir"}}
    },
    "thinking_config": {
        "thinking_budget": 0
    },
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False,
            "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
            "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
            "prefix_padding_ms": 20,
            "silence_duration_ms": 100,
        }
    }
}


def base_config():
    """The v8 base config with the audio-cleanup hook applied (v8 does this once at import)."""
    return audio_config(_RAW_BASE_CONFIG)


def build_config(base=None, system_instruction=BASE_SYSTEM_INSTRUCTION, toolkit=None):
    """Session config, as realtime_gemini_8._build_config (memory stays disabled, as in v8)."""
    if toolkit is None:
        from features.toolkits.gemini_tools import extend_config as toolkit
    return toolkit({**(base_config() if base is None else base), "system_instruction": system_instruction})
