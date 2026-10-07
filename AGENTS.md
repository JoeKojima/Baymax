# Ember development instructions

- The documented desktop entry point is `Baymax-main/run_pc.py`, using Gemini Live version 8. Confirm the actual hardware entry point before claiming deployed parity; version 9 has additional behavior.
- Preserve the base conversation prompt, Gemini models, audio sample rates and processing. Add shared features through `toolkit/`; put OS/device changes in `pc_hardware.py`.
- Do not open extra microphone/camera streams in toolkit features. Do not commit credentials, recordings, runtime snapshots, local databases or virtual environments.
- Each feature needs a README explaining inputs, outputs, dependencies, external data flows, enabling/disabling and tests.
- Run tests from `Baymax-main`: `python -m unittest discover -s tests`. Hardware, Gemini and provider tests require explicit knowledge of the data sent; do not start listening just to verify the UI.
- Review branches are approved by the software lead before merging/deploying. Do not change the robot's systemd startup or enable production alerts as part of desktop setup.
