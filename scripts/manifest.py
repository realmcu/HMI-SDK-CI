"""Read west manifests and derive their version model without fetching any project."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import yaml

SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# west's built-in default when neither the project nor `defaults` names a revision.
WEST_DEFAULT_REVISION = "master"
# Fields compared between a gitee manifest and its github counterpart (URLs are expected to differ).
PAIR_FIELDS = ("path", "revision", "repo", "import_")


class ManifestError(Exception):
    """The manifest file exists but is not a usable west manifest."""


@dataclass(frozen=True)
class Project:
    name: str
    path: str
    revision: str
    url: str
    repo: str
    import_: bool


def load_projects(path: Path) -> list[Project]:
    """Parse the top-level projects of a west manifest; imported manifests are not read.

    Raises:
        ManifestError: invalid YAML, or a structure west would reject.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot parse {path.name}: {exc}") from exc
    manifest = data.get("manifest") if isinstance(data, dict) else None
    if not isinstance(manifest, dict):
        raise ManifestError(f"{path.name}: missing top-level 'manifest' mapping")

    remotes = {}
    for remote in manifest.get("remotes") or []:
        if not isinstance(remote, dict) or "name" not in remote or "url-base" not in remote:
            raise ManifestError(f"{path.name}: every remote needs 'name' and 'url-base'")
        remotes[remote["name"]] = str(remote["url-base"]).rstrip("/")
    defaults = manifest.get("defaults") or {}
    default_remote = defaults.get("remote")
    default_revision = str(defaults.get("revision", WEST_DEFAULT_REVISION))

    projects = []
    for raw in manifest.get("projects") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ManifestError(f"{path.name}: every project needs a 'name'")
        name = str(raw["name"])
        repo = str(raw.get("repo-path", name))
        if "url" in raw:
            url = str(raw["url"])
        else:
            remote = raw.get("remote", default_remote)
            if remote not in remotes:
                raise ManifestError(f"{path.name}: project {name!r} uses unknown remote {remote!r}")
            url = f"{remotes[remote]}/{repo}"
        projects.append(
            Project(
                name=name,
                path=str(raw.get("path", name)),
                revision=str(raw.get("revision", default_revision)),
                url=url,
                repo=repo,
                import_=bool(raw.get("import")),
            )
        )
    if not projects:
        raise ManifestError(f"{path.name}: no projects")
    return projects


# Resolves (url, revision) to "tag", "branch", "missing", or None when the remote could not be reached.
RefResolver = Callable[[str, str], "str | None"]


def anonymous_url(url: str) -> str:
    """Rewrite gitee ssh URLs to https so they can be queried without a key."""
    for prefix in ("ssh://git@gitee.com/", "git@gitee.com:"):
        if url.startswith(prefix):
            return "https://gitee.com/" + url[len(prefix):]
    return url


def ls_remote_resolver(timeout: int = 60, attempts: int = 2) -> RefResolver:
    """Return a resolver backed by anonymous ``git ls-remote``, cached per (url, revision)."""
    cache: dict[tuple[str, str], str | None] = {}
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def resolve(url: str, revision: str) -> str | None:
        key = (url, revision)
        if key in cache:
            return cache[key]
        refs = [f"refs/heads/{revision}", f"refs/tags/{revision}"]
        result = None
        for _ in range(attempts):
            try:
                proc = subprocess.run(
                    ["git", "ls-remote", anonymous_url(url), *refs],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                continue
            if proc.returncode != 0:
                continue
            found = {line.split("\t", 1)[1] for line in proc.stdout.splitlines() if "\t" in line}
            # A name that is both a branch and a tag is fetched as the branch, so it floats.
            if refs[0] in found:
                result = "branch"
            elif refs[1] in found:
                result = "tag"
            else:
                result = "missing"
            break
        cache[key] = result
        return result

    return resolve


@dataclass(frozen=True)
class ModelResult:
    """Derived version model: pinned, floating, mixed, or None when it could not be decided."""

    model: str | None
    floating: tuple[str, ...]
    unresolved: tuple[str, ...]
    missing: tuple[str, ...]


def derive_model(projects: Iterable[Project], resolve: RefResolver) -> ModelResult:
    """Classify a manifest by whether its projects follow branches.

    A full SHA or a tag counts as pinned. Imported manifests are covered by the
    revision of the project that imports them.
    """
    pinned, floating, unresolved, missing = [], [], [], []
    for project in projects:
        if SHA_PATTERN.match(project.revision):
            pinned.append(project.name)
            continue
        kind = resolve(project.url, project.revision)
        if kind == "tag":
            pinned.append(project.name)
        elif kind == "branch":
            floating.append(project.name)
        elif kind == "missing":
            missing.append(project.name)
        else:
            unresolved.append(project.name)

    if pinned and floating:
        model = "mixed"
    elif unresolved or missing:
        model = None
    elif floating:
        model = "floating"
    else:
        model = "pinned"
    return ModelResult(model, tuple(floating), tuple(unresolved), tuple(missing))


def compare_pair(primary: list[Project], other: list[Project]) -> list[str]:
    """List differences other than URL between two manifests of the same project."""
    a = {p.name: p for p in primary}
    b = {p.name: p for p in other}
    diffs = []
    for name in sorted(set(a) | set(b)):
        if name not in b:
            diffs.append(f"project {name!r} only in primary")
        elif name not in a:
            diffs.append(f"project {name!r} only in fallback")
        else:
            for field in PAIR_FIELDS:
                left, right = getattr(a[name], field), getattr(b[name], field)
                if left != right:
                    label = field.rstrip("_")
                    diffs.append(f"{name}.{label}: primary={left!r} fallback={right!r}")
    return diffs
