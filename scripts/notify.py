"""Send the mail for a finished run (SPEC 8.2) and the weekly summary.

    notify.py run     after aggregate.py: new failures, recoveries, and FETCH_FAIL
                      that persisted for two runs; nothing for a still-failing target
    notify.py weekly  Thursday summary of the seven days before; it is also the
                      heartbeat that proves the nightly still runs

Mail goes out over SMTP with TLS (port 465), using SMTP_USER and SMTP_PASSWORD
from the environment; SMTP_HOST defaults to smtp.<domain of SMTP_USER>. The
recipients come from the `notify` table and the targets' `owners` in
targets.yml. With --outbox the messages are written as .eml files instead of
being sent.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Callable, Iterable

from aggregate import PASSING, SCHEMA_VERSION, UNVERIFIED
from targets import ConfigError, Notify, Target, load_notify, load_targets

FAILURES = ("BUILD_FAIL", "MANIFEST_FAIL", "INFRA_FAIL")
CODE_FAILURES = ("BUILD_FAIL", "MANIFEST_FAIL")
# SPEC 8.2: a single FETCH_FAIL is usually a network blip.
FETCH_ALERT_AFTER = 2
EXCERPT_LINES = 20
DRIFT_LINES = 40
WEEK_DAYS = 7
EMAIL_PATTERN = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
SMTP_PORT = 465
SMTP_TIMEOUT = 60
SEND_ATTEMPTS = 3
SENDER_NAME = "HMI SDK CI"

LABELS = {
    "new-failure": "New failure",
    "changed": "Failure changed",
    "recovered": "Recovered",
    "fetch-alert": f"Cannot fetch ({FETCH_ALERT_AFTER} runs)",
    "fetch-recovered": "Fetch recovered",
}
MEANING = {
    "BUILD_FAIL": "Fetched, but the build or a required artifact check failed.",
    "MANIFEST_FAIL": (
        "The manifest cannot be fetched for a reason a retry cannot fix (missing repository, branch "
        "or commit). Customers following the README hit the same error."
    ),
    "INFRA_FAIL": (
        "CI infrastructure problem (toolchain, runner, or targets.yml out of date), not an SDK "
        "problem. Only CI maintainers receive this."
    ),
    "FETCH_FAIL": (
        f"Network failures on every source tried, {FETCH_ALERT_AFTER} runs in a row. The code was "
        "not verified; customers may not be able to fetch it either."
    ),
}

Block = tuple[str, object]


class UsageError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    kind: str  # a key of LABELS
    entry: dict  # results.json entry
    audience: str  # code, network or infra: decides who is mailed

    @property
    def urgent(self) -> bool:
        return self.entry["track"] == "locked" and self.entry["status"] in CODE_FAILURES


def audience_for(status: str | None) -> str:
    return "infra" if status == "INFRA_FAIL" else "code"


def run_events(results: dict) -> list[Event]:
    """Events worth a mail in one run's results.json; preview targets never mail."""
    events = []
    for e in results.get("entries", []):
        if e.get("tier") != "customer":
            continue
        status, change = e["status"], e["change"]
        if status in FAILURES:
            if change in ("new-failure", "first-run"):
                events.append(Event("new-failure", e, audience_for(status)))
            elif change == "still-failing" and e.get("previous_failure_status") != status:
                events.append(Event("changed", e, audience_for(status)))
        elif status in UNVERIFIED:
            if e.get("unverified") == FETCH_ALERT_AFTER:
                events.append(Event("fetch-alert", e, "network"))
        elif status in PASSING:
            if change == "recovered":
                events.append(Event("recovered", e, audience_for(e.get("previous_failure_status"))))
            elif e.get("previous_unverified", 0) >= FETCH_ALERT_AFTER:
                events.append(Event("fetch-recovered", e, "network"))
    order = list(LABELS)
    events.sort(key=lambda ev: (not ev.urgent, order.index(ev.kind), ev.entry["key"]))
    return events


def parse_addresses(value: str, where: str) -> list[str]:
    addresses = [a.strip().lower() for a in re.split(r"[,\s]+", value or "") if a.strip()]
    bad = [a for a in addresses if not EMAIL_PATTERN.match(a)]
    if bad:
        raise UsageError(f"{where}: not an email address: {', '.join(bad)}")
    return addresses


