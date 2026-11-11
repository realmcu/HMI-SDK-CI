from __future__ import annotations

import email
import json
import smtplib
import sys
from datetime import date
from email import policy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import aggregate  # noqa: E402
import notify  # noqa: E402
from targets import Notify, Target  # noqa: E402

CI = "ci@example.com"
LEAD = "lead@example.com"
DASH_OWNER = "dash@example.com"
TABLE = Notify(ci_maintainers=(CI,), watchers=(LEAD,))
TARGETS = [
    Target("dash", "customer", "d.yml", "floating", "hmi-west", {}, {}, ("nightly",), owners=(DASH_OWNER,)),
    Target("gate", "customer", "g.yml", "pinned", "hmi-west", {}, {}, ("nightly",)),
]


def plan_for(*keys):
    include = [{"id": k.split("/")[0], "track": k.split("/")[1]} for k in keys]
    ids = sorted({k.split("/")[0] for k in keys})
    return {"event": "nightly", "matrix": {"include": include},
            "targets": [{"id": i, "tier": "customer", "status": "PLANNED", "errors": []} for i in ids]}


def run_through(sequence):
    """Feed {key: status} dicts through aggregate(); return the events of every run."""
    previous, events = {}, []
    for n, statuses in enumerate(sequence):
        results = [{"id": k.split("/")[0], "track": k.split("/")[1], "status": s} for k, s in statuses.items()]
        report = aggregate.aggregate(plan_for(*statuses), results, previous, {"run_id": str(n)})
        events.append([(ev.kind, ev.entry["key"]) for ev in notify.run_events(report)])
        previous = {e["key"]: e for e in report["carried"] + report["entries"]}
    return events


def test_only_changes_are_mailed():
    events = run_through([
        {"dash/floating": "PASS"},
        {"dash/floating": "BUILD_FAIL"},
        {"dash/floating": "BUILD_FAIL"},
        {"dash/floating": "PASS"},
        {"dash/floating": "PASS"},
    ])
    assert events == [[], [("new-failure", "dash/floating")], [], [("recovered", "dash/floating")], []]


def test_fetch_fail_mails_on_the_second_run_and_on_recovery():
    events = run_through([
        {"dash/floating": "PASS"},
        {"dash/floating": "FETCH_FAIL"},
        {"dash/floating": "FETCH_FAIL"},
        {"dash/floating": "FETCH_FAIL"},
        {"dash/floating": "PASS"},
    ])
    assert events == [[], [], [("fetch-alert", "dash/floating")], [], [("fetch-recovered", "dash/floating")]]


def test_single_fetch_fail_stays_silent():
    events = run_through([{"dash/floating": "PASS"}, {"dash/floating": "FETCH_FAIL"}, {"dash/floating": "PASS"}])
    assert events == [[], [], []]


def test_fetch_fail_inside_a_failure_neither_recovers_nor_repeats():
    events = run_through([
        {"dash/floating": "BUILD_FAIL"},
        {"dash/floating": "FETCH_FAIL"},
        {"dash/floating": "BUILD_FAIL"},
        {"dash/floating": "PASS"},
    ])
    assert events[1:] == [[], [], [("recovered", "dash/floating")]]


def test_failure_kind_change_is_mailed_once():
    events = run_through([
        {"dash/floating": "PASS"},
        {"dash/floating": "BUILD_FAIL"},
        {"dash/floating": "MANIFEST_FAIL"},
        {"dash/floating": "MANIFEST_FAIL"},
    ])
    assert events[2:] == [[("changed", "dash/floating")], []]


def test_first_run_failure_is_mailed_and_preview_never():
    report = aggregate.aggregate(plan_for("dash/floating"), [{"id": "dash", "track": "floating", "status": "BUILD_FAIL"}], {}, {})
    assert [ev.kind for ev in notify.run_events(report)] == ["new-failure"]
    report["entries"][0]["tier"] = "preview"
    assert notify.run_events(report) == []


def test_locked_failure_comes_first_and_is_urgent():
    events = notify.run_events({"entries": [
        {"key": "dash/floating", "id": "dash", "tier": "customer", "track": "floating", "status": "BUILD_FAIL", "change": "new-failure"},
        {"key": "dash/locked", "id": "dash", "tier": "customer", "track": "locked", "status": "BUILD_FAIL", "change": "new-failure"},
    ]})
    assert [(ev.entry["track"], ev.urgent) for ev in events] == [("locked", True), ("floating", False)]
    assert notify.run_subject(events).startswith("[HMI SDK CI] ⚠ ")


def test_recipients_follow_the_table():
    recipients = notify.Recipients(TARGETS, TABLE)
    assert recipients.of("code", "dash") == {CI, LEAD, DASH_OWNER}
    assert recipients.of("network", "dash") == {CI, LEAD, DASH_OWNER}
    assert recipients.of("code", "gate") == {CI, LEAD}
    assert recipients.of("infra", "dash") == {CI}


