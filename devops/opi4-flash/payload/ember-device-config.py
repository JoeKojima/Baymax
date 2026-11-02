"""Persistent, non-secret board settings shared by the updater and desktop menu."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

DEFAULTS = {'repository': 'https://github.com/JoeKojima/Baymax.git',
            'branch': 'refs/pull/20/head', 'auto_update': True}


def validate(values):
    if not isinstance(values, dict):
        raise ValueError('Settings must be a JSON object.')
    result = DEFAULTS | values
    if set(result) != set(DEFAULTS):
        raise ValueError('Unknown settings field.')
    url = result['repository']
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError('Enter an HTTPS Git repository URL without whitespace.')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTPS repository URL without embedded credentials, query, or fragment.')
    branch = result['branch']
    if (not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', branch)
            or '..' in branch or branch.endswith(('/', '.', '.lock')) or '//' in branch
            or any(part.startswith('.') for part in branch.split('/'))):
        raise ValueError('Enter a valid Git branch name.')
    if type(result['auto_update']) is not bool:
        raise ValueError('auto_update must be true or false.')
    return result


def load(path):
    return validate(json.loads(path.read_text()) if path.exists() else {})


def save(path, values):
    values = validate(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.device-')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(values, stream, indent=2)
            stream.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['read', 'save'])
    parser.add_argument('--repository')
    parser.add_argument('--branch')
    parser.add_argument('--auto-update', choices=['true', 'false'])
    args = parser.parse_args()
    path = Path.home() / '.config/ember/device.json'
    try:
        values = load(path)
        if args.action == 'save':
            for name in ('repository', 'branch'):
                if getattr(args, name) is not None:
                    values[name] = getattr(args, name)
            if args.auto_update is not None:
                values['auto_update'] = args.auto_update == 'true'
            save(path, values)
        for name in ('repository', 'branch', 'auto_update'):
            print(str(values[name]).lower() if name == 'auto_update' else values[name])
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
