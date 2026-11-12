"""Load and validate targets.yml, the list of SDK targets this CI verifies."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml

from adapters.zephyr_west import WORKAROUNDS
from toolchain import TOOLS

SCHEMA_VERSION = 1
TIERS = ("customer", "preview", "disabled")
VERSION_MODELS = ("pinned", "mixed", "floating")
TRIGGERS = ("nightly", "on-change")
# GitHub-hosted runners kill any job after 6 hours.
MAX_TIMEOUT_MINUTES = 360

ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TOOL_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MANIFEST_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
ARTIFACT_PATTERN = re.compile(r"^[A-Za-z0-9._/*?\[\]-]+$")
EMAIL_PATTERN = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

TOP_LEVEL_FIELDS = ("schema_version", "targets")
TOP_LEVEL_OPTIONAL = ("notify",)
NOTIFY_FIELDS = ("ci_maintainers", "watchers", "weekly")
REQUIRED_FIELDS = (
    "id",
    "tier",
    "manifest",
    "version_model",
    "builder",
    "build",
    "toolchain",
    "triggers",
)
OPTIONAL_FIELDS = ("fallback_manifest", "mirror_manifest", "lock_manifest", "artifacts", "owners", "timeout")


class ConfigError(Exception):
    """targets.yml is unreadable or violates the schema; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Target:
    id: str
    tier: str
    manifest: str
    version_model: str
    builder: str
    build: dict[str, Any]
    toolchain: dict[str, str]
    triggers: tuple[str, ...]
    fallback_manifest: str | None = None
    # Compared with the primary like a fallback, but never fetched (D13).
    mirror_manifest: str | None = None
    lock_manifest: str | None = None
    artifacts: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    timeout: int | None = None


@dataclass(frozen=True)
class Notify:
    """Fixed mail recipients besides each target's owners."""

    ci_maintainers: tuple[str, ...] = ()  # every mail; the only ones told about INFRA_FAIL
    watchers: tuple[str, ...] = ()  # every SDK mail and the weekly summary
    weekly: tuple[str, ...] = ()  # the weekly summary only


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _check_keys(
    mapping: dict[str, Any],
    where: str,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    errors: list[str],
) -> None:
    for key in required:
        if key not in mapping:
            errors.append(f"{_join(where, key)}: missing required field")
    for key in mapping:
        if key not in required and key not in optional:
            errors.append(f"{_join(where, str(key))}: unknown field")


def _check_choice(value: Any, choices: tuple[str, ...], where: str, errors: list[str]) -> None:
    if value not in choices:
        errors.append(f"{where}: must be one of {', '.join(choices)} (got {value!r})")


def _check_relative_path(
    value: Any, pattern: re.Pattern[str], where: str, errors: list[str]
) -> bool:
    if not isinstance(value, str) or not value:
        errors.append(f"{where}: must be a non-empty string")
        return False
    if not pattern.match(value):
        errors.append(f"{where}: contains characters outside {pattern.pattern} (got {value!r})")
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{where}: must be a relative path without '..' (got {value!r})")
        return False
    return True


def _check_manifest(value: Any, where: str, errors: list[str]) -> None:
    if _check_relative_path(value, MANIFEST_PATTERN, where, errors):
        if PurePosixPath(value).suffix != ".yml":
            errors.append(f"{where}: must name a .yml file (got {value!r})")


def _check_str_list(
    value: Any,
    where: str,
    errors: list[str],
    *,
    non_empty: bool,
    choices: tuple[str, ...] | None = None,
) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        errors.append(f"{where}: must be a list of non-empty strings")
        return
    if non_empty and not value:
        errors.append(f"{where}: must not be empty")
    for item, count in Counter(value).items():
        if count > 1:
            errors.append(f"{where}: duplicate entry {item!r}")
    if choices is not None:
        for item in value:
            _check_choice(item, choices, where, errors)


def _check_emails(value: Any, where: str, errors: list[str]) -> None:
    before = len(errors)
    _check_str_list(value, where, errors, non_empty=False)
    if len(errors) == before:
        for item in value:
            if not EMAIL_PATTERN.match(item) or item != item.lower():
                errors.append(f"{where}: must be lowercase email addresses (got {item!r})")


