"""Resolve saved devices or unambiguous USB hardware without opening streams."""
import argparse
import re
import sys
from pathlib import Path


def identity(name):
    return re.sub(r'\s*\(hw:\d+,\d+\)$', '', name)


def audio_device(devices, hosts, direction, saved_name='', sysfs=Path('/sys/class/sound')):
    candidates = []
    for index, device in enumerate(devices):
        if not device['max_' + direction + '_channels'] or hosts[device['hostapi']]['name'] != 'ALSA':
            continue
        if saved_name:
            if identity(device['name']) == identity(saved_name):
                candidates.append(index)
        else:
            match = re.search(r'\(hw:(\d+),0\)$', device['name'])
            if match and '/usb' in str((sysfs / ('card' + match[1]) / 'device').resolve()):
                candidates.append(index)
    if len(candidates) != 1:
        raise RuntimeError(f'Choose the {direction} device in Ember Setup; found {len(candidates)} matching devices.')
    return candidates[0]


def camera_device(saved, directory=Path('/dev/v4l/by-id')):
    if saved.get('camera') == 'none':
        return 'none', '', ''
    uid = saved.get('camera_uid', '')
    paths = [Path(uid)] if uid else sorted(directory.glob('*-video-index0'))
    if not paths:
        return 'none', '', ''
    if len(paths) != 1:
        raise RuntimeError('Choose a camera in Ember Setup; several cameras are connected.')
    target = paths[0].resolve(strict=True)
    if not re.fullmatch(r'video\d+', target.name):
        raise RuntimeError('Saved camera is not a Linux video device.')
    return target.name[5:], paths[0].name, str(paths[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    import sounddevice as sd
    sys.path.insert(0, str(args.runtime.resolve()))
    from dev_settings import public_configuration, save_configuration
    import fcntl
    with (args.runtime / '.hardware.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = public_configuration(args.runtime / '.env')
        devices, hosts = sd.query_devices(), sd.query_hostapis()
        microphone = audio_device(devices, hosts, 'input', saved.get('microphone_name', ''))
        speaker = audio_device(devices, hosts, 'output', saved.get('speaker_name', ''))
        camera, camera_name, camera_uid = camera_device(saved)
        selected = {'microphone': str(microphone), 'speaker': str(speaker), 'camera': camera}
        catalog = {'microphones': [], 'speakers': [], 'cameras': []}
        for index, device in enumerate(devices):
            entry = {'index': index, 'name': device['name'], 'hostapi': hosts[device['hostapi']]['name']}
            for kind, direction in (('microphones', 'input'), ('speakers', 'output')):
                if device['max_' + direction + '_channels']:
                    catalog[kind].append(entry)
        if camera != 'none':
            catalog['cameras'].append({'index': int(camera), 'name': camera_name, 'uid': camera_uid})
        if not args.check:
            save_configuration(args.runtime / '.env', selected, catalog)
    print('Hardware resolved:', selected, flush=True)


if __name__ == '__main__':
    main()
