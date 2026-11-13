"""Install MVP desktop-session autostart for the current Linux user; do not start capture."""
import os
from pathlib import Path
import shutil
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
        # WorkingDirectory is a single literal path, not an Exec command word.
        f"WorkingDirectory={str(runtime).replace('%', '%%')}\n"
        # Opt-in updater (BAYMAX_AUTO_UPDATE=1); '-' so it can never block Ember.
        # System python3: it must still roll back if an update broke the venv.
        f"ExecStartPre=-{systemd_quote(shutil.which('python3') or '/usr/bin/python3')} "
        f"{systemd_quote(project / 'devops/mvp/update.py')}\n"
        f"ExecStart={systemd_quote(python)} {systemd_quote(runtime / 'run_mvp.py')}\n"
        # After any exit: X eyes if Ember was killed or crashed before it could draw a face.
        f"ExecStopPost=-{systemd_quote(python)} {systemd_quote(runtime / 'features/eyes/show.py')} --after-exit\n"
        # Short stop timeout: a slow or hung exit must not hold up reboots (it was 180 s).
        "Restart=on-failure\nRestartSec=15\nTimeoutStartSec=1200\nTimeoutStopSec=15\n"
        "KillSignal=SIGTERM\nUMask=0077\n"
        "\n[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    entry = autostart_dir / "ember-mvp.desktop"
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=Ember MVP\n"
        f"Exec=/bin/bash {desktop_quote(project / 'devops/mvp/start-session.sh')}\n"
        "Terminal=false\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    (unit_dir / "ember-monitor.service").write_text(
        "[Unit]\nDescription=Ember local debugging Monitor\n\n[Service]\nType=simple\n"
        f"WorkingDirectory={str(runtime).replace('%', '%%')}\n"
        f"ExecStart={systemd_quote(python)} {systemd_quote(project / 'devops/mvp/monitor.py')} --serve\n"
        "Restart=always\nRestartSec=3\nUMask=0077\n\n[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    launcher = ("[Desktop Entry]\nType=Application\nName=Ember Monitor\n"
                "Comment=Open the local Ember debugging Monitor\nIcon=utilities-system-monitor\n"
                f"Exec=/bin/bash {desktop_quote(project / 'devops/mvp/start-session.sh')} --monitor\n"
                "Terminal=false\n")
    applications = home / ".local/share/applications"
    applications.mkdir(parents=True, exist_ok=True)
    (applications / "ember-monitor.desktop").write_text(launcher, encoding="utf-8")
    result = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=True)
    desktop = Path(result.stdout.strip())
    if not desktop.is_absolute() or desktop == home:
        desktop = home / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    shortcut = desktop / "ember-monitor.desktop"
    shortcut.write_text(launcher, encoding="utf-8")
    shortcut.chmod(0o755)
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
        subprocess.run(["systemctl", "--user", "enable", "ember-mvp.service", "ember-monitor.service"], check=True)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Autostart installation failed: {error}", file=sys.stderr)
        return 1
    print(f"Installed {unit} and {entry}. No runtime was started.")
    print("MVP and Monitor are enabled for user startup. Boot without intervention requires desktop auto-login.")
    print("Ember Monitor is available on the desktop and in Applications; GNOME may require Allow Launching once.")
    print("Start now deliberately: bash devops/mvp/start-session.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
