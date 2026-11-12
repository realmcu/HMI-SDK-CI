from __future__ import annotations

import copy
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from adapters import zephyr_west  # noqa: E402
from targets import ConfigError, parse_targets  # noqa: E402

# The adapter runs on Linux runners; on Windows, `bash` may resolve to WSL and file
# modes have no executable bit.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX bash and file modes")

BUILD = {
    "workarounds": ["d12-restore-exec-bits"],
    "products": [
        {"name": "app", "app": "applications/app", "board": "rtl87x3g_evb"},
        {"name": "sample", "app": "samples/demo", "board": "rtl87x3g_sample",
         "workarounds": ["d11-skip-version-generation"]},
    ],
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    """A workspace whose zsdk is a git repository with the tools tracked as 100644."""
    workspace = tmp_path / "ws"
    sdk = workspace / zephyr_west.SDK_DIR
    for rel in (*zephyr_west.EXEC_BIT_FILES, zephyr_west.FETCH_ASSETS):
        (sdk / rel).parent.mkdir(parents=True, exist_ok=True)
        (sdk / rel).write_text("#!/bin/sh\n")
    for app in ("applications/app", "samples/demo"):
        (sdk / app).mkdir(parents=True)
    git(sdk, "init", "-q")
    git(sdk, "config", "core.fileMode", "true")
    git(sdk, "add", ".")
    git(sdk, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    monkeypatch.setattr(zephyr_west, "fetch_sdk_tag", lambda workspace, sdk: "common/v1.0.0.21")
    d11 = zephyr_west.WORKAROUNDS["d11-skip-version-generation"]
    monkeypatch.setitem(zephyr_west.WORKAROUNDS, "d11-skip-version-generation",
                        dataclasses.replace(d11, probe=lambda sdk: True))
    return workspace, sdk


class FakeRunner:
    """Records commands; a build "succeeds" by writing the signed image unless told to fail."""

    def __init__(self, fail: set[str] | None = None, unsigned: set[str] | None = None, assets: int = 0):
        self.fail = fail or set()
        self.unsigned = unsigned or set()
        self.assets = assets
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command: list[str], cwd: Path, log: Path) -> int:
        self.calls.append((command, cwd))
        log.parent.mkdir(parents=True, exist_ok=True)
        if command[-1] == zephyr_west.FETCH_ASSETS:
            log.write_text("assets\n")
            return self.assets
        name = cwd.name
        if name in self.fail:
            log.write_text("[1/2] cc main.c\nmain.c:3:1: error: boom\nFAILED: app\n")
            return 1
        bin_dir = cwd / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "app.bin").write_bytes(b"x")
        if name not in self.unsigned:
            (bin_dir / "app_MP-v0.0.0.0-abc-123.bin").write_bytes(b"signed")
        log.write_text("ok\n")
        return 0


def run(sdk_fixture, runner, build=None):
    workspace, _ = sdk_fixture
    report = workspace.parent / "report"
    code = zephyr_west.main(
        ["--workspace", str(workspace), "--build", json.dumps(build or BUILD),
         "--toolchain", '{"zephyr-sdk": "0.16.9"}', "--report-dir", str(report)],
        run=runner,
    )
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    return code, summary, (report / "summary.md").read_text(encoding="utf-8")


def test_pass_with_workarounds(sdk):
    runner = FakeRunner()
    code, summary, markdown = run(sdk, runner)
    assert code == 0 and summary["result"] == "PASS" and summary["with_workarounds"]
    assert summary["modes"]["app"]["workarounds"] == ["d12-restore-exec-bits"]
    assert summary["modes"]["sample"]["cmake_args"] == ["-DENABLE_GENERATE_VERSION=OFF"]
    builds = [(c, cwd.name) for c, cwd in runner.calls if c[0] == "west"]
    assert builds[0] == (["west", "build", "-b", "rtl87x3g_evb", "-p", "always", "-d", "build"], "app")
    assert builds[1][0][-2:] == ["--", "-DENABLE_GENERATE_VERSION=OFF"]
    assert summary["workarounds"]["d12-restore-exec-bits"]["defect_present"] is True
    assert "**PASS (with workarounds)**" in markdown
    assert "defect still present" in markdown


@posix_only
def test_exec_bits_restored_before_building(sdk):
    _, sdk_dir = sdk
    tool = sdk_dir / zephyr_west.EXEC_BIT_FILES[0]
    tool.chmod(0o644)
    run(sdk, FakeRunner())
    assert tool.stat().st_mode & 0o111


def test_fixed_defect_asks_to_remove_workaround(sdk):
    _, sdk_dir = sdk
    git(sdk_dir, "update-index", "--chmod=+x", *zephyr_west.EXEC_BIT_FILES)
    _, summary, markdown = run(sdk, FakeRunner())
    assert summary["workarounds"]["d12-restore-exec-bits"]["defect_present"] is False
    assert "remove this workaround" in markdown


