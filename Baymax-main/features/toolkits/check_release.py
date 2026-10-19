"""Offline gate for the public-toolkit experiment; no devices or provider calls."""
import argparse
import unittest
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    outcomes = []
    for label, pattern in (("Core integration (simulated)", "test_public_core*.py"),
                           ("Public toolkit (mocked providers)", "test_toolkit*.py")):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern)
        count = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        passed = count > 0 and result.wasSuccessful()
        outcomes.append((label, passed, result.testsRun, len(result.failures), len(result.errors)))
    lines = ["# Public toolkit experimental review", ""]
    for label, passed, count, failures, errors in outcomes:
        lines.append(f"- {label}: {'PASS' if passed else 'FAIL'}; {count} tests, {failures} failures, {errors} errors.")
    lines += ["", "Live Gemini conversation, interruption/reconnect, robot hardware, Docker build,",
              "commercial provider terms and software-lead approval: PENDING.",
              "Offline success is not deployment approval. See features/toolkits/EXPERIMENTAL.md."]
    report = "\n".join(lines) + "\n"
    print(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    return 0 if all(row[1] for row in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
