"""Install MVP desktop-session autostart for the current Linux user; do not start capture."""
import os
from pathlib import Path
import subprocess
import sys


def systemd_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def desktop_quote(value):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    return '"' + value + '"'


def install(project, home, config_home):
    runtime = project / "Baymax-main"
    python = runtime / ".venv/bin/python"
    if not python.is_file():
        raise RuntimeError("Run devops/clickables/Linux/1 - Setup.sh first to install the private environment.")
    if any(c in str(project) for c in "\n\r"):
        raise RuntimeError("The checkout path must not contain newlines.")
    unit_dir = config_home / "systemd/user"
    autostart_dir = config_home / "autostart"
    unit_dir.mkdir(parents=True, exist_ok=True)
    autostart_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "ember-mvp.service"
    unit.write_text(
        "[Unit]\nDescription=Ember MVP conversation runtime\n\n[Service]\nType=simple\n"
        f"WorkingDirectory={systemd_quote(runtime)}\n"
        f"ExecStart={systemd_quote(python)} {systemd_quote(runtime / 'run_mvp.py')}\n"
        "Restart=on-failure\nRestartSec=15\nTimeoutStopSec=180\nKillSignal=SIGTERM\nUMask=0077\n",
        encoding="utf-8",
    )
    entry = autostart_dir / "ember-mvp.desktop"
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=Ember MVP\n"
        f"Exec=/bin/bash {desktop_quote(project / 'devops/mvp/start-session.sh')}\n"
        "Terminal=false\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return unit, entry


def main():
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        print("Run this as the logged-in Debian desktop user, without sudo.", file=sys.stderr)
        return 1
    project = Path(__file__).resolve().parents[2]
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        unit, entry = install(project, home, config_home)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Autostart installation failed: {error}", file=sys.stderr)
        return 1
    print(f"Installed {unit} and {entry}. No runtime was started.")
    print("MVP starts at the next desktop login. Boot without intervention requires desktop auto-login.")
    print("Start now deliberately: systemctl --user start ember-mvp.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
