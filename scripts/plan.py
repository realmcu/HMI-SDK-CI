"""Plan a CI run from targets.yml.

Only --validate exists so far: it checks targets.yml against the schema and
exits 0 when it is valid, 1 when it is not.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from targets import ConfigError, load_targets

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan a CI run from targets.yml.")
    parser.add_argument(
        "--targets",
        type=Path,
        default=REPO_ROOT / "targets.yml",
        help="target configuration file (default: targets.yml at the repository root)",
    )
    parser.add_argument("--validate", action="store_true", help="validate the file and exit")
    args = parser.parse_args(argv)
    if not args.validate:
        parser.error("nothing to do: only --validate is implemented")

    try:
        targets = load_targets(args.targets)
    except ConfigError as exc:
        annotate = os.environ.get("GITHUB_ACTIONS") == "true"
        for error in exc.errors:
            prefix = f"::error file={args.targets}::" if annotate else ""
            print(f"{prefix}{args.targets}: {error}", file=sys.stderr)
        return 1

    ids = ", ".join(target.id for target in targets)
    print(f"{args.targets}: {len(targets)} target(s) valid: {ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
