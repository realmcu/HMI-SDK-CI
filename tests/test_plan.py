from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import plan  # noqa: E402
from manifest import (  # noqa: E402
    ManifestError,
    anonymous_url,
    compare_pair,
    derive_model,
    load_projects,
)

SHA_A = "a" * 40
SHA_B = "b" * 40

FLOATING = """\
manifest:
  remotes:
    - name: origin
      url-base: {host}
  defaults:
    remote: origin
  projects:
    - name: sdk
      repo-path: realmcu/sdk
      revision: master
      path: sdk
    - name: app
      repo-path: realmcu/hmi
      revision: app-branch
      path: sdk/app
"""

LOCK = f"""\
manifest:
  projects:
    - name: sdk
      url: https://gitee.com/realmcu/sdk
      revision: {SHA_A}
      path: sdk
    - name: app
      url: https://gitee.com/realmcu/hmi
      revision: {SHA_B}
      path: sdk/app
"""

TAGGED = """\
manifest:
  remotes:
    - name: github
      url-base: https://github.com
    - name: gitee
      url-base: ssh://git@gitee.com
  defaults:
    remote: gitee
  projects:
    - name: zsdk
      remote: github
      repo-path: rtkconnectivity/zsdk
      revision: common/v1.0.0.21
      import:
        file: west.yml
    - name: app
      repo-path: realmcu/hmi
      revision: {app_revision}
      path: app
"""

KINDS = {"master": "branch", "app-branch": "branch", "common/v1.0.0.21": "tag"}


def fake_resolver(kinds=KINDS):
    calls = []

    def resolve(url, revision):
        calls.append((url, revision))
        return kinds.get(revision, "missing")

    resolve.calls = calls
    return resolve


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


TARGETS = """\
schema_version: 1
targets:
  - id: e-dashboard
    tier: customer
    manifest: dashboard/e-gitee.yml
    fallback_manifest: dashboard/e-github.yml
    lock_manifest: dashboard/e-gitee.lock.yml
    version_model: floating
    builder: hmi-west
    build: {profile: full}
    toolchain: {arm-gnu: 12.3.rel1}
    triggers: [nightly]
  - id: g-chargebox
    tier: preview
    manifest: chargebox/g-gitee.yml
    version_model: pinned
    builder: hmi-west
    build: {profile: quick}
    toolchain: {arm-gnu: 12.3.rel1}
    triggers: [on-change]
  - id: parked
    tier: disabled
    manifest: parked/parked-gitee.yml
    version_model: pinned
    builder: hmi-west
    build: {profile: quick}
    toolchain: {arm-gnu: 12.3.rel1}
    triggers: [nightly]
"""


@pytest.fixture
def workspace(tmp_path):
    manifests = tmp_path / "manifest"
    write(manifests, "dashboard/e-gitee.yml", FLOATING.format(host="https://gitee.com"))
    write(manifests, "dashboard/e-github.yml", FLOATING.format(host="https://github.com"))
    write(manifests, "dashboard/e-gitee.lock.yml", LOCK)
    write(manifests, "chargebox/g-gitee.yml", TAGGED.format(app_revision=SHA_A))
    targets = tmp_path / "targets.yml"
    targets.write_text(TARGETS, encoding="utf-8")
    return tmp_path, manifests, targets


def run_plan(workspace, *extra, resolver=None):
    tmp_path, manifests, targets = workspace
    output = tmp_path / "plan.json"
    code = plan.main(
        ["--targets", str(targets), "--manifest-root", str(manifests), "--output", str(output), *extra],
        resolve=resolver or fake_resolver(),
    )
    report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
    return code, report


def by_id(report):
    return {t["id"]: t for t in report["targets"]}


# --- manifest.py -------------------------------------------------------------


def test_load_projects_applies_west_defaults(tmp_path):
    write(tmp_path, "m.yml", TAGGED.format(app_revision="app-branch"))
    zsdk, app = load_projects(tmp_path / "m.yml")
    assert zsdk.url == "https://github.com/rtkconnectivity/zsdk"
    assert zsdk.path == "zsdk" and zsdk.import_
    assert app.url == "ssh://git@gitee.com/realmcu/hmi"