def test_one_message_per_distinct_set_of_events():
    entry = {"key": "dash/floating", "id": "dash", "track": "floating", "status": "BUILD_FAIL"}
    infra = {"key": "gate/main", "id": "gate", "track": "main", "status": "INFRA_FAIL"}
    events = [notify.Event("new-failure", entry, "code"), notify.Event("new-failure", infra, "infra")]
    recipients = notify.Recipients(TARGETS, TABLE)
    groups = notify.group_by_recipients(events, lambda ev: recipients.of(ev.audience, ev.entry["id"]))
    assert sorted((to, [ev.entry["key"] for ev in evs]) for to, evs in groups) == [
        ([CI], ["dash/floating", "gate/main"]),
        ([DASH_OWNER, LEAD], ["dash/floating"]),
    ]


def write_report(reports: Path, track: str, status: str) -> None:
    job = reports / f"report-dash-{track}-7"
    (job / "build").mkdir(parents=True)
    (job / "fetch").mkdir()
    (job / "build" / "summary.json").write_text(json.dumps({
        "result": "FAIL", "failed_modes": ["src_bank0"], "artifact_problems": [],
        "modes": {"src_bank0": {"status": "FAIL", "message": "build failed", "excerpt": "main.c:3: error: <boom> & co"}},
    }))
    (job / "fetch" / "fetch.json").write_text(json.dumps({"status": "OK", "reason": "", "attempts": []}))
    (job / "drift.md").write_text("# Drift\n\n- abc123 GUI: change\n")
    (job / "result.json").write_text(json.dumps({"id": "dash", "track": track, "status": status}))


def results_file(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"schema_version": 1, "run": {}, "entries": entries, "carried": []}))
    return path


def targets_file(tmp_path: Path) -> Path:
    path = tmp_path / "targets.yml"
    path.write_text(
        "schema_version: 1\n"
        f"notify: {{ci_maintainers: [{CI}], watchers: [{LEAD}]}}\n"
        "targets:\n"
        "  - {id: dash, tier: customer, manifest: d.yml, version_model: pinned, builder: hmi-west,\n"
        f"     build: {{profile: full}}, toolchain: {{arm-gnu: 12.3.rel1}}, triggers: [nightly], owners: [{DASH_OWNER}]}}\n"
    )
    return path


def read_eml(path: Path) -> email.message.EmailMessage:
    return email.message_from_bytes(path.read_bytes(), policy=policy.default)


def test_run_mail_content(tmp_path, capsys):
    reports = tmp_path / "reports"
    write_report(reports, "floating", "BUILD_FAIL")
    entry = {"key": "dash/floating", "id": "dash", "tier": "customer", "track": "floating",
             "status": "BUILD_FAIL", "change": "new-failure", "plan_errors": []}
    outbox = tmp_path / "out"
    summary = tmp_path / "summary.md"
    code = notify.main([
        "run", "--results", str(results_file(tmp_path, [entry])), "--reports", str(reports),
        "--targets", str(targets_file(tmp_path)), "--run-url", "https://example.com/run/7",
        "--outbox", str(outbox), "--summary", str(summary),
    ], env={"SMTP_USER": "ci-bot@163.com", "SMTP_PASSWORD": "secret"})
    assert code == 0
    (eml,) = sorted(outbox.glob("*.eml"))
    msg = read_eml(eml)
    assert msg["Subject"] == "[HMI SDK CI] new failure: dash (floating)"
    assert sorted(a.strip() for a in msg["To"].split(",")) == [CI, DASH_OWNER, LEAD]
    assert msg["From"] == "HMI SDK CI <ci-bot@163.com>"
    text = msg.get_body(("plain",)).get_content()
    assert "Mode src_bank0 failed: build failed" in text
    assert "main.c:3: error" in text and "abc123 GUI: change" in text
    assert "https://example.com/run/7" in text
    page = msg.get_body(("html",)).get_content()
    assert "&lt;boom&gt; &amp; co" in page and "<boom>" not in page
    out = capsys.readouterr().out
    assert "3 recipient(s)" in out and CI not in out
    assert "new failure: dash (floating)" in summary.read_text(encoding="utf-8")


def test_run_without_changes_sends_nothing(tmp_path):
    entry = {"key": "dash/floating", "id": "dash", "tier": "customer", "track": "floating",
             "status": "PASS", "change": "passing"}
    outbox = tmp_path / "out"
    assert notify.main([
        "run", "--results", str(results_file(tmp_path, [entry])), "--targets", str(targets_file(tmp_path)),
        "--run-url", "u", "--outbox", str(outbox),
    ], env={}) == 0
    assert not outbox.exists()


