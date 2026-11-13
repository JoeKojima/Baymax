"""Resolve a Gemini credential inside the application; never log or persist its value."""
import os
from pathlib import Path


class CredentialError(RuntimeError):
    pass


def gemini_key(values=None):
    values = os.environ if values is None else values
    filename = values.get('GOOGLE_API_KEY_FILE', '')
    if filename:
        try:
            with Path(filename).expanduser().open('r', encoding='utf-8-sig') as stream:
                key = stream.read(8193).strip()
        except (OSError, UnicodeError):
            raise CredentialError('Could not read the configured Gemini key file. Check its location and permissions.') from None
    else:
        key = values.get('GOOGLE_API_KEY', '') or ''
    if key and (len(key) > 4096 or any(c.isspace() for c in key)):
        raise CredentialError('The Gemini key file or setting must contain only one API key, without spaces.') from None
    if filename and not key:
        raise CredentialError('The configured Gemini key file is empty.') from None
    return key
