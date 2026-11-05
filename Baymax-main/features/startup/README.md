# Startup

`launcher.py` owns one-time Setup, the launch lock, environment defaults and eye
lifecycle. `run_mvp.py` remains the stable command for users and systemd; `run_pc.py`
still performs provider preflight and launches the existing v10 runtime.
Inputs are private saved configuration; outputs are the configured running
conversation. Device streams and provider calls remain owned by the runtime.
Startup greeting is controlled by `BAYMAX_STARTUP_GREETING`. Tests: `test_mvp.py`
and the required `python check_release.py` gate.
