# Conversational Ability (#12)

- Extract responsive audio into `features/conversational_ability/`, independent
  of Memory Palace, Chroma and face recognition (reviewed in #13).
- Integrate WebRTC AEC3, noise suppression, AGC2, short-answer detection and
  listening during replies into v8/v10; preserve models/rates/raw recording.
- Add a Setup feature toggle/noise level and microphone telemetry in Monitor.
- Include generic external-key-file loading for desktop startup.
- Combined Windows v10 live test was reported responsive by the user;
  standalone v8 integration still needs a separate live test.

# Update notes

## Unreleased — feature organization and toolkits (PR #11)

- Stack on PR #7 for all OS compatibility, setup, device and monitor infrastructure, including the Intel Mac profile. The PR review diff excludes that inherited foundation.
- Scope location/weather, optional CPU fall detection and opt-in public information/headline/feed toolkits to this branch. Existing camera frames, bounded provider calls, explicit preferences, monitoring, controls and disabled behavior are preserved.
- Organize features under sibling `memory_palace/`, `person_recognition/` and `toolkits/` namespaces; memory/person folders reserve integration boundaries. Prompt/identity/recall changes and Memory Palace implementation belong to PR #13, not this PR.
- Runtime modifications here are limited to feature import paths, toolkit dispatch/lifecycle and monitoring. Original v8 prompt/models/audio behavior remain covered by strict upstream parity checks; v9 matches main.
- All 169 combined offline checks pass on Windows/Python 3.12: 99 core and 70 toolkit, with zero failures/errors/skips. Actual Gemini/hardware, Mac/Linux/Orange Pi installs, fall accuracy/performance, Docker and live integration remain pending. Earlier provider smoke checks do not establish current provider availability or full conversation correctness.
