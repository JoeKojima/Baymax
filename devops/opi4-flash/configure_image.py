"""Edit the three private first-boot choices without echoing passwords or keys."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile
from image_settings import load, validate


def save_private(path, settings):
    settings = validate(settings)
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.image-settings-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(settings, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
        if os.name == 'nt':
            owner = subprocess.check_output(['whoami'], text=True).strip()
            subprocess.run(['icacls.exe', temporary, '/inheritance:r', '/grant:r', owner + ':(F)', 'SYSTEM:(F)'],
                           check=True, stdout=subprocess.DEVNULL)
        else:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path(__file__).parent / 'settings.private.json')
    args = parser.parse_args()
    old = load(args.settings) if args.settings.exists() else {'pr': 20, 'wifi': {'ssid': '', 'password': ''}, 'gemini_api_key': ''}
    pr = int(input(f'PR number [{old["pr"]}]: ').strip() or old['pr'])
    ssid = input(f'Wi-Fi name [{old["wifi"]["ssid"]}]: ').strip() or old['wifi']['ssid']
    password = getpass.getpass('Wi-Fi password (Enter keeps existing; - means open Wi-Fi): ')
    password = '' if password == '-' else password or (old['wifi']['password'] if ssid == old['wifi']['ssid'] else '')
    key = getpass.getpass('Gemini key (Enter keeps existing; - leaves manual Setup): ')
    key = '' if key == '-' else key or old['gemini_api_key']
    kept = {'access_password': old['access_password']} if 'access_password' in old else {}
    save_private(args.settings, {'pr': pr, 'wifi': {'ssid': ssid, 'password': password}, 'gemini_api_key': key} | kept)
    print(f'Saved PR #{pr}, Wi-Fi {ssid!r}, and Gemini key {"configured" if key else "blank"}. Rebuild the image to apply.')


if __name__ == '__main__':
    main()
