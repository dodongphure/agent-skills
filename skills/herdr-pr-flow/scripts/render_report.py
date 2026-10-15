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

All report content is treated as untrusted and HTML-escaped. In the default
``static`` mode the generated document contains no scripts, no remote assets and
no fetched resources. The opt-in ``offline-feedback`` and ``live`` modes inline a
single bundled UI script (never a remote one); ``live`` additionally posts to the
task-local loopback helper started by ``serve_report.py``. The mode is resolved
from the frozen ``workflow.resolved.json`` policy, never from a bare CLI flag.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import html
import json
import os
import pathlib
import re
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

# `kind` on timeline/history entries is free-form, so it is mapped to a colour
# tone by lookup with a muted default -- never by guessing from the body text.
# Tone names are the palette words, so the CSS selector and this table read alike.
KIND_TONES = (
    ("accent", ("snapshot", "review", "approval_request")),
    ("purple", ("dispatch", "handoff")),
    ("yellow", ("planned", "plan", "implement", "fix", "mock_fix")),
    ("green", ("verification", "mock_verification", "approval", "approved")),
    ("red", ("blocked", "failed", "error")),
)

# --- interactive feedback (report schema v2 / config v2) ------------------- #
REPORT_SCHEMA_VERSIONS = (1, 2)
REPORT_MODES = ("static", "offline-feedback", "live")
FEEDBACK_TOP_KEYS = (
    "revision",
    "current_view_id",
    "views",
    "threads",
    "comments",
    "decisions",
    "receipts",
)
THREAD_STATUSES = ("open", "recorded", "fix_required", "addressed", "outdated")
DECISION_DISPOSITIONS = ("pending", "recorded", "fix_required", "needs_clarification")
TARGET_KINDS = ("line", "thread", "file")
FEEDBACK_ENDPOINT = "/api/feedback"
FEEDBACK_UI_ASSET = "feedback-ui.js"

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


def _reject_extra_keys(obj: dict, keys, where: str) -> None:
    """Fail on any key not in ``keys``.

    A missing-key check alone silently accepts arbitrary extras, so a typo
    (``current_view`` for ``current_view_id``) or a half-migrated field rides
    along unnoticed until something downstream reads it and gets ``None``.
    """
    allowed = set(keys)
    extra = sorted(set(obj) - allowed)
    if extra:
        _fail(
            f"{where}: unexpected key(s) {', '.join(repr(k) for k in extra)}; "
            f"allowed keys are {', '.join(repr(k) for k in keys)}"
        )


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


def validate_report(data, mode: str = "static") -> None:
    """Validate a schema-v1 or schema-v2 report.

    Raises ``ValidationError`` with a human-readable message on the first
    problem found. Returns ``None`` when the report is valid.
    """
    if not _is_dict(data):
        _fail("report must be a JSON object")
    version = data.get("schema_version")
    if not _is_int(version) or version not in REPORT_SCHEMA_VERSIONS:
        _fail(f"schema_version must be one of {REPORT_SCHEMA_VERSIONS}")
    required = list(_TOP_KEYS)
    if version == 2:
        required.append("feedback")
    _require_keys(data, required, "report")

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

        # --- schema-v2 optional provenance -------------------------------- #
        # reporting.md documents both fields as optional-but-typed on a v2
        # finding. An unvalidated optional field is worse than an absent one:
        # the renderer and the READY gate read it as if it were trustworthy.
        if version == 2:
            if "anchor" in finding:
                anchor = finding["anchor"]
                # Explicitly nullable: a v1 finding migrated forward has no
                # anchor, and inventing one would fabricate provenance.
                if anchor is not None:
                    _validate_target(anchor, f"{at}.anchor")
            if "source_comment_ids" in finding:
                sources = finding["source_comment_ids"]
                _expect_list(sources, f"{at}.source_comment_ids")
                seen_sources: set = set()
                for s_index, comment_id in enumerate(sources):
                    sat = f"{at}.source_comment_ids[{s_index}]"
                    _expect_nonempty_str(comment_id, sat)
                    if comment_id in seen_sources:
                        _fail(
                            f"{at}.source_comment_ids: duplicate id {comment_id!r}"
                        )
                    seen_sources.add(comment_id)

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
        # Optional explicit join into events.jsonl (see _timeline_times). An
        # entry without it falls back to the positional contract, so it stays
        # optional; a non-integer is a typo that must not ride along as "no
        # timestamp" with no explanation.
        if "event_seq" in event and event["event_seq"] is not None:
            _expect_int(event["event_seq"], f"{at}.event_seq")

    finding_ids = {f["id"] for f in data["findings"]}
    if version == 2:
        roles = {p["id"]: p["role"] for p in data["participants"]}
        _validate_feedback(data, participant_ids, roles, snapshot_ids, finding_ids)

    # --- READY_FOR_HUMAN gate ---------------------------------------------- #
    if data["status"] == "READY_FOR_HUMAN":
        _validate_ready(data, reviewer_ids, snapshot_ids, version)


def _validate_target(target, where: str) -> None:
    """A tagged feedback target: line {anchor_id} | thread {thread_id} | file {file_id}."""
    if not _is_dict(target):
        _fail(f"{where} must be an object")
    kind = target.get("kind")
    if kind not in TARGET_KINDS:
        _fail(f"{where}.kind must be one of {TARGET_KINDS}")
    field = {"line": "anchor_id", "thread": "thread_id", "file": "file_id"}[kind]
    _require_keys(target, ("kind", field), where)
    _expect_nonempty_str(target[field], f"{where}.{field}")


def _validate_feedback(data, participant_ids: set, roles: dict,
                       snapshot_ids: set, finding_ids: set) -> None:
    """Validate the schema-v2 ``feedback`` projection.

    Human comments are a collection separate from participants, findings and
    approvals: a human is never a reviewer, never an owner, never an approver.
    ``roles`` maps participant id -> role, so a decision can be held to the
    controller role rather than to mere membership.
    """
    feedback = data["feedback"]
    if not _is_dict(feedback):
        _fail(
            "feedback must be an object with keys "
            + ", ".join(FEEDBACK_TOP_KEYS)
            + f"; got {type(feedback).__name__}. An empty collection is still "
            "an object, e.g. {\"revision\": 0, \"current_view_id\": \"\", "
            "\"views\": [], \"threads\": [], \"comments\": [], "
            "\"decisions\": [], \"receipts\": []}"
        )
    _require_keys(feedback, FEEDBACK_TOP_KEYS, "feedback")
    # Exact-set, not just "contains": an extra key here is never inert, it is a
    # field the controller meant to write under a different name.
    _reject_extra_keys(feedback, FEEDBACK_TOP_KEYS, "feedback")
    _expect_int(feedback["revision"], "feedback.revision")
    _expect_str(feedback["current_view_id"], "feedback.current_view_id")
    for key in ("views", "threads", "comments", "decisions", "receipts"):
        _expect_list(feedback[key], f"feedback.{key}")

    _unique_ids(feedback["views"], "feedback.views")
    for index, view in enumerate(feedback["views"]):
        at = f"feedback.views[{index}]"
        _require_keys(view, ("id", "digest"), at)
        _expect_nonempty_str(view["digest"], f"{at}.digest")

    thread_ids = _unique_ids(feedback["threads"], "feedback.threads")
    for index, thread in enumerate(feedback["threads"]):
        at = f"feedback.threads[{index}]"
        _require_keys(thread, ("id", "target", "status", "comment_ids"), at)
        _validate_target(thread["target"], f"{at}.target")
        if thread["status"] not in THREAD_STATUSES:
            _fail(f"{at}.status must be one of {THREAD_STATUSES}")
        _expect_list(thread["comment_ids"], f"{at}.comment_ids")
        for c_index, comment_id in enumerate(thread["comment_ids"]):
            _expect_nonempty_str(comment_id, f"{at}.comment_ids[{c_index}]")

    comment_ids = _unique_ids(feedback["comments"], "feedback.comments")
    for index, comment in enumerate(feedback["comments"]):
        at = f"feedback.comments[{index}]"
        _require_keys(
            comment,
            ("id", "thread_id", "target", "body", "origin_view_id",
             "received_at", "batch_id"),
            at,
        )
        if comment["thread_id"] not in thread_ids:
            _fail(f"{at}.thread_id {comment['thread_id']!r} is not a known thread")
        _validate_target(comment["target"], f"{at}.target")
        _expect_str(comment["body"], f"{at}.body")
        _expect_str(comment["origin_view_id"], f"{at}.origin_view_id")
        _expect_str(comment["received_at"], f"{at}.received_at")
        _expect_str(comment["batch_id"], f"{at}.batch_id")

    for index, decision in enumerate(feedback["decisions"]):
        at = f"feedback.decisions[{index}]"
        _require_keys(
            decision,
            ("comment_id", "controller", "disposition", "rationale", "snapshot_id"),
            at,
        )
        if decision["comment_id"] not in comment_ids:
            _fail(f"{at}.comment_id {decision['comment_id']!r} is not a known comment")
        if decision["disposition"] not in DECISION_DISPOSITIONS:
            _fail(f"{at}.disposition must be one of {DECISION_DISPOSITIONS}")
        if decision["controller"] not in participant_ids:
            _fail(f"{at}.controller {decision['controller']!r} is not a known participant")
        if roles.get(decision["controller"]) != "controller":
            # Membership alone is not enough: only the controller may dispose of
            # a human comment. Otherwise a reviewer or developer id satisfies
            # the check and the disposition looks legitimate in the report.
            _fail(
                f"{at}.controller {decision['controller']!r} is not a participant "
                "with role 'controller'"
            )
        _expect_str(decision["rationale"], f"{at}.rationale")
        snapshot = decision["snapshot_id"]
        if snapshot is not None:
            _expect_str(snapshot, f"{at}.snapshot_id")
            if snapshot not in snapshot_ids:
                _fail(f"{at}.snapshot_id {snapshot!r} is unknown")
        finding_id = decision.get("finding_id")
        if finding_id is not None:
            _expect_str(finding_id, f"{at}.finding_id")
            if finding_id not in finding_ids:
                _fail(f"{at}.finding_id {finding_id!r} is unknown")
        if decision["disposition"] == "fix_required" and not finding_id:
            _fail(f"{at}: a fix_required decision must link a finding_id")

    _unique_ids(feedback["receipts"], "feedback.receipts")
    for index, receipt in enumerate(feedback["receipts"]):
        at = f"feedback.receipts[{index}]"
        _require_keys(receipt, ("id", "batch_id", "received_at", "comment_ids"), at)
        _expect_str(receipt["batch_id"], f"{at}.batch_id")
        _expect_str(receipt["received_at"], f"{at}.received_at")
        _expect_list(receipt["comment_ids"], f"{at}.comment_ids")
        for c_index, comment_id in enumerate(receipt["comment_ids"]):
            _expect_nonempty_str(comment_id, f"{at}.comment_ids[{c_index}]")


def _validate_ready(data, reviewer_ids: set, snapshot_ids: set, version: int = 1) -> None:
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

    if version == 2:
        _validate_ready_feedback(data)


