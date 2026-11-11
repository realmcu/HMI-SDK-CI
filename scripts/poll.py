"""Decide whether the manifest repository moved since the last verified poll.

Compares the manifest repository's branch head with state.json, lists the
targets whose manifests changed, and rewrites state.json. Prints a JSON object:

    {"changed": bool, "targets": [...], "commit": bool, "head": "<sha>"}

`commit` tells the workflow whether to commit state.json to the state branch:
on a manifest change, and once a day as a keepalive (GitHub disables scheduled
workflows in repositories without activity for 60 days).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from targets import ConfigError, Target, load_targets

STATE_VERSION = 1


def changed_files(manifest_root: Path, old: str, new: str) -> list[str] | None:
    """Files changed between two commits, or None when ``old`` is not in the checkout."""
    proc = subprocess.run(
        ["git", "-C", str(manifest_root), "diff", "--name-only", old, new],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def affected_targets(targets: list[Target], files: list[str] | None) -> list[str]:
    """Targets with on-change whose manifest, fallback or lock is among ``files``.

    Unknown changes (``files`` is None) affect every on-change target.
    """
    result = []
    for target in targets:
        if target.tier == "disabled" or "on-change" not in target.triggers:
            continue
        own = {m for m in (target.manifest, target.fallback_manifest, target.lock_manifest) if m}
        if files is None or own & set(files):
            result.append(target.id)
    return result


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if data.get("version") == STATE_VERSION else {}


def poll(state: dict, head: str, targets: list[Target], manifest_root: Path, now: datetime) -> tuple[dict, dict]:
    previous = state.get("manifest_head")
    today = now.date().isoformat()
    changed = previous is not None and previous != head
    if changed:
        affected = affected_targets(targets, changed_files(manifest_root, previous, head))
    else:
        affected = []
    new_state = {
        "version": STATE_VERSION,
        "manifest_head": head,
        "last_poll": now.isoformat(timespec="seconds"),
        "last_commit_day": today,
    }
    commit = previous is None or changed or state.get("last_commit_day") != today
    if not commit:
        new_state["last_commit_day"] = state.get("last_commit_day")
    return new_state, {"changed": changed, "targets": affected, "commit": commit, "head": head}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True, help="state.json (rewritten in place)")
    parser.add_argument("--manifest-root", type=Path, required=True, help="manifest checkout at the new head")
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, help="also write head/targets/commit step outputs here")
    args = parser.parse_args(argv)

    try:
        targets = load_targets(args.targets)
    except ConfigError as exc:
        print("\n".join(exc.errors), file=sys.stderr)
        return 1
    head = subprocess.run(
        ["git", "-C", str(args.manifest_root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    new_state, decision = poll(load_state(args.state), head, targets, args.manifest_root, datetime.now(timezone.utc))
    args.state.write_text(json.dumps(new_state, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"head={decision['head']}\n")
            handle.write(f"targets={','.join(decision['targets'])}\n")
            handle.write(f"commit={'true' if decision['commit'] else 'false'}\n")
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