def test_worst_product_decides_and_failure_has_excerpt(sdk):
    code, summary, markdown = run(sdk, FakeRunner(fail={"demo"}))
    assert code == 1 and summary["result"] == "FAIL" and not summary["with_workarounds"]
    assert summary["failed_modes"] == ["sample"]
    assert summary["modes"]["app"]["status"] == "PASS"
    assert "error: boom" in summary["modes"]["sample"]["excerpt"]
    assert "sample log excerpt" in markdown


def test_excerpt_clips_compiler_command(tmp_path):
    log = tmp_path / "build.log"
    log.write_text("FAILED: x.obj\n" + "gcc " + "-I/very/long/path " * 500 + "\nx.c:1:1: error: boom\n")
    lines = zephyr_west.log_excerpt(log).splitlines()
    assert len(lines[1]) <= zephyr_west.EXCERPT_LINE_CHARS + 6 and lines[1].endswith("[...]")
    assert lines[2] == "x.c:1:1: error: boom"


def test_missing_signed_image_fails(sdk):
    code, summary, _ = run(sdk, FakeRunner(unsigned={"app"}))
    assert code == 1
    assert summary["modes"]["app"]["message"].startswith("signed image missing")


def test_missing_app_directory_fails_that_product(sdk):
    build = copy.deepcopy(BUILD)
    build["products"][0]["app"] = "applications/missing"
    code, summary, _ = run(sdk, FakeRunner(), build)
    assert code == 1 and summary["modes"]["app"]["message"].startswith("application directory not found")


def test_asset_failure_is_environment_error(sdk):
    code, summary, _ = run(sdk, FakeRunner(assets=1))
    assert code == 2 and summary["result"] == "ERROR"


def test_unknown_workaround_is_environment_error(sdk):
    build = {**BUILD, "workarounds": ["nope"]}
    code, summary, _ = run(sdk, FakeRunner(), build)
    assert code == 2 and "nope" in summary["error"]


@posix_only
def test_version_probe_reads_scratch_file(tmp_path):
    sdk = tmp_path / "sdk"
    script = sdk / "tools" / "Gadgets" / "version_gen_config.sh"
    script.parent.mkdir(parents=True)
    (sdk / "samples").mkdir()
    script.write_text('printf "VERSION_MAJOR = 1\\n" > "$1"\n')
    assert zephyr_west._version_generation_broken(sdk) is False
    script.write_text('echo "sed: unknown option to s" >&2; : > "$1"\n')
    assert zephyr_west._version_generation_broken(sdk) is True


# --- targets.yml validation for zephyr-west ----------------------------------

TARGET = {
    "id": "rtl8773g-x", "tier": "preview", "manifest": "x/x-github.yml", "version_model": "pinned",
    "builder": "zephyr-west", "build": BUILD, "toolchain": {"zephyr-sdk": "0.16.9"}, "triggers": ["nightly"],
}


def errors_of(**changes) -> list[str]:
    raw = {**copy.deepcopy(TARGET), **changes}
    with pytest.raises(ConfigError) as info:
        parse_targets({"schema_version": 1, "targets": [raw]})
    return info.value.errors


def test_valid_zephyr_target():
    (parsed,) = parse_targets({"schema_version": 1, "targets": [copy.deepcopy(TARGET)]})
    assert parsed.build["products"][1]["workarounds"] == ["d11-skip-version-generation"]


def test_customer_target_must_not_use_workarounds():
    errors = errors_of(tier="customer")
    assert any("customer targets must not use workarounds" in e for e in errors)


def product(**changes):
    return {"name": "p", "app": "samples/p", "board": "rtl87x3g_sample", **changes}


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ({}, ".build.products: missing required field"),
        ({"products": []}, ".build.products: must be a non-empty list"),
        ({"products": [product(name="P")]}, ".name: must be lowercase"),
        ({"products": [product(), product()]}, "duplicate name 'p'"),
        ({"products": [product(app="../x")]}, ".app: must be a relative path"),
        ({"products": [product(board="RTL")]}, ".board: must be a Zephyr board name"),
        ({"products": [product(cmake_args=["FOO=1"])]}, "must be -DNAME=value"),
        ({"products": [product(cmake_args=["-DENABLE_GENERATE_VERSION=OFF"])]}, "declare it under workarounds"),
        ({"products": [product(workarounds=["nope"])]}, "unknown workaround 'nope'"),
        ({"products": [product(workarounds=["d12-restore-exec-bits"])]}, "must be declared in build.workarounds"),
        ({"workarounds": ["d11-skip-version-generation"], "products": [product()]},
         "must be declared in each product's workarounds"),
        ({"products": [product(extra=1)]}, ".extra: unknown field"),
    ],
)
def test_zephyr_build_errors(build, expected):
    errors = errors_of(build=build)
    assert any(expected in e for e in errors), errors


def test_mirror_and_fallback_are_exclusive():
    errors = errors_of(mirror_manifest="x/x-gitee.yml", fallback_manifest="x/x-gitee2.yml")
    assert any("use one of the two" in e for e in errors)
