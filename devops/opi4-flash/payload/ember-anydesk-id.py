"""Refresh the public ID outside the read-only status server sandbox."""
import os
from pathlib import Path
import re
import subprocess
import tempfile


def refresh(target=Path('/var/lib/ember/anydesk-id')):
    try:
        result = subprocess.run(['anydesk', '--get-id'], capture_output=True, text=True, timeout=8)
        value = result.stdout.strip()
        if result.returncode or not re.fullmatch(r'[0-9]{5,20}', value):
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.anydesk-id-', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(value + '\n')
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        pass  # AnyDesk may not be installed or online yet; the timer retries.


if __name__ == '__main__':
    refresh()
