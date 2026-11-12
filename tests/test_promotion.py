from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import promotion  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40
ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "realmcu/HMI-SDK-CI",
    "GITHUB_WORKFLOW_REF": "realmcu/HMI-SDK-CI/.github/workflows/hmi-ci.yml@refs/heads/main",
    "GITHUB_RUN_ID": "7",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_SHA": SHA_A,
}

# Stand-in for the manifest repository's bump_lock.py --write: copies the candidate
# over the lock and writes a report, which is all promotion.py relies on.
FAKE_BUMP = textwrap.dedent(
    """\
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser()
    for a in ("--lock", "--candidate", "--floating", "--repos", "--report", "--verified"):
        p.add_argument(a)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    Path(args.lock).write_text(Path(args.candidate).read_text() + "# " + args.verified + "\\n")
    Path(args.report).write_text("report\\n")
    """
)


def test_package_holds_lock_report_and_provenance(tmp_path):
    manifest = tmp_path / "manifest"
    (manifest / "scripts").mkdir(parents=True)
    (manifest / "scripts" / "bump_lock.py").write_text(FAKE_BUMP)
    (manifest / "dash").mkdir()
    (manifest / "dash" / "d.lock.yml").write_text("old lock\n")
    (manifest / "dash" / "d.yml").write_text("floating\n")
    candidate = tmp_path / "frozen.yml"
    candidate.write_text("frozen\n")
    out = tmp_path / "promotion"

    assert promotion.main([
        "--manifest-dir", str(manifest), "--lock", "dash/d.lock.yml", "--floating", "dash/d.yml",
        "--candidate", str(candidate), "--repos", str(tmp_path), "--manifest-repository", "realmcu/HMI-MANIFEST",
        "--manifest-commit", SHA_B, "--out", str(out),
    ], env=ENV) == 0

    lock = out / "d.lock.yml"
    assert lock.read_text() == "frozen\n# GitHub Actions run 7 https://github.com/realmcu/HMI-SDK-CI/actions/runs/7\n"
    assert (manifest / "dash" / "d.lock.yml").read_text() == "old lock\n"
    assert (out / "d.yml").read_text() == "floating\n" and (out / "promotion-report.md").is_file()
    meta = json.loads((out / "promotion-metadata.json").read_text())
    assert meta == {
        "schema_version": 2,
        "repository": "realmcu/HMI-SDK-CI",
        "workflow_file": "hmi-ci.yml",
        "run_id": 7,
        "run_attempt": 1,
        "run_url": "https://github.com/realmcu/HMI-SDK-CI/actions/runs/7",
        "ci_commit": SHA_A,
        "manifest_repository": "realmcu/HMI-MANIFEST",
        "manifest_commit": SHA_B,
        "source_manifest": "d.yml",
        "source_manifest_sha256": hashlib.sha256((out / "d.yml").read_bytes()).hexdigest(),
        "lock_file": "d.lock.yml",
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "track": "floating",
        "build_result": "success",
    }


def test_failed_bump_is_an_error(tmp_path, capsys):
    manifest = tmp_path / "manifest"
    (manifest / "scripts").mkdir(parents=True)
    (manifest / "scripts" / "bump_lock.py").write_text("raise SystemExit(2)\n")
    (manifest / "l.yml").write_text("x\n")
    (manifest / "f.yml").write_text("x\n")
    assert promotion.main([
        "--manifest-dir", str(manifest), "--lock", "l.yml", "--floating", "f.yml",
        "--candidate", str(tmp_path / "c.yml"), "--repos", str(tmp_path), "--manifest-repository", "m",
        "--manifest-commit", SHA_B, "--out", str(tmp_path / "out"),
    ], env=ENV) == 1
    assert "cannot package the lock promotion" in capsys.readouterr().err
