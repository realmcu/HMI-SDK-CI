from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import plan  # noqa: E402
from targets import ConfigError, load_targets, parse_targets  # noqa: E402

VALID_TARGET = {
    "id": "rtl8773e-dashboard",
    "tier": "customer",
    "manifest": "dashboard/rtl8773e-dashboard-gitee.yml",
    "fallback_manifest": "dashboard/rtl8773e-dashboard-github.yml",
    "lock_manifest": "dashboard/rtl8773e-dashboard-gitee.lock.yml",
    "version_model": "floating",
    "builder": "hmi-west",
    "build": {"profile": "full"},
    "toolchain": {"arm-gnu": "12.3.rel1"},
    "triggers": ["nightly"],
    "timeout": 60,
}


def config(*targets: dict) -> dict:
    return {"schema_version": 1, "targets": list(targets) or [copy.deepcopy(VALID_TARGET)]}


def target(**changes) -> dict:
    result = copy.deepcopy(VALID_TARGET)
    for key, value in changes.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def errors_of(data) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse_targets(data)
    return info.value.errors


def test_repository_targets_file_is_valid():
    targets = load_targets(REPO_ROOT / "targets.yml")
    assert [t.id for t in targets] == ["rtl8773e-dashboard"]


def test_valid_target_round_trips():
    (parsed,) = parse_targets(config())
    assert parsed.lock_manifest == "dashboard/rtl8773e-dashboard-gitee.lock.yml"
    assert parsed.triggers == ("nightly",)
    assert parsed.artifacts == ()


def test_minimal_pinned_preview_target():
    minimal = target(
        id="minimal",
        tier="preview",
        version_model="pinned",
        fallback_manifest=None,
        lock_manifest=None,
        timeout=None,
    )
    (parsed,) = parse_targets(config(minimal))
    assert parsed.fallback_manifest is None
    assert parsed.timeout is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, "top level: must be a mapping"),
        ({"targets": [VALID_TARGET]}, "schema_version: missing required field"),
        ({"schema_version": 2, "targets": [VALID_TARGET]}, "schema_version: must be 1"),
        ({"schema_version": True, "targets": [VALID_TARGET]}, "schema_version: must be 1"),
        ({"schema_version": 1, "targets": []}, "targets: must be a non-empty list"),
        ({"schema_version": 1, "targets": [VALID_TARGET], "extra": 1}, "extra: unknown field"),
    ],
)
def test_top_level_errors(data, expected):
    assert any(expected in e for e in errors_of(data))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"tier": None}, ".tier: missing required field"),
        ({"owner": ["a@b"]}, ".owner: unknown field"),
        ({"id": "RTL8773E"}, ".id: must be lowercase"),
        ({"id": "a--b"}, ".id: must be lowercase"),
        ({"tier": "released"}, ".tier: must be one of"),
        ({"version_model": "locked"}, ".version_model: must be one of"),
        ({"builder": "keil"}, ".builder: must be one of"),
        ({"manifest": "../x.yml"}, ".manifest: must be a relative path"),
        ({"manifest": "/abs/x.yml"}, ".manifest: must be a relative path"),
        ({"manifest": "dashboard/x.yaml"}, ".manifest: must name a .yml file"),
        ({"manifest": "dash board/x.yml"}, ".manifest: contains characters"),
        ({"build": "full"}, ".build: must be a mapping"),
        ({"build": {"profile": "debug"}}, ".build.profile: must be one of"),
        ({"build": {"profile": "full", "jobs": 4}}, ".build.jobs: unknown field"),
        ({"toolchain": {}}, ".toolchain: must be a non-empty mapping"),
        ({"toolchain": {"arm-gnu": 12.3}}, ".toolchain.arm-gnu: version must be a quoted string"),
        ({"toolchain": {"Arm GNU": "12.3"}}, ".toolchain: tool name must be"),
        ({"toolchain": {"zephyr-sdk": "0.16.8"}}, ".toolchain.zephyr-sdk: unknown tool"),
        ({"toolchain": {"arm-gnu": "14.1.rel1"}}, ".toolchain.arm-gnu: no pinned release"),
        ({"triggers": []}, ".triggers: must not be empty"),
        ({"triggers": ["push"]}, ".triggers: must be one of"),
        ({"triggers": ["nightly", "nightly"]}, ".triggers: duplicate entry"),
        ({"artifacts": ["../escape.bin"]}, ".artifacts[0]: must be a relative path"),
        ({"owners": "a@b"}, ".owners: must be a list"),
        ({"timeout": 0}, ".timeout: must be whole minutes"),
        ({"timeout": 361}, ".timeout: must be whole minutes"),
        ({"timeout": True}, ".timeout: must be whole minutes"),
        ({"timeout": "60"}, ".timeout: must be whole minutes"),
    ],
)
def test_field_errors(changes, expected):
    errors = errors_of(config(target(**changes)))
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"fallback_manifest": VALID_TARGET["manifest"]}, ".fallback_manifest: must differ from manifest"),
        ({"lock_manifest": VALID_TARGET["manifest"]}, ".lock_manifest: must differ from manifest"),
        (
            {"fallback_manifest": VALID_TARGET["lock_manifest"]},
            ".fallback_manifest: must differ from lock_manifest",
        ),
        ({"version_model": "pinned"}, ".lock_manifest: requires version_model floating"),
        (
            {"version_model": "mixed", "lock_manifest": None},
            "customer targets must be pinned, or floating with a lock_manifest (got mixed)",
        ),
        (
            {"lock_manifest": None},
            "customer targets must be pinned, or floating with a lock_manifest (got floating)",
        ),
    ],
)
def test_cross_field_errors(changes, expected):
    errors = errors_of(config(target(**changes)))
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("model", ["mixed", "floating"])
def test_preview_accepts_any_version_model(model):
    parse_targets(config(target(tier="preview", version_model=model, lock_manifest=None)))


def test_all_errors_are_reported_together():
    errors = errors_of(config(target(tier="x"), target(id="other", builder="y")))
    assert any("targets[0] (rtl8773e-dashboard).tier" in e for e in errors)
    assert any("targets[1] (other).builder" in e for e in errors)


def test_duplicate_ids():
    errors = errors_of(config(target(), target()))
    assert "targets: duplicate id 'rtl8773e-dashboard'" in errors


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    path = tmp_path / "targets.yml"
    path.write_text("schema_version: 1\nschema_version: 1\ntargets: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'schema_version'"):
        load_targets(path)


def test_unsafe_yaml_tags_are_rejected(tmp_path):
    path = tmp_path / "targets.yml"
    path.write_text("!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_targets(path)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read file"):
        load_targets(tmp_path / "absent.yml")


def test_plan_validate_exit_codes(tmp_path, capsys):
    assert plan.main(["--validate"]) == 0
    assert "1 target(s) valid: rtl8773e-dashboard" in capsys.readouterr().out

    bad = tmp_path / "targets.yml"
    bad.write_text("schema_version: 1\ntargets:\n  - id: x\n", encoding="utf-8")
    assert plan.main(["--validate", "--targets", str(bad)]) == 1
    assert ".tier: missing required field" in capsys.readouterr().err
    assert plan.main(["--targets", str(bad), "--manifest-root", str(tmp_path)]) == 1
