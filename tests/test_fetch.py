from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fetch  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fetch"

# Fetch-step excerpts of failed realmcu/HMI-MANIFEST nightly runs (network-run<id>.log)
# and of west update against deliberately broken manifests (content-*.log).
REPLAY = {
    "network-run31835122621.log": ("network", ("rtl87x3ep-hmi-sdk",)),
    "network-run31967654841.log": ("network", None),
    "network-run33686748354.log": ("network", ("rtl87x3ep-hmi-sdk", "hmi-dashboard", "wearable", "HoneyGUI", "display", "ui")),
    "network-run33992052978.log": ("network", None),
    "network-run34060373307.log": ("network", None),
    "network-run35789767480.log": ("network", ("wearable",)),
    "network-run35926959865.log": ("network", None),
    "network-run36938772517.log": ("network", None),
    "content-notfound.log": ("content", ("x",)),
    "content-private.log": ("content", ("x",)),
    "content-badsha.log": ("content", ("x",)),
    "content-badbranch.log": ("content", ("x",)),
}


def test_every_fixture_is_covered():
    assert sorted(p.name for p in FIXTURES.glob("*.log")) == sorted(REPLAY)


@pytest.mark.parametrize(("name", "expected"), sorted(REPLAY.items()))
def test_replay_classification(name, expected):
    kind, projects = expected
    failure = fetch.classify((FIXTURES / name).read_text(encoding="utf-8"))
    assert failure.kind == kind, failure.reason
    if projects is not None:
        assert failure.projects == projects


def test_content_evidence_outranks_network():
    log = "error: RPC failed; HTTP 429\nfatal: couldn't find remote ref feature\n"
    assert fetch.classify(log).kind == "content"


def test_unknown_failure_keeps_last_error_line():
    failure = fetch.classify("--- x: fetching\nfatal: something new\nERROR: update failed for project x\n")
    assert failure.kind == "unknown"
    assert failure.reason == "ERROR: update failed for project x"


def test_urls_are_extracted():
    failure = fetch.classify((FIXTURES / "network-run31835122621.log").read_text(encoding="utf-8"))
    assert failure.urls == ("https://gitee.com/realmcu/rtl87x3ep-hmi-sdk",)


# --- fetch() with a scripted west -----------------------------------------------

MANIFEST = """\
manifest:
  remotes:
    - {name: gitee, url-base: https://gitee.com}
    - {name: github, url-base: https://github.com}
  defaults: {remote: gitee}
  projects:
    - {name: app, repo-path: realmcu/hmi, revision: main, path: app}
    - {name: zsdk, remote: github, repo-path: rtk/zsdk, revision: v1, path: zsdk}
"""

LOCK = """\
manifest:
  projects:
    - {name: app, url: https://gitee.com/realmcu/hmi, revision: %s, path: app}
""" % ("a" * 40)

NETWORK_APP = "fatal: unable to access 'https://gitee.com/realmcu/hmi/': SSL connection timeout\nERROR: update failed for project app\n"
NETWORK_ZSDK = "error: RPC failed; curl 56 GnuTLS recv error (-110)\nERROR: update failed for project zsdk\n"
CONTENT_APP = "fatal: couldn't find remote ref main\nERROR: update failed for project app\n"


class FakeWest:
    """Plays back west update results per manifest; init/config always succeed."""

    def __init__(self, outcomes: dict[str, list[str | None]]):
        self.outcomes = {k: list(v) for k, v in outcomes.items()}
        self.manifest = None
        self.updates: list[str] = []

    def __call__(self, command, cwd):
        if command[:2] == ["west", "config"]:
            self.manifest = command[-1]
        if command[:2] != ["west", "update"]:
            return 0, ""
        self.updates.append(self.manifest)
        error = self.outcomes[self.manifest].pop(0)
        return (0, "ok\n") if error is None else (1, error)


@pytest.fixture
def workspace(tmp_path):
    manifests = tmp_path / ".manifest" / "dashboard"
    manifests.mkdir(parents=True)
    (tmp_path / ".manifest" / ".git" / "info").mkdir(parents=True)
    (manifests / "p-gitee.yml").write_text(MANIFEST, encoding="utf-8")
    (manifests / "p-gitee.lock.yml").write_text(LOCK, encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    return tmp_path, logs


def run(workspace, west, manifest="dashboard/p-gitee.yml", fallback="dashboard/p-github.yml", derive=False):
    root, logs = workspace
    return fetch.fetch(root, ".manifest", manifest, fallback, derive, 3, 2, logs, run=west, sleep=lambda s: None)


def test_success_first_try(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [None]})
    result = run(workspace, west)
    assert (result.status, result.used_fallback, len(result.attempts)) == ("OK", False, 1)


