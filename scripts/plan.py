"""Plan a CI run from targets.yml.

--validate only checks targets.yml against the schema. Without it, the selected
targets are checked against a manifest repository checkout and the build matrix
and plan report are written.

Exit codes: 0 plan written (target findings are in the report), 1 targets.yml
invalid, 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from manifest import ManifestError, compare_pair, derive_model, load_projects, ls_remote_resolver
from manifest import SHA_PATTERN, RefResolver
from targets import ConfigError, Target, load_targets

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ("nightly", "manual")


@dataclass
class TargetPlan:
    target: Target
    # None: planned for building; otherwise a SPEC 5.3 result reached without building.
    status: str | None = None
    derived_model: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def select_targets(targets: list[Target], event: str, only: list[str] | None) -> list[Target]:
    """Pick the targets for this run; disabled targets never run.

    Raises:
        ValueError: ``only`` names an id that is not in targets.yml.
    """
    if only:
        known = {t.id for t in targets}
        unknown = [i for i in only if i not in known]
        if unknown:
            raise ValueError(f"unknown target id(s): {', '.join(unknown)}")
    selected = []
    for target in targets:
        if target.tier == "disabled":
            continue
        if only:
            if target.id in only:
                selected.append(target)
        elif event == "manual" or event in target.triggers:
            selected.append(target)
    return selected


def check_target(target: Target, manifest_root: Path, resolve: RefResolver) -> TargetPlan:
    result = TargetPlan(target)
    files = {
        name: value
        for name, value in (
            ("manifest", target.manifest),
            ("fallback_manifest", target.fallback_manifest),
            ("lock_manifest", target.lock_manifest),
        )
        if value is not None
    }
    # The manifest repository changes independently of this one, so a renamed or
    # moved manifest is a stale targets.yml (CI's fault), not a broken manifest.
    absent = [f"{name} {path!r}" for name, path in files.items() if not (manifest_root / path).is_file()]
    if absent:
        result.status = "INFRA_FAIL"
        result.errors.append(f"targets.yml references missing file(s): {', '.join(absent)}")
        return result

    parsed = {}
    for name, path in files.items():
        try:
            parsed[name] = load_projects(manifest_root / path)
        except ManifestError as exc:
            result.errors.append(str(exc))
    if "manifest" not in parsed:
        result.status = "MANIFEST_FAIL"
        return result

    model = derive_model(parsed["manifest"], resolve)
    result.derived_model = model.model
    if model.missing:
        result.errors.append(f"revision not found on remote for: {', '.join(model.missing)}")
    if model.unresolved:
        result.warnings.append(
            f"could not query remote for: {', '.join(model.unresolved)}; version model not checked"
        )
    if model.model is not None and model.model != target.version_model:
        detail = f" (following branches: {', '.join(model.floating)})" if model.floating else ""
        result.errors.append(
            f"version_model is {target.version_model!r} but the manifest is {model.model!r}{detail}"
        )

    lock = parsed.get("lock_manifest")
    if lock is not None:
        unpinned = [p.name for p in lock if not SHA_PATTERN.match(p.revision)]
        if unpinned:
            result.errors.append(f"lock_manifest has projects not pinned to a SHA: {', '.join(unpinned)}")

    fallback = parsed.get("fallback_manifest")
    if fallback is not None:
        diffs = compare_pair(parsed["manifest"], fallback)
        if diffs:
            result.errors.append("manifest and fallback_manifest differ: " + "; ".join(diffs))
    return result


def matrix_entries(plan: TargetPlan) -> list[dict]:
    target = plan.target
    base = {
        "id": target.id,
        "tier": target.tier,
        "builder": target.builder,
        "build": target.build,
        "toolchain": target.toolchain,
        "timeout": target.timeout,
        "artifacts": list(target.artifacts),
    }
    if target.lock_manifest is None:
        return [{**base, "track": "main", "manifest": target.manifest,
                 "fallback_manifest": target.fallback_manifest}]
    # The locked track gets no fallback entry: its mirror manifest is derived
    # from the lock at fetch time so the two can never drift apart.
    return [
        {**base, "track": "floating", "manifest": target.manifest,
         "fallback_manifest": target.fallback_manifest},
        {**base, "track": "locked", "manifest": target.lock_manifest, "fallback_manifest": None},
    ]


def build_report(event: str, plans: list[TargetPlan]) -> dict:
    include = [entry for plan in plans if plan.status is None for entry in matrix_entries(plan)]
    return {
        "event": event,
        "matrix": {"include": include},
        "targets": [
            {
                "id": plan.target.id,
                "tier": plan.target.tier,
                "status": plan.status or "PLANNED",
                "version_model": plan.target.version_model,
                "derived_model": plan.derived_model,
                "errors": plan.errors,
                "warnings": plan.warnings,
            }
            for plan in plans
        ],
    }


def summary_markdown(report: dict) -> str:
    lines = [
        f"## Plan ({report['event']})",
        "",
        "| Target | Tier | Status | Version model | Findings |",
        "| --- | --- | --- | --- | --- |",
    ]
    for t in report["targets"]:
        derived = t["derived_model"] or "?"
        model = t["version_model"] if derived == t["version_model"] else f"{t['version_model']} (derived {derived})"
        findings = [f"error: {e}" for e in t["errors"]] + [f"warning: {w}" for w in t["warnings"]]
        cell = "<br>".join(f.replace("|", "\\|") for f in findings) or "—"
        lines.append(f"| `{t['id']}` | {t['tier']} | {t['status']} | {model} | {cell} |")
    jobs = len(report["matrix"]["include"])
    lines += ["", f"{jobs} build job(s) planned."]
    return "\n".join(lines) + "\n"


def _write_github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main(argv: list[str] | None = None, resolve: RefResolver | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan a CI run from targets.yml.")
    parser.add_argument(
        "--targets",
        type=Path,
        default=REPO_ROOT / "targets.yml",
        help="target configuration file (default: targets.yml at the repository root)",
    )
    parser.add_argument("--validate", action="store_true", help="validate targets.yml and exit")
    parser.add_argument("--manifest-root", type=Path, help="checkout of the manifest repository")
    parser.add_argument("--event", choices=EVENTS, default="manual")
    parser.add_argument("--only", help="comma-separated target ids; overrides trigger selection")
    parser.add_argument("--output", type=Path, help="write the plan report (JSON) here")
    parser.add_argument("--summary", type=Path, help="append a Markdown summary here")
    args = parser.parse_args(argv)
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"

    try:
        targets = load_targets(args.targets)
    except ConfigError as exc:
        for error in exc.errors:
            prefix = f"::error file={args.targets}::" if annotate else ""
            print(f"{prefix}{args.targets}: {error}", file=sys.stderr)
        return 1

    if args.validate:
        ids = ", ".join(target.id for target in targets)
        print(f"{args.targets}: {len(targets)} target(s) valid: {ids}")
        return 0

    if args.manifest_root is None:
        parser.error("--manifest-root is required unless --validate is given")
    if not args.manifest_root.is_dir():
        print(f"manifest root is not a directory: {args.manifest_root}", file=sys.stderr)
        return 2
    only = [i.strip() for i in args.only.split(",") if i.strip()] if args.only else None
    try:
        selected = select_targets(targets, args.event, only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    resolve = resolve or ls_remote_resolver()
    plans = [check_target(target, args.manifest_root, resolve) for target in selected]
    report = build_report(args.event, plans)

    error_count = 0
    for plan in plans:
        for error in plan.errors:
            error_count += 1
            prefix = "::error::" if annotate else "error: "
            print(f"{prefix}{plan.target.id}: {error}", file=sys.stderr)
        for warning in plan.warnings:
            prefix = "::warning::" if annotate else "warning: "
            print(f"{prefix}{plan.target.id}: {warning}", file=sys.stderr)

    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary_markdown(report))
    _write_github_output("matrix", json.dumps(report["matrix"], separators=(",", ":")))
    _write_github_output("errors", str(error_count))
    print(f"{len(report['matrix']['include'])} build job(s), {error_count} plan error(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
