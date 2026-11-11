"""Install the toolchains a target declares, from official release archives.

Prints `<dir>` lines suitable for $GITHUB_PATH. Archives are verified against the
SHA-256 values below before extraction; a version not listed here is an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Release:
    url: str
    sha256: str
    top_dir: str


# Values from the .sha256asc file Arm publishes next to each archive.
ARM_GNU = {
    "12.3.rel1": Release(
        "https://developer.arm.com/-/media/Files/downloads/gnu/12.3.rel1/binrel/"
        "arm-gnu-toolchain-12.3.rel1-x86_64-arm-none-eabi.tar.xz",
        "12a2815644318ebcceaf84beabb665d0924b6e79e21048452c5331a56332b309",
        "arm-gnu-toolchain-12.3.rel1-x86_64-arm-none-eabi",
    ),
    "13.2.rel1": Release(
        "https://developer.arm.com/-/media/Files/downloads/gnu/13.2.rel1/binrel/"
        "arm-gnu-toolchain-13.2.rel1-x86_64-arm-none-eabi.tar.xz",
        "6cd1bbc1d9ae57312bcd169ae283153a9572bd6a8e4eeae2fedfbc33b115fdbb",
        "arm-gnu-toolchain-13.2.rel1-x86_64-arm-none-eabi",
    ),
}
TOOLS = {"arm-gnu": ARM_GNU}
CHUNK = 1 << 20


class ToolchainError(Exception):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path, attempts: int = 3) -> None:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as out:
                shutil.copyfileobj(response, out, CHUNK)
            return
        except OSError as exc:
            last = exc
    raise ToolchainError(f"download failed after {attempts} attempts: {url}: {last}")


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract a tar archive, rejecting members that would land outside ``destination``."""
    root = destination.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise ToolchainError(f"archive member escapes destination: {member.name}")
            if member.islnk() or member.issym():
                link = (target.parent / member.linkname).resolve()
                if link != root and root not in link.parents:
                    raise ToolchainError(f"archive link escapes destination: {member.name}")
        tar.extractall(root)


def install(tool: str, version: str, cache_dir: Path) -> Path:
    """Install one toolchain under ``cache_dir`` and return its bin directory.

    Raises:
        ToolchainError: unknown tool or version, download failure, or checksum mismatch.
    """
    releases = TOOLS.get(tool)
    if releases is None:
        raise ToolchainError(f"unknown tool {tool!r}; known: {', '.join(TOOLS)}")
    release = releases.get(version)
    if release is None:
        raise ToolchainError(f"no pinned release for {tool} {version}; known: {', '.join(releases)}")

    home = cache_dir / tool / version
    bin_dir = home / release.top_dir / "bin"
    marker = home / ".sha256"
    if bin_dir.is_dir() and marker.is_file() and marker.read_text().strip() == release.sha256:
        return bin_dir

    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        archive = Path(tmp) / Path(release.url).name
        download(release.url, archive)
        actual = sha256_of(archive)
        if actual != release.sha256:
            raise ToolchainError(f"checksum mismatch for {archive.name}: {actual} != {release.sha256}")
        safe_extract(archive, home)
    if not bin_dir.is_dir():
        raise ToolchainError(f"{release.top_dir}/bin not found after extracting {tool} {version}")
    marker.write_text(release.sha256 + "\n")
    return bin_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--toolchain", required=True, help="toolchain mapping from targets.yml (JSON)")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--cache-key", action="store_true", help="print a cache key and exit")
    args = parser.parse_args(argv)
    toolchain = json.loads(args.toolchain)

    if args.cache_key:
        parts = []
        for tool, version in sorted(toolchain.items()):
            release = TOOLS.get(tool, {}).get(version)
            parts.append(f"{tool}-{version}-{release.sha256[:12] if release else 'unknown'}")
        print("toolchain-" + "-".join(parts))
        return 0

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        for tool, version in toolchain.items():
            print(install(tool, version, args.cache_dir.resolve()))
    except ToolchainError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
