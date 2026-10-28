# MVP

POC integration of PR **#7 + #11 + #12**: setup, toolkits and responsive
conversation, with one-time device configuration and opt-in Debian desktop
autostart. Start with [MVP setup and validation](Baymax-main/MVP.md).
Includes normal-blinking MAX7219 eyes with thinking dots after waits over 2s.
People awareness (#8) and Memory Palace are deferred.

## Included Conversational Ability

Responsive listening and turn-taking, layered on #11. See
[the feature](Baymax-main/features/conversational_ability/README.md) for echo
cancellation, noise suppression, brief replies and enablement. Memory Palace
is separate in PR #13.

# Ember feature reorganization and toolkits

This branch stacks on PR #7 for Windows, Intel/Apple Silicon macOS, Linux/Orange Pi setup, devices, clickables and local monitoring. PR #11's review diff contains feature organization and callable toolkit behavior. Downloading the whole branch includes the compatibility foundation automatically.

- `Baymax-main/features/` organizes `memory_palace/`, `person_recognition/` and `toolkits/` as sibling namespaces. Memory/person folders reserve integration boundaries; prompt/identity/recall changes are scoped to PR #13.
- Location/weather uses IPWho/Open-Meteo with clarification, caching and bounded provider errors. `BAYMAX_TOOLKIT_ENABLED=0` disables toolkit declarations/preloading.
- Optional fall detection uses the existing camera (in v10 it reuses the core's pose instead of running a second model), is disabled by default and adds no caregiver alerts. It runs on the CPU unless the optional Orange Pi 5 NPU pose setup has been done (`Baymax-main/features/pose`).
- Public information tools and explicit local headline/feed profiles are opt-in, with Setup controls and documented sources, data flows and tests. Desktop v8 and robot v10 retain existing handlers and tool-response IDs.

Extract the whole branch and open `devops/clickables` → your OS → **1 - Setup**. Then Save key/devices and use **Monitor → Start Ember → Stop Ember**. On Intel macOS, Setup selects the Intel profile inherited from #7 automatically. Actual Intel/Apple Silicon Mac and ARM installations/devices remain pending.

- [Feature layout](Baymax-main/features/README.md)
- [Toolkit guide](Baymax-main/features/toolkits/README.md)
- [Public-tool experiment](Baymax-main/features/toolkits/EXPERIMENTAL.md)
- [CPU fall toolkit](Baymax-main/features/toolkits/FALL_DETECTION.md)
- [Clickables](devops/clickables/README.md)
- [Testing protocol](Baymax-main/TESTING_PROTOCOL.md)

Run `python check_release.py` from `Baymax-main` for separate core/toolkit offline results. Offline success does not establish real-device, live Gemini or provider accuracy, or authorize deployment. No production startup/service is changed by this PR. Deliberately starting the runtime sends microphone/camera data to Gemini and records audio locally; toolkit documentation describes additional provider calls.
