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
It also covers the config v2 report policy, snapshot-bound anchor identity (reordered files
and findings, multi-hunk, duplicate text, additions beginning `++`, renamed/deleted files,
Unicode paths, hunkless fragments, orphan findings), the feedback envelope (bounds, duplicate
keys, non-finite numbers, foreign task ids, unknown views/targets, blank/control bodies,
idempotent retry vs content conflict), and the loopback server (non-loopback client rejected,
wrong token 403, oversized body 413, path escape refused, busy fixed port a hard error,
same-origin POST only). The default static render is asserted byte-identical to the original.
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

## Interactive modes (config v2)

- **Not live-certified.** The interactive paths have deterministic test coverage only. No real
  Herdr task, no per-OS browser round trip and no forwarded-port (VS Code Remote / `ssh -L`)
  session has been exercised end to end. Do not claim otherwise.
- **The bundled renderer is mandatory.** Both interactive modes depend on it for the anchors,
  the view manifest, the CSP hash and the UI script. A real 2026-10-13 run showed the failure
  mode: the controller concluded "Python 3 is unavailable", hand-wrote a static page, and kept
  the `fb-*` CSS while omitting the script, the feedback bar and the view manifest — so the
  comment UI the user asked for was silently absent. Cause: on Windows a bare `python`/`python3`
  on PATH was the Microsoft Store alias stub, so the controller concluded "Python 3 is
  unavailable" while a working interpreter existed elsewhere. The contract now requires stopping
  and asking instead of degrading, and the ordered probe in usage.md ("Finding a usable
  interpreter") must be exhausted — including project virtualenvs and the Store-stub check —
  before Python is declared unavailable.
- **No wake-up.** Neither mode resumes a stopped controller. There is no preflighted host
  prompt API; the human must tell the controller to resume. Do not assume one exists.
- **"Exported" ≠ "received."** An offline export is a user-initiated blob download; the
  controller sees nothing until it imports an explicit path. Browser draft storage is
  best-effort and never canonical; a reload can lose unsent drafts.
- **Trust boundary.** Feedback is untrusted input: display as text only, never as markup,
  never as a command, never as approval or commit authorization. A local JSON import is not
  identity proof — the author is shown as **Human (imported)**.
- **Anchor staleness.** Comments bind to a snapshot; a comment on an older view imports as
  stale, stays visible and replyable, and is never rebased onto a newer line. Hunkless
  fragments and v1 findings without a side get no inline line anchor.
- **Server is a transport, not a store.** It writes only `inbox/human/<batch-id>.json` and
  never owns `report.json`/`state.json`/`events.jsonl`. The controller's receipt, not the
  HTTP 202, is what marks a comment imported.
