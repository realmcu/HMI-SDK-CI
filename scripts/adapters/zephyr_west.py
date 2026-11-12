"""Build adapter zephyr-west: RTL87x3G (x3G) Zephyr SDKs built with `west build`.

Fetches the SDK's release assets, then builds each declared product (application or
sample) in its own directory and checks for the signed image. Products are reported
separately; the target's result is the worst of them.

Exit codes: 0 pass, 1 build or artifact failure, 2 environment or configuration error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ADAPTER = "zephyr-west"
SDK_DIR = Path("zephyrproject") / "realtek-app"
SDK_PROJECT = "rtl87x3g-common-sdk"
FETCH_ASSETS = "fetch_assets.py"
# md5_generate.sh renames <app>/bin/app_MP.bin to app_MP-v<version>-<gcid>-<md5>.bin.
SIGNED_IMAGE = "bin/app_MP-*.bin"
EXCERPT_LINES = 40
# Ninja echoes the full compiler command (several KB) after FAILED:; clip it so the
# diagnostics stay readable in the summary and mail.
EXCERPT_LINE_CHARS = 300

Runner = Callable[[list[str], Path, Path], int]


@dataclass(frozen=True)
class Workaround:
    """A temporary, decision-backed deviation from building the SDK as shipped.

    ``apply`` changes the SDK checkout, so such workarounds are declared per target;
    ``cmake_args`` ones per product. ``probe`` returns True while the defect persists.
    """

    decision: str
    description: str
    cmake_args: tuple[str, ...] = ()
    apply: Callable[[Path], None] | None = None
    probe: Callable[[Path], bool] | None = None

    @property
    def scope(self) -> str:
        return "target" if self.apply else "product"


# Built by the post-build step without `bash`, but tracked as 100644 in the zsdk.
EXEC_BIT_FILES = (
    "tools/Gadgets/prepend_header",
    "tools/Gadgets/version_generation/GitGenerateVersion.sh",
)


def _tracked_modes(sdk: Path, files: tuple[str, ...]) -> dict[str, str]:
    out = subprocess.run(
        ["git", "-C", str(sdk), "ls-files", "-s", "--", *files],
        capture_output=True, text=True, check=False,
    ).stdout
    return {line.split("\t", 1)[1]: line.split()[0] for line in out.splitlines() if "\t" in line}


def _exec_bits_missing(sdk: Path) -> bool:
    modes = _tracked_modes(sdk, EXEC_BIT_FILES)
    return any(modes.get(f) != "100755" for f in EXEC_BIT_FILES)


def _restore_exec_bits(sdk: Path) -> None:
    for rel in EXEC_BIT_FILES:
        path = sdk / rel
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111)


def _version_generation_broken(sdk: Path) -> bool:
    """Run the SDK's version script into a scratch file; tracked files stay untouched."""
    script = sdk / "tools" / "Gadgets" / "version_gen_config.sh"
    if not script.is_file():
        return False
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "VERSION"
        subprocess.run(["bash", script.as_posix(), out.as_posix()], cwd=sdk / "samples",
                       capture_output=True, check=False)
        text = out.read_text(errors="replace") if out.is_file() else ""
    return not any(line.startswith("VERSION_MAJOR = ") and line[16:17].isdigit() for line in text.splitlines())


# Decision id (HMI-CI decisions.md) -> workaround. targets.yml may only reference these.
WORKAROUNDS: dict[str, Workaround] = {
    "d11-skip-version-generation": Workaround(
        "D11",
        "zsdk version script truncates the tracked VERSION of in-repo samples",
        cmake_args=("-DENABLE_GENERATE_VERSION=OFF",),
        probe=_version_generation_broken,
    ),
    "d12-restore-exec-bits": Workaround(
        "D12",
        "zsdk tracks post-build tools without the executable bit",
        apply=_restore_exec_bits,
        probe=_exec_bits_missing,
    ),
}


