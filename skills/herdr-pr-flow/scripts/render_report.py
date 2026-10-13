"""One-shot HTML renderer for herdr-pr-flow task reports.

Reads ``report.json`` from a task directory and writes a self-contained,
dark-theme, GitHub-PR-style ``report.html`` next to it.

Design constraints (intentional):
  * standard library only, Python >= 3.10;
  * no subprocess, no network, no deletion of user data;
  * a single one-shot render, not an orchestration service.

Public API:
    validate_report(data)  -> None      (raises ValidationError on any problem)
    render_html(data)      -> str       (validates, then returns HTML)
    write_report(task_dir) -> str       (path of the report.html it wrote)

Security model for ``write_report``:
  * the task directory must live at ``<workspace>/.agent-pr/tasks/<task-id>``;
  * the task directory itself, ``report.json`` and the output must not be
    symlinks, and ``report.json`` must not resolve outside the task directory;
  * the output is written to a randomly named sibling temp file and moved into
    place with ``os.replace`` (atomic on the same filesystem);
  * broad/sensitive targets (filesystem roots, the home directory) are refused.

All report content is treated as untrusted and HTML-escaped. The generated
document contains no scripts, no remote assets and no fetched resources.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import string
import tempfile

__all__ = [
    "ReportError",
    "ValidationError",
    "PathSecurityError",
    "validate_report",
    "render_html",
    "write_report",
]

SCHEMA_VERSION = 1

STATUSES = (
    "PLANNED",
    "IMPLEMENTING",
    "REVIEWING",
    "FIXING",
    "VERIFYING",
    "BLOCKED",
    "READY_FOR_HUMAN",
)
ROLES = ("controller", "developer", "reviewer")
SEVERITIES = ("high", "medium", "low")
FINDING_STATUSES = ("open", "awaiting_verification", "resolved")
HISTORY_KINDS = ("fix", "verification", "comment")
VERDICTS = ("addressed", "not_addressed")
APPROVAL_VERDICTS = ("approved", "changes_requested", "cannot_verify")

_TOP_KEYS = (
    "schema_version",
    "task_id",
    "title",
    "status",
    "requirements",
    "plan",
    "participants",
    "snapshots",
    "final_snapshot_id",
    "findings",
    "approvals",
    "checks",
    "changes",
    "timeline",
)

_HEX_DIGITS = frozenset(string.hexdigits)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ReportError(Exception):
    """Base class for all renderer failures."""


class ValidationError(ReportError):
    """The report data does not satisfy the schema/semantic contract."""


class PathSecurityError(ReportError):
    """A filesystem path failed a safety check."""


# --------------------------------------------------------------------------- #
# Small type predicates
# --------------------------------------------------------------------------- #
def _is_str(value) -> bool:
    return isinstance(value, str)


def _is_int(value) -> bool:
    # bool is a subclass of int; a boolean is never a valid integer here.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bool(value) -> bool:
    return isinstance(value, bool)


def _is_list(value) -> bool:
    return isinstance(value, list)


def _is_dict(value) -> bool:
    return isinstance(value, dict)


def _is_hex64(value) -> bool:
    return _is_str(value) and len(value) == 64 and all(c in _HEX_DIGITS for c in value)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _fail(message: str):
    raise ValidationError(message)


def _require_keys(obj: dict, keys, where: str) -> None:
    for key in keys:
        if key not in obj:
            _fail(f"{where}: missing required key {key!r}")


def _expect_str(value, where: str) -> None:
    if not _is_str(value):
        _fail(f"{where} must be a string")


def _expect_nonempty_str(value, where: str) -> None:
    if not _is_str(value) or not value:
        _fail(f"{where} must be a non-empty string")


def _expect_int(value, where: str) -> None:
    if not _is_int(value):
        _fail(f"{where} must be an integer")


def _expect_bool(value, where: str) -> None:
    if not _is_bool(value):
        _fail(f"{where} must be a boolean")


def _expect_list(value, where: str) -> None:
    if not _is_list(value):
        _fail(f"{where} must be an array")


def _unique_ids(items, where: str) -> set:
    """Validate that each item is an object with a unique non-empty string id."""
    seen: set = set()
    for index, item in enumerate(items):
        at = f"{where}[{index}]"
        if not _is_dict(item):
            _fail(f"{at} must be an object")
        _require_keys(item, ("id",), at)
        _expect_nonempty_str(item["id"], f"{at}.id")
        if item["id"] in seen:
            _fail(f"{where}: duplicate id {item['id']!r}")
        seen.add(item["id"])
    return seen


def validate_report(data) -> None:
    """Validate a schema-v1 report.

    Raises ``ValidationError`` with a human-readable message on the first
    problem found. Returns ``None`` when the report is valid.
    """
    if not _is_dict(data):
        _fail("report must be a JSON object")
    _require_keys(data, _TOP_KEYS, "report")

    if not _is_int(data["schema_version"]) or data["schema_version"] != SCHEMA_VERSION:
        _fail(f"schema_version must be {SCHEMA_VERSION}")

    _expect_nonempty_str(data["task_id"], "task_id")
    _expect_str(data["title"], "title")

    if data["status"] not in STATUSES:
        _fail(f"status must be one of {STATUSES}, got {data['status']!r}")

    requirements = data["requirements"]
    _expect_list(requirements, "requirements")
    for index, requirement in enumerate(requirements):
        _expect_str(requirement, f"requirements[{index}]")

    _expect_str(data["plan"], "plan")

    for key in ("participants", "snapshots", "findings", "approvals", "checks",
                "changes", "timeline"):
        _expect_list(data[key], key)

    # --- identity sets (uniqueness) ---------------------------------------- #
    participant_ids = _unique_ids(data["participants"], "participants")
    snapshot_ids = _unique_ids(data["snapshots"], "snapshots")
    _unique_ids(data["findings"], "findings")  # findings: uniqueness only

    # --- participants ------------------------------------------------------ #
    reviewer_ids: set = set()
    for index, participant in enumerate(data["participants"]):
        at = f"participants[{index}]"
        _require_keys(
            participant,
            ("id", "role", "cli", "requested_model", "observed_model",
             "model_provenance"),
            at,
        )
        if participant["role"] not in ROLES:
            _fail(f"{at}.role must be one of {ROLES}, got {participant['role']!r}")
        _expect_str(participant["cli"], f"{at}.cli")
        _expect_str(participant["requested_model"], f"{at}.requested_model")
        observed = participant["observed_model"]
        if observed is not None and not _is_str(observed):
            _fail(f"{at}.observed_model must be null or a string")
        _expect_str(participant["model_provenance"], f"{at}.model_provenance")
        if participant["role"] == "reviewer":
            reviewer_ids.add(participant["id"])

    # --- snapshots --------------------------------------------------------- #
    for index, snapshot in enumerate(data["snapshots"]):
        at = f"snapshots[{index}]"
        _require_keys(snapshot, ("id", "manifest_sha256"), at)
        if not _is_hex64(snapshot["manifest_sha256"]):
            _fail(f"{at}.manifest_sha256 must be a 64-character hex string")

    # --- final snapshot ---------------------------------------------------- #
    final_snapshot_id = data["final_snapshot_id"]
    if final_snapshot_id is not None:
        _expect_str(final_snapshot_id, "final_snapshot_id")
        if final_snapshot_id not in snapshot_ids:
            _fail(
                f"final_snapshot_id {final_snapshot_id!r} references an unknown snapshot"
            )

    # --- findings ---------------------------------------------------------- #
    for index, finding in enumerate(data["findings"]):
        at = f"findings[{index}]"
        _require_keys(
            finding,
            ("id", "owner", "severity", "path", "line", "snapshot_id", "body",
             "acceptance", "status", "history"),
            at,
        )
        if finding["owner"] not in participant_ids:
            _fail(f"{at}.owner {finding['owner']!r} is not a known participant")
        if finding["owner"] not in reviewer_ids:
            _fail(f"{at}.owner must reference a participant with role 'reviewer'")
        if finding["severity"] not in SEVERITIES:
            _fail(f"{at}.severity must be one of {SEVERITIES}")
        _expect_str(finding["path"], f"{at}.path")
        _expect_int(finding["line"], f"{at}.line")
        if finding["snapshot_id"] not in snapshot_ids:
            _fail(f"{at}.snapshot_id {finding['snapshot_id']!r} is unknown")
        _expect_str(finding["body"], f"{at}.body")
        _expect_str(finding["acceptance"], f"{at}.acceptance")
        if finding["status"] not in FINDING_STATUSES:
            _fail(f"{at}.status must be one of {FINDING_STATUSES}")

        history = finding["history"]
        _expect_list(history, f"{at}.history")
        for h_index, entry in enumerate(history):
            hat = f"{at}.history[{h_index}]"
            _require_keys(
                entry, ("actor", "snapshot_id", "kind", "body", "verdict"), hat
            )
            if "diff" in entry and not _is_str(entry["diff"]):
                _fail(f"{hat}.diff must be a string when present")
            if entry["actor"] not in participant_ids:
                _fail(f"{hat}.actor {entry['actor']!r} is not a known participant")
            if entry["snapshot_id"] not in snapshot_ids:
                _fail(f"{hat}.snapshot_id {entry['snapshot_id']!r} is unknown")
            if entry["kind"] not in HISTORY_KINDS:
                _fail(f"{hat}.kind must be one of {HISTORY_KINDS}")
            _expect_str(entry["body"], f"{hat}.body")
            if entry["kind"] == "verification":
                if entry["verdict"] not in VERDICTS:
                    _fail(
                        f"{hat}.verdict must be one of {VERDICTS} for a verification"
                    )
            elif entry["verdict"] is not None:
                _fail(f"{hat}.verdict must be null unless kind is 'verification'")

        # A finding may only be 'resolved' by the owner's latest verification
        # of 'addressed' against an existing snapshot.
        if finding["status"] == "resolved":
            verifications = [h for h in history if h["kind"] == "verification"]
            if not verifications:
                _fail(f"{at}: resolved finding has no verification history")
            last = verifications[-1]
            if last["actor"] != finding["owner"]:
                _fail(
                    f"{at}: non-owner verification cannot resolve a finding"
                )
            if last["verdict"] != "addressed":
                _fail(
                    f"{at}: resolved finding's latest verification is not 'addressed'"
                )

    # --- approvals --------------------------------------------------------- #
    for index, approval in enumerate(data["approvals"]):
        at = f"approvals[{index}]"
        _require_keys(approval, ("reviewer", "snapshot_id", "verdict", "body"), at)
        if approval["reviewer"] not in reviewer_ids:
            _fail(
                f"{at}.reviewer {approval['reviewer']!r} is not a known reviewer"
            )
        if approval["snapshot_id"] not in snapshot_ids:
            _fail(f"{at}.snapshot_id {approval['snapshot_id']!r} is unknown")
        if approval["verdict"] not in APPROVAL_VERDICTS:
            _fail(f"{at}.verdict must be one of {APPROVAL_VERDICTS}")
        _expect_str(approval["body"], f"{at}.body")

    # --- checks (a check id may recur across snapshots, not within one) ---- #
    seen_check_pairs: set = set()
    for index, check in enumerate(data["checks"]):
        at = f"checks[{index}]"
        _require_keys(
            check, ("id", "snapshot_id", "argv", "exit_code", "output", "required"), at
        )
        _expect_nonempty_str(check["id"], f"{at}.id")
        if check["snapshot_id"] not in snapshot_ids:
            _fail(f"{at}.snapshot_id {check['snapshot_id']!r} is unknown")
        _expect_list(check["argv"], f"{at}.argv")
        for a_index, arg in enumerate(check["argv"]):
            _expect_str(arg, f"{at}.argv[{a_index}]")
        if check["exit_code"] is not None:
            _expect_int(check["exit_code"], f"{at}.exit_code")
        _expect_str(check["output"], f"{at}.output")
        _expect_bool(check["required"], f"{at}.required")
        pair = (check["id"], check["snapshot_id"])
        if pair in seen_check_pairs:
            _fail(
                f"{at}: duplicate check id {check['id']!r} for snapshot "
                f"{check['snapshot_id']!r}"
            )
        seen_check_pairs.add(pair)

    # --- changes ----------------------------------------------------------- #
    for index, change in enumerate(data["changes"]):
        at = f"changes[{index}]"
        _require_keys(change, ("path", "diff"), at)
        _expect_str(change["path"], f"{at}.path")
        _expect_str(change["diff"], f"{at}.diff")

    # --- timeline ---------------------------------------------------------- #
    for index, event in enumerate(data["timeline"]):
        at = f"timeline[{index}]"
        _require_keys(event, ("actor", "kind", "body", "snapshot_id"), at)
        if event["actor"] not in participant_ids:
            _fail(f"{at}.actor {event['actor']!r} is not a known participant")
        _expect_str(event["kind"], f"{at}.kind")
        _expect_str(event["body"], f"{at}.body")
        if event["snapshot_id"] is not None and event["snapshot_id"] not in snapshot_ids:
            _fail(f"{at}.snapshot_id {event['snapshot_id']!r} is unknown")

    # --- READY_FOR_HUMAN gate ---------------------------------------------- #
    if data["status"] == "READY_FOR_HUMAN":
        _validate_ready(data, reviewer_ids, snapshot_ids)


def _validate_ready(data, reviewer_ids: set, snapshot_ids: set) -> None:
    participants = data["participants"]
    controllers = [p for p in participants if p["role"] == "controller"]
    developers = [p for p in participants if p["role"] == "developer"]

    if len(controllers) != 1:
        _fail("READY_FOR_HUMAN requires exactly one controller participant")
    if len(developers) != 1:
        _fail("READY_FOR_HUMAN requires exactly one developer participant")
    if not reviewer_ids:
        _fail("READY_FOR_HUMAN requires at least one reviewer participant")

    final_snapshot_id = data["final_snapshot_id"]
    if final_snapshot_id is None:
        _fail("READY_FOR_HUMAN requires a final_snapshot_id")

    # Every configured reviewer's *latest* approval must approve the final
    # snapshot; stale approvals (on older snapshots) do not count.
    for reviewer_id in sorted(reviewer_ids):
        approvals = [a for a in data["approvals"] if a["reviewer"] == reviewer_id]
        if not approvals:
            _fail(f"reviewer {reviewer_id!r} has no approval on record")
        latest = approvals[-1]
        if latest["snapshot_id"] != final_snapshot_id:
            _fail(
                f"reviewer {reviewer_id!r} latest approval is stale "
                f"(on {latest['snapshot_id']!r}, not the final snapshot)"
            )
        if latest["verdict"] != "approved":
            _fail(
                f"reviewer {reviewer_id!r} latest verdict is "
                f"{latest['verdict']!r}, not 'approved'"
            )

    # All findings must be resolved.
    for finding in data["findings"]:
        if finding["status"] != "resolved":
            _fail(
                f"READY_FOR_HUMAN requires all findings resolved; "
                f"{finding['id']!r} is {finding['status']!r}"
            )

    # At least one required check; the latest occurrence of every required
    # check id must have run on the final snapshot and exited 0.
    required_ids = [c["id"] for c in data["checks"] if c["required"]]
    if not required_ids:
        _fail("READY_FOR_HUMAN requires at least one required check")

    for check_id in sorted(set(required_ids)):
        occurrences = [c for c in data["checks"] if c["id"] == check_id]
        latest = occurrences[-1]
        if latest["snapshot_id"] != final_snapshot_id:
            _fail(
                f"required check {check_id!r} latest run is stale "
                f"(on {latest['snapshot_id']!r}, not the final snapshot)"
            )
        if latest["exit_code"] != 0:
            _fail(
                f"required check {check_id!r} did not pass on the final snapshot "
                f"(exit_code={latest['exit_code']!r})"
            )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_CSS = """
