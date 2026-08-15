#!/usr/bin/env python3
"""Run mutmut and fail the build when the score is below the threshold.

mutmut has no built-in break threshold, so this wraps it. The wrapper also reads
the exit code out of the process rather than off a pipe: `mutmut run | tail`
reports tail's status, which is always 0.

A timed-out mutant is counted as killed by every mutation tool, which means a
loaded machine inflates the score. Read this number from CI, not from a laptop:
on the Node SDK the same commit scored 86.19% locally and 84.70% on CI, against
a gate of 85.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def run(command: list[str]) -> tuple[int, str]:
    proc = subprocess.run(command, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def parse_results(text: str) -> dict[str, int]:
    """Pull the tallies out of mutmut's summary line.

    Format looks like: `🎉 120 🫥 0 ⏰ 2 🤔 0 🙁 14 🔇 0`
    (killed, no-tests, timeout, suspicious, survived, skipped)
    """
    counts = {"killed": 0, "timeout": 0, "survived": 0, "suspicious": 0}
    patterns = {
        "killed": r"🎉\s*(\d+)",
        "timeout": r"⏰\s*(\d+)",
        "survived": r"🙁\s*(\d+)",
        "suspicious": r"🤔\s*(\d+)",
    }
    for key, pattern in patterns.items():
        found = re.findall(pattern, text)
        if found:
            counts[key] = int(found[-1])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=85.0)
    args = parser.parse_args()

    code, output = run([sys.executable, "-m", "mutmut", "run"])
    print(output)

    counts = parse_results(output)
    # A timeout counts as killed: the mutant changed behaviour enough to hang,
    # which the suite did detect. Suspicious is not counted either way.
    killed = counts["killed"] + counts["timeout"]
    denominator = killed + counts["survived"]
    if denominator == 0:
        print("mutation gate: no mutants were evaluated — treating as failure")
        return 1

    score = killed / denominator * 100
    needed = -(-int(args.threshold * denominator) // 100)  # ceil
    print(
        f"\nmutation score {score:.2f}%  "
        f"({killed} killed / {denominator} evaluated, "
        f"{needed} needed for {args.threshold:.0f}%, margin {killed - needed})"
    )

    with open("mutation-summary.json", "w") as handle:
        json.dump({"score": score, "killed": killed, "evaluated": denominator}, handle)

    if score < args.threshold:
        print(f"FAIL: {score:.2f}% is below the {args.threshold:.0f}% threshold")
        return 1
    print(f"PASS: {score:.2f}% meets the {args.threshold:.0f}% threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
