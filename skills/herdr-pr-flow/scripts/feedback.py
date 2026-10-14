#!/usr/bin/env python3
"""Read-only feedback-envelope validation for herdr-pr-flow.

Validates a browser-produced feedback envelope against the task's immutable
view manifest, so a comment can only target an anchor that the rendered report
actually published. Standard library only.

Guarantees:
  * never searches the filesystem for input -- the caller supplies one path;
  * never contacts the network;
  * writes nothing by default (``--stage`` is an explicit controller action);
  * rejects duplicate JSON keys, non-finite numbers, foreign task ids, unknown
    views/targets, oversized input, and id/content conflicts.

Exit codes: 0 valid, 2 invalid.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile

FEEDBACK_SCHEMA_VERSION = 1
TARGET_KINDS = ("line", "thread", "file")
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_COMMENTS = 100
MAX_BODY_BYTES = 16 * 1024


class FeedbackError(ValueError):
    """The envelope, the manifest, or the task layout is unusable."""


# --------------------------------------------------------------------------- #
# Strict JSON
# --------------------------------------------------------------------------- #
def _no_duplicate_pairs(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise FeedbackError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name):
    raise FeedbackError(f"non-finite JSON value {name!r}")


def load_json(text: str):
    try:
        return json.loads(
            text, object_pairs_hook=_no_duplicate_pairs, parse_constant=_reject_constant
        )
    except FeedbackError:
        raise
    except ValueError as exc:
        raise FeedbackError(f"invalid JSON: {exc}") from exc


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _within(child: str, parent: str) -> bool:
    child_n = os.path.normcase(os.path.abspath(child))
    parent_n = os.path.normcase(os.path.abspath(parent))
    return child_n == parent_n or child_n.startswith(parent_n + os.sep)


def resolve_task_dir(task_dir) -> str:
    """Return the real task dir, refusing symlinks and non-task layouts.

    Junctions/reparse points do not always report through ``islink``, so the
    real path is compared against its own ancestors as well.
    """
    raw = os.path.abspath(os.fspath(task_dir))
    if os.path.islink(raw):
        raise FeedbackError("task directory must not be a symlink")
    real = os.path.realpath(raw)
    parts = pathlib.PurePath(real).parts
    if len(parts) < 3 or parts[-3] != ".agent-pr" or parts[-2] != "tasks":
        raise FeedbackError("task directory must be <workspace>/.agent-pr/tasks/<task-id>")
    if not os.path.isdir(real):
        raise FeedbackError("task directory does not exist")
    return real


def _safe_child(real_dir: str, *parts) -> str:
    """A path inside ``real_dir`` that is not a symlink and does not escape."""
    candidate = os.path.join(real_dir, *parts)
    if os.path.islink(candidate):
        raise FeedbackError(f"refusing a symlinked path: {os.path.join(*parts)}")
    if not _within(os.path.realpath(candidate), real_dir):
        raise FeedbackError(f"path escapes the task directory: {os.path.join(*parts)}")
    return candidate


def load_manifest(real_dir: str, view_id: str) -> dict:
    """Load ``views/<view-id>.json``; an unknown view is a hard failure."""
    if not isinstance(view_id, str) or not view_id or "/" in view_id or "\\" in view_id:
        raise FeedbackError("view_id must be a plain identifier")
    path = _safe_child(real_dir, "views", view_id + ".json")
    if not os.path.isfile(path):
        raise FeedbackError(f"unknown view {view_id!r}: no matching view manifest")
    with open(path, "r", encoding="utf-8-sig") as handle:
        manifest = load_json(handle.read())
    if not isinstance(manifest, dict):
        raise FeedbackError("view manifest must be a JSON object")
    for key in ("view_id", "task_id", "files", "line_anchors", "threads"):
        if key not in manifest:
            raise FeedbackError(f"view manifest is missing {key!r}")
    if manifest["view_id"] != view_id:
        raise FeedbackError("view manifest id does not match the requested view")
    return manifest


def existing_comment_index(real_dir: str) -> dict:
    """Map comment id -> canonical content for every staged submission."""
    inbox = os.path.join(real_dir, "inbox", "human")
    index: dict = {}
    if not os.path.isdir(inbox) or os.path.islink(inbox):
        return index
    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(inbox, name)
        if os.path.islink(path) or not _within(os.path.realpath(path), real_dir):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                staged = load_json(handle.read())
        except (OSError, FeedbackError):
            continue
        for comment in (staged or {}).get("comments", []) or []:
            if isinstance(comment, dict) and isinstance(comment.get("id"), str):
                index[comment["id"]] = _canonical(comment)
    return index


def _canonical(comment) -> str:
    return json.dumps(
        {"target": comment.get("target"), "body": comment.get("body")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


# --------------------------------------------------------------------------- #
# Envelope validation
# --------------------------------------------------------------------------- #
def _require(condition, message):
    if not condition:
        raise FeedbackError(message)


def _validate_body(body, where: str) -> None:
    _require(isinstance(body, str) and body.strip(), f"{where} must be a non-empty string")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise FeedbackError(f"{where} exceeds {MAX_BODY_BYTES} bytes")
    for char in body:
        if ord(char) < 32 and char not in "\n\t":
            raise FeedbackError(f"{where} contains a forbidden control character")


def _resolve_target(target, manifest, where: str) -> dict:
    _require(isinstance(target, dict), f"{where} must be an object")
    kind = target.get("kind")
    _require(kind in TARGET_KINDS, f"{where}.kind must be one of {TARGET_KINDS}")
    field = {"line": "anchor_id", "thread": "thread_id", "file": "file_id"}[kind]
    value = target.get(field)
    _require(isinstance(value, str) and value, f"{where}.{field} must be a non-empty string")
    if kind == "line":
        known = {item["anchor_id"] for item in manifest["line_anchors"]}
        if value not in known:
            raise FeedbackError(f"{where}: unknown line anchor {value!r} in this view")
    elif kind == "thread":
        known = {item["thread_id"] for item in manifest["threads"]}
        if value not in known:
            raise FeedbackError(f"{where}: unknown thread {value!r} in this view")
    else:
        known = {item["file_id"] for item in manifest["files"]}
        if value not in known:
            raise FeedbackError(f"{where}: unknown file {value!r} in this view")
    return {"kind": kind, field: value}


def validate_envelope(envelope, manifest, existing=None) -> dict:
    """Validate and normalize one envelope against a view manifest.

    ``existing`` maps a known comment id to its canonical content: an identical
    resubmission is a no-op retry, a differing one is a conflict.
    """
    _require(isinstance(envelope, dict), "envelope must be a JSON object")
    for key in ("schema_version", "task_id", "view_id", "batch_id", "comments"):
        _require(key in envelope, f"envelope is missing required key {key!r}")

    _require(
        envelope["schema_version"] == FEEDBACK_SCHEMA_VERSION,
        f"schema_version must be {FEEDBACK_SCHEMA_VERSION}",
    )
    task_id = envelope["task_id"]
    _require(isinstance(task_id, str) and task_id, "task_id must be a non-empty string")
    if task_id != manifest["task_id"]:
        raise FeedbackError(f"foreign task id {task_id!r} for this task directory")

    view_id = envelope["view_id"]
    _require(isinstance(view_id, str) and view_id, "view_id must be a non-empty string")
    if view_id != manifest["view_id"]:
        raise FeedbackError("envelope view_id does not match the supplied manifest")

    batch_id = envelope["batch_id"]
    _require(isinstance(batch_id, str) and batch_id, "batch_id must be a non-empty string")

    comments = envelope["comments"]
    _require(isinstance(comments, list), "comments must be an array")
    _require(comments, "comments must not be empty")
    _require(
        len(comments) <= MAX_COMMENTS,
        f"at most {MAX_COMMENTS} comments per batch",
    )

    existing = existing or {}
    seen: set = set()
    normalized = []
    retried = []
    for index, comment in enumerate(comments):
        at = f"comments[{index}]"
        _require(isinstance(comment, dict), f"{at} must be an object")
        for key in ("id", "target", "body"):
            _require(key in comment, f"{at} is missing required key {key!r}")
        comment_id = comment["id"]
        _require(
            isinstance(comment_id, str) and comment_id,
            f"{at}.id must be a non-empty string",
        )
        _require(comment_id not in seen, f"{at}: duplicate comment id {comment_id!r}")
        seen.add(comment_id)
        target = _resolve_target(comment["target"], manifest, f"{at}.target")
        _validate_body(comment["body"], f"{at}.body")

        entry = {"id": comment_id, "target": target, "body": comment["body"]}
        if comment_id in existing:
            if existing[comment_id] == _canonical(entry):
                retried.append(comment_id)
                continue
            raise FeedbackError(
                f"{at}: comment id {comment_id!r} was already accepted with different "
                "content (conflict); evidence preserved, nothing rewritten"
            )
        normalized.append(entry)

    return {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "task_id": task_id,
        "view_id": view_id,
        "batch_id": batch_id,
        "comments": normalized,
        "retried_comment_ids": retried,
    }


def stage_envelope(real_dir: str, batch_id: str, envelope_text: str) -> str:
    """Atomically preserve the raw submission under ``inbox/human/``."""
    _require(
        isinstance(batch_id, str) and batch_id and "/" not in batch_id
        and "\\" not in batch_id,
        "batch_id must be a plain identifier",
    )
    inbox = os.path.join(real_dir, "inbox", "human")
    if os.path.islink(os.path.join(real_dir, "inbox")):
        raise FeedbackError("refusing a symlinked inbox")
    os.makedirs(inbox, exist_ok=True)
    if os.path.islink(inbox):
        raise FeedbackError("refusing a symlinked inbox")
    target = _safe_child(real_dir, "inbox", "human", batch_id + ".json")
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as handle:
            if handle.read() == envelope_text:
                return target  # identical retry, no-op
        raise FeedbackError("a different submission already uses this batch_id")
    fd, tmp_path = tempfile.mkstemp(prefix=".inbox-", suffix=".json.tmp", dir=inbox)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(envelope_text)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def current_view_id(real_dir: str):
    """The view the report currently projects, or None when unavailable."""
    path = os.path.join(real_dir, "report.json")
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = load_json(handle.read())
    except (OSError, FeedbackError):
        return None
    feedback = data.get("feedback") if isinstance(data, dict) else None
    if isinstance(feedback, dict):
        return feedback.get("current_view_id")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="path to the feedback envelope JSON")
    source.add_argument("--stdin", action="store_true", help="read the envelope from stdin")
    parser.add_argument("--stage", action="store_true",
                        help="preserve the raw envelope under inbox/human/ (controller action)")
    parser.add_argument("--json", action="store_true", help="emit a JSON result")
    args = parser.parse_args()

    try:
        real_dir = resolve_task_dir(args.task_dir)
        if args.stdin:
            text = sys.stdin.read()
        else:
            path = os.path.abspath(os.fspath(args.input))
            if os.path.getsize(path) > MAX_ENVELOPE_BYTES:
                raise FeedbackError(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
        if len(text.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise FeedbackError(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")

        envelope = load_json(text)
        _require(isinstance(envelope, dict), "envelope must be a JSON object")
        manifest = load_manifest(real_dir, envelope.get("view_id"))
        result = validate_envelope(envelope, manifest, existing_comment_index(real_dir))
        result["stale"] = result["view_id"] != current_view_id(real_dir)
        if args.stage:
            result["staged_path"] = stage_envelope(real_dir, result["batch_id"], text)
        status = "FEEDBACK_VALID"
    except (FeedbackError, OSError, ValueError, TypeError) as exc:
        result = {"status": "FEEDBACK_INVALID", "message": str(exc)}
        status = result["status"]

    if args.json:
        print(json.dumps(dict(result, status=status), ensure_ascii=True, indent=2))
    else:
        print(json.dumps(dict(result, status=status), ensure_ascii=True))
    return 0 if status == "FEEDBACK_VALID" else 2


if __name__ == "__main__":
    sys.exit(main())
