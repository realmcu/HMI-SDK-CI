from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import toolchain  # noqa: E402
from adapters import hmi_west  # noqa: E402

# Stand-in for the manifest repository's verify_dashboard.py: writes a report in
# the same layout (<report-dir>/<run>/summary.json) and exits like the real one.
FAKE_VERIFY = textwrap.dedent(
    """\
    import argparse, json, sys
    from pathlib import Path
    p = argparse.ArgumentParser()
    for a in ("--gitee", "--profile", "--jobs", "--report-dir"):
        p.add_argument(a)
    args = p.parse_args()
    outcome = Path(args.gitee, "outcome").read_text().strip()
    if outcome == "crash":
        sys.exit(2)
    run = Path(args.report_dir, "20261111-000000")
    run.mkdir(parents=True)
    log = run / "src_bank0.log"
    log.write_text("ok\\nmain.c:1: error: boom\\n")
    status = "PASS" if outcome == "pass" else "FAIL"
    summary = {
        "overall": status, "profile": args.profile, "total_build_duration_seconds": 3.0,
        "tool_versions": {"arm-none-eabi-gcc": "arm-none-eabi-gcc 12.3.1"},
        "sources": {"gitee": {"repositories": [{"name": "sdk", "commit": "abc"}], "builds": {
            "src_bank0": {"status": status, "duration_seconds": 3.0, "log": str(log),
                          "message": "exit code 0", "artifact": None},
        }}},
    }
    (run / "summary.json").write_text(json.dumps(summary))
    sys.exit(0 if status == "PASS" else 1)
    """
)


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manifest_dir = tmp_path / "manifest"
    (manifest_dir / "scripts").mkdir(parents=True)
    (manifest_dir / "scripts" / "verify_dashboard.py").write_text(FAKE_VERIFY, encoding="utf-8")
    return workspace, manifest_dir, tmp_path / "report"


def run_adapter(setup, outcome, artifacts="[]"):
    workspace, manifest_dir, report = setup
    (workspace / "outcome").write_text(outcome)
    code = hmi_west.main(
        [
            "--workspace", str(workspace),
            "--manifest-dir", str(manifest_dir),
            "--build", '{"profile": "quick"}',
            "--toolchain", '{"arm-gnu": "12.3.rel1"}',
            "--artifacts", artifacts,
            "--report-dir", str(report),
        ]
    )
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    return code, summary, (report / "summary.md").read_text(encoding="utf-8")


def test_pass(setup):
    code, summary, markdown = run_adapter(setup, "pass")
    assert code == 0 and summary["result"] == "PASS"
    assert summary["toolchain"]["declared"] == {"arm-gnu": "12.3.rel1"}
    assert summary["toolchain"]["actual"]["arm-none-eabi-gcc"].endswith("12.3.1")
    assert "| src_bank0 | PASS | 3.0s |" in markdown


def test_build_failure_has_excerpt(setup):
    code, summary, markdown = run_adapter(setup, "fail")
    assert code == 1 and summary["failed_modes"] == ["src_bank0"]
    assert "error: boom" in summary["modes"]["src_bank0"]["excerpt"]
    assert "src_bank0 log excerpt" in markdown


def test_missing_artifact_fails_a_green_build(setup):
    workspace = setup[0]
    (workspace / "out").mkdir()
    (workspace / "out" / "empty.bin").write_bytes(b"")
    code, summary, _ = run_adapter(setup, "pass", '["out/*.bin"]')
    assert code == 1
    assert summary["artifact_problems"] == ["required artifact missing or empty: out/*.bin"]


def test_verify_crash_is_environment_error(setup):
    code, summary, _ = run_adapter(setup, "crash")
    assert code == 2 and summary["result"] == "ERROR"


def test_missing_verify_script_is_environment_error(setup):
    workspace, manifest_dir, report = setup
    (manifest_dir / "scripts" / "verify_dashboard.py").unlink()
    code = hmi_west.main(
        ["--workspace", str(workspace), "--manifest-dir", str(manifest_dir), "--build", "{}", "--report-dir", str(report)]
    )
    assert code == 2


def test_restore_permissions(tmp_path):
    tool = tmp_path / hmi_west.EXECUTABLES[0]
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)
    assert hmi_west.restore_permissions(tmp_path) == [hmi_west.EXECUTABLES[0]]


# --- toolchain.py ------------------------------------------------------------


def make_tar(path: Path, members: dict[str, bytes], link: tuple[str, str] | None = None) -> None:
    with tarfile.open(path, "w:xz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link[0])
            info.type = tarfile.SYMTYPE
            info.linkname = link[1]
            tar.addfile(info)


@pytest.fixture
def fake_release(tmp_path, monkeypatch):
    archive = tmp_path / "src" / "tc.tar.xz"
    archive.parent.mkdir()
    make_tar(archive, {"tc/bin/arm-none-eabi-gcc": b"gcc"})
    release = toolchain.Release("https://example.invalid/tc.tar.xz", hashlib.sha256(archive.read_bytes()).hexdigest(), "tc")
    monkeypatch.setitem(toolchain.TOOLS, "fake", {"1.0": release})
    downloads = []

    def fake_download(url, target, attempts=3):
        downloads.append(url)
        target.write_bytes(archive.read_bytes())

    monkeypatch.setattr(toolchain, "download", fake_download)
    return release, downloads


def test_install_verifies_and_caches(tmp_path, fake_release):
    _, downloads = fake_release
    cache = tmp_path / "cache"
    cache.mkdir()
    bin_dir = toolchain.install("fake", "1.0", cache)
    assert (bin_dir / "arm-none-eabi-gcc").read_bytes() == b"gcc"
    assert toolchain.install("fake", "1.0", cache) == bin_dir
    assert len(downloads) == 1


def test_install_rejects_checksum_mismatch(tmp_path, fake_release, monkeypatch):
    release, _ = fake_release
    monkeypatch.setitem(toolchain.TOOLS, "fake", {"1.0": toolchain.Release(release.url, "0" * 64, "tc")})
    cache = tmp_path / "cache"
    cache.mkdir()
    with pytest.raises(toolchain.ToolchainError, match="checksum mismatch"):
        toolchain.install("fake", "1.0", cache)


@pytest.mark.parametrize(("tool", "version"), [("nope", "1"), ("arm-gnu", "9.9.rel9")])
def test_install_rejects_unknown(tmp_path, tool, version):
    with pytest.raises(toolchain.ToolchainError):
        toolchain.install(tool, version, tmp_path)


@pytest.mark.parametrize(
    ("members", "link"),
    [({"../evil": b"x"}, None), ({"ok": b"x"}, ("tc/link", "../../outside"))],
)
def test_safe_extract_rejects_escape(tmp_path, members, link):
    archive = tmp_path / "bad.tar.xz"
    make_tar(archive, members, link)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(toolchain.ToolchainError, match="escapes"):
        toolchain.safe_extract(archive, dest)


def test_cache_key(capsys):
    assert toolchain.main(["--toolchain", '{"arm-gnu": "12.3.rel1"}', "--cache-dir", ".", "--cache-key"]) == 0
    assert capsys.readouterr().out.strip() == "toolchain-arm-gnu-12.3.rel1-12a281564431"