class Recipients:
    """The fixed table in targets.yml: CI maintainers hear about everything and are
    the only ones told about INFRA_FAIL; watchers and the target's owners hear about
    SDK problems."""

    def __init__(self, targets: list[Target], notify: Notify) -> None:
        self.targets = {t.id: t for t in targets}
        self.notify = notify

    def of(self, audience: str, target_id: str) -> set[str]:
        people = set(self.notify.ci_maintainers)
        target = self.targets.get(target_id)
        if audience == "infra":
            return people
        people.update(self.notify.watchers)
        if target is not None:
            people.update(target.owners)
        return people


def group_by_recipients(
    events: list[Event], recipients_of: Callable[[Event], set[str]]
) -> list[tuple[list[str], list[Event]]]:
    """One message per distinct set of events, sent to everyone who gets exactly that set."""
    per_person: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        for person in recipients_of(event):
            per_person.setdefault(person, []).append(index)
    groups: dict[tuple[int, ...], list[str]] = {}
    for person, indexes in per_person.items():
        groups.setdefault(tuple(indexes), []).append(person)
    return [(sorted(people), [events[i] for i in indexes]) for indexes, people in sorted(groups.items())]


# --- message content ---------------------------------------------------------


def first_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit] + [f"... ({len(lines) - limit} more lines in the run report)"])


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def find_report(reports: Path | None, entry: dict) -> Path | None:
    if reports is None:
        return None
    matches = sorted(p for p in reports.glob(f"report-{entry['id']}-{entry['track']}-*") if p.is_dir())
    return matches[-1] if matches else None


def failure_details(entry: dict, report: Path | None) -> list[Block]:
    blocks: list[Block] = [("p", f"Plan: {error}") for error in entry.get("plan_errors", [])]
    if report is None:
        return blocks
    if entry["status"] in ("FETCH_FAIL", "MANIFEST_FAIL"):
        fetch = read_json(report / "fetch" / "fetch.json")
        if fetch.get("reason"):
            blocks.append(("p", f"Fetch: {fetch['reason']}"))
        failures = [a["failure"] for a in fetch.get("attempts", []) if a.get("failure")]
        if failures and failures[-1].get("projects"):
            blocks.append(("p", "Projects: " + ", ".join(failures[-1]["projects"])))
        if failures and failures[-1].get("urls"):
            blocks.append(("p", "URLs: " + ", ".join(failures[-1]["urls"])))
    summary = read_json(report / "build" / "summary.json")
    if summary.get("error"):
        blocks.append(("p", f"Build adapter: {summary['error']}"))
    for problem in summary.get("artifact_problems", []):
        blocks.append(("p", problem))
    for mode in summary.get("failed_modes", []):
        info = summary.get("modes", {}).get(mode, {})
        blocks.append(("p", f"Mode {mode} failed: {info.get('message', '')}".rstrip(": ")))
        if info.get("excerpt"):
            blocks.append(("pre", first_lines(info["excerpt"], EXCERPT_LINES)))
    drift = report / "drift.md"
    if entry["status"] == "BUILD_FAIL" and drift.is_file():
        blocks.append(("p", "Changes since the locked manifest (the failure is likely among them):"))
        blocks.append(("pre", first_lines(drift.read_text(encoding="utf-8", errors="replace"), DRIFT_LINES)))
    return blocks


def event_blocks(event: Event, reports: Path | None) -> list[Block]:
    e = event.entry
    blocks: list[Block] = [("h", f"{LABELS[event.kind]}: {e['id']} ({e['track']}) {e['status']}")]
    if event.urgent:
        blocks.append(("alert", "The locked manifest no longer builds: the known-good snapshot is gone."))
    if event.kind in ("new-failure", "changed", "fetch-alert"):
        if event.kind == "changed":
            blocks.append(("p", f"Was {e.get('previous_failure_status')} until the previous run."))
        blocks.append(("p", MEANING[e["status"]]))
        if e.get("used_fallback"):
            blocks.append(("p", "Fetched from the GitHub mirror after a gitee network failure."))
        blocks += failure_details(e, find_report(reports, e))
    elif event.kind == "recovered":
        blocks.append(("p", f"Passes again; was {e.get('previous_failure_status') or 'failing'}."))
    else:
        blocks.append(("p", f"Fetched and built again after {e.get('previous_unverified')} runs that could not fetch."))
    return blocks


def status_table(entries: Iterable[dict]) -> Block:
    rows = [["Target", "Track", "Result", "Change"]]
    for e in entries:
        rows.append([e["id"], e["track"], e["status"], e["change"]])
    return ("table", rows)


