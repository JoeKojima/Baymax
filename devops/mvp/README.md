# MVP startup

`install_autostart.py` is a standard-library-only, opt-in installer for the
logged-in Linux desktop user. Inputs: complete project checkout, installed
`Baymax-main/.venv`, user configuration directory. Outputs: user service and
desktop autostart entry, independent Monitor service, and Monitor launchers in
the user's Applications and localized Desktop directories. It enables both
services for `default.target`, makes no network requests and starts no runtime.

`start-session.sh` imports the current desktop environment into the user service
manager, then starts `ember-mvp.service` and `ember-monitor.service`. Startup opens one-time local Setup if
needed; a completed configuration starts the existing integrated v8 runtime.
Saved settings skip Setup on subsequent launches. It uses the current user's
desktop/audio session; boot auto-login must be configured separately.

`monitor.py --serve` starts the local Monitor without a browser, including before
configuration is saved. Setup uses port 8767, independently of Monitor on
8766. The Monitor service restarts after exit and can be restarted without
stopping Ember. It opens no microphone/camera streams. The session script opens
the browser automatically only when Linux DRM reports a connected physical
display and the desktop environment is available. No connected display means
the server stays available and the desktop/Applications launcher can open it
later (including through remote desktop). Browser launch is checked at login;
hot-plugging a display later uses the shortcut. GNOME may require right-click
**Allow Launching** once, and desktop icons require desktop-icon support.

## Robot updates (`update.py`, opt-in)

Off unless `Baymax-main/.env` has `BAYMAX_AUTO_UPDATE=1`. It runs before every
`ember-mvp.service` start (`ExecStartPre=-`, system `python3`), so it can never
block Ember. Each start it:

1. Fetches `BAYMAX_UPDATE_BRANCH` (default `release`) with a 20 s limit; no
   network or no branch means Ember starts on the current version.
2. If the branch moved: switches to it, reinstalls `requirements-pc.txt` only when
   that file changed, then runs `check_release.py`. Any failure switches back.
3. Keeps the new version once `.runtime-status.json` shows `listening` after the
   switch. Three starts without it (crash loop or replugs) roll back.
4. Never retries a commit that rolled back; skips updating when tracked files
   were edited on the robot. `.env`, recordings and other ignored files are untouched.

Shipping: `git push origin MVP:release`. Undo a bad release: push the last good
commit with `git push --force origin <good>:release`; robots follow on next start.
For a rapid-testing robot set `BAYMAX_UPDATE_BRANCH=MVP`. Log lines start with
`[UPDATE]` in `journalctl --user -u ember-mvp.service`. Existing robots must rerun
`python3 devops/mvp/install_autostart.py` once to get the pre-start hook.

Tests: `python -m unittest discover -s tests -p test_mvp.py` from `Baymax-main`.
They verify generated services/launchers, physical-display detection, manual
opening without a display, and serving without runtime configuration. Real Debian
service startup, desktop integration and cold boots remain pending.

See [MVP.md](../../Baymax-main/MVP.md) for installation, data flows, stop/removal,
tests and pending real-device validation. No sensor identities or secrets are
checked into this directory. This service does not start the legacy Flask
dashboard; use the included local Setup/Monitor for this v8 POC.
