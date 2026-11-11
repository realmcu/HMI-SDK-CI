# HMI SDK CI

Continuous verification for the Realtek HMI SDKs published through
[realmcu/HMI-MANIFEST](https://github.com/realmcu/HMI-MANIFEST).

Every night this repository checks that each public manifest can still be fetched the way its README
describes (`west init` + `west update`) and that the SDK builds. It only reads the manifest repository
and never pushes to any product repository.

**This is not an SDK.** Customers do not need to clone it; follow the instructions in
[realmcu/HMI-MANIFEST](https://github.com/realmcu/HMI-MANIFEST) instead.

## Layout

| Path | Purpose |
| --- | --- |
| `targets.yml` | The SDK targets to verify. The workflow reads only this file; adding a target does not change the workflow. |
| `scripts/` | `plan.py` and the shared CI scripts |
| `tests/` | Tests for `scripts/` |
| `.github/workflows/hmi-ci.yml` | The workflow |

## `targets.yml`

```yaml
schema_version: 1
targets:
  - id: rtl8773e-dashboard
    tier: customer
    manifest: dashboard/rtl8773e-dashboard-gitee.yml
    # ...
```

Manifest paths are relative to the root of the manifest repository.

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique name: lowercase letters and digits joined by single hyphens. Used in job, artifact and mail names. |
| `tier` | yes | `customer`: runs nightly and mails on failure. `preview`: runs, results go to the job summary only. `disabled`: kept in the file but not run. |
| `manifest` | yes | Primary manifest (`.yml`). |
| `fallback_manifest` | no | Manifest to retry with when the primary fails for network reasons. A fallback build never produces a lock promotion. |
| `lock_manifest` | no | Fully pinned snapshot. Adds a `locked` track and a drift report. Requires `version_model: floating`. |
| `version_model` | yes | `pinned`, `mixed` or `floating`. A `customer` target must be `pinned`, or `floating` with a `lock_manifest`. |
| `builder` | yes | Build adapter. Currently only `hmi-west`. |
| `build` | yes | Adapter parameters, checked per adapter (see below). |
| `toolchain` | yes | Tool name to version, for example `arm-gnu: 12.3.rel1`. The version actually used is written to the report. |
| `triggers` | yes | Non-empty list of `nightly` and/or `on-change`. |
| `artifacts` | no | Relative paths or globs that must exist and be non-empty after the build. |
| `owners` | no | Extra mail recipients for this target. |
| `timeout` | no | Job time limit in minutes, 1 to 360. Defaults to the adapter's limit. |

Unknown fields and duplicate keys are errors, so a misspelt field cannot be silently ignored.

### Adapters

| `builder` | `build` fields |
| --- | --- |
| `hmi-west` | `profile`: `quick` (bank 0 only) or `full` (all four modes) |

## Local checks

```sh
python -m pip install -r requirements.txt
python -m pytest -q
python scripts/plan.py --validate
```
