"""Fetch a target's west workspace with retries and classify failures.

Writes fetch.json to --report-dir. Exit codes: 0 fetched, 1 FETCH_FAIL or
MANIFEST_FAIL (see fetch.json), 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from manifest import ManifestError, load_projects

# Not-a-network failures first: the mirror would fail the same way, so they must
# not be retried or reported as "unverified".
CONTENT_PATTERNS = (
    re.compile(r"repository '[^']*' not found", re.I),
    re.compile(r"\b404 not found\b", re.I),
    re.compile(r"Authentication failed", re.I),
    re.compile(r"could not read Username", re.I),
    re.compile(r"Permission denied \(publickey\)", re.I),
    re.compile(r"not our ref", re.I),
    re.compile(r"couldn't find remote ref", re.I),
    re.compile(r"unknown revision or path not in the working tree", re.I),
    re.compile(r"(would clobber existing tag|\[rejected\].*\(would clobber)", re.I),
    re.compile(r"Host key verification failed", re.I),
)
NETWORK_PATTERNS = (
    re.compile(r"HTTP (429|5\d\d)\b"),
    re.compile(r"returned error: (429|5\d\d)\b"),
    re.compile(r"GnuTLS recv error"),
    re.compile(r"SSL connection timeout", re.I),
    re.compile(r"(OpenSSL|SSL_read|SSL_connect).*(error|unexpected eof)", re.I),
    re.compile(r"early EOF"),
    re.compile(r"index-pack failed|invalid index-pack output"),
    re.compile(r"expected flush after ref listing"),
    re.compile(r"Could not resolve host", re.I),
    re.compile(r"Failed to connect", re.I),
    re.compile(r"Connection (reset by peer|timed out|refused)", re.I),
    re.compile(r"Recv failure", re.I),
    re.compile(r"Operation timed out", re.I),
    re.compile(r"remote end hung up", re.I),
    re.compile(r"RPC failed; curl (18|28|35|52|55|56)\b"),
)
FAILED_PROJECTS = re.compile(r"ERROR: update failed for projects?:? (.+)$", re.M)
FETCH_URL = re.compile(r"(?:unable to access|repository) '(https?://[^']+)'")

RETRY_DELAY_SECONDS = 20
GITEE_HOSTS = ("gitee.com",)


@dataclass(frozen=True)
class Failure:
    """Classification of one failed west update."""

    kind: str  # "network", "content", or "unknown"
    reason: str
    projects: tuple[str, ...]
    urls: tuple[str, ...]


def classify(log: str) -> Failure:
    """Classify west update output; content evidence outranks network evidence."""
    names = [n.strip() for group in FAILED_PROJECTS.findall(log) for n in group.split(",")]
    projects = tuple(dict.fromkeys(n for n in names if n))
    urls = tuple(dict.fromkeys(url.rstrip("/") for url in FETCH_URL.findall(log)))
    lines = log.splitlines()
    for kind, patterns in (("content", CONTENT_PATTERNS), ("network", NETWORK_PATTERNS)):
        for pattern in patterns:
            for line in lines:
                if pattern.search(line):
                    return Failure(kind, line.strip(), projects, urls)
    return Failure("unknown", _last_error_line(log), projects, urls)


def _last_error_line(log: str) -> str:
    for line in reversed(log.splitlines()):
        if re.search(r"error|fatal", line, re.I):
            return line.strip()
    return "west update failed without a recognizable error"


def is_gitee(url: str) -> bool:
    return any(host in url for host in GITEE_HOSTS)


def failed_on_gitee(failure: Failure, manifest_path: Path) -> bool:
    """Whether any failed fetch may have been served by gitee.

    Project names come from west's error lines and are looked up in the manifest;
    a project the manifest does not list (an import) or no evidence at all counts
    as possibly gitee, so the fallback is still tried.
    """
    urls = list(failure.urls)
    try:
        known = {p.name: p.url for p in load_projects(manifest_path)}
    except ManifestError:
        known = {}
    for name in failure.projects:
        if name not in known:
            return True
        urls.append(known[name])
    return not urls or any(is_gitee(url) for url in urls)


def derive_mirror_manifest(source: Path, target: Path) -> None:
    """Write a copy of a SHA-pinned manifest with gitee https URLs pointing at github.

    Raises:
        ValueError: a gitee URL survived the rewrite.
    """
    text = source.read_text(encoding="utf-8").replace("https://gitee.com/", "https://github.com/")
    if "gitee.com" in text:
        raise ValueError(f"could not rewrite every gitee URL in {source.name}")
    target.write_text(text, encoding="utf-8")


Runner = Callable[[Sequence[str], Path], "tuple[int, str]"]


def run_streaming(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    lines = []
    for line in process.stdout:
        print(line, end="")
        lines.append(line)
    return process.wait(), "".join(lines)


@dataclass
class Attempt:
    manifest: str
    number: int
    ok: bool
    failure: dict | None = None


@dataclass
class FetchResult:
    status: str  # "OK", "FETCH_FAIL", or "MANIFEST_FAIL"
    manifest: str
    used_fallback: bool = False
    reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)


def update_with(
    workspace: Path,
    manifest: str,
    retries: int,
    run: Runner,
    sleep: Callable[[float], None],
    attempts: list[Attempt],
    log_dir: Path,
) -> Failure | None:
    """Run west update up to ``retries`` times; stop early on a non-network failure."""
    code, output = run(["west", "config", "manifest.file", manifest], workspace)
    if code != 0:
        raise RuntimeError(f"west config manifest.file {manifest} failed:\n{output}")
    failure = None
    for number in range(1, retries + 1):
        print(f"::group::west update ({manifest}, attempt {number}/{retries})", flush=True)
        code, output = run(["west", "update", "-n"], workspace)
        print("::endgroup::", flush=True)
        log_name = f"west-update-{Path(manifest).stem}-{number}.log"
        (log_dir / log_name).write_text(output, encoding="utf-8")
        if code == 0:
            attempts.append(Attempt(manifest, number, True))
            return None
        failure = classify(output)
        attempts.append(Attempt(manifest, number, False, asdict(failure)))
        if failure.kind == "content":
            return failure
        if number < retries:
            print(f"::warning::west update failed ({failure.reason}); retrying in {RETRY_DELAY_SECONDS}s")
            sleep(RETRY_DELAY_SECONDS)
    return failure


def fetch(
    workspace: Path,
    manifest_dir: str,
    manifest: str,
    fallback: str | None,
    derive_fallback: bool,
    retries: int,
    fallback_retries: int,
    log_dir: Path,
    run: Runner = run_streaming,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    attempts: list[Attempt] = []
    code, output = run(["west", "init", "-l", manifest_dir, "--mf", manifest], workspace)
    if code != 0:
        raise RuntimeError(f"west init failed:\n{output}")

    failure = update_with(workspace, manifest, retries, run, sleep, attempts, log_dir)
    if failure is None:
        return FetchResult("OK", manifest, attempts=attempts)
    if failure.kind == "content":
        return FetchResult("MANIFEST_FAIL", manifest, reason=failure.reason, attempts=attempts)
    # An unrecognised error after every retry is still most likely transient; it
    # is reported as unverified rather than blamed on the manifest.

    # The mirror only helps when what failed came from gitee. x3G manifests pull
    # their SDK and zephyr from github, where there is nothing to fall back to.
    if not failed_on_gitee(failure, workspace / manifest_dir / manifest) or (
        fallback is None and not derive_fallback
    ):
        return FetchResult("FETCH_FAIL", manifest, reason=failure.reason, attempts=attempts)

    if derive_fallback:
        source = workspace / manifest_dir / manifest
        fallback = manifest.replace("-gitee.", "-github.", 1)
        if fallback == manifest:
            fallback = manifest.replace(".yml", ".github-mirror.yml")
        derive_mirror_manifest(source, workspace / manifest_dir / fallback)
        # verify_dashboard.py refuses to build when any west project is dirty,
        # and the manifest checkout is one of them.
        exclude = workspace / manifest_dir / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(f"/{fallback}\n")
    assert fallback is not None
    print(f"::warning::gitee fetch failed ({failure.reason}); retrying against {fallback}")
    second = update_with(workspace, fallback, fallback_retries, run, sleep, attempts, log_dir)
    if second is None:
        return FetchResult("OK", fallback, used_fallback=True, attempts=attempts)
    status = "MANIFEST_FAIL" if second.kind == "content" else "FETCH_FAIL"
    return FetchResult(status, fallback, used_fallback=True, reason=second.reason, attempts=attempts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True, help="west workspace root")
    parser.add_argument(
        "--manifest-dir", default=".manifest", help="manifest repository, relative to the workspace"
    )
    parser.add_argument("--manifest", required=True, help="manifest file inside --manifest-dir")
    parser.add_argument("--fallback", help="mirror manifest for network failures on gitee projects")
    parser.add_argument(
        "--derive-fallback",
        action="store_true",
        help="derive the mirror manifest from a SHA-pinned manifest instead of --fallback",
    )
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--fallback-retries", type=int, default=2)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.fallback and args.derive_fallback:
        parser.error("--fallback and --derive-fallback are mutually exclusive")

    args.report_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = fetch(
            args.workspace,
            args.manifest_dir,
            args.manifest,
            args.fallback,
            args.derive_fallback,
            args.retries,
            args.fallback_retries,
            args.report_dir,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    (args.report_dir / "fetch.json").write_text(
        json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8"
    )
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"FETCH_STATUS={result.status}\n")
            handle.write(f"USED_FALLBACK={'true' if result.used_fallback else 'false'}\n")
            handle.write(f"FETCHED_MANIFEST={result.manifest}\n")
    if result.status == "OK":
        suffix = " (GitHub mirror)" if result.used_fallback else ""
        print(f"fetched {result.manifest}{suffix}")
        return 0
    print(f"::error::{result.status}: {result.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
