"""Bounded provider text and record validation."""
import html
import re


def text(value, limit=1000):
    if not isinstance(value, str):
        raise ValueError("Expected provider text")
    return html.unescape(re.sub(r"<[^>]+>", "", value))[:limit]


def rows(value):
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise ValueError("Expected provider records")
    return value
