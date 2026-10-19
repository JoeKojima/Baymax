"""Explicit live-provider smoke check using synthetic queries, never devices/Gemini."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from .experimental import GROUPS, Toolkit
from .preferences import CATEGORIES, Preferences


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send the fixed synthetic queries documented in EXPERIMENTAL.md")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to explicitly enable public-provider requests")
    os.environ["EMBER_PUBLIC_TOOLKIT"] = "1"
    os.environ["EMBER_PUBLIC_TOOL_GROUPS"] = ",".join(GROUPS)
    failed = 0
    with tempfile.TemporaryDirectory() as directory:
        toolkit = Toolkit(preferences=Preferences(Path(directory) / "profiles.json"))
        checks = [("public_headlines", {"categories": [category], "hours": 168, "limit": 1}) for category in CATEGORIES]
        checks += [
            ("public_find_place", {"city": "Berlin", "country_code": "DE"}),
            ("public_weather", {"latitude": 52.52, "longitude": 13.41}),
            ("public_air_quality", {"latitude": 52.52, "longitude": 13.41}),
            ("public_exchange_rate", {"base": "USD", "quote": "EUR"}),
            ("public_holidays", {"country_code": "US", "year": 2026}),
            ("public_earthquakes", {"limit": 1}),
            ("public_knowledge", {"query": "semiconductor"}),
            ("public_books", {"query": "robotics"}),
        ]
        for name, values in checks:
            result = toolkit.execute(name, values)
            ok = not result.get("error") and not result.get("partial")
            failed += not ok
            # No downloaded headlines, excerpts or preferences are printed/saved.
            print(json.dumps({"tool": name, "category": values.get("categories"), "ok": ok,
                              "error": result.get("error"), "partial": result.get("partial", False),
                              "items": len(result.get("headlines", result.get("results", result.get("books", []))))}), flush=True)
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
