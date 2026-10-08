# Update notes

## Unreleased - Ember desktop development tools

### Added
- Local Gemini key/device setup UI with microphone, speaker and camera tests.
- Conversation monitor with incremental transcripts, grouped toolkit events and reply timing.
- Desktop hardware adapters and Windows/macOS/Linux launchers.
- Location/weather toolkit with approximate location, city clarification and caching.
- Setup documentation and offline tests for these additions plus upstream runtime parity checks.
- `check_release.py`: separate core/toolkit offline results and a pending live-review checklist.
- Testing protocol and PR template requiring core, toolkit and integration evidence.

### Verification and limitations
- 45 offline checks pass on Windows: 35 core/development checks and 10 toolkit checks. The total includes three tests for the review CLI itself.
- A Windows Gemini conversation and location/weather events were observed locally.
- Fresh-machine installation, macOS/Linux hardware, deployed robot version/parity and the full live checklist remain pending.
- No production deployment or merge is implied by these checks.