def _parse_notify(data: dict[str, Any], errors: list[str]) -> Notify:
    raw = data.get("notify", {})
    if not isinstance(raw, dict):
        errors.append("notify: must be a mapping")
        return Notify()
    _check_keys(raw, "notify", (), NOTIFY_FIELDS, errors)
    lists = {}
    for name in NOTIFY_FIELDS:
        if name in raw:
            before = len(errors)
            _check_emails(raw[name], f"notify.{name}", errors)
            if len(errors) == before:
                lists[name] = tuple(raw[name])
    return Notify(**lists)


def _check_toolchain(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, dict) or not value:
        errors.append(f"{where}: must be a non-empty mapping of tool name to version")
        return
    for tool, version in value.items():
        if not isinstance(tool, str) or not TOOL_PATTERN.match(tool):
            errors.append(f"{where}: tool name must be lowercase words joined by '-' (got {tool!r})")
            continue
        if not isinstance(version, str) or not version:
            errors.append(f"{_join(where, tool)}: version must be a quoted string (got {version!r})")
            continue
        if tool not in TOOLS:
            errors.append(f"{_join(where, tool)}: unknown tool; known: {', '.join(TOOLS)}")
        elif version not in TOOLS[tool]:
            errors.append(
                f"{_join(where, tool)}: no pinned release for {version!r}; known: {', '.join(TOOLS[tool])}"
            )


def _validate_hmi_west(build: dict[str, Any], where: str, errors: list[str]) -> None:
    _check_keys(build, where, ("profile",), (), errors)
    # Must match PROFILES in the manifest repository's scripts/verify_dashboard.py.
    if "profile" in build:
        _check_choice(build["profile"], ("quick", "full"), _join(where, "profile"), errors)


CMAKE_ARG_PATTERN = re.compile(r"^-D[A-Za-z0-9_]+=[A-Za-z0-9_.,/+-]*$")
BOARD_PATTERN = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*$")
# A workaround's cmake argument written out by hand would hide it from reports (D11).
WORKAROUND_ARGS = {arg: name for name, w in WORKAROUNDS.items() for arg in w.cmake_args}


def _check_workarounds(value: Any, where: str, scope: str, errors: list[str]) -> None:
    before = len(errors)
    _check_str_list(value, where, errors, non_empty=True)
    if len(errors) != before:
        return
    for name in value:
        workaround = WORKAROUNDS.get(name)
        if workaround is None:
            errors.append(f"{where}: unknown workaround {name!r}; known: {', '.join(WORKAROUNDS)}")
        elif workaround.scope != scope:
            place = "build.workarounds" if workaround.scope == "target" else "each product's workarounds"
            errors.append(f"{where}: {name!r} must be declared in {place}")


def _validate_zephyr_west(build: dict[str, Any], where: str, errors: list[str]) -> None:
    _check_keys(build, where, ("products",), ("workarounds",), errors)
    if "workarounds" in build:
        _check_workarounds(build["workarounds"], _join(where, "workarounds"), "target", errors)
    products = build.get("products")
    if "products" not in build:
        return
    if not isinstance(products, list) or not products:
        errors.append(f"{_join(where, 'products')}: must be a non-empty list")
        return
    names = []
    for i, product in enumerate(products):
        at = f"{where}.products[{i}]"
        if not isinstance(product, dict):
            errors.append(f"{at}: must be a mapping")
            continue
        _check_keys(product, at, ("name", "app", "board"), ("cmake_args", "workarounds"), errors)
        name = product.get("name")
        if "name" in product:
            if isinstance(name, str) and ID_PATTERN.match(name):
                names.append(name)
            else:
                errors.append(f"{at}.name: must be lowercase letters and digits joined by single hyphens (got {name!r})")
        if "app" in product:
            _check_relative_path(product["app"], MANIFEST_PATTERN, f"{at}.app", errors)
        if "board" in product and not (isinstance(product["board"], str) and BOARD_PATTERN.match(product["board"])):
            errors.append(f"{at}.board: must be a Zephyr board name (got {product['board']!r})")
        if "cmake_args" in product:
            before = len(errors)
            _check_str_list(product["cmake_args"], f"{at}.cmake_args", errors, non_empty=True)
            if len(errors) == before:
                for arg in product["cmake_args"]:
                    if not CMAKE_ARG_PATTERN.match(arg):
                        errors.append(f"{at}.cmake_args: must be -DNAME=value (got {arg!r})")
                    elif arg in WORKAROUND_ARGS:
                        errors.append(f"{at}.cmake_args: {arg!r} is workaround {WORKAROUND_ARGS[arg]!r}; "
                                      "declare it under workarounds")
        if "workarounds" in product:
            _check_workarounds(product["workarounds"], f"{at}.workarounds", "product", errors)
    for name, count in Counter(names).items():
        if count > 1:
            errors.append(f"{_join(where, 'products')}: duplicate name {name!r}")