def test_network_retry_then_success(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [NETWORK_APP, None]})
    result = run(workspace, west)
    assert result.status == "OK" and not result.used_fallback
    assert west.updates == ["dashboard/p-gitee.yml"] * 2


def test_content_failure_is_not_retried(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [CONTENT_APP]})
    result = run(workspace, west)
    assert result.status == "MANIFEST_FAIL"
    assert west.updates == ["dashboard/p-gitee.yml"]


def test_gitee_network_failure_falls_back(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [NETWORK_APP] * 3, "dashboard/p-github.yml": [None]})
    result = run(workspace, west)
    assert (result.status, result.used_fallback, result.manifest) == ("OK", True, "dashboard/p-github.yml")


def test_github_network_failure_has_no_fallback(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [NETWORK_ZSDK] * 3})
    result = run(workspace, west)
    assert result.status == "FETCH_FAIL"
    assert set(west.updates) == {"dashboard/p-gitee.yml"}


def test_both_hosts_down_is_fetch_fail(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [NETWORK_APP] * 3, "dashboard/p-github.yml": [NETWORK_APP] * 2})
    result = run(workspace, west)
    assert (result.status, result.used_fallback) == ("FETCH_FAIL", True)
    assert len(result.attempts) == 5


def test_no_fallback_configured(workspace):
    west = FakeWest({"dashboard/p-gitee.yml": [NETWORK_APP] * 3})
    assert run(workspace, west, fallback=None).status == "FETCH_FAIL"


def test_locked_track_derives_mirror(workspace):
    root, _ = workspace
    west = FakeWest({"dashboard/p-gitee.lock.yml": [NETWORK_APP] * 3, "dashboard/p-github.lock.yml": [None]})
    result = run(workspace, west, manifest="dashboard/p-gitee.lock.yml", fallback=None, derive=True)
    assert (result.status, result.manifest) == ("OK", "dashboard/p-github.lock.yml")
    derived = (root / ".manifest/dashboard/p-github.lock.yml").read_text(encoding="utf-8")
    assert "https://github.com/realmcu/hmi" in derived and "gitee" not in derived
    exclude = (root / ".manifest/.git/info/exclude").read_text(encoding="utf-8")
    assert "/dashboard/p-github.lock.yml" in exclude


def test_derive_primary_fetches_only_the_github_copy(workspace):
    root, logs = workspace
    west = FakeWest({"dashboard/p-github.lock.yml": [NETWORK_APP] * 3})
    result = fetch.fetch(root, ".manifest", "dashboard/p-gitee.lock.yml", None, False, 3, 2, logs,
                         run=west, sleep=lambda s: None, derive_primary=True)
    assert (result.status, result.manifest, result.used_fallback) == ("FETCH_FAIL", "dashboard/p-github.lock.yml", False)
    assert set(west.updates) == {"dashboard/p-github.lock.yml"}


def test_derive_primary_excludes_fallbacks(workspace):
    root, logs = workspace
    with pytest.raises(SystemExit):
        fetch.main(["--workspace", str(root), "--manifest", "dashboard/p-gitee.lock.yml", "--report-dir", str(logs),
                    "--derive-primary", "--derive-fallback"])


def test_derive_refuses_leftover_gitee_url(tmp_path):
    source = tmp_path / "a.yml"
    source.write_text("manifest:\n  remotes:\n    - {name: g, url-base: ssh://git@gitee.com}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not rewrite"):
        fetch.derive_mirror_manifest(source, tmp_path / "b.yml")


def test_main_writes_report_and_env(workspace, monkeypatch):
    root, logs = workspace
    env_file = root / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    west = FakeWest({"dashboard/p-gitee.yml": [CONTENT_APP]})
    monkeypatch.setattr(fetch, "run_streaming", west)
    monkeypatch.setattr(fetch.fetch, "__defaults__", (west, lambda s: None, False))
    code = fetch.main(["--workspace", str(root), "--manifest", "dashboard/p-gitee.yml", "--report-dir", str(logs)])
    assert code == 1
    assert json.loads((logs / "fetch.json").read_text(encoding="utf-8"))["status"] == "MANIFEST_FAIL"
    assert "FETCH_STATUS=MANIFEST_FAIL" in env_file.read_text(encoding="utf-8")
