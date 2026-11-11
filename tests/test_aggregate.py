from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import aggregate  # noqa: E402


def plan(status="PLANNED", tracks=("floating", "locked")):
    include = [{"id": "dash", "track": t} for t in tracks] if status == "PLANNED" else []
    return {
        "event": "nightly",
        "matrix": {"include": include},
        "targets": [{"id": "dash", "tier": "customer", "status": status, "errors": []}],
    }


def results(**by_track):
    return [{"id": "dash", "track": track, "status": status} for track, status in by_track.items()]


def run_sequence(statuses, track="floating"):
    """Feed one status per run through aggregate(), carrying results.json forward."""
    previous = {}
    entries = []
    for i, status in enumerate(statuses):
        report = aggregate.aggregate(plan(tracks=(track,)), results(**{track: status}), previous, {"run_id": str(i)})
        (entry,) = report["entries"]
        entries.append(entry)
        previous = {e["key"]: e for e in report["entries"]}
    return entries


def test_pass_fail_fail_pass():
    entries = run_sequence(["PASS", "BUILD_FAIL", "BUILD_FAIL", "PASS"])
    assert [(e["change"], e["streak"]) for e in entries] == [
        ("first-run", 0),
        ("new-failure", 1),
        ("still-failing", 2),
        ("recovered", 0),
    ]


def test_fetch_fail_does_not_break_or_start_a_streak():
    entries = run_sequence(["PASS", "FETCH_FAIL", "FETCH_FAIL", "PASS"])
    assert [(e["change"], e["failing"], e["unverified"]) for e in entries] == [
        ("first-run", False, 0),
        ("unverified", False, 1),
        ("unverified", False, 2),
        ("passing", False, 0),
    ]


def test_fetch_fail_inside_a_failure_keeps_it_failing():
    entries = run_sequence(["BUILD_FAIL", "BUILD_FAIL", "FETCH_FAIL", "BUILD_FAIL", "PASS"])
    assert [e["change"] for e in entries] == [
        "first-run", "still-failing", "unverified", "still-failing", "recovered",
    ]
    assert entries[3]["streak"] == 4


def test_failure_kind_change_is_still_failing():
    entries = run_sequence(["PASS", "BUILD_FAIL", "MANIFEST_FAIL"])
    assert entries[2]["change"] == "still-failing"


def test_missing_job_result_is_infra_fail():
    report = aggregate.aggregate(plan(), results(floating="PASS"), {}, {})
    statuses = {e["track"]: e["status"] for e in report["entries"]}
    assert statuses == {"floating": "PASS", "locked": "INFRA_FAIL"}


def test_plan_level_failure_is_recorded():
    report = aggregate.aggregate(plan(status="INFRA_FAIL"), [], {}, {})
    (entry,) = report["entries"]
    assert (entry["key"], entry["status"]) == ("dash/main", "INFRA_FAIL")


def test_locked_failure_is_flagged_in_summary():
    report = aggregate.aggregate(plan(), results(floating="PASS", locked="BUILD_FAIL"), {}, {})
    assert "known-good snapshot is gone" in aggregate.summary_markdown(report)


def test_main_round_trip(tmp_path):
    (tmp_path / "plan.json").write_text(json.dumps(plan()), encoding="utf-8")
    job = tmp_path / "reports" / "report-dash-floating"
    job.mkdir(parents=True)
    (job / "result.json").write_text(json.dumps(results(floating="BUILD_FAIL")[0]), encoding="utf-8")
    job2 = tmp_path / "reports" / "report-dash-locked"
    job2.mkdir()
    (job2 / "result.json").write_text(json.dumps(results(locked="PASS")[0]), encoding="utf-8")
    previous = tmp_path / "prev.json"
    previous.write_text(
        json.dumps({"schema_version": 1, "entries": [{"key": "dash/floating", "status": "PASS", "failing": False}]}),
        encoding="utf-8",
    )
    out = tmp_path / "results.json"
    summary = tmp_path / "summary.md"
    assert aggregate.main([
        "--plan", str(tmp_path / "plan.json"), "--reports", str(tmp_path / "reports"),
        "--previous", str(previous), "--previous-run-id", "41", "--output", str(out), "--summary", str(summary),
    ]) == 0
    entries = {e["key"]: e for e in json.loads(out.read_text(encoding="utf-8"))["entries"]}
    assert entries["dash/floating"]["change"] == "new-failure"
    assert entries["dash/locked"]["change"] == "first-run"
    assert "Compared with run 41." in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", [None, {"schema_version": 99, "entries": []}])
def test_unusable_previous_is_ignored(tmp_path, content):
    path = tmp_path / "prev.json"
    if content is not None:
        path.write_text(json.dumps(content), encoding="utf-8")
    assert aggregate.load_previous(path) == {}