def test_load_projects_defaults_revision_to_master(tmp_path):
    write(tmp_path, "m.yml", "manifest:\n  projects:\n    - name: x\n      url: https://h/x\n")
    (project,) = load_projects(tmp_path / "m.yml")
    assert project.revision == "master"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("manifest: [", "cannot parse"),
        ("projects: []\n", "missing top-level 'manifest'"),
        ("manifest:\n  projects: []\n", "no projects"),
        ("manifest:\n  projects:\n    - name: x\n      remote: nope\n", "unknown remote 'nope'"),
    ],
)
def test_load_projects_errors(tmp_path, text, expected):
    write(tmp_path, "m.yml", text)
    with pytest.raises(ManifestError, match=expected):
        load_projects(tmp_path / "m.yml")


@pytest.mark.parametrize(
    ("app_revision", "model", "floating"),
    [
        (SHA_A, "pinned", ()),
        ("app-branch", "mixed", ("app",)),
    ],
)
def test_derive_model_tag_counts_as_pinned(tmp_path, app_revision, model, floating):
    write(tmp_path, "m.yml", TAGGED.format(app_revision=app_revision))
    result = derive_model(load_projects(tmp_path / "m.yml"), fake_resolver())
    assert (result.model, result.floating) == (model, floating)


def test_derive_model_floating_and_sha_skips_remote(tmp_path):
    write(tmp_path, "f.yml", FLOATING.format(host="https://gitee.com"))
    write(tmp_path, "l.yml", LOCK)
    assert derive_model(load_projects(tmp_path / "f.yml"), fake_resolver()).model == "floating"
    resolver = fake_resolver()
    assert derive_model(load_projects(tmp_path / "l.yml"), resolver).model == "pinned"
    assert resolver.calls == []


def test_derive_model_unknown_when_unresolved_or_missing(tmp_path):
    write(tmp_path, "m.yml", FLOATING.format(host="https://gitee.com"))
    projects = load_projects(tmp_path / "m.yml")
    unreachable = derive_model(projects, lambda url, rev: None)
    assert unreachable.model is None and unreachable.unresolved == ("sdk", "app")
    missing = derive_model(projects, fake_resolver({"master": "branch"}))
    assert missing.model is None and missing.missing == ("app",)


def test_compare_pair_ignores_url_only(tmp_path):
    write(tmp_path, "a.yml", FLOATING.format(host="https://gitee.com"))
    write(tmp_path, "b.yml", FLOATING.format(host="https://github.com"))
    write(tmp_path, "c.yml", FLOATING.format(host="https://github.com").replace("app-branch", "other"))
    a, b, c = (load_projects(tmp_path / n) for n in ("a.yml", "b.yml", "c.yml"))
    assert compare_pair(a, b) == []
    assert compare_pair(a, c) == ["app.revision: primary='app-branch' fallback='other'"]
    assert compare_pair(a, b[:1]) == ["project 'app' only in primary"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ssh://git@gitee.com/realmcu/hmi", "https://gitee.com/realmcu/hmi"),
        ("git@gitee.com:realmcu/hmi", "https://gitee.com/realmcu/hmi"),
        ("https://github.com/x/y", "https://github.com/x/y"),
    ],
)
def test_anonymous_url(url, expected):
    assert anonymous_url(url) == expected


# --- plan.py -----------------------------------------------------------------


def test_nightly_plan(workspace):
    code, report = run_plan(workspace, "--event", "nightly")
    assert code == 0
    targets = by_id(report)
    assert list(targets) == ["e-dashboard"]
    assert targets["e-dashboard"]["status"] == "PLANNED"
    assert targets["e-dashboard"]["errors"] == []
    tracks = [
        (e["track"], e["manifest"], e["fallback_manifest"], e["lock_manifest"], e["derive_fallback"])
        for e in report["matrix"]["include"]
    ]
    assert tracks == [
        ("floating", "dashboard/e-gitee.yml", "dashboard/e-github.yml", "dashboard/e-gitee.lock.yml", False),
        ("locked", "dashboard/e-gitee.lock.yml", None, None, True),
    ]


def test_manual_plan_runs_every_enabled_target(workspace):
    code, report = run_plan(workspace)
    assert code == 0
    assert list(by_id(report)) == ["e-dashboard", "g-chargebox"]
    assert [e["track"] for e in report["matrix"]["include"]][-1] == "main"


def test_only_selects_and_rejects_unknown(workspace):
    _, report = run_plan(workspace, "--only", "g-chargebox")
    assert list(by_id(report)) == ["g-chargebox"]
    code, _ = run_plan(workspace, "--only", "nope")
    assert code == 2