:root {
  --bg: #0d1117; --panel: #161b22; --panel-2: #1c2129; --border: #30363d;
  --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --purple: #bc8cff;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
.page { max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 8px; }
h2 { font-size: 16px; margin: 0 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
h3 { font-size: 14px; margin: 16px 0 6px; }
.mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
.muted { color: var(--muted); }
.empty { color: var(--muted); font-style: italic; margin: 4px 0; }
.head { margin-bottom: 20px; }
.meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px 18px; margin: 16px 0; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid transparent; }
.st-planned { background: #21262d; color: var(--muted); border-color: var(--border); }
.st-implementing { background: #12261e; color: var(--green); border-color: #1f6f43; }
.st-reviewing { background: #12283d; color: var(--accent); border-color: #1f6feb; }
.st-fixing { background: #2b2411; color: var(--yellow); border-color: #9e6a03; }
.st-verifying { background: #2a1f3d; color: var(--purple); border-color: #8957e5; }
.st-blocked { background: #2d1517; color: var(--red); border-color: #8b2f2f; }
.st-ready_for_human { background: #12261e; color: var(--green); border-color: #2ea043; }
.role-controller { background: #12283d; color: var(--accent); border-color: #1f6feb; }
.role-developer { background: #12261e; color: var(--green); border-color: #1f6f43; }
.role-reviewer { background: #2a1f3d; color: var(--purple); border-color: #8957e5; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.unverified { color: var(--yellow); font-weight: 600; }
ul.reqs { margin: 0; padding-left: 20px; }
pre { margin: 8px 0 0; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; overflow-x: auto; white-space: pre; }
pre.diff { border-left: 3px solid var(--accent); }
.timeline { list-style: none; margin: 0; padding: 0; }
.tl-item, .history-item { border-left: 2px solid var(--border); padding: 4px 0 12px 14px; margin-left: 4px; }
.tl-head, .history-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 3px; }
.kind { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; }
.verdict { font-size: 11px; font-weight: 700; border-radius: 999px; padding: 0 8px; }
.verdict.addressed { background: #12261e; color: var(--green); }
.verdict.not_addressed { background: #2d1517; color: var(--red); }
.sev { font-size: 11px; font-weight: 700; border-radius: 999px; padding: 0 8px; }
.sev-high { background: #2d1517; color: var(--red); }
.sev-medium { background: #2b2411; color: var(--yellow); }
.sev-low { background: #21262d; color: var(--muted); }
.fstatus { font-size: 11px; border-radius: 999px; padding: 0 8px; border: 1px solid var(--border); }
.fs-resolved { color: var(--green); }
.fs-open { color: var(--red); }
.fs-awaiting_verification { color: var(--yellow); }
.finding { border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 12px 0; background: var(--panel-2); }
.finding-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.field { margin: 6px 0; }
.field .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.ok { color: var(--green); font-weight: 600; }
.bad { color: var(--red); font-weight: 600; }
"""


def _esc(value) -> str:
    """Escape any value for safe insertion into HTML text/attributes."""
    return html.escape(str(value), quote=True)


def _badge(text, css_class: str) -> str:
    return f'<span class="badge {css_class}">{_esc(text)}</span>'


def _section(title: str, body: str) -> str:
    return f'<section class="card"><h2>{_esc(title)}</h2>{body}</section>'


def _model_label(participant: dict) -> str:
    """Observed model label; null is shown as 'unverified', never invented."""
    observed = participant["observed_model"]
    if observed is None:
        return '<span class="unverified">unverified</span>'
    return _esc(observed)


def _render_participants(data) -> str:
    rows = []
    for participant in data["participants"]:
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(participant["id"])}</td>'
            f'<td>{_badge(participant["role"], "role-" + participant["role"])}</td>'
            f'<td class="mono">{_esc(participant["cli"])}</td>'
            f'<td class="mono">{_esc(participant["requested_model"])}</td>'
            f'<td class="mono">{_model_label(participant)}</td>'
            f'<td>{_esc(participant["model_provenance"])}</td>'
            "</tr>"
        )
    if not rows:
        return _section("Participants", '<p class="empty">No participants.</p>')
    body = (
        "<table><thead><tr>"
        "<th>ID</th><th>Role</th><th>CLI</th><th>Requested model</th>"
        "<th>Observed model</th><th>Provenance</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    return _section("Participants", body)


def _render_timeline(data) -> str:
    items = []
    for event in data["timeline"]:
        snapshot = event["snapshot_id"]
        if snapshot is None:
            snapshot_html = '<span class="muted">no snapshot</span>'
        else:
            snapshot_html = f'<span class="mono muted">{_esc(snapshot)}</span>'
        items.append(
            '<li class="tl-item">'
            f'<div class="tl-head"><span class="mono">{_esc(event["actor"])}</span>'
            f'<span class="kind">{_esc(event["kind"])}</span>{snapshot_html}</div>'
            f'<div class="tl-body">{_esc(event["body"])}</div>'
            "</li>"
        )
    if not items:
        return _section("Conversation timeline", '<p class="empty">No timeline entries.</p>')
    return _section("Conversation timeline", '<ol class="timeline">' + "".join(items) + "</ol>")


def _render_findings(data) -> str:
    threads = []
    for finding in data["findings"]:
        history_items = []
        for entry in finding["history"]:
            diff_html = ""
            if entry.get("diff"):
                diff_html = f'<pre class="diff">{_esc(entry["diff"])}</pre>'
            verdict_html = ""
            if entry["verdict"] is not None:
                verdict_html = (
                    f' <span class="verdict {entry["verdict"]}">'
                    f'{_esc(entry["verdict"])}</span>'
                )
            history_items.append(
                '<li class="history-item">'
                f'<div class="history-head"><span class="mono">{_esc(entry["actor"])}</span>'
                f'<span class="kind">{_esc(entry["kind"])}</span>'
                f'<span class="mono muted">{_esc(entry["snapshot_id"])}</span>'
                f"{verdict_html}</div>"
                f'<div class="history-body">{_esc(entry["body"])}</div>'
                f"{diff_html}"
                "</li>"
            )
        history_html = (
            '<ol class="timeline">' + "".join(history_items) + "</ol>"
            if history_items
            else '<p class="empty">No history.</p>'
        )
        threads.append(
            '<article class="finding">'
            '<div class="finding-head">'
            f'<span class="mono">{_esc(finding["id"])}</span>'
            f'<span class="sev sev-{finding["severity"]}">{_esc(finding["severity"])}</span>'
            f'<span class="fstatus fs-{finding["status"]}">{_esc(finding["status"])}</span>'
            f'<span class="mono muted">{_esc(finding["path"])}:{_esc(finding["line"])}</span>'
            f'<span class="muted">owner {_esc(finding["owner"])}</span>'
            f'<span class="mono muted">{_esc(finding["snapshot_id"])}</span>'
            "</div>"
            f'<div class="field"><div class="label">Body</div><div>{_esc(finding["body"])}</div></div>'
            f'<div class="field"><div class="label">Acceptance</div><div>{_esc(finding["acceptance"])}</div></div>'
            f'<div class="field"><div class="label">History</div>{history_html}</div>'
            "</article>"
        )
    if not threads:
        return _section("Review threads", '<p class="empty">No findings.</p>')
    return _section("Review threads", "".join(threads))


def _render_changes(data) -> str:
    blocks = []
    for change in data["changes"]:
        blocks.append(
            f'<h3 class="mono">{_esc(change["path"])}</h3>'
            f'<pre class="diff">{_esc(change["diff"])}</pre>'
        )
    if not blocks:
        return _section("Cumulative changes", '<p class="empty">No changes.</p>')
    return _section("Cumulative changes", "".join(blocks))


def _render_checks(data) -> str:
    rows = []
    for check in data["checks"]:
        if check["exit_code"] is None:
            exit_html = '<span class="muted">not run</span>'
        elif check["exit_code"] == 0:
            exit_html = f'<span class="ok">{_esc(check["exit_code"])}</span>'
        else:
            exit_html = f'<span class="bad">{_esc(check["exit_code"])}</span>'
        required_html = "yes" if check["required"] else '<span class="muted">no</span>'
        argv = " ".join(check["argv"])
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(check["id"])}</td>'
            f'<td class="mono muted">{_esc(check["snapshot_id"])}</td>'
            f'<td>{required_html}</td>'
            f"<td>{exit_html}</td>"
            f'<td class="mono">{_esc(argv)}</td>'
            f'<td><pre>{_esc(check["output"])}</pre></td>'
            "</tr>"
        )
    if not rows:
        return _section("Checks", '<p class="empty">No checks.</p>')
    body = (
        "<table><thead><tr>"
        "<th>ID</th><th>Snapshot</th><th>Required</th><th>Exit</th>"
        "<th>Command</th><th>Output</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    return _section("Checks", body)


def _render_approvals(data) -> str:
    rows = []
    for approval in data["approvals"]:
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(approval["reviewer"])}</td>'
            f'<td class="mono muted">{_esc(approval["snapshot_id"])}</td>'
            f'<td>{_esc(approval["verdict"])}</td>'
            f'<td>{_esc(approval["body"])}</td>'
            "</tr>"
        )
    if not rows:
        return _section("Approvals", '<p class="empty">No approvals.</p>')
    body = (
        "<table><thead><tr>"
        "<th>Reviewer</th><th>Snapshot</th><th>Verdict</th><th>Body</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    return _section("Approvals", body)


def _render_requirements(data) -> str:
    items = "".join(f"<li>{_esc(r)}</li>" for r in data["requirements"])
    body = f'<ul class="reqs">{items}</ul>' if items else '<p class="empty">No requirements.</p>'
    plan = f'<h3>Plan</h3><pre>{_esc(data["plan"])}</pre>'
    return _section("Requirements & plan", body + plan)


def render_html(data) -> str:
    """Validate ``data`` and return a self-contained dark-theme HTML report.

    Every dynamic value is HTML-escaped; the output embeds no scripts, no
    remote assets and performs no fetching. Diffs are rendered in full.
    """
    validate_report(data)

    status = data["status"]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(data['title'])} — {_esc(data['task_id'])}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<main class="page">',
        '<header class="head">',
        f"<h1>{_esc(data['title'])}</h1>",
        '<div class="meta">'
        f'<span class="mono">{_esc(data["task_id"])}</span>'
        f'{_badge(status, "st-" + status.lower())}'
        f'<span class="muted">schema v{_esc(data["schema_version"])}</span>'
        "</div>",
        "</header>",
        _render_requirements(data),
        _render_participants(data),
        _render_timeline(data),
        _render_findings(data),
        _render_changes(data),
        _render_checks(data),
        _render_approvals(data),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Safe atomic writer
# --------------------------------------------------------------------------- #
def _within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or nested inside it (case-aware)."""
    child_n = os.path.normcase(os.path.abspath(child))
    parent_n = os.path.normcase(os.path.abspath(parent))
    return child_n == parent_n or child_n.startswith(parent_n + os.sep)


def _assert_safe_task_dir(task_dir) -> str:
    """Validate the task directory layout and return its resolved real path."""
    raw = os.path.abspath(os.fspath(task_dir))

    if os.path.islink(raw):
        raise PathSecurityError("task directory must not be a symlink")

    for candidate in (raw, os.path.dirname(raw), os.path.dirname(os.path.dirname(raw))):
        if os.path.islink(candidate):
            raise PathSecurityError("task metadata ancestors must not be symlinks")
    real = os.path.realpath(raw)
    parts = pathlib.PurePath(real).parts
    if len(parts) < 3 or parts[-3] != ".agent-pr" or parts[-2] != "tasks":
        raise PathSecurityError(
            "task directory must be <workspace>/.agent-pr/tasks/<task-id>"
        )

    task_id = parts[-1]
    if task_id in ("", ".", "..") or os.sep in task_id or (os.altsep and os.altsep in task_id):
        raise PathSecurityError("invalid task id in path")

    if not os.path.isdir(real):
        raise PathSecurityError("task directory does not exist")

    # Refuse broad/sensitive targets.
    anchor = pathlib.PurePath(real).anchor
    if anchor and os.path.normcase(real) == os.path.normcase(os.path.abspath(anchor)):
        raise PathSecurityError("refusing to write into a filesystem root")
    home = os.path.expanduser("~")
    if home and os.path.normcase(real) == os.path.normcase(os.path.realpath(home)):
        raise PathSecurityError("refusing to write into the home directory")

    return real


def write_report(task_dir) -> str:
    """Read ``report.json`` from ``task_dir`` and atomically write ``report.html``.

    Returns the path of the written file. Raises ``PathSecurityError`` for
    unsafe paths/symlinks and ``ValidationError`` for an invalid report.
    """
    real_dir = _assert_safe_task_dir(task_dir)

    report_path = os.path.join(real_dir, "report.json")
    if os.path.islink(report_path):
        raise PathSecurityError("report.json must not be a symlink")
    if not os.path.isfile(report_path):
        raise ReportError(f"report.json not found in {real_dir!r}")
    if not _within(os.path.realpath(report_path), real_dir):
        raise PathSecurityError("report.json resolves outside the task directory")

    try:
        with open(report_path, "r", encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        raise ReportError(f"failed to read report.json: {exc}") from exc

    validate_report(data)
    if data.get("task_id") != os.path.basename(real_dir):
        raise ValidationError("report task_id must match the task directory name")
    if data.get("status") == "READY_FOR_HUMAN":
        config_path = os.path.join(real_dir, "config.resolved.json")
        if os.path.islink(config_path) or not os.path.isfile(config_path):
            raise ValidationError("READY requires frozen config.resolved.json")
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        import importlib.util
        spec = importlib.util.spec_from_file_location("herdr_config_gate", pathlib.Path(__file__).with_name("check_config.py"))
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        gate.validate_config(config)
        expected_reviewers = {r["id"] for r in config["roles"]["reviewers"]}
        actual_reviewers = {p["id"] for p in data["participants"] if p["role"] == "reviewer"}
        for participant in data["participants"]:
            if participant["role"] == "reviewer":
                matches = [r for r in config["roles"]["reviewers"] if r["id"] == participant["id"]]
                configured = matches[0] if matches else None
            else:
                configured = config["roles"].get(participant["role"])
            if not configured or participant["cli"] != configured["cli"] or participant["requested_model"] != configured["model"]:
                raise ValidationError("participant CLI/model does not match frozen config")
        if actual_reviewers != expected_reviewers:
            raise ValidationError("configured reviewer set does not match report")
        expected_checks = {c["id"]: c["argv"] for c in config["verification"]["required_checks"]}
        for check_id, argv in expected_checks.items():
            runs = [c for c in data["checks"] if c["id"] == check_id]
            if not runs or not runs[-1]["required"] or runs[-1]["argv"] != argv:
                raise ValidationError("configured check missing or command changed: " + check_id)
        actual_checks = {c["id"] for c in data["checks"] if c["required"]}
        if actual_checks != set(expected_checks):
            raise ValidationError("required check set does not match frozen config")
        if config["workflow"]["model_diversity"] == "require_distinct":
            participants = data["participants"]
            developer = next(p for p in participants if p["role"] == "developer")
            if not developer["observed_model"] or not developer["model_provenance"]:
                raise ValidationError("strict model policy requires developer runtime provenance")
            for participant in participants:
                if participant["role"] == "reviewer" and (not participant["observed_model"] or not participant["model_provenance"] or participant["observed_model"] == developer["observed_model"]):
                    raise ValidationError("strict policy requires distinct verified reviewer model")
    html_text = render_html(data)  # validates before any write

    out_path = os.path.join(real_dir, "report.html")
    if os.path.islink(out_path):
        raise PathSecurityError("refusing to write to a symlink output path")
    if not _within(os.path.realpath(out_path), real_dir):
        raise PathSecurityError("output path escapes the task directory")

    # Atomic: write to a randomly named sibling, then os.replace into position.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".report-", suffix=".html.tmp", dir=real_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(html_text)
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)  # only ever our own freshly created temp file
        except OSError:
            pass
        raise

    return out_path


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Render one task-local PR report, without orchestrating agents")
    parser.add_argument("--task-dir", required=True)
    args = parser.parse_args()
    try:
        print(write_report(args.task_dir))
    except (ReportError, OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