def run_subject(events: list[Event]) -> str:
    parts = [f"{LABELS[ev.kind].lower()}: {ev.entry['id']} ({ev.entry['track']})" for ev in events]
    if len(parts) > 3:
        parts = [f"{len(events)} changes"]
    prefix = "⚠ " if any(ev.urgent for ev in events) else ""
    return f"[HMI SDK CI] {prefix}{'; '.join(parts)}"


def run_blocks(events: list[Event], reports: Path | None, run_url: str) -> list[Block]:
    blocks: list[Block] = [status_table(ev.entry for ev in events)]
    for event in events:
        blocks += event_blocks(event, reports)
    blocks.append(("link", ("Open the run", run_url)))
    return blocks


def cell_html(text: str) -> str:
    if text in FAILURES or text in UNVERIFIED:
        return f'<b style="color:#c00">{html.escape(text)}</b>'
    return html.escape(text)


def render_text(blocks: list[Block]) -> str:
    out: list[str] = []
    for kind, value in blocks:
        if kind == "h":
            out += ["", str(value), "-" * len(str(value))]
        elif kind in ("p", "alert"):
            out.append(("!! " if kind == "alert" else "") + str(value))
        elif kind == "pre":
            out += ["    " + line for line in str(value).splitlines()]
        elif kind == "table":
            rows = value  # type: ignore[assignment]
            widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
            for n, row in enumerate(rows):
                out.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
                if n == 0:
                    out.append("  ".join("-" * w for w in widths))
        elif kind == "link":
            text, url = value  # type: ignore[misc]
            out += ["", f"{text}: {url}"]
    return "\n".join(out).strip() + "\n"


def render_html(blocks: list[Block]) -> str:
    esc = html.escape
    out = ['<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px">']
    for kind, value in blocks:
        if kind == "h":
            out.append(f"<h3>{esc(str(value))}</h3>")
        elif kind == "p":
            out.append(f"<p>{esc(str(value))}</p>")
        elif kind == "alert":
            out.append(f'<p style="color:#c00"><b>&#9888; {esc(str(value))}</b></p>')
        elif kind == "pre":
            out.append(f'<pre style="background:#f6f8fa;padding:8px">{esc(str(value))}</pre>')
        elif kind == "table":
            head, *rows = value  # type: ignore[misc]
            out.append('<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse">')
            out.append("<tr>" + "".join(f"<th>{esc(c)}</th>" for c in head) + "</tr>")
            for row in rows:
                out.append("<tr>" + "".join(f"<td>{cell_html(c)}</td>" for c in row) + "</tr>")
            out.append("</table>")
        elif kind == "link":
            text, url = value  # type: ignore[misc]
            out.append(f'<p><a href="{esc(url, quote=True)}">{esc(text)}</a></p>')
    out.append("</body></html>")
    return "\n".join(out) + "\n"


def build_message(sender: str, to: list[str], subject: str, blocks: list[Block]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, sender))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg.set_content(render_text(blocks))
    msg.add_alternative(render_html(blocks), subtype="html")
    return msg


# --- weekly summary ------------------------------------------------------------


def week_window(today: date) -> list[date]:
    return [today - timedelta(days=n) for n in range(WEEK_DAYS, 0, -1)]


def weekly_report(runs: list[dict], results_by_run: dict[str, dict], today: date) -> dict:
    """Summarise the recorded runs created in the seven UTC days before ``today``."""
    window = week_window(today)
    recorded = []
    for run in runs:
        created = datetime.fromisoformat(run["createdAt"].replace("Z", "+00:00"))
        results = results_by_run.get(str(run["databaseId"]))
        if results is not None and window[0] <= created.date() < today:
            recorded.append((created, run["event"], results))
    recorded.sort(key=lambda item: item[0])
    nights = {created.date() for created, event, _ in recorded if event == "schedule"}

    tally: dict[str, Counter] = {}
    for _, _, results in recorded:
        for e in results.get("entries", []):
            tally.setdefault(e["key"], Counter())[e["status"]] += 1
    latest: dict[str, dict] = {}
    if recorded:
        last = recorded[-1][2]
        latest = {e["key"]: e for e in last.get("carried", []) + last.get("entries", [])}

    rows = []
    for key in sorted(set(latest) | set(tally)):
        e = latest.get(key, {})
        counts = tally.get(key, Counter())
        if e.get("failing"):
            state = f"failing for {e.get('streak', 0)} runs"
        elif e.get("status") in UNVERIFIED:
            state = f"unverified for {e.get('unverified', 0)} runs"
        else:
            state = "passing" if e else "no result"
        verified = sum(n for status, n in counts.items() if status not in UNVERIFIED)
        rows.append(
            {
                "key": key,
                "id": e.get("id", key.split("/")[0]),
                "track": e.get("track", key.split("/")[-1]),
                "tier": e.get("tier", ""),
                "status": e.get("status", ""),
                "failing": bool(e.get("failing")),
                "state": state,
                "passed": sum(n for status, n in counts.items() if status in PASSING),
                "verified": verified,
                "unverified": sum(n for status, n in counts.items() if status in UNVERIFIED),
            }
        )
    return {
        "first": window[0].isoformat(),
        "last": window[-1].isoformat(),
        "runs": len(recorded),
        "nights": len(nights),
        "missing": [d.isoformat() for d in window if d not in nights],
        "rows": rows,
    }