def uses_workarounds(builder: str, build: dict[str, Any]) -> bool:
    if builder != "zephyr-west":
        return False
    return bool(build.get("workarounds")) or any(p.get("workarounds") for p in build.get("products", ()))


# Builder name -> validator for its ``build`` parameters. A builder is accepted
# in targets.yml only once its adapter and validator exist.
BUILDERS: dict[str, Callable[[dict[str, Any], str, list[str]], None]] = {
    "hmi-west": _validate_hmi_west,
    "zephyr-west": _validate_zephyr_west,
}


def _check_cross_fields(raw: dict[str, Any], where: str, errors: list[str]) -> None:
    manifest = raw["manifest"]
    fallback = raw.get("fallback_manifest")
    mirror = raw.get("mirror_manifest")
    lock = raw.get("lock_manifest")
    model = raw["version_model"]

    for name, value in (("fallback_manifest", fallback), ("mirror_manifest", mirror), ("lock_manifest", lock)):
        if value == manifest:
            errors.append(f"{where}.{name}: must differ from manifest")
    if fallback is not None and mirror is not None:
        errors.append(f"{where}.mirror_manifest: a fallback_manifest is already compared; use one of the two")
    if fallback is not None and fallback == lock:
        errors.append(f"{where}.fallback_manifest: must differ from lock_manifest")
    if lock is not None and model != "floating":
        errors.append(f"{where}.lock_manifest: requires version_model floating (got {model!r})")

    if raw["tier"] == "customer":
        if model == "mixed" or (model == "floating" and lock is None):
            shape = model if lock is None else f"{model} with a lock"
            errors.append(
                f"{where}: customer targets must be pinned, or floating with a lock_manifest "
                f"(got {shape})"
            )
        if uses_workarounds(raw["builder"], raw["build"]):
            errors.append(f"{where}: customer targets must not use workarounds; drop them or stay preview")


