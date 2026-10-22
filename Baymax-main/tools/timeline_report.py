#!/usr/bin/env python3
"""Summarize a single robot process's local rotating timeline files.

Aggregates cover every instrumented event; duration percentiles use the most
recent 2,048 measurements per metric. Production spans alone are sampled.
"""
import argparse
import json
from pathlib import Path

import numpy as np


def summarize(directory):
    aggregates = []
    corrupt = 0
    for path in Path(directory).glob("timeline.jsonl*"):
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if record.get("type") == "aggregate":
                        aggregates.append(record)
                except ValueError:
                    corrupt += 1
    if not aggregates:
        return {"error": "no aggregate snapshot; close the process or wait 30 seconds", "corrupt_lines": corrupt}
    latest = max(aggregates, key=lambda r: r["timestamp_ns"])
    return {"aggregate_timestamp_ns": latest["timestamp_ns"], "counts": latest["counts"],
            "duration_window_limit": 2048,
            "durations_ms": {name: {"n": len(values), **{f"p{p}": float(np.percentile(values, p))
                for p in (50, 95, 99)}} for name, values in latest["durations_ms"].items() if values},
            "corrupt_lines": corrupt,
            "patient_wait_label": "estimated speech end to output device; uncertain/overlap/ambiguous excluded"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--output")
    args = parser.parse_args()
    text = json.dumps(summarize(args.directory), indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text)
