"""Start Ember: `python -m ember --profile v8|mvp|v9|v10 [--core ember|legacy] [--devices real|virtual]`.

run_pc.py / run_mvp.py remain the documented launchers (credential checks,
setup, eyes); they call run() here. Starting this module directly skips their
key preflight, so use it for development and virtual-device runs.

--devices virtual  no hardware: a silent (or --mic-wav) microphone, a recording
                   speaker and a grey camera. Gemini still receives that audio and video.
--plugins          print the feature registry and exit (connects to nothing).
"""
import argparse
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def apply_defaults(profile, environ=os.environ):
    for name, value in profile.defaults.items():
        environ.setdefault(name, value)


def build(profile, devices="real", mic_wav=None):
    """A prepared Runtime for an ember-core profile (devices selected, cleanup built)."""
    from ember.devices import hardware
    from ember.plugins import Registry
    from ember.session.engine import Runtime
    from ember.settings import Settings
    settings = Settings.load()
    if devices == "virtual":
        from ember.devices.virtual import MicSource, VirtualSoundDevice
        microphone = MicSource(room_noise=200)
        if mic_wav:
            import wave
            import numpy as np
            with wave.open(str(mic_wav), "rb") as audio:
                microphone.say(np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16))
        chosen = hardware.virtual(settings, sd=VirtualSoundDevice(microphone=microphone))
    else:
        chosen = hardware.real(settings)
    return Runtime(settings, chosen, registry=Registry(profile.plugins())).prepare()


def run(name="v8", core=None, devices="real", mic_wav=None):
    from ember import profiles
    profile = profiles.get(name)
    apply_defaults(profile)
    core = core or profile.core
    if core == "legacy":
        print(f"[EMBER] Profile {profile.name}: legacy core {profile.legacy_file}.", flush=True)
        runpy.run_path(str(ROOT / profile.legacy_file), run_name="__main__")
        return 0
    if profile.plugins is None:
        raise SystemExit(f"Profile {profile.name} has no ember-core port yet; use --core legacy.")
    from dotenv import load_dotenv
    load_dotenv()  # As the v8 module did at import (cwd .env, no override).
    print(f"[EMBER] Profile {profile.name}: ember core ({devices} devices).", flush=True)
    build(profile, devices, mic_wav).main()
    return 0


def main(argv=None):
    from ember import profiles
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", default="v8", choices=sorted(profiles.PROFILES))
    parser.add_argument("--core", choices=["ember", "legacy"], help="Override the profile's core")
    parser.add_argument("--devices", choices=["real", "virtual"], default="real")
    parser.add_argument("--mic-wav", type=Path, help="With --devices virtual: what the microphone hears first")
    parser.add_argument("--plugins", action="store_true", help="Print the feature registry and exit")
    parser.add_argument("--list", action="store_true", help="Print the profiles and exit")
    args = parser.parse_args(argv)
    if args.list:
        for profile in profiles.PROFILES.values():
            print(f"{profile.name:5s} {profile.core:7s} {profile.about}")
        return 0
    if args.plugins:
        from ember.plugins import Registry
        profile = profiles.get(args.profile)
        if profile.plugins is None:
            print(f"{profile.name} runs {profile.legacy_file} unchanged; it has no registry yet.")
            return 0
        for name, state, hooks, about in Registry(profile.plugins()).describe():
            print(f"{name:14s} {state:3s}  {hooks:40s} {about}")
        return 0
    return run(args.profile, args.core, args.devices, args.mic_wav)


if __name__ == "__main__":
    sys.exit(main())