def test_missing_manifest_is_infra_fail(workspace):
    _, manifests, _ = workspace
    (manifests / "dashboard/e-github.yml").rename(manifests / "dashboard/renamed.yml")
    code, report = run_plan(workspace, "--event", "nightly")
    assert code == 0
    entry = by_id(report)["e-dashboard"]
    assert entry["status"] == "INFRA_FAIL"
    assert "fallback_manifest 'dashboard/e-github.yml'" in entry["errors"][0]
    assert report["matrix"]["include"] == []


def test_unparseable_primary_is_manifest_fail(workspace):
    _, manifests, _ = workspace
    (manifests / "dashboard/e-gitee.yml").write_text("manifest: [", encoding="utf-8")
    _, report = run_plan(workspace, "--event", "nightly")
    assert by_id(report)["e-dashboard"]["status"] == "MANIFEST_FAIL"


def test_version_model_mismatch_is_reported_but_still_built(workspace):
    _, manifests, _ = workspace
    write(manifests, "chargebox/g-gitee.yml", TAGGED.format(app_revision="app-branch"))
    _, report = run_plan(workspace, "--only", "g-chargebox")
    entry = by_id(report)["g-chargebox"]
    assert entry["derived_model"] == "mixed"
    assert entry["errors"] == ["version_model is 'pinned' but the manifest is 'mixed' (following branches: app)"]
    assert entry["status"] == "PLANNED"
    assert len(report["matrix"]["include"]) == 1


def test_pair_difference_is_an_error(workspace):
    _, manifests, _ = workspace
    write(
        manifests,
        "dashboard/e-github.yml",
        FLOATING.format(host="https://github.com").replace("app-branch", "other"),
    )
    _, report = run_plan(workspace, "--event", "nightly")
    (error,) = by_id(report)["e-dashboard"]["errors"]
    assert error.startswith("manifest and fallback_manifest differ: app.revision")


def test_mirror_difference_is_an_error_and_mirror_is_not_a_fallback(workspace):
    tmp_path, manifests, targets = workspace
    write(manifests, "chargebox/g-github.yml", TAGGED.format(app_revision=SHA_B))
    text = targets.read_text(encoding="utf-8").replace(
        "    manifest: chargebox/g-gitee.yml\n",
        "    manifest: chargebox/g-gitee.yml\n    mirror_manifest: chargebox/g-github.yml\n",
    )
    targets.write_text(text, encoding="utf-8")
    _, report = run_plan(workspace, "--event", "manual")
    (error,) = by_id(report)["g-chargebox"]["errors"]
    assert error.startswith("manifest and mirror_manifest differ: app.revision")
    assert "mirror=" in error
    (entry,) = [e for e in report["matrix"]["include"] if e["id"] == "g-chargebox"]
    assert entry["fallback_manifest"] is None


def test_unpinned_lock_is_an_error(workspace):
    _, manifests, _ = workspace
    write(manifests, "dashboard/e-gitee.lock.yml", LOCK.replace(SHA_B, "master"))
    _, report = run_plan(workspace, "--event", "nightly")
    assert by_id(report)["e-dashboard"]["errors"] == ["lock_manifest has projects not pinned to a SHA: app"]


def test_unreachable_remote_is_a_warning(workspace):
    _, report = run_plan(workspace, "--event", "nightly", resolver=lambda url, rev: None)
    entry = by_id(report)["e-dashboard"]
    assert entry["errors"] == []
    assert entry["derived_model"] is None
    assert "version model not checked" in entry["warnings"][0]


def test_summary_and_github_output(workspace, monkeypatch):
    tmp_path, _, _ = workspace
    summary = tmp_path / "summary.md"
    gh_output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))
    run_plan(workspace, "--event", "nightly", "--summary", str(summary))
    assert "| `e-dashboard` | customer | PLANNED | floating | — |" in summary.read_text(encoding="utf-8")
    outputs = dict(line.split("=", 1) for line in gh_output.read_text(encoding="utf-8").splitlines())
    assert outputs["errors"] == "0"
    assert outputs["jobs"] == "2"
    assert len(json.loads(outputs["matrix"])["include"]) == 2


def test_manifest_root_required(workspace):
    _, _, targets = workspace
    with pytest.raises(SystemExit):
        plan.main(["--targets", str(targets)])
