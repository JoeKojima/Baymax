# Compatibility testing and review

From `Baymax-main`, run `python check_release.py --report .local-review/report.md` using the installed private environment. Also run `python -m unittest discover -s tests`. The gate requires the core test modules, discovers all test modules and rejects failures and empty sections. PR #7 has core checks only; toolkit implementation and its required separate gate/checklist belong to PR #11. A missing toolkit section here is not a toolkit validation claim.

Offline checks simulate settings, HTTP protections, devices, telemetry, upstream runtime parity, key validation and installer failures/retries. They do not start microphone/camera capture or contact Gemini/providers. Preserve prompts/models/audio behavior when adding OS adapters.

Before approval, record the commit, date, OS/architecture, device names and actual results for fresh install, key/device Save and reopen, actual microphone/speaker/camera tests, rejected-key startup before capture, multi-turn Gemini speech, interruptions, transcript monitor, stop/restart and device release. Verify Windows, Intel/Apple Silicon Mac and Linux/Orange Pi on those targets; Windows simulations do not establish Mac/ARM behavior. On the Intel student laptop, record macOS 15.7.9 and the 2020 13-inch Intel model when testing.

Actual runtime tests send audio/video to Gemini and record audio locally. Do not start capture automatically, commit keys/recordings, or report pending checks as pass. Use safe deliberate tests. The lead reviews evidence and approves merge/deployment; an offline pass is not release approval. Toolkit changes in PR #11 need separate toolkit and live integration evidence, including disabled behavior and provider failures.