def _validate_ready_feedback(data) -> None:
    """v2: every accepted human comment needs a terminal, non-blocking decision.

    Human comments are never approvals and never authorize a commit; they only
    block READY until the controller records a disposition for each one.
    """
    feedback = data["feedback"]
    latest: dict = {}
    for decision in feedback["decisions"]:
        latest[decision["comment_id"]] = decision
    findings = {f["id"]: f for f in data["findings"]}
    for comment in feedback["comments"]:
        decision = latest.get(comment["id"])
        if decision is None or decision["disposition"] == "pending":
            _fail(
                "READY requires a terminal decision for human comment "
                f"{comment['id']!r}"
            )
        if decision["disposition"] == "needs_clarification":
            _fail(
                "READY is blocked by an unresolved clarification for human comment "
                f"{comment['id']!r}"
            )
        if decision["disposition"] == "fix_required":
            finding = findings.get(decision.get("finding_id"))
            if finding is None or finding["status"] != "resolved":
                _fail(
                    f"READY requires the fix for human comment {comment['id']!r} "
                    "to be owner-verified as resolved"
                )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_CSS = """
:root {
  --bg: #0d1117; --panel: #161b22; --panel-2: #1c2129; --border: #30363d;
  --tl-sep: #6e7681;
  --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --purple: #bc8cff;
  /* Scrollbar colours, kept a step off the panel so the thumb reads as a
     control on this dark surface rather than a light-grey system default. */
  --scroll-track: #0d1117; --scroll-thumb: #30363d; --scroll-thumb-hover: #484f58;
  /* Height of a collapsed file row. Used as the sticky offset so a jumped-to
     finding/file lands BELOW the pinned row instead of under it. */
  --filerow-h: 40px;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  /* Tells Chromium to render native widgets (scrollbars above all) for a dark
     surface. Set on :root as well so the property applies before <body> paints. */
  color-scheme: dark;
}
html { color-scheme: dark; background: var(--bg); }
/* Scrollbars. Two mechanisms are needed: the standard `scrollbar-color`/`-width`
   (Chromium 121+, Firefox) and the legacy `::-webkit-scrollbar` pseudo-elements.
   Set both so the thumb matches the theme instead of the light system default.
   Scoped to `*` because the page itself, the diff panes and the `pre` blocks all
   scroll independently and must look identical. */
* {
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) var(--scroll-track);
}
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--scroll-track); }
::-webkit-scrollbar-thumb {
  background: var(--scroll-thumb); border-radius: 6px;
  /* The transparent border + background-clip insets the thumb inside the track,
     which is what gives it the rounded, floating look rather than a full-width
     grey bar. */
  border: 2px solid var(--scroll-track); background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background: var(--scroll-thumb-hover); background-clip: padding-box; }
::-webkit-scrollbar-corner { background: var(--scroll-track); }
.page { max-width: 1200px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 8px; }
h2 { font-size: 16px; margin: 0 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
h3 { font-size: 14px; margin: 16px 0 6px; }
.mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
.muted { color: var(--muted); }
.empty { color: var(--muted); font-style: italic; margin: 4px 0; }
.head { margin-bottom: 20px; }
.meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px 18px; margin: 16px 0; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 14px; font-weight: 600; border: 1px solid transparent; text-transform: capitalize; }
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
/* Fixed layout so a long Command/Output cell cannot stretch the table (and the
   page) wider than the card; the Output column gets the space and wraps. */
table.checks { table-layout: fixed; }
table.checks th, table.checks td { word-break: break-word; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-size: 14px; text-transform: uppercase; letter-spacing: .04em; }
.unverified { color: var(--yellow); font-weight: 600; }
ul.reqs { margin: 0; padding-left: 20px; }
pre { margin: 8px 0 0; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; overflow-x: auto; white-space: pre; }
/* Prose-like blocks wrap so the card grows instead of scrolling: the plan, and
   check output inside a table cell (an unwrapped <pre> there stretches the whole
   table -- and the page -- wider than the card). Diffs keep `white-space: pre`
   so their line alignment survives. */
pre.plan, pre.out { white-space: pre-wrap; word-break: break-word; overflow-x: visible; }
pre.diff { border-left: 3px solid var(--accent); }
.timeline { list-style: none; margin: 0; padding: 0; }
/* Rail with one tone-coloured ring per entry, so the log reads as a sequence.
   Only .tl-item is touched -- the nested finding history uses .history-item and
   keeps its own border-left. */
.tl-item { position: relative; padding: 1px 0 14px 26px; }
.tl-item::before { content: ""; position: absolute; left: 4px; top: 6px; width: 10px; height: 10px; border-radius: 50%; background: var(--panel); border: 2px solid var(--muted); box-sizing: border-box; }
.tl-item::after { content: ""; position: absolute; left: 8px; top: 16px; bottom: -6px; width: 2px; background: var(--border); }
.tl-item:last-child::after { content: none; }
.tl-item:last-child { padding-bottom: 2px; }
/* A little more air between entries, and a separator tick ACROSS THE RAIL.
   Geometry: the ring (::before) is top:6px height:10px, so its centre is 11px;
   the rail (::after) starts at 16px -- below that centre -- so nothing painted
   inside the rail at face value can ever cross the ring. Lifting the rail's top
   to 11px puts a 1px tick on the ring's centre line. Never repaint ::before's
   background here: that is the ring's fill and hollow rings result. */
.timeline .tl-item { padding-bottom: 22px; }
.timeline .tl-item + .tl-item { padding-top: 22px; }
.timeline .tl-item + .tl-item::after {
  top: 11px;
  background: linear-gradient(to bottom, var(--tl-sep) 0 1px, var(--border) 1px 100%);
}
.tl-when { margin-left: auto; font-size: 14px; color: var(--muted);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.tl-head { align-items: center; }
/* A jumped-to review thread needs breathing room under the sticky file row.
   `--filerow-h` is the row's own min-height; anything less and the landing is
   hidden behind the pinned header. */
.finding { scroll-margin-top: calc(var(--filerow-h) + 8px); }
/* The file rows themselves are jump targets too (the nav rail links to
   `#file-<n>`) and must clear the pinned row above them. */
details.fileitem[open] > summary.filerow { scroll-margin-top: calc(var(--filerow-h) + 8px); }
.tl-accent::before { border-color: var(--accent); }
.tl-purple::before { border-color: var(--purple); }
.tl-yellow::before { border-color: var(--yellow); }
.tl-green::before { border-color: var(--green); }
.tl-red::before { border-color: var(--red); }
.history-item { border-left: 2px solid var(--border); padding: 4px 0 12px 14px; margin-left: 4px; }
.tl-head, .history-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 3px; }
.kind { font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; }
.kind.k-accent { color: var(--accent); border-color: #1f6feb; background: rgba(88,166,255,.12); }
.kind.k-purple { color: var(--purple); border-color: #8957e5; background: rgba(188,140,255,.12); }
.kind.k-yellow { color: var(--yellow); border-color: #9e6a03; background: rgba(210,153,34,.12); }
.kind.k-green { color: var(--green); border-color: #1f6f43; background: rgba(63,185,80,.12); }
.kind.k-red { color: var(--red); border-color: #f85149; background: rgba(248,81,73,.12); }
.verdict { font-size: 14px; font-weight: 700; border-radius: 999px; padding: 0 8px; }
.verdict.addressed { background: #12261e; color: var(--green); }
.verdict.not_addressed { background: #2d1517; color: var(--red); }
.sev { font-size: 14px; font-weight: 700; border-radius: 999px; padding: 0 8px; }
.sev-high { background: #2d1517; color: var(--red); }
.sev-medium { background: #2b2411; color: var(--yellow); }
.sev-low { background: #21262d; color: var(--muted); }
.fstatus { font-size: 14px; border-radius: 999px; padding: 0 8px; border: 1px solid var(--border); }
.fs-resolved { color: var(--green); }
.fs-open { color: var(--red); }
.fs-awaiting_verification { color: var(--yellow); }
.fs-recorded { color: var(--accent); }
/* Imported human feedback. These rules live in the BASE stylesheet, not in the
   interactive-only _FEEDBACK_CSS: an imported comment is server-rendered, so it
   must be styled in static mode too. Only the comment *controls* (bar, editor,
   draft cards) belong to the interactive stylesheet. */
.role-human { background: #2b2411; color: var(--yellow); border-color: #9e6a03; }
.fb-thread { border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 12px 0; background: var(--panel-2); }
.fb-thread-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.fb-comment { border-left: 2px solid var(--border); padding: 4px 0 12px 14px; margin-left: 4px; }
.fb-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 3px; }
.fb-body, .fb-rationale { font-size: 16px; }
/* The controller's reply is badged like every other speaker in the report. Plain
   `Controller: …` prose carried the same weight as the comment it answered and
   left the reader to parse a prefix to see who was talking. */
.fb-rationale { display: flex; gap: 8px; align-items: baseline; color: var(--muted); margin-top: 6px; }
.fb-rationale .badge { flex: 0 0 auto; }
.fb-reply { min-width: 0; overflow-wrap: anywhere; }
.fdisp { font-size: 14px; font-weight: 700; border-radius: 999px; padding: 0 8px; border: 1px solid var(--border); }
.fd-recorded { color: var(--green); }
.fd-fix_required { color: var(--yellow); }
.fd-needs_clarification { color: var(--red); }
.fd-pending { color: var(--muted); }
.finding { border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 12px 0; background: var(--panel-2); }
.finding-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.field { margin: 6px 0; }
.field .label { color: var(--muted); font-size: 14px; text-transform: uppercase; letter-spacing: .04em; }
.field .value, .history-body { font-size: 16px; }
.ok { color: var(--green); font-weight: 600; }
.bad { color: var(--red); font-weight: 600; }
/* -------------------------------------------------------------------------
   Page tabs: Conversation | Files changed, laid out like a GitHub PR page.

   Styling is a direct port of GitHub's Primer UnderlineNav (values taken from
   primer/react's UnderlineTabbedInterface.module.css and
   CounterLabel.module.css, colours from primer/primitives base orange-3):
   the active tab is marked by a 2px #fd8c73 underline, not by a raised card.
   Radio + label only, no script element -- that is what keeps the static
   contract intact.

   Two structural rules the markup must respect or the tabs silently break:
   * the two <input>s are BARE siblings sharing one parent with both the label
     strip and the panels. Wrapping them in a div makes the panels cousins and
     neither the highlight nor the visibility rule can match.
   * labels are addressed by [for="..."] rather than by adjacency, so they do
     not have to sit next to their input.
   ------------------------------------------------------------------------- */
:root {
  /* base orange-3, the "current tab" marker */
  --tab-marker: #fd8c73;
  --hover-wash: #212830;
  --counter-border: #2f3742;
}
.prtabinput { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; margin: 0; }
.prtabbar { display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
  padding: 8px 0 0; min-height: 32px; box-shadow: inset 0 -1px var(--border);
  margin-bottom: 16px; }
.prtab { position: relative; display: inline-flex; align-items: center;
  height: 32px; padding: 6px 8px; margin-bottom: 8px;
  font-size: 16px; font-weight: 400; line-height: 1.4285; color: var(--muted);
  background-color: transparent; border: 0; border-radius: 8px; cursor: pointer;
  user-select: none; text-decoration: none; }
.prtab:hover { background-color: var(--hover-wash); transition: background-color .12s ease-out; }
.prtab .ic { display: inline-flex; align-items: center; margin-inline-end: 8px; color: var(--muted); }
.prtab .ic svg { display: block; }
/* Reserves the box space the label takes when it goes semibold, so switching
   tabs never nudges the neighbours sideways. */
.prtab .tlabel::before { display: block; height: 0; font-weight: 600;
  white-space: nowrap; visibility: hidden; content: attr(data-content); }
.prtab .cnt { display: inline-flex; align-items: center; margin-inline-start: 8px;
  padding: 2px 6px; font-size: 14px; font-weight: 600; line-height: 1;
  color: var(--text); background-color: var(--hover-wash);
  border: 1px solid var(--counter-border); border-radius: 20px; }
.prtab .stat { margin-inline-start: 8px; font-size: 14px; }
.prtab .stat .ok { color: var(--green); font-weight: 600; }
.prtab .stat .bad { color: var(--red); font-weight: 600; }
/* Always present at 2px so the layout never shifts; transparent until current.
   Pulled 8px down to land on the wrapper's inset hairline. */
.prtab::after { position: absolute; inset: auto 0 0; content: '';
  margin-bottom: -8px; height: 2px; pointer-events: none; background-color: transparent; }
#ptab-conv:checked ~ .prtabbar .prtab[for="ptab-conv"],
#ptab-files:checked ~ .prtabbar .prtab[for="ptab-files"] { color: var(--text); font-weight: 600; }
#ptab-conv:checked ~ .prtabbar .prtab[for="ptab-conv"]::after,
#ptab-files:checked ~ .prtabbar .prtab[for="ptab-files"]::after { background-color: var(--tab-marker); }
#ptab-conv:checked ~ .prtabbar .prtab[for="ptab-conv"]:hover,
#ptab-files:checked ~ .prtabbar .prtab[for="ptab-files"]:hover { background-color: transparent; }
#ptab-conv:checked ~ .prtabbar .prtab[for="ptab-conv"] .ic,
#ptab-files:checked ~ .prtabbar .prtab[for="ptab-files"] .ic { color: var(--text); }
.prtabinput:focus-visible ~ .prtabbar .prtab { outline: 2px solid transparent;
  box-shadow: inset 0 0 0 2px var(--accent); }
.prpanel { display: none; }
#ptab-conv:checked ~ .prpanel[data-panel="conv"] { display: block; }
#ptab-files:checked ~ .prpanel[data-panel="files"] { display: block; }
/* Static-mode deep-link fallback. `#file-…` / `#thread-…` ids only exist in the
   Files panel, whose panel is hidden while the Conversation radio is checked, so
   a plain fragment navigation would land nowhere visible. `:has()` lets the
   Files panel reveal itself when the page's target lives inside it, with no
   script. Interactive modes do the same thing in JS (see feedback-ui.js) because
   they must also scroll and open the right <details> row. */
.prpanel[data-panel="files"]:has(:target) { display: block; }
/* Keep the tab strip honest: the marker follows the panel that is actually
   shown, not the radio, so a deep link never highlights the wrong tab. */
.prtabbar:has(~ .prpanel[data-panel="files"] :target) .prtab[for="ptab-files"] {
  color: var(--text); font-weight: 600; }
.prtabbar:has(~ .prpanel[data-panel="files"] :target) .prtab[for="ptab-files"]::after {
  background-color: var(--tab-marker); }
/* Files-changed view: a flat list of changed files, one collapsible row each.
   Native <details> rather than radio tabs, so rows open independently and two
   diffs can be read side by side. */
/* `overflow: hidden` would make this a clipping container, which kills
   `position: sticky` in every descendant row - so the rounded corners are done
   without it (each row keeps its own background). */
.filelist { border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); }
.fileitem + .fileitem { border-top: 1px solid var(--border); }
/* Round the outer corners per-row, since the list can no longer clip. */
.fileitem:first-child > summary.filerow { border-radius: 7px 7px 0 0; }
.fileitem:last-child:not([open]) > summary.filerow { border-radius: 0 0 7px 7px; }
.fileitem:last-child[open] > .filebody-wrap { border-radius: 0 0 7px 7px; }
.fileitem:only-child > summary.filerow { border-radius: 7px 7px 0 0; }
.fileitem > summary.filerow { display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; min-height: var(--filerow-h); cursor: pointer; user-select: none;
  list-style: none; background: var(--panel); }
.fileitem > summary.filerow::-webkit-details-marker { display: none; }
.fileitem > summary.filerow:hover { background: var(--panel-2); }
.fileitem[open] > summary.filerow { background: var(--panel-2); box-shadow: inset 2px 0 0 var(--accent); }
/* The "current file" header: a row sticks to the top of the viewport while its
   own diff is on screen, then hands over to the next row as that one arrives.
   No JavaScript: a <summary> is confined to its own <details>, so it un-sticks
   automatically once the file's body has scrolled past - exactly the
   "which file am I reading" affordance. `top` clears the app's own sticky head
   offset. */
.fileitem[open] > summary.filerow {
  position: sticky; top: 0; z-index: 3; }
/* Needed or the sticky row is painted under the following file bodies. */
.fileitem[open] { position: relative; z-index: 2; }
.fileitem[open] > .filebody-wrap { position: relative; z-index: 1; }
.filerow .caret { flex: 0 0 auto; width: 16px; height: 16px; color: var(--muted);
  transition: transform .12s ease-out; }
.fileitem[open] .filerow .caret { transform: rotate(90deg); }
.filerow .fpath { flex: 1 1 auto; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filerow .fdir { color: var(--muted); }
.filerow .fstat { flex: 0 0 auto; font-size: 14px; font-variant-numeric: tabular-nums; }
.filerow .fstat .ok { color: var(--green); font-weight: 600; }
.filerow .fstat .bad { color: var(--red); font-weight: 600; }
.filerow .cc { flex: 0 0 auto; font-size: 14px; border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; color: var(--muted); }
.fileitem[open] .filerow .cc { border-color: var(--accent); color: var(--accent); }
/* A comment-panel jump targets a `.finding` thread inside a file row. Chromium's
   native fragment navigation opens the ancestor <details> for free, but it
   scrolls on the pre-expansion layout, so the landing can miss. The row that
   owns the target gets the open-row affordances (so it reads as expanded and is
   obvious which file the comment belongs to) and the thread flashes. */
details.fileitem:has(.finding:target) > summary.filerow {
  background: var(--panel-2); box-shadow: inset 2px 0 0 var(--accent); }
details.fileitem:has(.finding:target) > summary.filerow .caret { transform: rotate(90deg); }
details.fileitem:has(.finding:target) > summary.filerow .cc {
  border-color: var(--accent); color: var(--accent); }
.finding:target { animation: cmt-flash 1.6s ease-out 1; }
@keyframes cmt-flash {
  from { outline: 2px solid var(--accent); outline-offset: 2px;
         background: rgba(88, 166, 255, .12); }
  to   { outline: 2px solid transparent; outline-offset: 2px;
         background: transparent; }
}
.filerow:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.filebody-wrap { border-top: 1px solid var(--border); }
/* Sticky nav rail beside the changed-file list: files on top, comments below.
   Both lists are independent scrollers, and the rail collapses above them at
   narrow widths so the diffs keep the full column. */
/* The rail needs 340px + a 20px gap, so it only pays for itself once the page's
   content box can still give the diffs a readable ~800px. .page is max 1200px
   with 20px side padding, so collapse below 1160px + a little slack. */
.files-layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px;
  gap: 20px; align-items: start; }
.rail { position: sticky; top: 16px; align-self: start;
  display: flex; flex-direction: column; gap: 14px;
  max-height: calc(100vh - 32px); }
.panel { border: 1px solid var(--border); border-radius: 8px; background: var(--panel);
  overflow: hidden; display: flex; flex-direction: column; min-height: 0; }
.panel > h3 { margin: 0; padding: 10px 12px; font-size: 14px; font-weight: 600;
  color: var(--muted); text-transform: uppercase; letter-spacing: .04em;
  border-bottom: 1px solid var(--border); display: flex; align-items: center;
  justify-content: space-between; flex: 0 0 auto; }
.panel .pcount { background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 20px; padding: 1px 8px; font-size: 14px; color: var(--text); }
.panel ol { list-style: none; margin: 0; padding: 4px 0; overflow-y: auto; min-height: 0; }
.panel li { border-top: 1px solid var(--border); }
.panel li:first-child { border-top: 0; }
.panel a { display: block; padding: 8px 12px; text-decoration: none; color: var(--text);
  border-left: 2px solid transparent; }
.panel a:hover { background: var(--panel-2); }
.panel a:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
/* 14px is this report's font floor: the rail is body text, not fine print, and
   the hierarchy is carried by colour and weight instead of size. */
.fpanel a { display: flex; align-items: baseline; gap: 8px; }
.fpanel .fname { flex: 1 1 auto; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo,
  Consolas, monospace; font-size: 14px; line-height: 1.35;
  overflow-wrap: anywhere; word-break: break-word; }
.fpanel .fdir { color: var(--muted); }
.fpanel .fdelta { flex: 0 0 auto; font-size: 14px; font-variant-numeric: tabular-nums;
  white-space: nowrap; }
.fpanel .fdelta .ok { color: var(--green); font-weight: 600; }
.fpanel .fdelta .bad { color: var(--red); font-weight: 600; }
.fpanel .fcmt { flex: 0 0 auto; font-size: 14px; border: 1px solid var(--border);
  border-radius: 999px; padding: 0 7px; color: var(--muted); }
.fpanel a.has-cmt .fcmt { border-color: var(--accent); color: var(--accent); }
.cpanel .jtop { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
/* The owner badge names the reviewer; never let it squeeze or wrap. */
.cpanel .jtop .badge { flex: 0 0 auto; white-space: nowrap; }
.cpanel .jid { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 14px; }
.cpanel .jpath { font-size: 14px; color: var(--muted); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.cpanel .jsev { font-size: 14px; font-weight: 700; border-radius: 999px; padding: 0 7px;
  flex: 0 0 auto; }
.cpanel .jsev.sev-high { background: #2d1517; color: var(--red); }
.cpanel .jsev.sev-medium { background: #2b2411; color: var(--yellow); }
.cpanel .jsev.sev-low { background: var(--panel-2); color: var(--muted); }
.panel-empty { padding: 12px; color: var(--muted); font-style: italic; font-size: 14px; }
.rail .fpanel ol { max-height: 44vh; }
.rail .cpanel ol { max-height: 32vh; }
/* Breakpoint LAST: it must override the .rail/.files-layout rules above, which
   share its specificity. The rail needs 340px + a 20px gap, so it only pays for
   itself while the page's content box can still give the diffs a readable
   ~800px. .page is max 1200px with 20px side padding, so collapse just under
   the natural desktop width. */
@media (max-width: 1180px) {
  .files-layout { grid-template-columns: 1fr; }
  .rail { position: static; max-height: none; }
  .rail .fpanel ol, .rail .cpanel ol { max-height: 32vh; }
}
.comments { padding: 6px 14px 12px; border-top: 1px dashed var(--border); }
.ftsummary { color: var(--muted); font-size: 14px; margin: 0 0 12px; }
.ftsummary .ok, .ftsummary .bad { font-weight: 700; }
.diffview { margin: 0; border: none; border-radius: 0; padding: 0; background: var(--panel); overflow-x: auto; }
.diffview .dl { display: flex; min-height: 1.35em; }
.diffview .ln { flex: 0 0 auto; width: 3.6em; padding: 1px 10px 1px 0; text-align: right; color: var(--muted); user-select: none; font-variant-numeric: tabular-nums; }
.diffview .code { flex: 1 1 auto; padding: 1px 8px; white-space: pre-wrap; word-break: break-word; }
.diffview .dl-add { background: rgba(63,185,80,.15); }
.diffview .dl-del { background: rgba(248,81,73,.15); }
.diffview .dl-add .code { color: #7ee787; }
.diffview .dl-del .code { color: #ffa198; }
.diffview .dl-hunk .code { color: var(--purple); }
.diffview .dl-meta .code { color: var(--muted); font-weight: 600; }
"""


