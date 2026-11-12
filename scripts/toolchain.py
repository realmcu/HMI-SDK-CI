"""Install the toolchains a target declares, from official release archives.

Prints `<dir>` lines suitable for $GITHUB_PATH and, with --env-file, appends the
environment a tool needs (for example ZEPHYR_SDK_INSTALL_DIR). Archives are verified
against the SHA-256 values below before extraction; a version not listed here is an error.
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
class Archive:
    url: str
    sha256: str


@dataclass(frozen=True)
class Release:
    url: str
    sha256: str
    top_dir: str
    # Further archives extracted inside top_dir, e.g. Zephyr SDK toolchains.
    extra: tuple[Archive, ...] = ()
    bin_dir: str = "bin"
    # Environment variable set to the installed top_dir.
    home_env: str | None = None


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
# Values from the sha256.sum file attached to each sdk-ng release. The minimal
# bundle carries cmake/ and sdk_version; only the arm-zephyr-eabi toolchain is added.
_SDK_NG = "https://github.com/zephyrproject-rtos/sdk-ng/releases/download"
ZEPHYR_SDK = {
    "0.16.9": Release(
        f"{_SDK_NG}/v0.16.9/zephyr-sdk-0.16.9_linux-x86_64_minimal.tar.xz",
        "b433f19b334cd15f4d7696a958dda214a7737cf41432900d076adce4253782a5",
        "zephyr-sdk-0.16.9",
        extra=(
            Archive(
                f"{_SDK_NG}/v0.16.9/toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz",
                "81162a8326e3cb82fcc53f20528aa7abec29a0bf8376fa009e0c887ba864aaf8",
            ),
        ),
        bin_dir="arm-zephyr-eabi/bin",
        home_env="ZEPHYR_SDK_INSTALL_DIR",
    ),
}
TOOLS = {"arm-gnu": ARM_GNU, "zephyr-sdk": ZEPHYR_SDK}
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


def fetch_verified(archive: Archive, directory: Path) -> Path:
    path = directory / Path(archive.url).name
    download(archive.url, path)
    actual = sha256_of(path)
    if actual != archive.sha256:
        raise ToolchainError(f"checksum mismatch for {path.name}: {actual} != {archive.sha256}")
    return path


def cache_digest(release: Release) -> str:
    """Changes whenever any archive of the release changes."""
    return "+".join([release.sha256, *(a.sha256 for a in release.extra)])


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
    bin_dir = home / release.top_dir / release.bin_dir
    marker = home / ".sha256"
    digest = cache_digest(release)
    if bin_dir.is_dir() and marker.is_file() and marker.read_text().strip() == digest:
        return bin_dir

    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        safe_extract(fetch_verified(Archive(release.url, release.sha256), Path(tmp)), home)
        top = home / release.top_dir
        if not top.is_dir():
            raise ToolchainError(f"{release.top_dir} not found after extracting {tool} {version}")
        for extra in release.extra:
            safe_extract(fetch_verified(extra, Path(tmp)), top)
    if not bin_dir.is_dir():
        raise ToolchainError(f"{release.top_dir}/{release.bin_dir} not found after extracting {tool} {version}")
    marker.write_text(digest + "\n")
    return bin_dir


def home_of(tool: str, version: str, cache_dir: Path) -> Path:
    return cache_dir / tool / version / TOOLS[tool][version].top_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--toolchain", required=True, help="toolchain mapping from targets.yml (JSON)")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--cache-key", action="store_true", help="print a cache key and exit")
    parser.add_argument("--env-file", type=Path, help="append NAME=value lines here (e.g. $GITHUB_ENV)")
    args = parser.parse_args(argv)
    toolchain = json.loads(args.toolchain)

    if args.cache_key:
        parts = []
        for tool, version in sorted(toolchain.items()):
            release = TOOLS.get(tool, {}).get(version)
            if release is None:
                tag = "unknown"
            elif release.extra:
                tag = hashlib.sha256(cache_digest(release).encode()).hexdigest()[:12]
            else:
                tag = release.sha256[:12]
            parts.append(f"{tool}-{version}-{tag}")
        print("toolchain-" + "-".join(parts))
        return 0

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = args.cache_dir.resolve()
    env_lines = []
    try:
        for tool, version in toolchain.items():
            print(install(tool, version, cache_dir))
            home_env = TOOLS[tool][version].home_env
            if home_env:
                env_lines.append(f"{home_env}={home_of(tool, version, cache_dir)}")
    except ToolchainError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    if env_lines and args.env_file:
        with args.env_file.open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in env_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
