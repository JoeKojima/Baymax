"""Scratchpad utilities for storing conversation history."""
import json
from datetime import datetime, UTC
from typing import List, Dict


def load_scratchpad(path: str = "scratchpad.json") -> List[Dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_scratchpad(pad: List[Dict[str, str]], path: str = "scratchpad.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pad, f, ensure_ascii=False, indent=2)


def create_entry(user_text: str, movement: bool, verbal_output: str, motion_info: str) -> Dict[str, str]:
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "user_text": user_text,
        "movement_required": str(movement),
        "verbal_output": verbal_output,
        "motion_info": motion_info
    }


def initialize_scratchpad(path: str = "scratchpad.json"):
    try:
        with open(path, "r") as f:
            json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        with open(path, "w") as f:
            f.write("[]")
