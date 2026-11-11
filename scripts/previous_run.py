"""Find the results.json of the previous recorded run of this workflow.

Only schedule and workflow_dispatch runs count; push and pull-request runs
exercise CI changes and must not move a target's failure streak. Prints the run
id on stdout (empty when there is none) and downloads the artifact to --dest.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

RECORDED_EVENTS = ("schedule", "workflow_dispatch")


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def candidate_runs(repo: str, workflow: str, current: str, limit: int = 30) -> list[str]:
    runs = json.loads(
        gh(
            "run", "list", "-R", repo, "--workflow", workflow, "--limit", str(limit),
            "--json", "databaseId,event,status",
        )
    )
    return [
        str(run["databaseId"])
        for run in runs
        if run["event"] in RECORDED_EVENTS
        and run["status"] == "completed"
        and str(run["databaseId"]) != current
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--current-run", required=True)
    parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        runs = candidate_runs(args.repo, args.workflow, args.current_run)
    except subprocess.CalledProcessError as exc:
        print(f"::warning::cannot list previous runs: {exc.stderr.strip()}", file=sys.stderr)
        return 0
    # A run that died before aggregating has no results artifact; use the one before it.
    for run_id in runs:
        proc = subprocess.run(
            ["gh", "run", "download", run_id, "-R", args.repo, "-n", f"results-{run_id}", "-D", str(args.dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and (args.dest / "results.json").is_file():
            print(run_id)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