def weekly_subject(report: dict) -> str:
    failing = sum(1 for row in report["rows"] if row["failing"])
    alarm = report["runs"] == 0 or any(r["failing"] and r["track"] == "locked" for r in report["rows"])
    return (
        f"[HMI SDK CI] {'⚠ ' if alarm else ''}weekly {report['first']}..{report['last']}: "
        f"{report['nights']}/{WEEK_DAYS} nightly runs, {failing} failing"
    )


def weekly_blocks(report: dict, runs_url: str) -> list[Block]:
    blocks: list[Block] = [
        ("p", f"Nightly runs with results: {report['nights']} of {WEEK_DAYS} ({report['first']} to {report['last']}, UTC).")
    ]
    if report["runs"] == 0:
        blocks.append(("alert", f"No recorded run at all in the last {WEEK_DAYS} days: the nightly may be disabled."))
    elif report["missing"]:
        blocks.append(
            ("p", "No nightly results for " + ", ".join(report["missing"]) + ": the schedule was skipped, or the run "
             "died before recording results.")
        )
    rows = [["Target", "Track", "Tier", "Latest", "State", "This week"]]
    for row in report["rows"]:
        week = f"{row['passed']}/{row['verified']} passed"
        if row["unverified"]:
            week += f", {row['unverified']} unverified"
        rows.append([row["id"], row["track"], row["tier"], row["status"], row["state"], week])
    if len(rows) > 1:
        blocks.append(("table", rows))
    blocks.append(("link", ("All runs", runs_url)))
    return blocks


def load_week_results(results_dir: Path) -> dict[str, dict]:
    found = {}
    for path in results_dir.glob("results-*/results.json"):
        data = read_json(path)
        if data.get("schema_version") == SCHEMA_VERSION:
            found[path.parent.name.removeprefix("results-")] = data
    return found


# --- delivery ------------------------------------------------------------------


@dataclass(frozen=True)
class Smtp:
    user: str
    password: str
    host: str


def smtp_from_env(env: dict[str, str]) -> Smtp | None:
    user = env.get("SMTP_USER", "").strip()
    password = env.get("SMTP_PASSWORD", "").strip()
    if not user or not password:
        return None
    host = env.get("SMTP_HOST", "").strip() or f"smtp.{user.rsplit('@', 1)[-1]}"
    return Smtp(user, password, host)


