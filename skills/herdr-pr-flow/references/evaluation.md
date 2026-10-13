# Evaluation and honest release status

This skill is a first-release draft, not a live multi-model or WSL certification.

## Automated local coverage

Run from any cwd with an explicit interpreter:
`<python> -m unittest discover -s <skill>/tests -v`.
Run repository authoring validator against this skill directory.

Coverage includes missing-config no-write gate, null template rejection, duplicate roles,
zero reviewers, model selectors, git/permission flags, explicit scoped snapshots and hash
verification, stale approvals/checks, owner-only resolution, HTML escaping/full diffs,
missing frozen config, omission of configured reviewers/checks, task-local atomic writes.
Real symlink cases may skip when host cannot create symlinks; mocked guards remain exercised.
Helpers never call Herdr or providers. Unit tests use isolated disposable temp dirs only.

## Instruction review

Independent read-only reviewer audits all bundled scripts/instructions for missing-config
consent, no-commit boundaries, partial report quorum, command/path risks and schema mismatch.
Fix material findings before installation. Manual code audit is not a sandbox proof.

## Required live evaluation (not yet done)

For each supported environment, use an explicitly authorized toy repository task twice:
1. baseline ordinary controller without this skill;
2. same task with skill, configured actual CLIs/models and one/two reviewer sessions.
Compare missed findings, inappropriate source writes, test evidence, duplicate dispatches,
recovery and report fidelity. Record actual model IDs and costs if visible; no fictional approvals.
Do not touch real production source as an unannounced evaluation.

Trigger set: should fire on "Use Herdr with two reviewers and local HTML", "Run a Herdr
implementation/review loop", "Resume this Herdr task"; near misses include "Explain code
reviews", "Create a real GitHub PR", "Edit this file normally", "Show the existing HTML".
Run multiple repetitions before claiming reliable auto-trigger rates. Slash invocation bypasses
trigger ambiguity but not configuration/authorization gates.

## Known limits

- Controller-driven ordering and append log are instruction-level; concurrent task lock is not
  an OS-enforced distributed lock. One active controller/project only.
- Config checker refuses common unsafe arguments but cannot establish arbitrary project command
  safety or provider auth. No hard sandbox from skill instructions alone.
- Renderer validates canonical records and, for ready output, frozen reviewer/check sets.
  It cannot prove real model honesty or live source hashes; controller must reconcile them.
- Manifest helper includes explicit files only; controller checks unexpected paths separately.
- Model metadata may be unavailable; show unverified and apply workspace diversity policy.
- Windows junctions, permissions and native CLI shells differ from Linux; live testing required.
- No cross-Windows/WSL task coordination, automatic provider setup or update compatibility promise.
