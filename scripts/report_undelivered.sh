#!/usr/bin/env bash
# Report mail that notify.py could not send on the repository's single open
# "CI mail undelivered" issue: comment on it when it is open, open it otherwise.
# Usage: report_undelivered.sh BODY.md   (needs GH_TOKEN and GITHUB_REPOSITORY)
set -euo pipefail

body="$1"
title="CI mail undelivered"
repo="${GITHUB_REPOSITORY:?}"

number="$(gh issue list -R "${repo}" --state open --limit 100 --json number,title \
  --jq "[.[] | select(.title == \"${title}\")][0].number // empty")"
if [ -n "${number}" ]; then
  gh issue comment "${number}" -R "${repo}" --body-file "${body}"
else
  gh issue create -R "${repo}" --title "${title}" --body-file "${body}"
fi
