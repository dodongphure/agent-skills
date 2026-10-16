#!/usr/bin/env python3
"""Time an already approved command; keep diagnostics separate from workflow evidence.

No shell, dispatch policy, report mutation, automatic retry or background service.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

CATEGORIES = ("preflight", "agent_wait", "snapshot", "bookkeeping", "render", "check")


def log_path(task_dir):
    task = Path(task_dir).absolute()
    if task.parent.name != "tasks" or task.parent.parent.name != ".agent-pr":
        raise ValueError("task must be project-local .agent-pr/tasks/<id>")
    if not task.is_dir():
        raise ValueError("task directory must already exist")
    for part in (task, task.parent, task.parent.parent):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("task path must not contain symlinks or junctions")
    evidence = task / "evidence"
    path = evidence / "timings.jsonl"
    for part in (evidence, path):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("timing path must not be a link")
    if evidence.exists() and not evidence.is_dir():
        raise ValueError("evidence must be a directory")
    if path.exists() and not path.is_file():
        raise ValueError("timing log must be a regular file")
    return path


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def measure(task_dir, category, command):
    if category not in CATEGORIES or not command:
        raise ValueError("provide a known category and command after --")
    path = log_path(task_dir)
    path.parent.mkdir(exist_ok=True)
    # Open before execution: a denied log must not cause an unrecorded command run.
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        started_at, start = utc_now(), time.perf_counter()
        try:
            result = subprocess.run(command, shell=False, check=False)
            code, outcome = result.returncode, "exited"
        except OSError:
            code, outcome = 127, "launch_error"
        elapsed = time.perf_counter() - start
        record = {"category": category, "started_at": started_at,
                  "finished_at": utc_now(), "elapsed_seconds": elapsed,
                  "exit_code": code, "outcome": outcome}
        # No argv, prompts or output: avoid duplicating credentials or source text.
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return code


def summarize(task_dir):
    path = log_path(task_dir)
    groups = {}
    unreadable_lines = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                unreadable_lines += 1
                continue
            category, seconds = row["category"], row["elapsed_seconds"]
            if category not in CATEGORIES or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
                raise ValueError("invalid timing record")
            group = groups.setdefault(category, {"count": 0, "seconds": 0.0})
            group["count"] += 1
            group["seconds"] += seconds
    return {"categories": groups, "unreadable_lines": unreadable_lines, "note": "Measured command durations only; sums are not task wall time or model thinking time. Overlapping waits may double-count."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--category", choices=CATEGORIES)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.summary and (args.category or command):
        parser.error("--summary cannot be combined with a category or command")
    if not args.summary and (not args.category or not command):
        parser.error("execution requires --category and a command after --")
    try:
        if args.summary:
            print(json.dumps(summarize(args.task_dir), sort_keys=True))
            return 0
        return measure(args.task_dir, args.category, command)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("TIMING_ERROR: " + str(exc) + "; reconcile before retrying any command", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