def _validate_target(raw: Any, index: int, errors: list[str]) -> Target | None:
    where = f"targets[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return None

    target_id = raw.get("id")
    id_ok = isinstance(target_id, str) and bool(ID_PATTERN.match(target_id))
    if id_ok:
        where = f"{where} ({target_id})"
    before = len(errors)

    _check_keys(raw, where, REQUIRED_FIELDS, OPTIONAL_FIELDS, errors)
    if "id" in raw and not id_ok:
        errors.append(
            f"{where}.id: must be lowercase letters and digits joined by single hyphens "
            f"(got {target_id!r})"
        )
    if "tier" in raw:
        _check_choice(raw["tier"], TIERS, f"{where}.tier", errors)
    if "version_model" in raw:
        _check_choice(raw["version_model"], VERSION_MODELS, f"{where}.version_model", errors)
    for name in ("manifest", "fallback_manifest", "mirror_manifest", "lock_manifest"):
        if name in raw:
            _check_manifest(raw[name], f"{where}.{name}", errors)
    if "builder" in raw:
        _check_choice(raw["builder"], tuple(BUILDERS), f"{where}.builder", errors)
    if "build" in raw:
        if not isinstance(raw["build"], dict):
            errors.append(f"{where}.build: must be a mapping")
        elif raw.get("builder") in BUILDERS:
            BUILDERS[raw["builder"]](raw["build"], f"{where}.build", errors)
    if "toolchain" in raw:
        _check_toolchain(raw["toolchain"], f"{where}.toolchain", errors)
    if "triggers" in raw:
        _check_str_list(raw["triggers"], f"{where}.triggers", errors, non_empty=True, choices=TRIGGERS)
    if "artifacts" in raw:
        _check_str_list(raw["artifacts"], f"{where}.artifacts", errors, non_empty=False)
        if isinstance(raw["artifacts"], list):
            for i, item in enumerate(raw["artifacts"]):
                if isinstance(item, str) and item:
                    _check_relative_path(item, ARTIFACT_PATTERN, f"{where}.artifacts[{i}]", errors)
    if "owners" in raw:
        _check_emails(raw["owners"], f"{where}.owners", errors)
    if "timeout" in raw:
        timeout = raw["timeout"]
        # bool is a subclass of int, so `timeout: true` would otherwise pass as 1.
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 1 <= timeout <= MAX_TIMEOUT_MINUTES
        ):
            errors.append(
                f"{where}.timeout: must be whole minutes between 1 and {MAX_TIMEOUT_MINUTES} "
                f"(got {timeout!r})"
            )

    if len(errors) != before:
        return None
    _check_cross_fields(raw, where, errors)
    if len(errors) != before:
        return None

    return Target(
        id=raw["id"],
        tier=raw["tier"],
        manifest=raw["manifest"],
        version_model=raw["version_model"],
        builder=raw["builder"],
        build=dict(raw["build"]),
        toolchain=dict(raw["toolchain"]),
        triggers=tuple(raw["triggers"]),
        fallback_manifest=raw.get("fallback_manifest"),
        mirror_manifest=raw.get("mirror_manifest"),
        lock_manifest=raw.get("lock_manifest"),
        artifacts=tuple(raw.get("artifacts", ())),
        owners=tuple(raw.get("owners", ())),
        timeout=raw.get("timeout"),
    )


def parse_targets(data: Any) -> list[Target]:
    """Validate already-loaded targets.yml content.

    Raises:
        ConfigError: listing every schema violation found, not just the first.
    """
    if not isinstance(data, dict):
        raise ConfigError(["top level: must be a mapping"])

    errors: list[str] = []
    _check_keys(data, "", TOP_LEVEL_FIELDS, TOP_LEVEL_OPTIONAL, errors)
    _parse_notify(data, errors)
    version = data.get("schema_version")
    if "schema_version" in data and (isinstance(version, bool) or version != SCHEMA_VERSION):
        errors.append(f"schema_version: must be {SCHEMA_VERSION} (got {version!r})")

    raw_targets = data.get("targets")
    targets: list[Target] = []
    if "targets" in data:
        if not isinstance(raw_targets, list) or not raw_targets:
            errors.append("targets: must be a non-empty list")
        else:
            for index, raw in enumerate(raw_targets):
                target = _validate_target(raw, index, errors)
                if target is not None:
                    targets.append(target)
            ids = [raw.get("id") for raw in raw_targets if isinstance(raw, dict)]
            for target_id, count in Counter(i for i in ids if isinstance(i, str)).items():
                if count > 1:
                    errors.append(f"targets: duplicate id {target_id!r}")

    if errors:
        raise ConfigError(errors)
    return targets


def parse_notify(data: Any) -> Notify:
    """The ``notify`` table of already-validated targets.yml content."""
    errors: list[str] = []
    notify = _parse_notify(data if isinstance(data, dict) else {}, errors)
    if errors:
        raise ConfigError(errors)
    return notify


def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read file: {exc.strerror or exc}"]) from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError([f"invalid YAML: {exc}"]) from exc
    return data


def load_targets(path: Path) -> list[Target]:
    """Read and validate a targets.yml file.

    Raises:
        ConfigError: the file cannot be read, is not valid YAML, or violates the schema.
    """
    return parse_targets(_read(path))


def load_notify(path: Path) -> Notify:
    """Read targets.yml and return its validated ``notify`` table.

    Raises:
        ConfigError: as for load_targets().
    """
    data = _read(path)
    parse_targets(data)
    return parse_notify(data)
