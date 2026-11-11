from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import poll  # noqa: E402
from targets import Target  # noqa: E402

DAY1 = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
DAY1_LATER = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)


def target(id, manifest, triggers=("on-change",), tier="customer", lock=None):
    return Target(id, tier, manifest, "pinned", "hmi-west", {}, {}, tuple(triggers), lock_manifest=lock)


TARGETS = [
    target("e", "dashboard/e-gitee.yml", lock="dashboard/e-gitee.lock.yml"),
    target("g", "chargebox/g-gitee.yml"),
    target("night", "lvgl/n-gitee.yml", triggers=("nightly",)),
    target("off", "x/off.yml", tier="disabled"),
]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    for path in ("dashboard/e-gitee.yml", "chargebox/g-gitee.yml", "README.md"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "1")
    return tmp_path


def commit(repo, path, text="v2\n"):
    (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path)
    return git(repo, "rev-parse", "HEAD")


def test_first_poll_records_head_without_triggering(repo):
    head = git(repo, "rev-parse", "HEAD")
    state, decision = poll.poll({}, head, TARGETS, repo, DAY1)
    assert decision == {"changed": False, "targets": [], "commit": True, "head": head}
    assert state["manifest_head"] == head


def test_unchanged_head_skips_and_commits_once_a_day(repo):
    head = git(repo, "rev-parse", "HEAD")
    state, _ = poll.poll({}, head, TARGETS, repo, DAY1)
    state, decision = poll.poll(state, head, TARGETS, repo, DAY1_LATER)
    assert decision["changed"] is False and decision["commit"] is False
    _, decision = poll.poll(state, head, TARGETS, repo, DAY2)
    assert decision["changed"] is False and decision["commit"] is True


def test_change_triggers_only_affected_on_change_targets(repo):
    state, _ = poll.poll({}, git(repo, "rev-parse", "HEAD"), TARGETS, repo, DAY1)
    head = commit(repo, "chargebox/g-gitee.yml")
    state, decision = poll.poll(state, head, TARGETS, repo, DAY1_LATER)
    assert decision["targets"] == ["g"] and decision["commit"] is True
    head = commit(repo, "README.md")
    _, decision = poll.poll(state, head, TARGETS, repo, DAY1_LATER)
    assert decision["changed"] is True and decision["targets"] == []


def test_lock_change_affects_its_target(repo):
    state, _ = poll.poll({}, git(repo, "rev-parse", "HEAD"), TARGETS, repo, DAY1)
    (repo / "dashboard/e-gitee.lock.yml").write_text("lock\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "lock")
    _, decision = poll.poll(state, git(repo, "rev-parse", "HEAD"), TARGETS, repo, DAY1_LATER)
    assert decision["targets"] == ["e"]


def test_unknown_previous_commit_affects_every_on_change_target(repo):
    state = {"version": 1, "manifest_head": "f" * 40, "last_commit_day": "2026-11-11"}
    _, decision = poll.poll(state, git(repo, "rev-parse", "HEAD"), TARGETS, repo, DAY1)
    assert decision["targets"] == ["e", "g"]


def test_main(repo, tmp_path, capsys):
    targets = REPO_ROOT / "targets.yml"
    state = tmp_path / "state.json"
    output = tmp_path / "gh_output"
    args = ["--state", str(state), "--manifest-root", str(repo), "--targets", str(targets), "--github-output", str(output)]
    assert poll.main(args) == 0
    assert '"commit": true' in capsys.readouterr().out
    head = git(repo, "rev-parse", "HEAD")
    assert poll.load_state(state)["manifest_head"] == head
    assert output.read_text(encoding="utf-8") == f"head={head}\ntargets=\ncommit=true\n"
