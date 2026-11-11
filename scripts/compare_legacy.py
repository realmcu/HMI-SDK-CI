"""Compare a run of this CI with a run of the legacy nightly in realmcu/HMI-MANIFEST.

Used while both run in parallel (PLAN P0-9): for each track, the verified
revisions and the per-mode results must agree. Exit code 0 when every compared
track agrees, 1 when any differs, 2 when an artifact is missing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

NEW_REPO = "realmcu/HMI-SDK-CI"
OLD_REPO = "realmcu/HMI-MANIFEST"
TARGET = "rtl8773e-dashboard"
TRACKS = ("floating", "locked")


def download(repo: str, run_id: str, name: str, dest: Path) -> Path | None:
    target = dest / name
    proc = subprocess.run(
        ["gh", "run", "download", run_id, "-R", repo, "-n", name, "-D", str(target)],
        capture_output=True,
        text=True,
    )
    return target if proc.returncode == 0 else None


def revisions(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {p["name"]: p["revision"] for p in data["manifest"]["projects"]}


def old_modes(root: Path) -> dict[str, str]:
    (summary,) = root.glob("reports/*/summary.json")
    builds = json.loads(summary.read_text(encoding="utf-8"))["sources"]["gitee"]["builds"]
    return {mode: result["status"] for mode, result in builds.items()}


def new_modes(root: Path) -> dict[str, str]:
    summary = json.loads((root / "build" / "summary.json").read_text(encoding="utf-8"))
    return {mode: result["status"] for mode, result in summary.get("modes", {}).items()}


def compare_track(old: Path, new: Path) -> list[str]:
    problems = []
    old_rev = revisions(old / "manifest-revisions.yml")
    new_rev = revisions(new / "manifest-revisions.yml")
    for name in sorted(set(old_rev) | set(new_rev)):
        if old_rev.get(name) != new_rev.get(name):
            problems.append(f"revision {name}: legacy={old_rev.get(name)} new={new_rev.get(name)}")
    if problems:
        # Different inputs make the mode comparison meaningless.
        return ["inputs differ, results not compared"] + problems
    old_result, new_result = old_modes(old), new_modes(new)
    if old_result != new_result:
        problems.append(f"modes: legacy={old_result} new={new_result}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new-run", required=True, help=f"run id in {NEW_REPO}")
    parser.add_argument("--old-run", required=True, help=f"run id in {OLD_REPO}")
    args = parser.parse_args(argv)

    status = 0
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        for track in TRACKS:
            old = download(OLD_REPO, args.old_run, f"dashboard-verification-{track}-{args.old_run}", dest)
            new = download(NEW_REPO, args.new_run, f"report-{TARGET}-{track}-{args.new_run}", dest)
            if old is None or new is None:
                print(f"{track}: artifact missing (legacy={old is not None}, new={new is not None})")
                status = max(status, 2)
                continue
            problems = compare_track(old, new)
            if problems:
                status = max(status, 1)
                print(f"{track}: DIFFERENT")
                for problem in problems:
                    print(f"  {problem}")
            else:
                print(f"{track}: same revisions, same mode results {new_modes(new)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