def test_missing_smtp_credentials_fail_loudly(tmp_path):
    entry = {"key": "dash/floating", "id": "dash", "tier": "customer", "track": "floating",
             "status": "BUILD_FAIL", "change": "new-failure"}
    assert notify.main([
        "run", "--results", str(results_file(tmp_path, [entry])), "--targets", str(targets_file(tmp_path)),
        "--run-url", "u",
    ], env={}) == 1


def test_weekly_report_counts_nights_and_missing_days():
    def run(run_id, day, event="schedule"):
        return {"databaseId": run_id, "event": event, "createdAt": f"2026-11-{day:02d}T15:30:00Z"}

    def results(status, failing=False, streak=0):
        return {"entries": [{"key": "dash/floating", "id": "dash", "track": "floating", "tier": "customer",
                             "status": status, "failing": failing, "streak": streak}], "carried": []}

    runs = [run(1, 2), run(2, 4), run(3, 5), run(4, 5, "workflow_dispatch"), run(5, 9), run(6, 1)]
    by_run = {"1": results("PASS"), "2": results("FETCH_FAIL"), "3": results("BUILD_FAIL", True, 1),
              "4": results("BUILD_FAIL", True, 2), "5": results("BUILD_FAIL"), "6": results("PASS")}
    report = notify.weekly_report(runs, by_run, date(2026, 11, 9))
    assert (report["first"], report["last"]) == ("2026-11-02", "2026-11-08")
    assert report["nights"] == 3 and report["runs"] == 4
    assert report["missing"] == ["2026-11-03", "2026-11-06", "2026-11-07", "2026-11-08"]
    (row,) = report["rows"]
    assert (row["state"], row["passed"], row["verified"], row["unverified"]) == ("failing for 2 runs", 1, 3, 1)
    assert notify.weekly_subject(report) == "[HMI SDK CI] weekly 2026-11-02..2026-11-08: 3/7 nightly runs, 1 failing"


def test_weekly_with_no_runs_is_an_alarm():
    report = notify.weekly_report([], {}, date(2026, 11, 9))
    assert notify.weekly_subject(report).startswith("[HMI SDK CI] ⚠ weekly")
    assert any(kind == "alert" for kind, _ in notify.weekly_blocks(report, "u"))


def test_weekly_goes_to_everyone_in_the_table(tmp_path):
    (tmp_path / "runs.json").write_text("[]")
    outbox = tmp_path / "out"
    assert notify.main([
        "weekly", "--runs", str(tmp_path / "runs.json"), "--results-dir", str(tmp_path),
        "--targets", str(targets_file(tmp_path)), "--today", "2026-11-09", "--runs-url", "u",
        "--outbox", str(outbox),
    ], env={}) == 0
    (eml,) = outbox.glob("*.eml")
    assert sorted(a.strip() for a in read_eml(eml)["To"].split(",")) == [CI, DASH_OWNER, LEAD]


class FakeSMTP:
    sent: list = []
    logins = 0
    failures = 0
    auth_failures = 0

    def __init__(self, host, port, context=None, timeout=None):
        if FakeSMTP.failures:
            FakeSMTP.failures -= 1
            raise OSError("connection reset")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSMTP.logins += 1
        if FakeSMTP.auth_failures:
            FakeSMTP.auth_failures -= 1
            raise smtplib.SMTPAuthenticationError(535, b"Error: authentication failed")
        if password != "right":
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg["Subject"])


def send_with_fake(password, failures=0, auth_failures=0):
    msg = notify.build_message("ci-bot@163.com", [CI], "s", [("p", "x")])
    FakeSMTP.sent, FakeSMTP.logins = [], 0
    FakeSMTP.failures, FakeSMTP.auth_failures = failures, auth_failures
    notify.send(notify.Smtp("ci-bot@163.com", password, "smtp.163.com"), [msg], connect=FakeSMTP, sleep=lambda s: None)


def test_send_retries_network_errors_and_sporadic_auth_rejections():
    send_with_fake("right", failures=1)
    assert FakeSMTP.sent == ["s"]
    send_with_fake("right", auth_failures=1)
    assert FakeSMTP.sent == ["s"]


def test_send_gives_up_on_bad_credentials():
    with pytest.raises(smtplib.SMTPAuthenticationError):
        send_with_fake("wrong")
    assert FakeSMTP.logins == notify.SEND_ATTEMPTS and FakeSMTP.sent == []


def test_smtp_host_defaults_to_sender_domain():
    assert notify.smtp_from_env({"SMTP_USER": "a@163.com", "SMTP_PASSWORD": "p"}).host == "smtp.163.com"
    assert notify.smtp_from_env({"SMTP_USER": "a@163.com", "SMTP_PASSWORD": "p", "SMTP_HOST": "mx"}).host == "mx"
    assert notify.smtp_from_env({"SMTP_USER": "a@163.com"}) is None
