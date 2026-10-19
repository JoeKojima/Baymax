# Ember cross-platform setup and device compatibility

Extract the whole branch, open `devops/clickables` → your OS folder → **1 - Setup**. Setup installs private Python 3.12, selects dependencies, runs offline checks and opens local key/device configuration. Use **Monitor → Start Ember → Stop Ember** afterwards.

PR #7 contains Windows, macOS (Intel and Apple Silicon), Linux and experimental Orange Pi setup, device adapters, Gemini startup validation and local transcript/stop monitoring. PR #11 adds feature reorganization and toolkits, including location/weather and optional CPU fall detection; those capabilities are absent here.

Windows installation and earlier conversation were tested on the developer PC. Intel Mac dependency resolution and simulated installer tests pass; actual Mac/Linux installations, device behavior and end-to-end shutdown remain pending. The student's 2020 Intel MacBook Pro on macOS 15.7.9 is an intended test target, not a verified hardware result.

- [Clickables and platform details](devops/clickables/README.md)
- [Manual setup](Baymax-main/PC_SETUP.md)
- [Mac checks](Baymax-main/MACOS_TESTING.md)
- [Orange Pi setup](Baymax-main/ORANGE_PI_SETUP.md)
- [Review protocol](Baymax-main/TESTING_PROTOCOL.md)

The v8 base prompt, models and audio processing are preserved against the pinned upstream snapshot. No production startup, systemd deployment or capture is started by the offline gate. Deliberately starting Ember sends microphone/camera data to Gemini and records audio locally. No production credentials are included.
