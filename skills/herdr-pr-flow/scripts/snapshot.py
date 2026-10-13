#!/usr/bin/env python3
"""Capture explicit scoped files into a new task-local frozen copy, or verify it.
No directory scanning, subprocesses, network, Git mutation or overwrite of snapshots.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def scoped_path(root, name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("source paths must be nonempty forward-slash relative paths")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or ":" in name:
        raise ValueError("source path escapes project")
    if any(p in (".git", ".agent-pr", ".ssh", "node_modules", "__pycache__") for p in rel.parts):
        raise ValueError("excluded metadata/cache path")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("source symlinks are not captured")
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("source path escapes project")
    if current.exists() and not current.is_file():
        raise ValueError("source entries must be files, not directories")
    return current


def capture(workspace, task_dir, name, paths):
    root = Path(workspace).resolve(strict=True)
    task = Path(task_dir).absolute()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ValueError("unsafe snapshot label")
    expected = root / ".agent-pr" / "tasks"
    if task.parent.resolve() != expected.resolve() or task.is_symlink() or not task.is_dir():
        raise ValueError("task must be existing project-local .agent-pr/tasks/<id>")
    for p in (root / ".agent-pr", expected, task, task / "snapshots"):
        if p.is_symlink():
            raise ValueError("task metadata symlink rejected")
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise ValueError("provide a nonempty unique explicit source-file list")
    buffers, manifest = {}, []
    for path in sorted(paths):
        file = scoped_path(root, path)
        if file.exists():
            content = file.read_bytes()
            buffers[path] = content
            manifest.append({"path": path, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
        else:
            manifest.append({"path": path, "sha256": None, "bytes": 0})
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    dest = task / "snapshots" / name
    dest.mkdir(parents=True, exist_ok=False)
    for path, content in buffers.items():
        target = dest / "source" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    record = {"id": name, "manifest_sha256": digest, "files": manifest}
    (dest / "manifest.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def verify(workspace, manifest_path):
    root = Path(workspace).resolve(strict=True)
    path = Path(manifest_path)
    if path.is_symlink() or not path.resolve().is_relative_to(root / ".agent-pr" / "tasks"):
        raise ValueError("manifest must be task-local")
    record = json.loads(path.read_text(encoding="utf-8"))
    mismatches = []
    for entry in record["files"]:
        file = scoped_path(root, entry["path"])
        current = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
        if current != entry["sha256"]:
            mismatches.append(entry["path"])
    return {"snapshot_id": record["id"], "matches": not mismatches, "changed_paths": mismatches}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", required=True)
    p.add_argument("--task-dir")
    p.add_argument("--name")
    p.add_argument("--paths-json")
    p.add_argument("--verify")
    a = p.parse_args()
    try:
        if a.verify:
            result = verify(a.workspace, a.verify)
            code = 0 if result["matches"] else 2
        else:
            if not all((a.task_dir, a.name, a.paths_json)):
                raise ValueError("capture requires --task-dir --name --paths-json")
            paths = json.loads(Path(a.paths_json).read_text(encoding="utf-8"))
            result, code = capture(a.workspace, a.task_dir, a.name, paths), 0
        print(json.dumps(result))
        return code
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "SNAPSHOT_ERROR", "message": str(exc)}))
        return 2

if __name__ == "__main__":
    sys.exit(main())
