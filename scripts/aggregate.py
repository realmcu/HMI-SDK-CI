"""Combine the per-job results of a run and compare them with the previous run.

Reads every result.json under --reports, plus the plan report, and writes
results.json (the input of the next run's comparison, notify.py and the weekly
summary) and a
Markdown summary. "Previous" means the previous recorded run, not the previous
calendar day: scheduled runs are sometimes delayed by hours or skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
PASSING = ("PASS",)
# Results that say nothing about the code: they neither start nor end a failure streak.
UNVERIFIED = ("FETCH_FAIL",)


@dataclass(frozen=True)
class State:
    """Where a target/track stands after this run."""

    change: str  # new-failure, still-failing, recovered, passing, unverified, first-run
    failing: bool  # the last verified result was a failure
    streak: int  # consecutive failing runs, unverified runs included; 0 when not failing
    unverified: int  # consecutive FETCH_FAIL runs


def classify(status: str, previous: dict | None) -> State:
    failing_before = bool(previous and previous.get("failing"))
    streak_before = previous.get("streak", 0) if previous else 0
    unverified_before = previous.get("unverified", 0) if previous else 0

    if status in UNVERIFIED:
        # No verdict on the code: keep whatever state the last verified run left,
        # and count the run so FETCH_FAIL can require two in a row before alerting.
        streak = streak_before + 1 if failing_before else 0
        return State("unverified", failing_before, streak, unverified_before + 1)
    if status in PASSING:
        if previous is None:
            return State("first-run", False, 0, 0)
        return State("recovered" if failing_before else "passing", False, 0, 0)
    if previous is None:
        return State("first-run", True, 1, 0)
    if failing_before:
        return State("still-failing", True, streak_before + 1, 0)
    return State("new-failure", True, 1, 0)


def load_results(reports: Path) -> list[dict]:
    results = []
    for path in sorted(reports.rglob("result.json")):
        results.append(json.loads(path.read_text(encoding="utf-8")))
    return results


def load_previous(path: Path | None) -> dict[str, dict]:
    if path is None or not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        return {}
    entries = data.get("carried", []) + data.get("entries", [])
    return {entry["key"]: entry for entry in entries}


def aggregate(plan: dict, results: list[dict], previous: dict[str, dict], run: dict) -> dict:
    entries = []
    built = {(r["id"], r["track"]): r for r in results}
    planned_tracks = {}
    for entry in plan.get("matrix", {}).get("include", []):
        planned_tracks.setdefault(entry["id"], []).append(entry["track"])

    for target in plan.get("targets", []):
        tracks = planned_tracks.get(target["id"]) or ["main"]
        for track in tracks:
            key = f"{target['id']}/{track}"
            result = built.get((target["id"], track))
            if target["status"] != "PLANNED":
                status = target["status"]
            elif result is None:
                # The job did not report: cancelled, timed out, or the runner died.
                status = "INFRA_FAIL"
            else:
                status = result["status"]
            prev = previous.get(key)
            state = classify(status, prev)
            prev_failure = None
            if prev and prev.get("failing"):
                prev_failure = prev.get("failure_status") or prev.get("status")
            # The failure a streak is about; FETCH_FAIL inside a streak does not replace it.
            if not state.failing:
                failure_status = None
            elif status in UNVERIFIED:
                failure_status = prev_failure
            else:
                failure_status = status
            entries.append(
                {
                    "key": key,
                    "id": target["id"],
                    "tier": target["tier"],
                    "track": track,
                    "status": status,
                    "change": state.change,
                    "streak": state.streak,
                    "failing": state.failing,
                    "unverified": state.unverified,
                    "failure_status": failure_status,
                    "previous_failure_status": prev_failure,
                    "previous_unverified": prev.get("unverified", 0) if prev else 0,
                    "used_fallback": bool(result and result.get("used_fallback")),
                    "with_workarounds": bool(result and result.get("with_workarounds")),
                    "plan_errors": target.get("errors", []),
                }
            )
    # A run limited to some targets (a manifest-change dispatch) must not erase
    # the others' history, or their next run would look like a first run. Tracks
    # a built target no longer has (its lock was dropped) are not carried.
    seen = {e["key"] for e in entries}
    carried = [
        {**entry, "change": "not-run"}
        for key, entry in previous.items()
        if key not in seen and entry.get("id") not in planned_tracks
    ]
    return {"schema_version": SCHEMA_VERSION, "run": run, "entries": entries, "carried": carried}


def summary_markdown(report: dict) -> str:
    run = report["run"]
    lines = ["## Results", ""]
    if run.get("previous_run_id"):
        lines.append(f"Compared with run {run['previous_run_id']}.")
    else:
        lines.append("No previous run to compare with.")
    lines += ["", "| Target | Track | Tier | Result | Change |", "| --- | --- | --- | --- | --- |"]
    for e in report["entries"]:
        change = e["change"]
        if change == "still-failing":
            change = f"still failing ({e['streak']} runs)"
        elif change == "unverified" and e["unverified"] > 1:
            change = f"unverified ({e['unverified']} runs)"
        mark = " ⚠️" if e["track"] == "locked" and e["status"] in ("BUILD_FAIL", "MANIFEST_FAIL") else ""
        status = f"{e['status']} (with workarounds)" if e.get("with_workarounds") else e["status"]
        lines.append(f"| `{e['id']}` | {e['track']} | {e['tier']} | {status}{mark} | {change} |")
    if any(e["track"] == "locked" and e["status"] in ("BUILD_FAIL", "MANIFEST_FAIL") for e in report["entries"]):
        lines += ["", "⚠️ A locked manifest no longer builds: the known-good snapshot is gone."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, required=True, help="plan.json from plan.py")
    parser.add_argument("--reports", type=Path, required=True, help="directory holding the job reports")
    parser.add_argument("--previous", type=Path, help="results.json of the previous run")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--previous-run-id", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    run = {"run_id": args.run_id, "previous_run_id": args.previous_run_id or None, "event": plan.get("event")}
    report = aggregate(plan, load_results(args.reports), load_previous(args.previous), run)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary_markdown(report))
    for e in report["entries"]:
        print(f"{e['key']}: {e['status']} ({e['change']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