def run_logged(command: list[str], cwd: Path, log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as handle:
        return subprocess.run(command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, check=False).returncode


def log_excerpt(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    marks = [i for i, line in enumerate(lines) if "error:" in line.lower() or "FAILED:" in line
             or "Permission denied" in line or "CMake Error" in line]
    start = max(0, marks[0] - 5) if marks else max(0, len(lines) - EXCERPT_LINES)
    clip = EXCERPT_LINE_CHARS
    return "\n".join(line if len(line) <= clip else line[:clip] + " [...]"
                     for line in lines[start : start + EXCERPT_LINES])


def fetch_sdk_tag(workspace: Path, sdk: Path) -> str:
    """Bring the zsdk's own tag, which `west update --narrow` leaves out.

    A plain `west update`, as customers run it, has the tag; git describe in the
    SDK's version scripts depends on it.
    """
    listed = subprocess.run(
        ["west", "list", "-f", "{url} {revision}", SDK_PROJECT],
        cwd=workspace, capture_output=True, text=True, check=False,
    )
    parts = listed.stdout.split()
    if listed.returncode != 0 or len(parts) != 2:
        return ""
    url, revision = parts
    subprocess.run(["git", "-C", str(sdk), "fetch", "-q", url, f"refs/tags/{revision}:refs/tags/{revision}"],
                   capture_output=True, check=False)
    described = subprocess.run(["git", "-C", str(sdk), "describe", "--tags"],
                               capture_output=True, text=True, check=False)
    return described.stdout.strip()


def signed_image(app_dir: Path) -> Path | None:
    for match in sorted(glob.glob(str(app_dir / SIGNED_IMAGE))):
        path = Path(match)
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def build_product(product: dict, sdk: Path, log_dir: Path, run: Runner) -> dict:
    names = list(product.get("workarounds", ()))
    cmake_args = [*product.get("cmake_args", ()), *(a for n in names for a in WORKAROUNDS[n].cmake_args)]
    app_dir = sdk / product["app"]
    log = log_dir / f"{product['name']}.log"
    # post_build.cmake finds mp.ini, VERSION and bin/ through ${CMAKE_BINARY_DIR}/..,
    # so the build directory must be <app>/build.
    command = ["west", "build", "-b", product["board"], "-p", "always", "-d", "build"]
    if cmake_args:
        command += ["--", *cmake_args]
    started = time.monotonic()
    code = run(command, app_dir, log) if app_dir.is_dir() else None
    duration = round(time.monotonic() - started, 1)

    image = signed_image(app_dir) if code == 0 else None
    if code is None:
        status, message = "FAIL", f"application directory not found: {product['app']}"
    elif code != 0:
        status, message = "FAIL", f"west build exited with {code}"
    elif image is None:
        status, message = "FAIL", f"signed image missing: {product['app']}/{SIGNED_IMAGE}"
    else:
        status, message = "PASS", image.name
    return {
        "status": status,
        "app": product["app"],
        "board": product["board"],
        "cmake_args": cmake_args,
        "workarounds": names,
        "duration_seconds": duration,
        "message": message,
        "log": str(log),
        "excerpt": log_excerpt(log) if status != "PASS" and code is not None else "",
    }


def probe_workarounds(names: list[str], sdk: Path) -> dict[str, dict]:
    results = {}
    for name in names:
        workaround = WORKAROUNDS[name]
        present = workaround.probe(sdk) if workaround.probe else None
        results[name] = {"decision": workaround.decision, "description": workaround.description,
                         "defect_present": present}
    return results


def gcc_version(toolchain: dict) -> str:
    gcc = shutil.which("arm-zephyr-eabi-gcc")
    if "zephyr-sdk" not in toolchain or gcc is None:
        return ""
    out = subprocess.run([gcc, "--version"], capture_output=True, text=True, check=False)
    return out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else ""


def summarize(products: dict[str, dict], workarounds: dict[str, dict], toolchain: dict,
              actual: dict, sdk_describe: str) -> dict:
    failed = [name for name, r in products.items() if r["status"] != "PASS"]
    used = sorted({w for r in products.values() for w in r["workarounds"]} | set(workarounds))
    return {
        "adapter": ADAPTER,
        "result": "FAIL" if failed else "PASS",
        "with_workarounds": bool(used) and not failed,
        "modes": products,
        "failed_modes": failed,
        "artifact_problems": [],
        "workarounds": {name: workarounds[name] for name in used if name in workarounds},
        "toolchain": {"declared": toolchain, "actual": actual},
        "sdk_describe": sdk_describe,
        "total_build_seconds": round(sum(r["duration_seconds"] for r in products.values()), 1),
    }


def summary_markdown(summary: dict) -> str:
    result = summary["result"]
    if summary["with_workarounds"]:
        result += " (with workarounds)"
    lines = [f"**{result}** · adapter `{ADAPTER}`", ""]
    lines += ["| Product | Board | Result | Workarounds | Duration |", "| --- | --- | --- | --- | --- |"]
    for name, r in summary["modes"].items():
        shown = ", ".join(summary["workarounds"].get(w, {}).get("decision", w) for w in r["workarounds"]) or "—"
        lines.append(f"| `{r['app']}` | `{r['board']}` | {r['status']} | {shown} | {r['duration_seconds']:.1f}s |")
    if summary["workarounds"]:
        lines += ["", "Temporary workarounds (see the decision for its exit condition):", ""]
        for name, w in summary["workarounds"].items():
            present = w["defect_present"]
            state = ("defect still present" if present else
                     "**defect no longer reproduces: remove this workaround**" if present is False else "not probed")
            lines.append(f"- {w['decision']} `{name}`: {w['description']}; {state}")
    actual = summary["toolchain"]["actual"]
    if actual.get("arm-zephyr-eabi-gcc"):
        lines += ["", f"Compiler: `{actual['arm-zephyr-eabi-gcc']}`"]
    if summary.get("sdk_describe"):
        lines.append(f"SDK: `{summary['sdk_describe']}`")
    for name in summary["failed_modes"]:
        excerpt = summary["modes"][name]["excerpt"]
        lines += ["", f"- `{name}`: {summary['modes'][name]['message']}"]
        if excerpt:
            lines += ["", f"<details><summary>{name} log excerpt</summary>", "", "```text", excerpt, "```", "",
                      "</details>"]
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


def main(argv: list[str] | None = None, run: Runner = run_logged) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True, help="updated west workspace root")
    parser.add_argument("--manifest-dir", type=Path, help="unused; accepted for a uniform adapter interface")
    parser.add_argument("--build", required=True, help="build parameters from targets.yml (JSON)")
    parser.add_argument("--toolchain", default="{}", help="toolchain from targets.yml (JSON)")
    parser.add_argument("--artifacts", default="[]", help="unused; products are checked for the signed image")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        build = json.loads(args.build)
        toolchain = json.loads(args.toolchain)
    except json.JSONDecodeError as exc:
        return env_error(args.report_dir, f"invalid JSON argument: {exc}")
    sdk = args.workspace / SDK_DIR
    if not (sdk / FETCH_ASSETS).is_file():
        return env_error(args.report_dir, f"{SDK_DIR / FETCH_ASSETS} not found in the workspace")
    products = build.get("products", [])
    target_workarounds = list(build.get("workarounds", ()))
    declared = {w for p in products for w in p.get("workarounds", ())} | set(target_workarounds)
    unknown = sorted(declared - set(WORKAROUNDS))
    if not products or unknown:
        return env_error(args.report_dir, f"invalid build parameters (unknown workarounds: {unknown})")

    log_dir = args.report_dir / "logs"
    if run([sys.executable, FETCH_ASSETS], sdk, log_dir / "fetch-assets.log") != 0:
        return env_error(args.report_dir, f"{FETCH_ASSETS} failed; see logs/fetch-assets.log")
    sdk_describe = fetch_sdk_tag(args.workspace, sdk)

    # Target workarounds are probed and applied first: the D12 probe reads git's tracked
    # mode, unaffected by chmod, and the D11 probe runs a script executable only after D12.
    probes = {}
    for name in sorted(declared, key=lambda n: WORKAROUNDS[n].scope != "target"):
        probes.update(probe_workarounds([name], sdk))
        if name in target_workarounds and WORKAROUNDS[name].apply:
            WORKAROUNDS[name].apply(sdk)

    results = {}
    for product in products:
        merged = {**product, "workarounds": [*target_workarounds, *product.get("workarounds", ())]}
        print(f"::group::{product['name']}: west build -b {product['board']} {product['app']}", flush=True)
        results[product["name"]] = build_product(merged, sdk, log_dir, run)
        print(f"{product['name']}: {results[product['name']]['status']}", flush=True)
        print("::endgroup::", flush=True)

    actual = {"arm-zephyr-eabi-gcc": gcc_version(toolchain)}
    summary = summarize(results, probes, toolchain, actual, sdk_describe)
    write_outputs(args.report_dir, summary)
    print(f"{ADAPTER}: {summary['result']}{' (with workarounds)' if summary['with_workarounds'] else ''}")
    return 0 if summary["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
