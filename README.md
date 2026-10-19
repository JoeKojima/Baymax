# Memory Palace

Integrates #8's people-awareness and person-linked memories with #11's public tools
and strongest shared debugging UI. See [Memory Palace](Baymax-main/features/memory_palace/README.md)
for implemented behavior, debugging setup and the planned graph architecture.
In Monitor, **Open Memory Palace** opens a separate per-person memory inspector
with shared memories, search and current-session context markers.

This branch adds desktop development tools to the existing `Baymax-main` application. **Download/extract the whole branch, open `devops/clickables` -> your OS folder -> `1 - Setup`.** The launcher installs Python 3.12 and dependencies, runs offline checks, creates Desktop clickables and opens configuration. See [clickable instructions](devops/clickables/README.md); [manual PC setup](Baymax-main/PC_SETUP.md) remains available for developers.

- Gemini Live version-8 conversation verified on a Windows PC with a webcam microphone, camera and USB speaker.
- Local key/device setup and hardware tests.
- Live speech transcripts and compact activity messages, toolkit start/end grouped together, and speaking/reply timing inside speech bubbles.
- Location/weather tools with approximate IP location, city clarification and caching; provider runtime events were observed locally, but weather accuracy, ambiguity and commercial deployment need further review.
- Offline tests cover settings, HTTP protections, adapters, runtime parity, toolkit failures/caching and monitor events.

Verified: automatic first-run environment installation and Desktop shortcuts on the developer Windows PC. Still pending: another clean PC, Mac/Linux hardware/installations, exact robot deployment version and end-to-end stop behavior across all operating systems. Intel/Rosetta Macs are currently unsupported by the pinned dependencies.

The desktop extension is intended for software-lead review before merging or deploying. No secrets, recordings, conversation screenshots or virtual environments are included.

Testing/review: [core and toolkit protocol](Baymax-main/TESTING_PROTOCOL.md). Run `python check_release.py` from `Baymax-main`; see [update notes](CHANGELOG.md).

Mac compatibility work: [simulation scope and real-device checklist](Baymax-main/MACOS_TESTING.md). Native camera naming/identity is implemented; actual Mac hardware validation is pending.

Daily click order: **Setup once (Save key/devices) -> Monitor -> Start Ember -> Stop Ember**. Later sessions only need Monitor/Start/Stop; Monitor may open in either order. See [clickable instructions](devops/clickables/README.md).

Shareable onboarding: [Discord ZIP, API-key and usage instructions](docs/DISCORD_QUICKSTART.md).

Experimental Orange Pi 5 Max support: [Debian 13 ARM setup](Baymax-main/ORANGE_PI_SETUP.md) and [opt-in CPU fall toolkit](Baymax-main/features/toolkits/FALL_DETECTION.md). No GPU/NPU is required for the CPU backend; Pi source-build success, throughput and fall accuracy remain unverified.
