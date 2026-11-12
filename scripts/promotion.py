"""Package a lock promotion after a green floating build.

The lock is written by the manifest repository's own bump_lock.py from the
revisions this job verified; CI never commits it. A developer downloads the
package with the manifest repository's promote_lock.py, which checks the
metadata written here and submits the lock through Gerrit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

METADATA_NAME = "promotion-metadata.json"
REPORT_NAME = "promotion-report.md"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(env: dict[str, str], *, lock: Path, floating: Path, manifest_repository: str,
             manifest_commit: str) -> dict:
    run_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    return {
        "schema_version": 2,
        "repository": env["GITHUB_REPOSITORY"],
        # GITHUB_WORKFLOW_REF is "<owner>/<repo>/.github/workflows/<file>@<ref>".
        "workflow_file": env["GITHUB_WORKFLOW_REF"].split("@")[0].rsplit("/", 1)[-1],
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "run_url": run_url,
        "ci_commit": env["GITHUB_SHA"],
        "manifest_repository": manifest_repository,
        "manifest_commit": manifest_commit,
        "source_manifest": floating.name,
        "source_manifest_sha256": sha256(floating),
        "lock_file": lock.name,
        "lock_sha256": sha256(lock),
        "track": "floating",
        "build_result": "success",
    }


def package(*, manifest_dir: Path, lock: str, floating: str, candidate: Path, repos: Path, out: Path,
            manifest_repository: str, manifest_commit: str, env: dict[str, str]) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    packaged_lock = out / Path(lock).name
    packaged_floating = out / Path(floating).name
    shutil.copyfile(manifest_dir / lock, packaged_lock)
    shutil.copyfile(manifest_dir / floating, packaged_floating)
    run_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    subprocess.run(
        [
            sys.executable, str(manifest_dir / "scripts" / "bump_lock.py"),
            "--lock", str(packaged_lock),
            "--candidate", str(candidate),
            "--floating", str(packaged_floating),
            "--repos", str(repos),
            "--report", str(out / REPORT_NAME),
            "--write",
            "--verified", f"GitHub Actions run {env['GITHUB_RUN_ID']} {run_url}",
        ],
        check=True,
    )
    data = metadata(env, lock=packaged_lock, floating=packaged_floating,
                    manifest_repository=manifest_repository, manifest_commit=manifest_commit)
    (out / METADATA_NAME).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest-dir", type=Path, required=True, help="manifest repository checkout")
    parser.add_argument("--lock", required=True, help="lock manifest, relative to --manifest-dir")
    parser.add_argument("--floating", required=True, help="floating manifest, relative to --manifest-dir")
    parser.add_argument("--candidate", type=Path, required=True, help="west manifest --freeze output")
    parser.add_argument("--repos", type=Path, required=True, help="workspace root")
    parser.add_argument("--manifest-repository", required=True)
    parser.add_argument("--manifest-commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        package(
            manifest_dir=args.manifest_dir, lock=args.lock, floating=args.floating, candidate=args.candidate,
            repos=args.repos, out=args.out, manifest_repository=args.manifest_repository,
            manifest_commit=args.manifest_commit, env=dict(os.environ) if env is None else env,
        )
    except (subprocess.CalledProcessError, OSError, KeyError) as exc:
        print(f"::error::cannot package the lock promotion: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
