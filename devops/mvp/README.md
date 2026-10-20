# MVP startup

`install_autostart.py` is a standard-library-only, opt-in installer for the
logged-in Linux desktop user. Inputs: complete project checkout, installed
`Baymax-main/.venv`, user configuration directory. Outputs: user service and
desktop autostart entry. It makes no network requests and starts no runtime.

`start-session.sh` imports the current desktop environment into the user service
manager, then starts `ember-mvp.service`. Startup opens one-time local Setup if
needed; a completed configuration starts the existing integrated v8 runtime.
Saved settings skip Setup on subsequent launches. It uses the current user's
desktop/audio session; boot auto-login must be configured separately.

See [MVP.md](../../Baymax-main/MVP.md) for installation, data flows, stop/removal,
tests and pending real-device validation. No sensor identities or secrets are
checked into this directory. This service does not start the legacy Flask
dashboard; use the included local Setup/Monitor for this v8 POC.
