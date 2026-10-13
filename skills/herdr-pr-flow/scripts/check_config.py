#!/usr/bin/env python3
"""Read-only workspace workflow-config gate. No execution, network or writes.

Checks ``<project>/.agent-pr/workflow.json``. The file is not committed to the
repository by default, but the workflow cannot run without it: an absent file
returns CONFIG_REQUIRED so the caller stops and asks the user to configure.
Structural validation only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys

class ConfigError(ValueError):
    pass


def keys(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ConfigError(f"{label}: expected exactly {sorted(required)}")


def text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{label}: expected nonempty plain string")


def identity(value, label):
    text(value, label)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise ConfigError(f"{label}: use lowercase identifier")


def arguments(value, label, extra=False):
    if not isinstance(value, list) or (not value and not extra):
        raise ConfigError(f"{label}: expected argv array")
    for arg in value:
        text(arg, label)
        if any(c in arg for c in ("\n", "\r", "\x00", ";", "|", "&", "`")):
            raise ConfigError(f"{label}: shell operators/control characters are not allowed")
        if extra and (arg in ("-p", "-m", "-w", "-a", "-s") or any(arg.startswith(prefix) for prefix in (
            "--full-auto", "--sandbox", "--ask-for-approval", "--yolo", "--model", "--provider", "--api-key", "--print", "--dangerously", "--permission", "--allow-dangerously", "--auto", "--background", "--bg", "--worktree", "--plan-yolo", "--session", "--resume", "--continue", "--system-prompt", "--append-system", "--settings", "--config", "--agent", "--tools", "--allowed", "--disallowed", "--mcp", "--plugin", "--add-dir", "--profile", "--cwd", "--mode", "--export", "--shell"))):
            raise ConfigError(f"{label}: dangerous or role-overriding argument {arg!r}")
    if extra and any(arg not in ("--no-lsp", "--hide-thinking") for arg in value):
        raise ConfigError(f"{label}: v1 only permits --no-lsp and --hide-thinking; extend after audit")
    if not extra:
        # This is a conservative syntax check, never a proof a command is harmless.
        cmd = Path(value[0]).name.lower().removesuffix(".exe").removesuffix(".cmd")
        if cmd in ("cmd", "powershell", "pwsh", "bash", "sh", "wsl", "curl", "wget", "rm", "del", "env", "nice", "timeout", "nohup", "xargs", "mv", "cp", "dd", "ssh", "scp", "docker", "icacls", "chmod", "chown", "sudo", "runas", "robocopy", "rsync"): 
            raise ConfigError(f"{label}: shell/network/destructive command requires separate authorization")
        if cmd == "git":
            raise ConfigError(f"{label}: use project checks, not git commands or aliases")


def validate_config(data):
    keys(data, ("schema_version", "roles", "workflow", "verification", "safety"), "config")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ConfigError("schema_version must be 1")
    roles = data["roles"]
    keys(roles, ("controller", "developer", "reviewers"), "roles")
    reviewers = roles["reviewers"]
    if not isinstance(reviewers, list) or not reviewers:
        raise ConfigError("configure at least one independent reviewer")
    seen = {"controller", "developer"}
    actors = [("controller", roles["controller"], False), ("developer", roles["developer"], False)]
    actors += [(f"reviewers[{i}]", r, True) for i, r in enumerate(reviewers)]
    for label, actor, reviewer in actors:
        keys(actor, ("cli", "model", "extra_args", "id", "focus") if reviewer else ("cli", "model", "extra_args"), label)
        if actor["cli"] not in ("claude", "opencode", "omp", "codex"):
            raise ConfigError(f"{label}.cli unsupported; verify and extend deliberately")
        text(actor["model"], label + ".model")
        if any(c in actor["model"] for c in "<>;&|`$ "):
            raise ConfigError(f"{label}.model: placeholder or unsafe selector")
        if actor["cli"] in ("opencode", "omp") and "/" not in actor["model"]:
            raise ConfigError(f"{label}.model: require exact provider/model selector")
        arguments(actor["extra_args"], label + ".extra_args", extra=True)
        if reviewer:
            identity(actor["id"], label + ".id")
            if actor["id"] in seen:
                raise ConfigError("duplicate or reserved reviewer id")
            seen.add(actor["id"])
            if not isinstance(actor["focus"], list) or not actor["focus"]:
                raise ConfigError("reviewer focus must be nonempty array")
            for item in actor["focus"]:
                text(item, "focus")
    workflow = data["workflow"]
    keys(workflow, ("max_fix_attempts_per_finding", "max_task_minutes", "wait_timeout_ms", "model_diversity"), "workflow")
    for name in ("max_fix_attempts_per_finding", "max_task_minutes", "wait_timeout_ms"):
        if type(workflow[name]) is not int or workflow[name] < 1:
            raise ConfigError(f"workflow.{name}: expected positive integer")
    if workflow["model_diversity"] not in ("warn", "require_distinct"):
        raise ConfigError("unknown model_diversity policy")
    keys(data["verification"], ("required_checks",), "verification")
    checks = data["verification"]["required_checks"]
    if not isinstance(checks, list) or not checks:
        raise ConfigError("configure at least one trusted required project check")
    check_ids = set()
    for check in checks:
        keys(check, ("id", "argv"), "check")
        identity(check["id"], "check.id")
        if check["id"] in check_ids:
            raise ConfigError("duplicate check id")
        check_ids.add(check["id"])
        arguments(check["argv"], "check.argv")
    keys(data["safety"], ("allow_stage", "allow_commit", "allow_push", "allow_merge"), "safety")
    if any(value is not False for value in data["safety"].values()):
        raise ConfigError("all git mutation flags must be exactly false")
    return data


def load_config(workspace, config=None):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ConfigError("workspace must be an existing directory")
    path = Path(config) if config else root / ".agent-pr" / "workflow.json"
    if not path.is_absolute():
        path = root / path
    if path.is_symlink():
        raise ConfigError("configuration symlink requires explicit manual inspection")
    if not path.exists():
        return {"status": "CONFIG_REQUIRED", "path": str(path), "message": "No .agent-pr/workflow.json. Ask the user to configure this workspace before dispatching or editing code."}, 3
    if path.stat().st_size > 1024 * 1024:
        raise ConfigError("configuration exceeds 1 MiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_config(data)
    return {"status": "CONFIG_VALID", "path": str(path), "reviewers": len(data["roles"]["reviewers"]), "message": "Structure valid; still require user approval, CLI/auth/permissions and model provenance checks."}, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        result, code = load_config(args.workspace, args.config)
    except (ConfigError, OSError, ValueError, TypeError) as exc:
        result, code = {"status": "INVALID_CONFIG", "message": str(exc)}, 2
    print(json.dumps(result, ensure_ascii=True))
    return code

if __name__ == "__main__":
    sys.exit(main())
