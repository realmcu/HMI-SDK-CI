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

from manifest import ManifestError, load_projects

METADATA_NAME = "promotion-metadata.json"
REPORT_NAME = "promotion-report.md"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RehostError(Exception):
    """A github candidate cannot be mapped back onto the floating manifest's URLs."""


def rehost_candidate(candidate: Path, floating: Path, target: Path) -> None:
    """Write ``candidate``, fetched from github, with the gitee URLs of ``floating``.

    The revisions are unchanged; every project must end up with exactly the URL
    the floating manifest gives it, so a project from another host is an error.
    """
    text = candidate.read_text(encoding="utf-8").replace("https://github.com/", "https://gitee.com/")
    target.write_text(text, encoding="utf-8")
    try:
        expected = {p.name: p.url for p in load_projects(floating)}
        actual = {p.name: p.url for p in load_projects(target)}
    except ManifestError as exc:
        raise RehostError(str(exc)) from exc
    wrong = sorted(name for name in actual if actual[name] != expected.get(name))
    if wrong or set(actual) != set(expected):
        raise RehostError(f"candidate URLs do not match {floating.name} after rehosting: "
                          f"{', '.join(wrong) or 'project sets differ'}")


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
            manifest_repository: str, manifest_commit: str, env: dict[str, str],
            rehost: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    packaged_lock = out / Path(lock).name
    packaged_floating = out / Path(floating).name
    shutil.copyfile(manifest_dir / lock, packaged_lock)
    shutil.copyfile(manifest_dir / floating, packaged_floating)
    if rehost:
        # Outside the package: promote_lock.py rejects unexpected files.
        rehosted = out.parent / f"{out.name}-candidate.yml"
        rehost_candidate(candidate, packaged_floating, rehosted)
        candidate = rehosted
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
    parser.add_argument(
        "--rehost-candidate",
        action="store_true",
        help="the candidate was fetched from github; give it the URLs of the gitee --floating",
    )
    args = parser.parse_args(argv)
    try:
        package(
            manifest_dir=args.manifest_dir, lock=args.lock, floating=args.floating, candidate=args.candidate,
            repos=args.repos, out=args.out, manifest_repository=args.manifest_repository,
            manifest_commit=args.manifest_commit, env=dict(os.environ) if env is None else env,
            rehost=args.rehost_candidate,
        )
    except (subprocess.CalledProcessError, OSError, KeyError, RehostError) as exc:
        print(f"::error::cannot package the lock promotion: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
