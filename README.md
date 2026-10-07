# Baymax desktop development review

This branch adds desktop development tools to the existing `Baymax-main` application. Start with [PC setup](Baymax-main/PC_SETUP.md) and [clickables](devops/clickables/README.md).

- Gemini Live version-8 conversation verified on a Windows PC with a webcam microphone, camera and USB speaker.
- Local key/device setup and hardware tests.
- Live speech transcripts and compact activity messages, toolkit start/end grouped together, and speaking/reply timing inside speech bubbles.
- Location/weather tools with approximate IP location, city clarification and caching; provider runtime events were observed locally, but weather accuracy, ambiguity and commercial deployment need further review.
- Offline tests cover settings, HTTP protections, adapters, runtime parity, toolkit failures/caching and monitor events.

Not yet confirmed: clean installs on another PC, Mac/Linux hardware, exact robot deployment version, and end-to-end stop behavior across all operating systems. These launchers are not a one-click installer.

The desktop extension is intended for software-lead review before merging or deploying. No secrets, recordings, conversation screenshots or virtual environments are included.
