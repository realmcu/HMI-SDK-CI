# HMI-SDK-CI

Nightly verification of the public HMI SDK manifests in `realmcu/HMI-MANIFEST`. See `README.md` for
how it works; this file lists the rules and pitfalls for changing it.

## Hard rules

- This repository is **public**: logs, artifacts, job summaries and issues are world-readable. Never
  print secrets or mail addresses; report recipient *counts* only.
- CI only reads the manifest repository and never pushes to any product repository. The only thing
  it writes is this repository's own `state` branch (`poll.yml`).
- `SMTP_USER` / `SMTP_PASSWORD` are used only by steps gated on `schedule` / `workflow_dispatch`,
  so pull requests (including forks) never see them. Keep that gate on every new secret.
- Mail is sent as the `SMTP_USER` account itself; never put another domain in `From`.

## Lock promotion is a contract with the manifest repository

`scripts/promote_lock.py` in the manifest repository consumes `lock-promotion-<id>-<run id>` and
trusts nothing in its metadata. It asks GitHub for the run and requires event `schedule` or
`workflow_dispatch`, workflow path `.github/workflows/hmi-ci.yml`, branch `main`, head repository
`realmcu/HMI-SDK-CI` and `head_sha == ci_commit`. Renaming the workflow, the artifact, the metadata
fields (`schema_version` 2) or the gating in `hmi-ci.yml` breaks promotion: change the manifest
side in the same step (it goes through code review there, so it lands later than a push here).

A package is produced only when the `floating` track passed, did not fall back to the mirror, ran
manifest `master`, and was a scheduled or dispatched run.

## Workflow

- Push straight to `main` (`git push origin HEAD:main`); there is no PR flow. Every push runs
  `hmi-ci.yml` as a self-test: no mail, not recorded in the result history.
- Real checks: `gh workflow run hmi-ci.yml -R realmcu/HMI-SDK-CI --ref main`, then `gh run watch`.
  `notify_to` limits that run's mail to the given addresses; `targets` limits the targets.
- To test a failure mail, force the failure on a temporary branch and delete the branch afterwards.
- Commit as the local-config identity (hellsge); English Conventional Commits; LF line endings.

## Tests

- `python -m pytest -q tests` must pass before pushing; the workflow runs it too.
- On Windows `Path.write_text` writes CRLF. Tests that check hashes must hash the bytes actually on
  disk (`read_bytes()`), not the string that was written.

## Things that look like bugs but are not

- GitHub delays scheduled runs by hours and sometimes skips them; `poll.yml`'s two-hour cron may run
  only a few times a day. Results are counted per run, never per calendar day.
- GitHub disables schedules after 60 days without repository activity; the daily `state` commit
  exists to prevent that. Do not "optimise" it away.
- `FETCH_FAIL` neither starts nor ends a failure streak; only schedule / dispatch runs are history
  (`scripts/previous_run.py`).
- smtp.163.com often answers `535` to correct credentials from runner IPs; it is retried like a
  network error, and undelivered mail is reported on the `CI mail undelivered` issue.
- A new toolchain version needs its official archive URL and SHA-256 in `scripts/toolchain.py`.
