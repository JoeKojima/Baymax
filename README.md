# Ember feature reorganization and toolkits

This branch stacks on PR #7 for Windows, Intel/Apple Silicon macOS, Linux/Orange Pi setup, devices, clickables and local monitoring. PR #11's review diff contains feature organization and callable toolkit behavior. Downloading the whole branch includes the compatibility foundation automatically.

- `Baymax-main/features/` organizes `memory_palace/`, `person_recognition/` and `toolkits/` as sibling namespaces. Memory/person folders reserve integration boundaries; prompt/identity/recall changes are scoped to PR #12.
- Location/weather uses IPWho/Open-Meteo with clarification, caching and bounded provider errors. `BAYMAX_TOOLKIT_ENABLED=0` disables toolkit declarations/preloading.
- Optional CPU fall detection shares existing camera frames, is disabled by default and adds no caregiver alerts or NPU support.
- Public information tools and explicit local headline/feed profiles are opt-in, with Setup controls and documented sources, data flows and tests. Desktop v8 and robot v10 retain existing handlers and tool-response IDs.

Extract the whole branch and open `devops/clickables` → your OS → **1 - Setup**. Then Save key/devices and use **Monitor → Start Ember → Stop Ember**. On Intel macOS, Setup selects the Intel profile inherited from #7 automatically. Actual Intel/Apple Silicon Mac and ARM installations/devices remain pending.

- [Feature layout](Baymax-main/features/README.md)
- [Toolkit guide](Baymax-main/features/toolkits/README.md)
- [Public-tool experiment](Baymax-main/features/toolkits/EXPERIMENTAL.md)
- [CPU fall toolkit](Baymax-main/features/toolkits/FALL_DETECTION.md)
- [Clickables](devops/clickables/README.md)
- [Testing protocol](Baymax-main/TESTING_PROTOCOL.md)

Run `python check_release.py` from `Baymax-main` for separate core/toolkit offline results. Offline success does not establish real-device, live Gemini or provider accuracy, or authorize deployment. No production startup/service is changed by this PR. Deliberately starting the runtime sends microphone/camera data to Gemini and records audio locally; toolkit documentation describes additional provider calls.
