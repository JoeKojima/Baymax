# OPI4-Flash validation

Recorded 2026-11-01. Runtime baseline: PR #20 commit
`e212fd129652a0a09af8cb19b1afd27d0b76bf37`; application source is unchanged by this
packaging PR. Tests do not start new microphone/camera streams or contact Gemini.

## Passed

- Linux ARM64, kernel 6.6.98-vendor-sun60iw2, system Python 3 builder interpreter:
  `python3 -m unittest discover -s devops/opi4-flash -p 'test_*.py'` — **28 passed**,
  no failures, errors or skips. Includes settings validation/quoting, WPA/open Wi-Fi,
  private writes, generated identities, checksummed downloads, device renumbering,
  progress/AnyDesk reporting, update fallback and wrong-PR bundle rejection. Two
  launcher regressions verify `run_mvp.py` routing and independent Setup startup.
- Linux ARM64/Python 3.12.15 application environment:
  `python check_release.py --report .local-review/opi4-flash.md` from `Baymax-main` —
  **284 core + 158 toolkit passed**, no failures, errors or skips.
- Generic `build.py` completed using a dummy key and dummy Wi-Fi, cached assets
  checked against `assets.json`, and PR #20's earlier `233e7aa` head. Produced a **2,147,483,648
  byte image file**, verified its board firmware/services/private-file ownership
  and permissions, ran the then-current 26 provisioning tests inside the assembled build
  workspace, and completed clean filesystem checks. Generated access details and
  images stayed private; no physical card was written.
- Git bundle verification: complete source history and selected PR commit/ref.
- Python and shell syntax; Git whitespace check; explicit staged-file audit against
  the operator's real API key/passwords. No matching secrets, personalized images,
  third-party binaries or machine-specific maintenance scripts were staged.
- Earlier Windows/Python 3.12.15 pure-Python check: 21 passed; five Linux/Bash updater
  cases skipped there and passed on Linux. The two new Linux-only launcher cases
  passed on Linux; the updated suite was not rerun on Windows.

The image build used verified cached asset bytes. Download cache-hit and
bad-checksum behavior were tested with mocks; a complete fresh-download build on a
second machine has not been recorded.

## Pending

- Flash the reusable package's generated image to a spare card and record a full
  first boot, Wi-Fi association/DHCP, package installation, virtual desktop,
  AnyDesk and progress reporting.
- Validate preconfigured/blank-key behavior and spoken v10 conversation, real
  audio/video quality, stop/restart and disabled-camera behavior on that fresh card.
- Validate ambiguous/missing physical USB devices and mixed-room conversation.
- Connect and validate eye hardware separately. No eyes were connected for these
  checks; synthetic tests do not establish physical eye behavior.
- Verify Linux x86-64 VM preparation and any claimed Windows/macOS VM workflow.
- Software-lead review/approval. Offline checks and a successful image assembly
  are not deployment approval or a completed live-integration checklist.
