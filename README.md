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
| `scripts/` | `plan.py`, `fetch.py`, `toolchain.py` and other shared CI scripts |
| `scripts/adapters/` | Build adapters, one per build system |
| `tests/` | Tests for `scripts/` |
| `.github/workflows/hmi-ci.yml` | Plan, build and aggregate |
| `.github/workflows/poll.yml` | Manifest change polling |
| `.github/workflows/weekly.yml` | Thursday summary mail |

## `targets.yml`

```yaml
schema_version: 1
targets:
  - id: rtl8773e-dashboard
    tier: customer
    manifest: dashboard/rtl8773e-dashboard-gitee.yml
    # ...
```

Manifest paths are relative to the root of the manifest repository. The optional top-level `notify`
table lists fixed mail recipients (see [Mail](#mail)).

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
| `owners` | no | Email addresses of the people responsible for this SDK; they get this target's mail. |
| `timeout` | no | Job time limit in minutes, 1 to 360. Defaults to the adapter's limit. |

Unknown fields and duplicate keys are errors, so a misspelt field cannot be silently ignored.

### Adapters

| `builder` | `build` fields |
| --- | --- |
| `hmi-west` | `profile`: `quick` (bank 0 only) or `full` (all four modes). Runs the manifest repository's `scripts/verify_dashboard.py`. |

| `toolchain` key | Versions |
| --- | --- |
| `arm-gnu` | `12.3.rel1`, `13.2.rel1` (Arm GNU Toolchain, x86_64 Linux host) |

## Plan

`scripts/plan.py` selects the targets for a run and checks each one against the manifest repository
before anything is fetched:

| Check | Result |
| --- | --- |
| A manifest named in `targets.yml` does not exist | `INFRA_FAIL`: `targets.yml` is out of date; the target is not built |
| The primary manifest is not a valid west manifest | `MANIFEST_FAIL`; the target is not built |
| `version_model` differs from the one derived from the manifest | Error in the plan report; the target is still built |
| `manifest` and `fallback_manifest` differ in anything other than URLs | Error in the plan report; the target is still built |
| `lock_manifest` has a project not pinned to a SHA | Error in the plan report; the target is still built |

The version model is derived from each project's `revision`: a full SHA or a tag is pinned, a branch
floats. Branches and tags are told apart with anonymous `git ls-remote`; if a remote cannot be
reached the model is not checked and a warning is reported instead.

A target with a `lock_manifest` gets two build jobs, `floating` and `locked`; any other target gets
one, `main`. Every job builds the manifest commit the plan saw, even if master moves during the run.

## Build jobs

Each job installs the declared toolchain (`scripts/toolchain.py`: official release archive, SHA-256
checked, cached), fetches the workspace (`scripts/fetch.py`), writes a drift report against the lock
when there is one, and runs the build adapter. Reports are uploaded as
`report-<id>-<track>-<run id>`.

| Result | Meaning |
| --- | --- |
| `PASS` | Fetched and built; required artifacts present |
| `BUILD_FAIL` | Fetched, but the build or an artifact check failed |
| `FETCH_FAIL` | Network failure on every source tried (timeouts, resets, TLS errors, HTTP 429/5xx). The code was not verified |
| `MANIFEST_FAIL` | Fetch failed for a reason a retry cannot fix: repository not found or private, missing branch, unknown SHA |
| `INFRA_FAIL` | Toolchain install failed, an adapter reported an environment error, or `targets.yml` is out of date |

A network failure on a gitee repository is retried, then retried against `fallback_manifest` (for the
`locked` track, a mirror copy derived from the lock). A failure on a github repository has no mirror
to fall back to. A build from the mirror never produces a lock promotion.

## Results across runs

After the builds, `scripts/aggregate.py` compares every target/track with the previous recorded run
(a scheduled or manually dispatched run; push and pull-request runs are CI self-tests and are not
recorded) and labels it *new failure*, *still failing (N runs)*, *recovered* or *unverified*.
`FETCH_FAIL` neither starts nor ends a failure streak. Counts are per run, not per calendar day,
because scheduled runs are sometimes delayed by hours or skipped. The comparison input is the
`results-<run id>` artifact, kept for 90 days.

## Mail

`scripts/notify.py` mails after every scheduled or dispatched run, and `weekly.yml` sends a summary
every Thursday. Push and pull-request runs never mail.

| Event | Mailed |
| --- | --- |
| A `customer` target/track fails after passing, or fails in a different way | once, when it happens |
| It passes again | once |
| It keeps failing the same way | not again; it appears in the weekly summary |
| `FETCH_FAIL` | on the second run in a row, and when fetching works again |
| `preview` targets | never; Job Summary only |
| Thursday | weekly summary: every target's state, pass counts, and the nights with no results |

A failing `locked` track is marked ⚠ and listed first. A run's mail holds every change of that run.

| Recipients | Get |
| --- | --- |
| `notify.ci_maintainers` | everything, and are the only ones told about `INFRA_FAIL` |
| `notify.watchers` | everything except `INFRA_FAIL`, and the weekly summary |
| a target's `owners` | that target's mail, and the weekly summary |

Mail is sent through SMTP over TLS with the repository secrets `SMTP_USER` and `SMTP_PASSWORD`;
the server is `smtp.<domain of SMTP_USER>`. The `notify_to` input of a manual run sends that
run's mail only to the addresses given, for testing.

## Manifest change polling

`.github/workflows/poll.yml` runs every two hours. It compares the manifest repository's `master`
with the head recorded in `state.json` on this repository's `state` branch. When it moved, the
targets with `on-change` whose manifest, fallback or lock changed are dispatched to `hmi-ci.yml` at
that exact commit; when nothing moved, nothing is built. `state.json` is committed when the head
moves and at least once a day, which also keeps this repository active so GitHub does not disable
its scheduled workflows.

## Local checks

```sh
python -m pip install -r requirements.txt
python -m pytest -q tests
python scripts/plan.py --validate
git clone https://github.com/realmcu/HMI-MANIFEST manifest
python scripts/plan.py --manifest-root manifest --summary plan.md
```
