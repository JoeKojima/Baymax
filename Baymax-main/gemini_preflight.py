"""Check key acceptance/model metadata before hardware startup; no generation/capture."""
import ast
from pathlib import Path
import re

import requests


class PreflightError(RuntimeError):
    """Safe, user-facing startup failure; never include a raw provider exception."""


def configured_models(runtime_path):
    # Read the core's actual settings without importing its hardware/audio modules.
    try:
        tree = ast.parse(Path(runtime_path).read_text(encoding="utf-8"))
        values = {target.id: ast.literal_eval(node.value)
                  for node in tree.body if isinstance(node, ast.Assign)
                  for target in node.targets if isinstance(target, ast.Name)
                  and target.id in ("MODEL", "SUMMARY_MODEL")}
        names = tuple(values[name] for name in ("MODEL", "SUMMARY_MODEL"))
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) for name in names):
            raise ValueError()
        return tuple(dict.fromkeys(names))
    except (OSError, SyntaxError, KeyError, ValueError, TypeError):
        raise PreflightError("Could not read the configured Gemini models. Check the runtime installation.") from None


def validate_key(api_key, runtime_path, session_factory=None):
    if not isinstance(api_key, str) or not api_key or len(api_key) > 4096 or any(c.isspace() for c in api_key):
        raise PreflightError("Open Setup, paste a Gemini API key value without spaces, and Save configuration.")
    models = configured_models(runtime_path)
    factory = session_factory or requests.Session
    try:
        with factory() as session:
            for model in models:
                # The credential is a header, never a URL/query parameter. Refuse redirects.
                response = session.get(f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                                       headers={"x-goog-api-key": api_key}, timeout=(5, 10), allow_redirects=False)
                code = response.status_code
                if code != 200:
                    if code in (400, 401):
                        message = "Google rejected the key or API request. Check your key in Google AI Studio, then update it in Setup."
                    elif code == 403:
                        message = "Google denied access. Check the key's project permissions, API restrictions and regional eligibility."
                    elif code == 404:
                        message = f"Configured Gemini model {model} is unavailable to this key/API. Ask the software lead to review model access; do not switch it silently."
                    elif code == 429:
                        message = "Google reports a quota/rate limit. Check project usage and retry later; ask your lead before enabling paid billing."
                    elif code >= 500:
                        message = "Google's service is unavailable. Try starting Ember again later."
                    else:
                        message = "Google returned an unexpected response. Key verification did not complete; retry or contact your lead."
                    raise PreflightError(message)
                try:
                    data = response.json()
                except ValueError:
                    raise PreflightError("Google returned an unreadable response; key verification did not complete.") from None
                if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].startswith("models/"):
                    raise PreflightError("Google returned unexpected model metadata; key verification did not complete.")
    except requests.Timeout:
        raise PreflightError("Gemini key verification timed out. Check your internet connection and retry.") from None
    except requests.RequestException:
        raise PreflightError("Could not reach Google to verify the key. Check your internet/proxy settings and retry.") from None
    return models