def _esc(value) -> str:
    """Escape any value for safe insertion into HTML text/attributes."""
    return html.escape(str(value), quote=True)


def _badge(text, css_class: str) -> str:
    return f'<span class="badge {css_class}">{_esc(text)}</span>'


def _section(title: str, body: str) -> str:
    return f'<section class="card"><h2>{_esc(title)}</h2>{body}</section>'


# --------------------------------------------------------------------------- #
# Stable identity: typed, snapshot-bound anchors + immutable view manifest
# --------------------------------------------------------------------------- #
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dom_id(value: str) -> str:
    """A DOM-safe id derived from a stable string, never from array position."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", value) or "x"


def _diff_has_hunks(diff: str) -> bool:
    return any(_HUNK_HEADER.match(line) for line in diff.split("\n"))


def _diff_paths(diff: str, path: str):
    """Return ``(old_path, new_path)`` for a unified diff.

    The ``---``/``+++`` headers carry the real baseline and candidate paths, so
    a rename (where the two differ) is representable in the anchor tuple. The
    change entry's single ``path`` is the fallback for a fragment with no
    headers -- e.g. a hand-written diff or a ``/dev/null`` side.
    """
    old_path = new_path = None
    for line in diff.split("\n"):
        if old_path is None and line.startswith("--- "):
            candidate_path = line[4:].strip().split("\t")[0]
            if candidate_path != "/dev/null":
                old_path = candidate_path[2:] if candidate_path.startswith(("a/", "b/")) else candidate_path
        elif new_path is None and line.startswith("+++ "):
            candidate_path = line[4:].strip().split("\t")[0]
            if candidate_path != "/dev/null":
                new_path = candidate_path[2:] if candidate_path.startswith(("a/", "b/")) else candidate_path
    return old_path or path, new_path or path


def _line_anchor_id(task_id, baseline, baseline_hash, candidate, candidate_hash,
                    old_path, new_path, diff_hash, side, line) -> str:
    """Hash the full identity tuple, not just a line number.

    A bare line number is only meaningful relative to a snapshot. Binding the
    anchor to the baseline/candidate snapshots, both sides' paths and the file's
    diff hash means a comment can never silently reattach to the same number in
    a different snapshot.
    """
    canonical = "\n".join([
        str(task_id), str(baseline), str(baseline_hash), str(candidate),
        str(candidate_hash), str(old_path), str(new_path), str(diff_hash),
        str(side), str(line),
    ])
    return "line-" + _sha256(canonical)


def _resolve_snapshot_pair(data):
    """Return ``(baseline, candidate)`` snapshot dicts, order-independently.

    Anchor identity and the view id hash the baseline/candidate ids and their
    manifest hashes, so *which* snapshot is picked as baseline must never depend
    on the order of ``snapshots[]``. Resolving positionally made a re-serialised
    (byte-identical but reordered) report produce different anchors for the same
    comment, which imports as stale rather than as a flagged mismatch.

    The contract is semantic, not positional:

    * the **baseline** is the snapshot named ``baseline`` (as captured by
      ``snapshot.py --name baseline``);
    * the **candidate** is the snapshot named by ``final_snapshot_id`` when that
      is set, otherwise the last snapshot that is not the baseline.

    When neither name is present -- a record that never labelled its snapshots --
    the pair is derived from a **stable total order on the content hashes**
    (sorted by ``manifest_sha256``), so permuting ``snapshots[]`` still yields
    the same pair. Falling back to ``snapshots[0]`` / ``snapshots[-1]`` here would
    reintroduce exactly the order dependence this function exists to remove.

    A single-snapshot record is its own baseline and candidate.
    """
    snapshots = data["snapshots"]
    if not snapshots:
        return None, None, "", ""

    baseline_snapshot = next(
        (item for item in snapshots if item["id"] == "baseline"), None
    )

    final_snapshot_id = data.get("final_snapshot_id")
    candidate_snapshot = None
    if final_snapshot_id is not None:
        candidate_snapshot = next(
            (item for item in snapshots if item["id"] == final_snapshot_id), None
        )

    if baseline_snapshot is None:
        # No named baseline: order by content hash, which is invariant under a
        # re-serialisation of snapshots[].
        ordered = sorted(snapshots, key=lambda item: (item["manifest_sha256"], item["id"]))
        if candidate_snapshot is not None:
            # Keep the explicit candidate; the baseline is the other end.
            rest = [item for item in ordered if item["id"] != candidate_snapshot["id"]]
            baseline_snapshot = rest[0] if rest else candidate_snapshot
        else:
            baseline_snapshot = ordered[0]
            candidate_snapshot = ordered[-1]

    if candidate_snapshot is None:
        # Last snapshot that is not the baseline; falls back to the baseline
        # itself for a single-snapshot record. Ordered by content hash so the
        # choice survives a permuted array.
        rest = sorted(
            (item for item in snapshots if item["id"] != baseline_snapshot["id"]),
            key=lambda item: (item["manifest_sha256"], item["id"]),
        )
        candidate_snapshot = rest[-1] if rest else baseline_snapshot

    return (
        baseline_snapshot["id"],
        candidate_snapshot["id"],
        baseline_snapshot["manifest_sha256"],
        candidate_snapshot["manifest_sha256"],
    )


def _build_view(data, interactive: bool) -> dict:
    """Derive the typed anchor index and the immutable view manifest.

    Hunkless fragments get no line anchors: their numbers are synthetic, not
    source locations. Duplicate line targets are flagged ambiguous and are not
    offered for inline commenting rather than risk a misattachment.
    """
    task_id = data["task_id"]
    baseline, candidate, baseline_hash, candidate_hash = _resolve_snapshot_pair(data)
    baseline = baseline if baseline is not None else "none"
    candidate = candidate if candidate is not None else "none"

    files: dict = {}
    lines: dict = {}
    counts: dict = {}
    file_records = []
    for change in data["changes"]:
        path = change["path"]
        diff_hash = _sha256(change["diff"])
        old_path, new_path = _diff_paths(change["diff"], path)
        file_id = "file-" + _sha256(path)
        files[path] = file_id
        file_records.append(
            {"path": path, "file_id": file_id, "diff_sha256": diff_hash}
        )
        if not _diff_has_hunks(change["diff"]):
            continue  # synthetic numbers are not source anchors
        for kind, _text, old_no, new_no in _parse_diff(change["diff"]):
            if kind == "add" and new_no is not None:
                side, line = "new", new_no
            elif kind == "del" and old_no is not None:
                side, line = "old", old_no
            elif kind == "ctx" and new_no is not None:
                side, line = "new", new_no
            else:
                continue
            anchor_id = _line_anchor_id(
                task_id, baseline, baseline_hash, candidate, candidate_hash,
                old_path, new_path, diff_hash, side, line,
            )
            lines[(path, side, line)] = anchor_id
            counts[anchor_id] = counts.get(anchor_id, 0) + 1

    ambiguous = {anchor for anchor, n in counts.items() if n > 1}

    threads = [{"thread_id": "finding/" + finding["id"]} for finding in data["findings"]]
    feedback = data.get("feedback")
    if isinstance(feedback, dict):
        for thread in feedback.get("threads", []):
            threads.append({"thread_id": thread["id"]})

    manifest = {
        "schema_version": 1,
        "task_id": task_id,
        "baseline": {"snapshot_id": baseline, "manifest_sha256": baseline_hash},
        "candidate": {"snapshot_id": candidate, "manifest_sha256": candidate_hash},
        "files": file_records,
        "line_anchors": sorted(
            (
                {"anchor_id": anchor_id, "path": path, "side": side, "line": line}
                for (path, side, line), anchor_id in lines.items()
                if anchor_id not in ambiguous
            ),
            key=lambda item: (item["path"], item["side"], item["line"]),
        ),
        "threads": threads,
    }
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    manifest["view_id"] = "rv-" + _sha256(payload)[:16]

    return {
        "interactive": interactive,
        "files": files,
        "lines": lines,
        "ambiguous": ambiguous,
        "manifest": manifest,
    }


def _file_paths_by_id(view) -> dict:
    """``file_id -> path`` for the rendered view (empty without a view)."""
    if view is None:
        return {}
    return {file_id: path for path, file_id in view["files"].items()}


def _target_path(target, data=None, view=None):
    """The changed-file path a feedback target anchors to, or ``None``.

    Resolving a target back to its file is what lets an imported comment be
    shown beside the diff it is about instead of only in the Conversation log.
    A ``thread`` target that names a review finding resolves through it.
    """
    kind = target.get("kind")
    if kind == "file":
        return _file_paths_by_id(view).get(target.get("file_id"))
    if kind == "line":
        if view is None:
            return None
        for (path, _side, _line), anchor_id in view["lines"].items():
            if anchor_id == target.get("anchor_id"):
                return path
        return None
    if kind == "thread":
        if data is None:
            return None
        thread_id = str(target.get("thread_id") or "")
        prefix = "finding/"
        if not thread_id.startswith(prefix):
            return None
        wanted = thread_id[len(prefix):]
        for finding in data["findings"]:
            if finding["id"] == wanted:
                return finding["path"]
        return None
    return None


def _target_label(target, data=None, view=None) -> str:
    """Name a feedback target by where it is, not by the id that encodes it.

    A resolvable target reads as ``.gitattributes`` or ``src/widget.py:12``.
    The raw ``file-<64 hex>`` / anchor-id form is kept only as a fallback: an
    anchor that no longer resolves must stay *visibly* unidentified rather than
    be silently dressed up as something it is not.
    """
    kind = target["kind"]
    if kind == "line":
        anchor_id = str(target["anchor_id"])
        if view is not None:
            for anchor in view["manifest"]["line_anchors"]:
                if anchor["anchor_id"] == anchor_id:
                    return f'{anchor["path"]}:{anchor["line"]}'
        return "line " + anchor_id
    if kind == "thread":
        thread_id = str(target["thread_id"])
        path = _target_path(target, data, view)
        if path is not None:
            for finding in (data["findings"] if data else []):
                if "finding/" + finding["id"] == thread_id:
                    return f'{path}:{finding["line"]}'
            return path
        return "thread " + thread_id
    path = _target_path(target, data, view)
    if path is not None:
        return path
    return "file " + str(target["file_id"])


# Extra styles are emitted only in interactive modes so the static document is
# byte-for-byte unchanged. 14px floor / 16px comment text, matching _CSS.
_FEEDBACK_CSS = """
.fbbar { position: sticky; top: 0; z-index: 5; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin: 16px 0; }
.fbbar .fbcount { font-size: 14px; color: var(--muted); }
.fbbar button, .fb-btn { font: inherit; font-size: 14px; color: var(--text); background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; padding: 5px 12px; cursor: pointer; }
.fbbar button:hover { border-color: var(--accent); }
.fb-btn:hover { border-color: var(--accent); color: var(--accent); }
.fbbar button:focus-visible, .fb-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.fbbar button:active, .fb-btn:active { background: var(--panel); }
.fbbar button:disabled, .fb-btn:disabled { color: var(--muted); border-color: var(--border); background: var(--panel-2); cursor: not-allowed; opacity: .6; }
.fb-btn-primary { background: rgba(88,166,255,.18); border-color: var(--accent); color: var(--text); }
.fb-btn-primary:hover { background: rgba(88,166,255,.28); color: var(--text); }
.fb-btn-danger { color: var(--red); }
.fb-btn-danger:hover { border-color: var(--red); color: var(--red); background: rgba(248,81,73,.12); }
.fb-btn-ghost { background: transparent; }
.fb-btn-gutter { width: 1.6em; padding: 0; line-height: 1; text-align: center; background: transparent; color: var(--muted); border-color: transparent; }
.fb-btn-gutter:hover { color: var(--accent); border-color: var(--accent); }
.fbbar .fbsubmit { background: rgba(88,166,255,.18); border-color: var(--accent); }
.fbbar .fbsubmit:hover { background: rgba(88,166,255,.28); }
.fbbar .fbstatus { font-size: 14px; color: var(--muted); }
.fbbar .fbstatus-ok { color: var(--green); }
.fbbar .fbstatus-error { color: var(--red); }
.fbbar .fbstatus-warn { color: var(--yellow); }
.dl-addressable { position: relative; }
.dl-gutter { flex: 0 0 auto; width: 1.6em; text-align: center; }
.diffview .dl:hover .fb-btn-gutter { color: var(--accent); border-color: var(--accent); }
.fb-replymount { margin-top: 8px; }
.fb-editor { margin: 6px 0 10px; white-space: normal; font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.fb-editor textarea { width: 100%; min-height: 72px; font: inherit; font-size: 16px; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; resize: vertical; }
.fb-editor textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.fb-editor .fb-row { display: flex; gap: 10px; align-items: center; margin-top: 6px; }
.fb-editor .fb-hint { font-size: 14px; color: var(--muted); margin-left: auto; }
.fb-draft { box-shadow: inset 3px 0 0 var(--yellow); }
/* A saved draft renders as a visible card, so the reviewer can see the reply
   they just wrote instead of it vanishing into localStorage. The card is hidden
   (not rendered at all) while its own editor is open, so the same text never
   appears twice with a second set of live Edit/Remove buttons. */
.fb-draftcard { margin: 8px 0 10px; padding: 8px 10px; background: var(--panel-2);
  border: 1px solid var(--yellow); border-left-width: 3px; border-radius: 6px;
  white-space: normal; font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.fb-draftcard-head { margin-bottom: 4px; }
.fb-draftcard-tag { font-size: 14px; font-weight: 600; color: var(--yellow);
  text-transform: uppercase; letter-spacing: .04em; }
.fb-draftcard-body { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }
.fb-draftcard-actions { display: flex; gap: 8px; margin-top: 8px; }
"""


def _load_ui_script() -> str:
    """Read the bundled UI script that the renderer inlines (never a remote one)."""
    path = pathlib.Path(__file__).resolve().parent.parent / "assets" / FEEDBACK_UI_ASSET
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"bundled UI asset {FEEDBACK_UI_ASSET!r} is unavailable: {exc}") from exc


def _script_csp_hash(script_text: str) -> str:
    digest = hashlib.sha256(script_text.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _latest_decisions(feedback) -> dict:
    """``comment_id -> latest decision``; a later decision replaces an earlier one."""
    latest: dict = {}
    if isinstance(feedback, dict):
        for decision in feedback.get("decisions", []):
            latest[decision["comment_id"]] = decision
    return latest


def _comments_by_thread(feedback) -> dict:
    """``thread_id -> [comment, ...]`` preserving the recorded order."""
    by_thread: dict = {}
    if isinstance(feedback, dict):
        for comment in feedback.get("comments", []):
            by_thread.setdefault(comment["thread_id"], []).append(comment)
    return by_thread


def _fb_comment_row(comment, decision, data=None, view=None) -> str:
    """One imported human comment with the controller's disposition and reply.

    Shared by the per-file view and the Conversation log, so the two can never
    disagree about what the controller answered. The clock is rendered in the
    reader's own timezone -- a raw ``…Z`` value in the header is exactly the
    thing the reader would otherwise have to convert by hand.
    """
    if decision is None:
        disp = '<span class="fdisp fd-pending">pending</span>'
        reply = ""
    else:
        disp = (f'<span class="fdisp fd-{_dom_id(decision["disposition"])}">'
                f'{_esc(decision["disposition"])}</span>')
        reply = _controller_reply(decision)
    return (
        '<li class="fb-comment">'
        '<div class="fb-head">'
        f'{_badge("Human (imported)", "role-human")}{disp}'
        f'<span class="mono muted">{_esc(comment["id"])}</span>'
        f'<span class="mono muted">{_esc(_target_label(comment["target"], data, view))}</span>'
        f'{_ts_display(comment["received_at"])}'
        "</div>"
        f'<div class="fb-body">{_esc(comment["body"])}</div>'
        f"{reply}"
        "</li>"
    )


def _fb_thread_article(thread, comments, latest, data=None, view=None) -> str:
    """One imported human thread inside a file row: comments + controller replies.

    Deliberately id-less. The Conversation log's row is the jump target the rail
    links to, and two elements may not share an id.
    """
    rows = [
        _fb_comment_row(comment, latest.get(comment["id"]), data, view)
        for comment in comments
    ]
    body = (
        '<ol class="timeline">' + "".join(rows) + "</ol>"
        if rows else '<p class="empty">No comments.</p>'
    )
    status = thread["status"]
    return (
        '<article class="fb-thread">'
        '<div class="fb-thread-head">'
        f'<span class="mono">{_esc(thread["id"])}</span>'
        f'<span class="fstatus fs-{_dom_id(status)}">{_esc(status)}</span>'
        f'<span class="mono muted">{_esc(_target_label(thread["target"], data, view))}</span>'
        "</div>" + body + "</article>"
    )


def _human_threads_by_path(data, view) -> dict:
    """Imported human threads grouped by the changed-file path they target.

    A reviewer leaves a comment from the Files panel (``#file-<n>`` deep-links
    land there), and importing it clears the draft from localStorage. Without
    this grouping the reviewer's own comment and the controller's reply both
    disappear from the panel they were written in, and only survive in the
    Conversation log the deep link never showed them.
    """
    feedback = data.get("feedback")
    if not isinstance(feedback, dict):
        return {}
    by_thread = _comments_by_thread(feedback)
    grouped: dict = {}
    for thread in feedback.get("threads", []):
        path = _target_path(thread["target"], data, view)
        if path is None:
            continue  # unresolvable anchor: still listed in the Conversation log
        grouped.setdefault(path, []).append((thread, by_thread.get(thread["id"], [])))
    return grouped


def _render_feedback_bar(data, view, mode: str) -> str:
    """Mount point for the comment UI.

    Emitted once per tab panel, so the controls are addressed by **class**, never
    by id: duplicate ids are invalid HTML and ``getElementById`` would only ever
    reach the first bar, leaving every control in the other panel inert. The
    bundled script queries ``.fbbar`` and binds each one.
    """
    manifest = view["manifest"]
    return (
        '<div class="fbbar"'
        f' data-task-id="{_esc(data["task_id"])}"'
        f' data-view-id="{_esc(manifest["view_id"])}"'
        f' data-mode="{_esc(mode)}"'
        f' data-endpoint="{_esc(FEEDBACK_ENDPOINT)}">'
        '<span class="fbcount">0 comments</span>'
        '<button type="button" class="fbexport">Export feedback</button>'
        '<button type="button" class="fbcopy">Copy JSON</button>'
        '<button type="button" class="fbsubmit" hidden>Submit to controller</button>'
        '<span class="fbstatus" role="status" aria-live="polite"></span>'
        "</div>"
    )


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


def _timeline_times(timeline, events):
    """Resolve each timeline entry's ``ts`` from the ``events.jsonl`` log.

    ``report.json`` timeline entries carry no timestamp; the authoritative clock
    is ``events.jsonl``'s ``ts`` (UTC ISO-8601). Two joins are supported:

    * **Explicit** -- an entry carrying ``event_seq`` takes the ``ts`` of the
      event with that ``seq``, whatever the positions are. This is the only
      join that survives a log holding coordination events the timeline does
      not summarise (dispatches, tab resets, receipts).
    * **Positional** -- with no ``event_seq``, entry *i* takes event *i*: the
      original contract, where the timeline is a prefix of the log. It is kept
      as a fallback for records written before ``event_seq`` existed.

    The positional fallback is only trusted while the log is **no longer** than
    the timeline. A longer log proves the timeline omits events and says nothing
    about where, so position stops being a join key: unstamped entries get no
    time at all. That is the documented promise ("a bad log degrades to no
    timestamps") -- the previous code silently borrowed the neighbouring row's
    clock instead (observed 2026-10-15: 9 events against 8 entries put the
    human-feedback row at the check-run's time).

    An entry whose ``event_seq`` does not resolve also gets **no** time, never a
    positional guess: a wrong time presented as a fact is worse than an absent
    one. The result is padded to the timeline length with ``None``.
    """
    by_seq = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            by_seq[seq] = event

    positional = []
    for event in events or []:
        ts = event.get("ts") if isinstance(event, dict) else None
        positional.append(ts if isinstance(ts, str) and ts.strip() else None)

    positional_safe = len(positional) <= len(timeline)

    times = []
    for index, entry in enumerate(timeline):
        seq = entry.get("event_seq") if isinstance(entry, dict) else None
        if isinstance(seq, int) and not isinstance(seq, bool):
            event = by_seq.get(seq)
            ts = event.get("ts") if isinstance(event, dict) else None
        elif positional_safe and index < len(positional):
            ts = positional[index]
        else:
            ts = None
        times.append(ts if isinstance(ts, str) and ts.strip() else None)
    return times


def _parse_ts(ts):
    """Parse a UTC ISO-8601 timestamp, or ``None`` if it is unusable.

    Deliberately stdlib-only and tolerant: an unparseable timestamp must never
    break the report, it just means that one entry shows no time (and, for the
    human-comment clock, sorts after everything that does).
    """
    if not ts or not isinstance(ts, str):
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_ts(ts):
    """Render a UTC ISO-8601 timestamp in the reader's own clock, or ``None``.

    Every timestamp the report displays goes through here, so a reader never has
    to convert a raw `…Z` value in their head. The machine-readable UTC form is
    kept alongside it in the `<time datetime>` attribute.
    """
    stamp = _parse_ts(ts)
    if stamp is None:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()  # show the reviewer's local clock
    return stamp.strftime("%d %b %Y, %H:%M")


def _ts_display(ts) -> str:
    """A ``<time>`` for a timestamp, falling back to the raw value verbatim.

    Showing the unparsed string beats showing nothing: a malformed clock is a
    data defect the reader should see, not one to hide behind a blank cell.
    """
    display = _format_ts(ts)
    if not display:
        return f'<span class="mono muted">{_esc(ts)}</span>'
    return (f'<time class="tl-when" datetime="{_esc(ts)}">{_esc(display)}</time>')


def _timeline_order(ts, index):
    """Chronological sort key; a row with no usable clock keeps its order, last."""
    stamp = _parse_ts(ts)
    if stamp is None:
        return (1, 0.0, index)
    return (0, stamp.timestamp(), index)


def _controller_reply(decision) -> str:
    """The controller's answer to a human comment, badged like every other actor.

    Plain ``Controller: …`` prose carried the same weight as the comment it
    answered, and the reader had to parse the prefix to work out who was
    speaking. The role badge is what the rest of the report already uses for
    that, and it is what makes the reply scannable next to the comment.
    """
    if not decision["rationale"]:
        return ""
    return (
        '<div class="fb-rationale">'
        f'{_badge("Controller", "role-controller")}'
        f'<span class="fb-reply">{_esc(decision["rationale"])}</span></div>'
    )


def _timeline_item(event, actors, ts) -> str:
    """One controller-authored timeline row."""
    snapshot = event["snapshot_id"]
    if snapshot is None:
        snapshot_html = '<span class="muted">no snapshot</span>'
    else:
        snapshot_html = f'<span class="mono muted">{_esc(snapshot)}</span>'
    when = _ts_display(ts) if ts else ""
    return (
        f'<li class="tl-item tl-{_kind_tone(event["kind"])}">'
        f'<div class="tl-head">{_actor_html(actors, event["actor"])}'
        f'{_kind_chip(event["kind"])}{snapshot_html}{when}</div>'
        f'<div class="tl-body">{_esc(event["body"])}</div>'
        "</li>"
    )


def _human_timeline_item(thread, comment, decision, data, view, anchor) -> str:
    """One imported human comment as a Conversation-timeline row.

    The comment, its target and the controller's reply travel together, so the
    log answers "what did the human say, and what happened to it" in one place.
    ``anchor`` puts the thread's jump id on its **first** row only: the Files
    panel's rail links there, and two rows may not share an id.
    """
    attrs = f' id="{_esc("fb-" + _dom_id(thread["id"]))}"' if anchor else ""
    if decision is None:
        disp = '<span class="fdisp fd-pending">pending</span>'
        reply = ""
    else:
        disp = (f'<span class="fdisp fd-{_dom_id(decision["disposition"])}">'
                f'{_esc(decision["disposition"])}</span>')
        reply = _controller_reply(decision)
    return (
        f'<li class="tl-item tl-yellow"{attrs}>'
        '<div class="tl-head">'
        f'{_badge("Human (imported)", "role-human")}{disp}'
        f'<span class="mono muted">{_esc(thread["id"])}</span>'
        f'<span class="mono muted">{_esc(_target_label(comment["target"], data, view))}</span>'
        f'{_ts_display(comment["received_at"])}</div>'
        f'<div class="tl-body">{_esc(comment["body"])}{reply}</div>'
        "</li>"
    )


def _human_timeline_items(data, view) -> list:
    """``(ts, html)`` for every imported human comment, in recorded order."""
    feedback = data.get("feedback")
    if not isinstance(feedback, dict):
        return []
    latest = _latest_decisions(feedback)
    by_thread = _comments_by_thread(feedback)
    items = []
    for thread in feedback.get("threads", []):
        for offset, comment in enumerate(by_thread.get(thread["id"], [])):
            items.append((
                comment["received_at"],
                _human_timeline_item(thread, comment, latest.get(comment["id"]),
                                     data, view, anchor=(offset == 0)),
            ))
    return items


def _human_comment_count(data) -> int:
    """How many imported human comments the log will carry."""
    feedback = data.get("feedback")
    if not isinstance(feedback, dict):
        return 0
    return len(feedback.get("comments", []))


def _render_timeline(data, view=None, events=None) -> str:
    """The Conversation log: controller actions **and** imported human comments.

    Human comments are rows in this list rather than a section of their own, so
    the page holds one chronological record of the task instead of a log plus a
    parallel comment table. They merge by clock, so a comment lands where it
    happened; anything without a usable clock keeps its recorded order after the
    timestamped rows.
    """
    actors = _actors_map(data)
    times = _timeline_times(data["timeline"], events)
    rows = []
    for index, event in enumerate(data["timeline"]):
        ts = times[index] if index < len(times) else None
        rows.append((ts, index, _timeline_item(event, actors, ts)))
    for offset, (ts, html) in enumerate(_human_timeline_items(data, view)):
        rows.append((ts, len(data["timeline"]) + offset, html))
    rows.sort(key=lambda row: _timeline_order(row[0], row[1]))
    items = [row[2] for row in rows]
    if not items:
        return _section("Conversation timeline", '<p class="empty">No timeline entries.</p>')
    return _section("Conversation timeline", '<ol class="timeline">' + "".join(items) + "</ol>")


def _actors_map(data):
    """Map participant id -> (display label, role) in config order.

    Reviewer labels are 1-based and stable, so the timeline, the review threads
    and the participants table all name the same person the same way.
    """
    actors, seen = {}, {"controller": 0, "developer": 0, "reviewer": 0}
    for participant in data["participants"]:
        role = participant["role"]
        seen[role] = seen.get(role, 0) + 1
        label = f"Reviewer {seen[role]}" if role == "reviewer" else role.capitalize()
        actors[participant["id"]] = (label, role)
    return actors


def _kind_tone(kind) -> str:
    """Palette word for a free-form ``kind``; ``muted`` when unrecognised."""
    lowered = (kind or "").strip().lower()
    for tone, kinds in KIND_TONES:
        if lowered in kinds:
            return tone
    return "muted"


def _kind_chip(kind) -> str:
    """The kind label as a tone-tinted chip (see ``KIND_TONES``)."""
    return f'<span class="kind k-{_kind_tone(kind)}">{_esc(kind)}</span>'


def _actor_html(actors, actor_id) -> str:
    """Role badge when the actor is known, otherwise the raw id in mono."""
    return _actor_badge(actors.get(actor_id)) or f'<span class="mono">{_esc(actor_id)}</span>'


def _actor_badge(actor) -> str:
    """Badge for an (label, role) actor; empty when the actor is unknown."""
    if not actor:
        return ""
    label, role = actor
    if role in ("controller", "developer", "reviewer"):
        return _badge(label, "role-" + role)
    return ""


def _finding_article(finding, actors, view=None) -> str:
    """One review-thread card: comment -> fix -> owner verification."""
    history_items = []
    for entry in finding["history"]:
        diff_html = ""
        if entry.get("diff"):
            diff_html = _diff_block(entry["diff"])
        verdict_html = ""
        if entry["verdict"] is not None:
            verdict_html = (
                f' <span class="verdict {entry["verdict"]}">'
                f'{_esc(entry["verdict"])}</span>'
            )
        history_items.append(
            '<li class="history-item">'
            f'<div class="history-head">{_actor_html(actors, entry["actor"])}'
            f'{_kind_chip(entry["kind"])}'
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
    owner = finding["owner"]
    owner_html = _actor_badge(actors.get(owner)) or (
        f'<span class="mono">{_esc(owner)}</span>'
    )
    thread_id = "finding/" + finding["id"]
    # The id is emitted in EVERY mode: the comment sidebar jumps to it, and in
    # static mode nothing else can provide an anchor. An unused id is inert, so
    # the static contract is unaffected. Only the reply mount needs the script.
    article_attrs = f' id="thread-{_dom_id(thread_id)}"'
    reply_mount = ""
    if view is not None and view["interactive"]:
        article_attrs += f' data-thread-id="{_esc(thread_id)}"'
        reply_mount = (
            f'<div class="fb-replymount" data-reply-anchor="{_esc(thread_id)}"></div>'
        )
    return (
        f'<article class="finding"{article_attrs}>'
        '<div class="finding-head">'
        f'<span class="mono">{_esc(finding["id"])}</span>'
        f'<span class="sev sev-{finding["severity"]}">{_esc(finding["severity"])}</span>'
        f'<span class="fstatus fs-{finding["status"]}">{_esc(finding["status"])}</span>'
        f'<span class="mono muted">{_esc(finding["path"])}:{_esc(finding["line"])}</span>'
        f'<span class="muted">owner</span>{owner_html}'
        f'<span class="mono muted">{_esc(finding["snapshot_id"])}</span>'
        "</div>"
        f'<div class="field"><div class="label">Body</div><div class="value">{_esc(finding["body"])}</div></div>'
        f'<div class="field"><div class="label">Acceptance</div><div class="value">{_esc(finding["acceptance"])}</div></div>'
        f'<div class="field"><div class="label">History</div>{history_html}</div>'
        f"{reply_mount}"
        "</article>"
    )


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# kind -> CSS class used by the diff renderer
_DIFF_CSS = {
    "meta": "dl-meta",
    "hunk": "dl-hunk",
    "add": "dl-add",
    "del": "dl-del",
    "ctx": "dl-ctx",
    "nonewline": "dl-meta",
}


def _parse_diff(diff):
    """Classify unified-diff rows by hunk state, not by bare prefix.

    Yields ``(kind, text, old_no, new_no)`` where ``kind`` is one of ``meta``
    (file headers, index/mode lines), ``hunk`` (a ``@@`` header), ``add``,
    ``del``, ``ctx`` or ``nonewline``. Line numbers are 1-based ints for the
    side they belong to, else ``None``.

    Inside a hunk the first character decides the kind, so an added line whose
    own text begins with ``++`` stays an addition instead of being mistaken for
    a ``+++`` file header (git disambiguates by hunk state, not by prefix).
    A diff with no ``@@`` header at all -- a hand-written fragment -- falls back
    to prefix classification, the only signal it offers.
    """
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    has_hunks = any(_HUNK_HEADER.match(line) for line in lines)
    old_no = new_no = 1
    old_left = new_left = 0
    in_hunk = False
    for line in lines:
        if in_hunk:
            if line.startswith("\\"):  # "\ No newline at end of file"
                yield "nonewline", line, None, None
                continue
            if line.startswith("+"):
                yield "add", line, None, new_no
                new_no += 1
                new_left -= 1
            elif line.startswith("-"):
                yield "del", line, old_no, None
                old_no += 1
                old_left -= 1
            else:  # context line, including a bare empty line in lenient diffs
                yield "ctx", line, old_no, new_no
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
            if old_left <= 0 and new_left <= 0:
                in_hunk = False
            continue
        header = _HUNK_HEADER.match(line)
        if header:
            old_no = int(header.group(1))
            new_no = int(header.group(3))
            old_left = int(header.group(2)) if header.group(2) is not None else 1
            new_left = int(header.group(4)) if header.group(4) is not None else 1
            in_hunk = True
            yield "hunk", line, None, None
        elif line.startswith("@@"):
            yield "meta", line, None, None  # malformed hunk header
        elif not has_hunks and line.startswith("+"):
            if line.startswith("+++"):
                yield "meta", line, None, None
            else:
                yield "add", line, None, new_no
                new_no += 1
        elif not has_hunks and line.startswith("-"):
            if line.startswith("---"):
                yield "meta", line, None, None
            else:
                yield "del", line, old_no, None
                old_no += 1
        elif not has_hunks and not line.startswith("\\"):
            yield "ctx", line, old_no, new_no
            old_no += 1
            new_no += 1
        else:
            yield "meta", line, None, None


def _diff_block(diff: str, view=None, path=None) -> str:
    """Render a unified diff GitHub-style: old/new line-number gutter + colour.

    With ``view`` supplied in an interactive mode, addressable rows carry the
    typed, snapshot-bound anchor so the UI can attach a comment to a precise
    side/line. Called without them (as the diff-parsing tests and finding
    history do) the markup is exactly the static output.
    """
    rows = []
    addressable = (
        view is not None
        and view["interactive"]
        and path is not None
        and _diff_has_hunks(diff)
    )
    for kind, text, old_no, new_no in _parse_diff(diff):
        old_cell = "" if old_no is None else str(old_no)
        new_cell = "" if new_no is None else str(new_no)
        attrs = ""
        if addressable:
            side = line = None
            if kind == "add" and new_no is not None:
                side, line = "new", new_no
            elif kind == "del" and old_no is not None:
                side, line = "old", old_no
            elif kind == "ctx" and new_no is not None:
                side, line = "new", new_no
            if side is not None:
                anchor_id = view["lines"].get((path, side, line))
                if anchor_id:
                    attrs = (
                        f' data-path="{_esc(path)}" data-side="{side}"'
                        f' data-line="{line}" data-anchor="{anchor_id}"'
                    )
                    attrs += (
                        ' data-ambiguous="1"'
                        if anchor_id in view["ambiguous"]
                        else ' data-addressable="1"'
                    )
        rows.append(
            f'<span class="dl {_DIFF_CSS[kind]}"{attrs}>'
            f'<span class="ln">{old_cell}</span>'
            f'<span class="ln">{new_cell}</span>'
            f'<span class="code">{_esc(text)}</span>'
            "</span>"
        )
    return f'<pre class="diffview">{"".join(rows)}</pre>'


def _diff_stat(diff: str):
    """Count added/removed lines with the same hunk-aware classification."""
    added = removed = 0
    for kind, _text, _old_no, _new_no in _parse_diff(diff):
        if kind == "add":
            added += 1
        elif kind == "del":
            removed += 1
    return added, removed


_FILE_PATH_CSS = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace'

# GitHub Octicon "chevron-right" (16px, MIT), rotated 90deg while a row is open.
_CARET_SVG = (
    '<svg class="caret" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" '
    'fill="currentColor">'
    '<path d="M6.22 3.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25'
    'a.75.75 0 0 1-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 0 1 0-1.06Z"/></svg>'
)


def _split_path(path):
    """Split a change path into (directory-with-slash, basename).

    The row shows the directory in muted text and the basename in full
    contrast, so a deep path stays scannable without losing the full location.
    """
    normalized = str(path).replace("\\", "/")
    if "/" not in normalized:
        return "", normalized
    head, _, tail = normalized.rpartition("/")
    return head + "/", tail


def _file_row(index, path, added, removed, n_comments, diff_html, comment_html,
              view=None) -> str:
    """One row of the flat changed-files list: ``<details>`` + ``<summary>``.

    Rows open independently (unlike the old one-tab-per-file strip), so two
    diffs can be read side by side. The first row starts open so the view never
    looks empty. Uses no radio and no script, so the static contract holds.
    """
    directory, name = _split_path(path)
    if added is None:
        stat = '<span class="fstat muted">no diff</span>'
        body = ""
    else:
        stat = (f'<span class="fstat"><span class="ok">+{added}</span> '
                f'<span class="bad">-{removed}</span></span>')
        body = f'<div class="filebody">{diff_html}</div>'
    wrap_attrs = ""
    comments_attrs = ""
    if view is not None and view["interactive"]:
        file_id = view["files"].get(path)
        if file_id:
            wrap_attrs = f' data-file-id="{file_id}"'
            comments_attrs = f' data-file-anchor="{file_id}"'
    count = ""
    if n_comments:
        plural = "" if n_comments == 1 else "s"
        count = (f'<span class="cc" title="{n_comments} comment{plural}">'
                 f"{n_comments}</span>")
    opened = " open" if index == 0 else ""
    return (
        f'<details class="fileitem" id="file-{index}"{opened}>'
        f'<summary class="filerow" title="{_esc(path)}">'
        f"{_CARET_SVG}"
        f'<span class="fpath"><span class="fdir">{_esc(directory)}</span>'
        f"{_esc(name)}</span>"
        f"{stat}{count}"
        "</summary>"
        f'<div class="filebody-wrap"{wrap_attrs}>'
        f"{body}"
        f'<div class="comments"{comments_attrs}>{comment_html}</div>'
        "</div>"
        "</details>"
    )


def _file_comments_html(findings, human, actors, data, view) -> str:
    """Everything the Files panel shows under one file's diff.

    Review findings come first (they are the review of record), then the
    imported human threads targeting this file, each with the controller's
    disposition and reply.
    """
    parts = [_finding_article(f, actors, view) for f in findings]
    if human:
        latest = _latest_decisions(data.get("feedback"))
        for thread, comments in human:
            parts.append(
                _fb_thread_article(thread, comments, latest, data, view)
            )
    if not parts:
        return '<p class="empty">No review comments on this file.</p>'
    return "".join(parts)


def _count_human_comments(human) -> int:
    """Imported comments on one file (a thread may hold more than one)."""
    return sum(len(comments) for _thread, comments in human)


def _render_files_changed(data, view=None) -> str:
    """Review threads & changes: each file's diff with its comments."""
    findings_by_path = {}
    for finding in data["findings"]:
        findings_by_path.setdefault(finding["path"], []).append(finding)
    human_by_path = _human_threads_by_path(data, view)
    actors = _actors_map(data)

    blocks = []
    row_index = []
    seen_paths = set()
    index = 0
    added_total = 0
    removed_total = 0
    for change in data["changes"]:
        path = change["path"]
        seen_paths.add(path)
        added, removed = _diff_stat(change["diff"])
        added_total += added
        removed_total += removed
        comments = findings_by_path.get(path, [])
        human = human_by_path.get(path, [])
        n_comments = len(comments) + _count_human_comments(human)
        blocks.append(_file_row(index, path, added, removed, n_comments,
                                  _diff_block(change["diff"], view, path),
                                  _file_comments_html(comments, human, actors, data, view),
                                  view))
        row_index.append({"index": index, "path": path, "added": added,
                          "removed": removed, "comments": n_comments})
        index += 1

    # Never hide a comment: surface any thread whose path has no diff entry.
    for path in sorted((set(findings_by_path) | set(human_by_path)) - seen_paths):
        comments = findings_by_path.get(path, [])
        human = human_by_path.get(path, [])
        n_comments = len(comments) + _count_human_comments(human)
        blocks.append(_file_row(index, path, None, None, n_comments, "",
                                _file_comments_html(comments, human, actors, data, view),
                                view))
        row_index.append({"index": index, "path": path, "added": None,
                          "removed": None, "comments": n_comments})
        index += 1

    if not blocks:
        return _section("Review threads & changes", '<p class="empty">No changes.</p>')
    n_comments = len(data["findings"]) + _count_human_comments(
        [item for threads in human_by_path.values() for item in threads]
    )
    summary = (
        '<p class="ftsummary">'
        f'{len(blocks)} file{"s" if len(blocks) != 1 else ""} changed · '
        f'<span class="ok">+{added_total}</span> <span class="bad">-{removed_total}</span> · '
        f'{n_comments} comment{"s" if n_comments != 1 else ""}</p>'
    )
    # Row order defines the anchor numbering, so the rail is derived from the
    # same ordered list the rows were built from -- never a second guess at it.
    rail = _files_rail(row_index, data, actors, human_by_path)
    return _section(
        "Review threads & changes",
        '<div class="files-layout">'
        '<div class="files-main">'
        + summary + '<div class="filelist">' + "".join(blocks) + "</div>"
        + "</div>" + rail + "</div>",
    )


def _files_rail(rows, data, actors, human_by_path=None) -> str:
    """The sticky side rail: every file, then every comment.

    Both panels are plain anchor lists and navigate with the browser's own
    fragment handling, so this stays script-free. Every target id is emitted
    unconditionally by ``_file_row``/``_finding_article``, which is what makes
    the jumps resolve in static mode too.

    Imported human threads are listed after the review findings: they jump to
    the Conversation log's copy (``#fb-…``), which is the only copy carrying an
    id, so the rail never points at a duplicate.
    """
    if not rows:
        return ""
    file_rows = []
    for row in rows:
        path = row["path"]
        directory, name = _split_path(path)
        if row["added"] is None:
            delta = '<span class="muted">no diff</span>'
        else:
            delta = (f'<span class="fdelta"><span class="ok">+{row["added"]}</span> '
                     f'<span class="bad">-{row["removed"]}</span></span>')
        badge = ""
        if row["comments"]:
            plural = "" if row["comments"] == 1 else "s"
            badge = (f'<span class="fcmt" title="{row["comments"]} '
                     f'comment{plural}">{row["comments"]}</span>')
            cls = ' class="has-cmt"'
        else:
            cls = ""
        file_rows.append(
            f'<li><a{cls} href="#file-{row["index"]}" title="{_esc(path)}">'
            f'<span class="fname"><span class="fdir">{_esc(directory)}</span>'
            f"{_esc(name)}</span>{delta}{badge}</a></li>"
        )

    comment_rows = []
    for finding in data["findings"]:
        anchor = "thread-" + _dom_id("finding/" + finding["id"])
        # The owner badge names the reviewer who raised it, using the same
        # labels as the participants table and the timeline.
        owner_html = _actor_badge(actors.get(finding["owner"])) or (
            f'<span class="mono">{_esc(finding["owner"])}</span>'
        )
        comment_rows.append(
            f'<li><a href="#{_esc(anchor)}" title="{_esc(finding["path"])}:'
            f'{_esc(finding["line"])}">'
            f'<span class="jtop"><span class="jsev sev-{finding["severity"]}">'
            f'{_esc(finding["severity"])}</span>'
            f'{owner_html}'
            f'<span class="jid">{_esc(finding["id"])}</span></span>'
            f'<span class="jpath">{_esc(finding["path"])}:'
            f'{_esc(finding["line"])}</span></a></li>'
        )
    for path in sorted(human_by_path or {}):
        for thread, _comments in human_by_path[path]:
            comment_rows.append(
                f'<li><a href="#{_esc("fb-" + _dom_id(thread["id"]))}" '
                f'title="{_esc(path)}">'
                '<span class="jtop"><span class="jsev sev-low">human</span>'
                f'{_badge("Human (imported)", "role-human")}'
                f'<span class="jid">{_esc(thread["id"])}</span></span>'
                f'<span class="jpath">{_esc(path)}</span></a></li>'
            )

    if comment_rows:
        comments_panel = (
            f'<div class="panel cpanel"><h3>Comments '
            f'<span class="pcount">{len(comment_rows)}</span></h3>'
            '<ol>' + "".join(comment_rows) + "</ol></div>"
        )
    else:
        comments_panel = (
            '<div class="panel cpanel"><h3>Comments '
            '<span class="pcount">0</span></h3>'
            '<p class="panel-empty">No review comments yet.</p></div>'
        )
    return (
        '<div class="rail">'
        f'<div class="panel fpanel"><h3>Files '
        f'<span class="pcount">{len(file_rows)}</span></h3>'
        '<ol>' + "".join(file_rows) + "</ol></div>"
        + comments_panel
        + "</div>"
    )


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
            f'<td><pre class="out">{_esc(check["output"])}</pre></td>'
            "</tr>"
        )
    if not rows:
        return _section("Checks", '<p class="empty">No checks.</p>')
    body = (
        '<table class="checks"><colgroup>'
        '<col style="width:11%"><col style="width:12%"><col style="width:12%">'
        '<col style="width:7%"><col style="width:20%"><col style="width:38%">'
        "</colgroup><thead><tr>"
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
    plan = f'<h3>Plan</h3><pre class="plan">{_esc(data["plan"])}</pre>'
    return _section("Requirements & plan", body + plan)


# GitHub Octicons (16px, MIT): comment-discussion and file, one per tab.
_COMMENT_OCTICON = (
    '<span class="ic"><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" '
    'fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0 1 '
    '10.25 10H7.061l-2.574 2.573A1.458 1.458 0 0 1 2 11.543V10h-.25A1.75 1.75 0 0 1 0 8.25'
    'v-5.5C0 1.784.784 1 1.75 1ZM1.5 2.75v5.5c0 .138.112.25.25.25h1a.75.75 0 0 1 .75.75v2.19'
    'l2.72-2.72a.749.749 0 0 1 .53-.22h3.5a.25.25 0 0 0 .25-.25v-5.5a.25.25 0 0 0-.25-.25h-8.5'
    'a.25.25 0 0 0-.25.25Zm13 2a.25.25 0 0 0-.25-.25h-.5a.75.75 0 0 1 0-1.5h.5c.966 0 1.75'
    '.784 1.75 1.75v5.5A1.75 1.75 0 0 1 14.25 12H14v1.543a1.458 1.458 0 0 1-2.487 1.03'
    'L9.22 12.28a.749.749 0 0 1 .531-1.28h.5a.75.75 0 0 1 0 1.5H9.5l1.53 1.53V11.25'
    'a.75.75 0 0 1 .75-.75h2.25a.25.25 0 0 0 .25-.25V4.75Z"/></svg></span>'
)
_FILE_OCTICON = (
    '<span class="ic"><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" '
    'fill="currentColor"><path d="M2 1.75C2 .784 2.784 0 3.75 0h6.586c.464 0 .909.184 1.237'
    '.513l2.914 2.914c.329.328.513.773.513 1.237v9.586A1.75 1.75 0 0 1 13.25 16h-9.5A1.75 '
    '1.75 0 0 1 2 14.25Zm1.75-.25a.25.25 0 0 0-.25.25v12.5c0 .138.112.25.25.25h9.5a.25.25 '
    '0 0 0 .25-.25V6h-2.75A1.75 1.75 0 0 1 9 4.25V1.5Zm6.75.062V4.25c0 .138.112.25.25.25'
    'h2.688l-.011-.013-2.914-2.914-.013-.011Z"/></svg></span>'
)


def _tab(field, label, extra="", checked=False) -> str:
    """One page tab label. ``field`` also names the radio it points at.

    The label addresses its radio by ``for=`` rather than by DOM adjacency, so
    the two radios are free to live outside the strip (see ``render_html``).
    The ``data-content`` copy reserves the semibold width so switching tabs
    never nudges the neighbouring tab sideways.

    NO ``role="tab"``/``aria-selected`` here, deliberately. Those attributes
    cannot be updated without JavaScript, and the static contract forbids
    scripts, so a hard-coded ``aria-selected`` went stale the moment a user
    clicked the other tab — a screen reader would keep announcing the tab that
    is no longer visible. The radios already expose the real state natively
    (``checked`` is announced, and arrow keys move between the two), so the
    accessible name comes from the radio group and the labels stay decorative.
    ``aria-hidden`` on the panel that CSS hides would have the same staleness
    problem, so the panels carry no aria-hidden either.
    """
    return (
        f'<label class="prtab" for="ptab-{field}" id="ptab-{field}-label">'
        f'{_COMMENT_OCTICON if field == "conv" else _FILE_OCTICON}'
        f'<span class="tlabel" data-content="{_esc(label)}">{_esc(label)}</span>'
        f"{extra}</label>"
    )


def render_html(data, mode: str = "static", events=None) -> str:
    """Validate ``data`` and return a dark-theme HTML report.

    In the default ``static`` mode every dynamic value is HTML-escaped and the
    output embeds no scripts, no remote assets and performs no fetching. In
    ``offline-feedback``/``live`` the bundled UI script is inlined (never a
    remote one) and diff rows carry stable, snapshot-bound anchors. Diffs are
    always rendered in full.
    """
    if mode not in REPORT_MODES:
        raise ValidationError(f"mode must be one of {REPORT_MODES}")
    validate_report(data, mode=mode)

    view = _build_view(data, interactive=(mode != "static"))

    head_extra = ""
    body_extra = ""
    if view["interactive"]:
        script = _load_ui_script()
        connect = "'self'" if mode == "live" else "'none'"
        # Export feedback builds the envelope with URL.createObjectURL and clicks
        # a download link. Chromium checks that navigation/download against
        # default-src, so `blob:` must be allowed there or Export silently does
        # nothing. Everything else stays locked down; `blob:` serves no remote
        # content and the page still loads no asset of its own.
        csp = (
            "default-src blob:; "
            f"script-src '{_script_csp_hash(script)}'; "
            "style-src 'unsafe-inline'; "
            f"connect-src {connect}; "
            "img-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
        )
        # The policy is a fixed string we build (no user data), and it contains
        # single quotes that html-escape would mangle, so it is inserted raw
        # inside a double-quoted attribute.
        head_extra = (
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
            f"<style>{_FEEDBACK_CSS}</style>"
        )
        body_extra = f"<script>{script}</script>"

    status = data["status"]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        head_extra,
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
        _render_page_tabs(data, view, mode, events),
        body_extra,
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(part for part in parts if part)


def _render_page_tabs(data, view, mode: str, events=None) -> str:
    """The two page tabs, laid out like a GitHub pull request.

    Conversation holds the review log (requirements, participants, timeline,
    imported human feedback, check evidence, approvals). Files changed holds the
    diffs. The comment bar is repeated in both so a comment can be left from
    whichever view the reviewer is reading.

    Ordering matters and is load-bearing:

      <input id=ptab-conv> <input id=ptab-files>   <- bare siblings
      <div class=prtabbar>  two <label for=...>    <- the strip
      <div class=prpanel data-panel=conv>
      <div class=prpanel data-panel=files>

    The radios must share ONE parent with both the strip and the panels, and
    must not be wrapped in a container: a wrapper makes the panels cousins and
    both the highlight rule and the visibility rule silently stop matching.
    """
    # Imported human comments are rows in the log, so the tab counter counts them.
    n_timeline = len(data["timeline"]) + _human_comment_count(data)
    n_files = len(data["changes"])
    added = removed = 0
    for change in data["changes"]:
        change_added, change_removed = _diff_stat(change["diff"])
        added += change_added
        removed += change_removed

    conv_extra = f'<span class="cnt">{n_timeline}</span>'
    files_extra = (
        f'<span class="cnt">{n_files}</span>'
        f'<span class="stat"><span class="ok">+{added}</span> '
        f'<span class="bad">-{removed}</span></span>'
    )

    conversation = "".join([
        _render_requirements(data),
        _render_participants(data),
        _render_timeline(data, view, events),
        _render_checks(data),
        _render_approvals(data),
    ])
    if view["interactive"]:
        conversation += _render_feedback_bar(data, view, mode)
    files = _render_files_changed(data, view)
    if view["interactive"]:
        files += _render_feedback_bar(data, view, mode)

    return "".join([
        '<input class="prtabinput" type="radio" name="prtab" id="ptab-conv" checked>',
        '<input class="prtabinput" type="radio" name="prtab" id="ptab-files">',
        '<div class="prtabbar">',
        _tab("conv", "Conversation", conv_extra, checked=True),
        _tab("files", "Files changed", files_extra),
        "</div>",
        '<div class="prpanel" data-panel="conv">' + conversation + "</div>",
        '<div class="prpanel" data-panel="files">' + files + "</div>",
    ])


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


def _read_events(real_dir: str):
    """Best-effort read of ``events.jsonl`` for timeline timestamps.

    The event log is optional and advisory: it supplies the ``ts`` that
    ``report.json``'s timeline entries deliberately do not carry. A missing,
    unreadable or malformed log must never fail the render -- it only means the
    timeline shows no times.
    """
    path = os.path.join(real_dir, "events.jsonl")
    if os.path.islink(path) or not os.path.isfile(path):
        return []
    if not _within(os.path.realpath(path), real_dir):
        return []
    events = []
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # one bad line must not drop the whole log
                if isinstance(entry, dict):
                    events.append(entry)
    except OSError:
        return []
    return events


def write_report(task_dir, mode=None) -> str:
    """Read ``report.json`` from ``task_dir`` and atomically write ``report.html``.

    Returns the path of the written file. Raises ``PathSecurityError`` for
    unsafe paths/symlinks and ``ValidationError`` for an invalid report. The
    interactive mode is resolved from the frozen ``workflow.resolved.json``
    policy; an explicit ``mode`` may only confirm it, never override it.
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
        # utf-8-sig tolerates a UTF-8 BOM, which Windows editors and PowerShell
        # writers commonly add; a BOM must never make a valid report unreadable.
        with open(report_path, "r", encoding="utf-8-sig") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        raise ReportError(f"failed to read report.json: {exc}") from exc

    validate_report(data)
    if data.get("task_id") != os.path.basename(real_dir):
        raise ValidationError("report task_id must match the task directory name")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "herdr_config_gate", pathlib.Path(__file__).with_name("check_config.py")
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    config_path = os.path.join(real_dir, "workflow.resolved.json")
    config = None
    if not os.path.islink(config_path) and os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ReportError(f"failed to read workflow.resolved.json: {exc}") from exc

    if data.get("status") == "READY_FOR_HUMAN" and config is None:
        raise ValidationError("READY requires frozen workflow.resolved.json")
    if config is not None:
        gate.validate_config(config)

    if data.get("status") == "READY_FOR_HUMAN":
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

    policy = (
        gate.report_policy(config) if config is not None
        else {"mode": "static", "port": None}
    )
    resolved_mode = policy["mode"]
    if mode is not None:
        if mode not in REPORT_MODES:
            raise ValidationError(f"mode must be one of {REPORT_MODES}")
        if mode != resolved_mode:
            raise ValidationError(
                f"requested mode {mode!r} contradicts the frozen report policy "
                f"{resolved_mode!r}"
            )
    mode = resolved_mode

    html_text = render_html(data, mode=mode, events=_read_events(real_dir))  # validates before any write

    if mode != "static":
        _write_view_manifest(real_dir, data)

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


def _write_view_manifest(real_dir: str, data) -> str:
    """Write the immutable ``views/<view-id>.json`` anchor + thread manifest.

    The manifest is content-addressed, so re-rendering the same view is
    idempotent. It is written before ``report.html`` so the served page never
    references a missing manifest.
    """
    manifest = _build_view(data, interactive=True)["manifest"]
    views_dir = os.path.join(real_dir, "views")
    if os.path.islink(views_dir):
        raise PathSecurityError("refusing to write into a symlinked views directory")
    os.makedirs(views_dir, exist_ok=True)
    manifest_path = os.path.join(views_dir, manifest["view_id"] + ".json")
    if os.path.islink(manifest_path):
        raise PathSecurityError("view manifest must not be a symlink")
    if not _within(os.path.realpath(manifest_path), real_dir):
        raise PathSecurityError("view manifest path escapes the task directory")

    payload = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".view-", suffix=".json.tmp", dir=views_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp_path, manifest_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return manifest_path


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Render one task-local PR report, without orchestrating agents")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument(
        "--mode",
        choices=REPORT_MODES,
        default=None,
        help="may only confirm the frozen workflow.resolved.json report policy",
    )
    args = parser.parse_args()
    try:
        print(write_report(args.task_dir, mode=args.mode))
    except (ReportError, OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
