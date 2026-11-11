"""Build adapter hmi-west: RTL8773E-style HMI SDKs built with `west build` and Arm GNU.

Delegates the build to scripts/verify_dashboard.py from the manifest checkout,
so this CI and the manifest repository's own nightly run the same build, and
converts its report into the common summary.json / summary.md.

Exit codes: 0 pass, 1 build or artifact failure, 2 environment or configuration error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

ADAPTER = "hmi-west"
VERIFY_SCRIPT = Path("scripts") / "verify_dashboard.py"
EXCERPT_LINES = 40
# Release tools in the SDK that lose their executable bit in the gitee mirror.
EXECUTABLES = (
    "sdk/tool/Gadgets/gcc_tool_linux/Hex2Bin",
    "sdk/tool/Gadgets/gcc_tool_linux/prepend_header",
    "sdk/tool/Gadgets/gcc_tool_linux/build_debug_table",
    "sdk/tool/Gadgets/version_generation/GitGenerateVersion.sh",
)


def restore_permissions(workspace: Path) -> list[str]:
    restored = []
    for rel in EXECUTABLES:
        path = workspace / rel
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111)
            restored.append(rel)
    sdk = workspace / "sdk"
    if (sdk / ".git").exists():
        subprocess.run(["git", "-C", str(sdk), "config", "core.fileMode", "false"], check=False)
    return restored


def find_report(report_root: Path) -> Path | None:
    reports = sorted(report_root.glob("*/summary.json"))
    return reports[-1] if reports else None


def log_excerpt(log_path: str | None) -> str:
    if not log_path:
        return ""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    errors = [i for i, line in enumerate(lines) if "error" in line.lower() or "FAILED:" in line]
    start = max(0, errors[0] - 5) if errors else max(0, len(lines) - EXCERPT_LINES)
    return "\n".join(lines[start : start + EXCERPT_LINES])


def check_artifacts(workspace: Path, patterns: list[str]) -> list[str]:
    """Return a problem for every pattern that matches no non-empty file."""
    problems = []
    for pattern in patterns:
        matches = [Path(p) for p in glob.glob(str(workspace / pattern), recursive=True)]
        if not any(p.is_file() and p.stat().st_size > 0 for p in matches):
            problems.append(f"required artifact missing or empty: {pattern}")
    return problems


def convert(verify: dict, artifact_problems: list[str], toolchain: dict) -> dict:
    """Map a verify_dashboard.py report onto the common adapter summary."""
    source = verify.get("sources", {}).get("gitee", {})
    modes = {}
    for mode, build in source.get("builds", {}).items():
        artifact = build.get("artifact") or {}
        modes[mode] = {
            "status": build.get("status", "FAIL"),
            "duration_seconds": build.get("duration_seconds"),
            "message": artifact.get("message") or build.get("message", ""),
            "log": build.get("log"),
            "excerpt": log_excerpt(build.get("log")) if build.get("status") != "PASS" else "",
        }
    failed = [m for m, r in modes.items() if r["status"] != "PASS"]
    result = "PASS" if verify.get("overall") == "PASS" and not failed and not artifact_problems else "FAIL"
    return {
        "adapter": ADAPTER,
        "result": result,
        "profile": verify.get("profile"),
        "modes": modes,
        "failed_modes": failed,
        "artifact_problems": artifact_problems,
        "toolchain": {"declared": toolchain, "actual": verify.get("tool_versions", {})},
        "total_build_seconds": verify.get("total_build_duration_seconds"),
        "repositories": source.get("repositories", []),
    }


def summary_markdown(summary: dict) -> str:
    lines = [f"**{summary['result']}** · adapter `{ADAPTER}` · profile `{summary['profile']}`", ""]
    lines += ["| Mode | Result | Duration |", "| --- | --- | --- |"]
    for mode, result in summary["modes"].items():
        duration = result["duration_seconds"]
        shown = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "—"
        lines.append(f"| {mode} | {result['status']} | {shown} |")
    for problem in summary["artifact_problems"]:
        lines.append(f"\n- {problem}")
    actual = summary["toolchain"]["actual"]
    if actual.get("arm-none-eabi-gcc"):
        lines += ["", f"Compiler: `{actual['arm-none-eabi-gcc']}`"]
    for mode in summary["failed_modes"]:
        excerpt = summary["modes"][mode]["excerpt"]
        if excerpt:
            lines += ["", f"<details><summary>{mode} log excerpt</summary>", "", "```text", excerpt, "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def write_outputs(report_dir: Path, summary: dict) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (report_dir / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")


def env_error(report_dir: Path, message: str) -> int:
    summary = {"adapter": ADAPTER, "result": "ERROR", "error": message, "modes": {}}
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (report_dir / "summary.md").write_text(f"**ERROR** · adapter `{ADAPTER}`: {message}\n", encoding="utf-8")
    print(f"::error::{message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True, help="updated west workspace root")
    parser.add_argument(
        "--manifest-dir", type=Path, required=True, help="manifest checkout providing verify_dashboard.py"
    )
    parser.add_argument("--build", required=True, help="build parameters from targets.yml (JSON)")
    parser.add_argument("--toolchain", default="{}", help="toolchain from targets.yml (JSON)")
    parser.add_argument("--artifacts", default="[]", help="artifact patterns from targets.yml (JSON)")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        build = json.loads(args.build)
        toolchain = json.loads(args.toolchain)
        artifacts = json.loads(args.artifacts)
    except json.JSONDecodeError as exc:
        return env_error(args.report_dir, f"invalid JSON argument: {exc}")
    script = args.manifest_dir / VERIFY_SCRIPT
    if not script.is_file():
        return env_error(args.report_dir, f"{VERIFY_SCRIPT} not found in {args.manifest_dir}")

    restore_permissions(args.workspace)
    raw_dir = args.report_dir / "verify"
    command = [
        sys.executable,
        str(script),
        "--gitee",
        str(args.workspace),
        "--profile",
        build["profile"],
        "--jobs",
        str(args.jobs),
        "--report-dir",
        str(raw_dir),
    ]
    code = subprocess.run(command, check=False).returncode
    if code not in (0, 1):
        return env_error(args.report_dir, f"verify_dashboard.py exited with {code}")
    report = find_report(raw_dir)
    if report is None:
        return env_error(args.report_dir, "verify_dashboard.py produced no summary.json")

    verify = json.loads(report.read_text(encoding="utf-8"))
    summary = convert(verify, check_artifacts(args.workspace, artifacts), toolchain)
    write_outputs(args.report_dir, summary)
    print(f"{ADAPTER}: {summary['result']}")
    return 0 if summary["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