def send(
    smtp: Smtp,
    messages: list[EmailMessage],
    connect: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for msg in messages:
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                with connect(smtp.host, SMTP_PORT, context=ssl.create_default_context(), timeout=SMTP_TIMEOUT) as server:
                    server.login(smtp.user, smtp.password)
                    server.send_message(msg)
                break
            except smtplib.SMTPAuthenticationError:
                raise
            except (smtplib.SMTPException, OSError) as exc:
                if attempt == SEND_ATTEMPTS:
                    raise
                print(f"::warning::sending failed ({type(exc).__name__}: {exc}), retrying", file=sys.stderr)
                sleep(30)


def deliver(
    groups: list[tuple[list[str], str, list[Block]]], outbox: Path | None, env: dict[str, str]
) -> int:
    smtp = smtp_from_env(env)
    sender = smtp.user if smtp else "hmi-sdk-ci@example.invalid"
    messages = [build_message(sender, to, subject, blocks) for to, subject, blocks in groups]
    for msg, (to, _, _) in zip(messages, groups):
        # Logs are public: say how many, not who.
        print(f"mail: {msg['Subject']} -> {len(to)} recipient(s)")
    if outbox is not None:
        outbox.mkdir(parents=True, exist_ok=True)
        for n, msg in enumerate(messages, 1):
            (outbox / f"{n:02d}.eml").write_bytes(msg.as_bytes())
        return 0
    if smtp is None:
        print(f"::error::SMTP_USER / SMTP_PASSWORD are not set; {len(messages)} message(s) not sent", file=sys.stderr)
        return 1
    try:
        send(smtp, messages)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"::error::cannot send mail via {smtp.host}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def append_summary(path: Path | None, lines: list[str]) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


# --- commands ------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, env: dict[str, str]) -> int:
    results = read_json(args.results)
    if results.get("schema_version") != SCHEMA_VERSION:
        raise UsageError(f"{args.results}: not a results.json of schema {SCHEMA_VERSION}")
    events = run_events(results)
    if not events:
        print("no mail: no new failure, recovery or fetch alert")
        append_summary(args.summary, ["", "## Mail", "", "No mail: no new failure, recovery or fetch alert."])
        return 0

    if args.to:
        groups = [(parse_addresses(args.to, "--to"), events)]
        orphans: list[Event] = []
    else:
        recipients = Recipients(load_targets(args.targets), load_notify(args.targets))
        groups = group_by_recipients(events, lambda ev: recipients.of(ev.audience, ev.entry["id"]))
        orphans = [ev for ev in events if not recipients.of(ev.audience, ev.entry["id"])]
    messages = [(to, run_subject(evs), run_blocks(evs, args.reports, args.run_url)) for to, evs in groups]
    append_summary(
        args.summary,
        ["", "## Mail", ""] + [f"- {subject} ({len(to)} recipient(s))" for to, subject, _ in messages],
    )
    status = deliver(messages, args.outbox, env)
    for event in orphans:
        print(f"::error::nobody to mail about {event.entry['key']}; fill notify in targets.yml", file=sys.stderr)
    return 1 if orphans else status


def cmd_weekly(args: argparse.Namespace, env: dict[str, str]) -> int:
    runs = json.loads(args.runs.read_text(encoding="utf-8"))
    report = weekly_report(runs, load_week_results(args.results_dir), date.fromisoformat(args.today))
    subject = weekly_subject(report)
    blocks = weekly_blocks(report, args.runs_url)
    if args.to:
        to = parse_addresses(args.to, "--to")
    else:
        targets = load_targets(args.targets)
        recipients = Recipients(targets, load_notify(args.targets))
        people: set[str] = set(recipients.notify.ci_maintainers) | set(recipients.notify.watchers)
        for target in targets:
            if target.tier == "customer":
                people |= recipients.of("code", target.id)
        to = sorted(people)
    if not to:
        print("::error::nobody to send the weekly summary to; fill notify in targets.yml", file=sys.stderr)
        return 1
    append_summary(args.summary, ["## Weekly summary", "", f"- {subject} ({len(to)} recipient(s))"])
    return deliver([(to, subject, blocks)], args.outbox, env)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--targets", type=Path, default=Path("targets.yml"))
    common.add_argument("--to", help="send everything only to these addresses (testing)")
    common.add_argument("--outbox", type=Path, help="write .eml files here instead of sending")
    common.add_argument("--summary", type=Path)

    run = sub.add_parser("run", parents=[common], help="mail the changes of one run")
    run.add_argument("--results", type=Path, required=True, help="results.json from aggregate.py")
    run.add_argument("--reports", type=Path, help="directory holding the job reports")
    run.add_argument("--run-url", required=True)

    weekly = sub.add_parser("weekly", parents=[common], help="mail the weekly summary")
    weekly.add_argument("--runs", type=Path, required=True, help="`gh run list --json databaseId,event,createdAt` output")
    weekly.add_argument("--results-dir", type=Path, required=True, help="holds results-<run id>/results.json")
    weekly.add_argument("--today", default=date.today().isoformat(), help="UTC date of the summary (YYYY-MM-DD)")
    weekly.add_argument("--runs-url", required=True)

    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    try:
        return cmd_run(args, env) if args.command == "run" else cmd_weekly(args, env)
    except ConfigError as exc:
        print("\n".join(exc.errors), file=sys.stderr)
        return 2
    except UsageError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
