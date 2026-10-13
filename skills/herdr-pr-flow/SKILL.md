---
name: herdr-pr-flow
description: "Use when the user explicitly requests Herdr-backed coding orchestration, independent model reviews, or a local PR-style HTML report. Act as the controller in the first Herdr TUI tab, dispatch implementation and one or more reviewer tabs, and route sequential fixes and owner rechecks. Require an approved `.agent-pr/workflow.json` first; if it is absent, stop and ask the user to configure one. Do not use for ordinary single-agent edits, hidden subagent teams, real GitHub PR automation, or unattended commits."
agent_created: true
compatibility: "Herdr-managed interactive agent with terminal and file tools; native Windows or Linux/WSL. Optional bundled validation, snapshot and HTML helpers need Python 3.10+."
---

# Herdr PR flow

Act as the controller in the caller's Herdr tab. Control real interactive agent
sessions directly through the installed Herdr CLI. Do not install or run a separate
orchestration app, daemon, new TUI, hidden subagent team, or raw socket client.

Read these references at the relevant step, not all on every task:
- [Configuration and first-use gate](references/configuration.md)
- [Herdr compatibility and dispatch](references/compatibility.md)
- [Workflow and recovery](references/workflow.md)
- [Report format and completion gates](references/reporting.md)
- [Installation and usage](references/usage.md)
- [Evaluation and limitations](references/evaluation.md)
- [Workflow config template](assets/workflow.example.json)
- [Developer instructions](assets/developer-instructions.txt)
- [Reviewer instructions](assets/reviewer-instructions.txt)

## 1. Mandatory workspace gate — before implementation or tab creation

Identify the user's intended project root from caller cwd, project context and
repository root. Ask if these disagree. Never infer the project from the currently
focused Herdr sidebar item or scan unrelated home directories.

Look for `<project>/.agent-pr/workflow.json`. It is not committed to the repository
by default, but this workflow cannot run until it exists; an explicit user-specified
path is also accepted and must be recorded.

**If it is absent, STOP and ask the user to configure it first.** Say:

> This workspace has no `.agent-pr/workflow.json`. Configure the controller,
> developer, at least one reviewer, and project checks before this workflow runs.
> Shall I create a draft from the included template for you to review?

Do not silently create a usable default, infer models, inherit another workspace's
JSON, dispatch agents, create task artifacts, or change source code. Configuration
assistance is allowed only after consent. Never accept the template's null models
or checks. Ask for exact CLI/model choices and trusted project verification commands;
write the JSON only after the user approves the values. Do not require paid models
or change provider credentials. Store credentials in native CLI auth, not this JSON.

If the file exists, validate it before use. Prefer the read-only bundled helper:

```text
<python> <skill-dir>/scripts/check_config.py --workspace <project>
```

Use an available Python 3.10+ interpreter explicitly; `python3` on WSL, an approved
Windows interpreter on Windows. Do not install Python or dependencies without consent.
If unavailable, manually apply the same rules in configuration.md. Invalid JSON,
missing values or zero reviewers stops the workflow and requires configuration repair.
Successful structural validation is NOT permission, authentication or model proof.

## 2. Herdr and authorization preflight

Verify `HERDR_ENV=1` and caller workspace/tab/pane context. Outside Herdr, stop
before inspecting or controlling live sessions and explain how to load this skill
inside the intended Herdr controller tab. Configuration assistance can happen outside.

Read installed `herdr --version`, `herdr --help`, relevant subcommand `--help`, and
`herdr --skill` unless its guidance is already loaded. Check client/server capability
if needed. Never upgrade Herdr, stop its server, bypass permissions or kill existing
sessions because a command fails. Use the actual installed capability set.

Verify configured CLIs/model flags and current controller identity. The config's
controller entry documents the existing tab; do not launch a duplicate controller
or change the running model silently. Ask if it differs or cannot be verified.
Read each actor's native session provenance from `herdr agent get <target>` (its
`agent_session` field) and record it in state; a completed `agent prompt --wait` or
`agent wait` also returns `completion_seq`, which identifies the finished turn.

Before a new task reuses any retained tab, confirm that tab's interactive CLI is in a
fresh session. If it still holds a previous task, reset it with that CLI's own `/new`
command and wait until the agent reports `idle` before dispatching anything. See
compatibility.md for the reset and state-monitoring commands.

Distinguish planning from implementation authorization. A request to plan stays
plan-only. Explicit "implement" can authorize the bounded approved task and real
worker tabs. An independent reviewer is mandatory and must not be the controller.
Different CLIs alone do not prove different models; show requested vs observed IDs.

## 3. Prepare task records and baseline

After configuration and task authorization gates, create a unique task directory:
`<project>/.agent-pr/tasks/<task-id>/`. Keep all paths in the execution environment.
Only one task/source writer may be active in a project at a time (this is the developer;
reviewers are read-only and may run concurrently). Check the local
active-task record and ask before adopting stale state; never delete evidence.

Record the plan, requirements, allowed source paths, existing dirty/staged/untracked
state, approved verification commands, frozen config and permission limitations.
Create `report.json`, `events.jsonl`, `state.json`, role outboxes and snapshot artifacts.
Write an append-only event before dispatch, then update state and render HTML after
accepted replies. Controller alone owns these canonical records; actors write only
assigned result files. `events.jsonl` is the shared, append-only progress record that any
tab may read for cross-tab coordination; only the controller appends to it.
Do not fabricate messages, tests, model IDs or approvals.

Capture the pre-task source baseline and each candidate with the snapshot helper
or equivalent explicit file manifest and frozen copy. Include authorized source and
relevant inputs; exclude secrets and record exclusions. Do not use unchanging HEAD
as candidate identity. Preserve user changes and index state; task diff is against
this baseline, including new/deleted files. Hash the scoped manifest and record it.

## 4. Dispatch implementation to the configured developer

Create one task-specific developer tab in the caller's workspace with `--no-focus`;
parse returned tab/pane IDs. Start its configured interactive CLI/model using
verified arguments after Herdr's `--`. No `--print` worker, permission-bypass flags,
new workspaces or uncontrolled cross-Windows/WSL workers.

Give the developer instructions, plan path, authorized files, task/request IDs,
result outbox path and test contract. Developer alone edits authorized source.
It must not spawn reviewers, mutate canonical report state or stage/commit/push/merge.
If this task reuses a retained developer tab, reset it to a fresh session first
(send `/new`, wait for `idle`) and record the reset event.
Monitor progress with `herdr agent wait <target> --until idle --until done --until
blocked --timeout <ms>` and a `herdr agent list` snapshot; wait in bounded intervals,
inspect liveness, and require its matching result artifact. A timeout is uncertain
delivery; reconcile before any retry. Idle is not completion.

## 5. Independent reviews

Pause developer writes, freeze the candidate, and open one tab per configured reviewer
(reset any retained reviewer tab with `/new` and wait for `idle` before reusing it).
Supply each the same requirements, baseline/candidate/diff and reviewer instructions,
with an isolated review copy and separate writable outbox. Do not share peer findings
until all independent initial reports arrive. Dispatch both before waiting when parallel
execution is available; do not complete reviewer 1's full review before starting reviewer 2.
Concurrent reviewers share no writable state: the candidate is frozen and read-only, each
reviewer writes only its own outbox, and only the controller appends to events.jsonl,
state.json and report.json. Keep the developer paused until every reviewer's report is
collected, not merely the first.

Validate replies by actor/task/request/snapshot IDs. All configured reviewers are
required: one minimum is not a one-approval quorum. Unreachable, missing or unverifiable
reviews block, not pass. Collect stable finding IDs, original model-attributed comments,
file/snapshot anchors, severity, evidence and acceptance conditions. Preserve duplicate
findings with separate owner provenance; do not let consolidation erase ownership.

## 6. Sequential fixes and owner rechecks

Send exactly one open finding to the same developer session. Require its explanation,
fix delta and relevant test evidence. Capture the next candidate. Mark the thread
`awaiting_verification`, never resolved merely because the developer says fixed.

Ask the original owner in its existing reviewer tab to verify that exact fix snapshot.
Pause developer writes during verification. The owner returns addressed/not_addressed
with evidence. A failed recheck sends the same finding back to the developer; retain
all attempts. Add new findings to the queue, not the trash. Do not erase disputed
findings or waive real defects to end a loop. Apply configured per-finding attempts
and task budget; BLOCKED means ask the user, never auto-approve at the limit.

If a session dies, try its verified native resume method. Otherwise log a replacement
session and restore it from task artifacts; never pretend it is the original session.

## 7. Final checks, HTML and human handoff

After owner resolutions, run configured required checks and collect real commands,
exit codes and output on the final candidate. Ask EVERY configured reviewer to review
the cumulative task diff and approve the same final snapshot. New source changes
invalidate old final approvals and check evidence. Recheck live scoped file hashes
before handoff; conflicting user edits pause rather than get overwritten.

Render `report.html` using the bundled local renderer or faithfully equivalent escaped
static HTML. Include original reviews, every fix/recheck, model provenance, complete
cumulative diffs, check evidence, pending questions and final status. Keep it in the
project. No external scripts, CDN, service or GitHub account needed.

`READY_FOR_HUMAN` requires all finding owners resolved, all configured reviewer final
approvals on the same current snapshot, all required checks passed, and no missing
identity/evidence policy gate. The renderer validates RECORDS, not model truth or live
files; the controller must independently reconcile reality with those records.

Return the report path and concise source-change/check summary in tab 1. User inspects
and commits. Never stage, commit, push, merge or create a real PR. Do not close retained
tabs or delete task artifacts unless the user separately requests it.

## Gotchas / what not to do

- Missing workspace JSON means ask first, not create defaults and continue.
- A discovered skill description is not a fully preloaded controller; explicitly load
  this skill once in the first CLI session and reload after compaction if needed.
- Never derive handles from tab numbers or a different client's focus.
- A retained tab may still hold a previous task's session; reset it with the CLI's `/new`
  and confirm `idle` before dispatch. Never assume a reused tab starts clean.
- Native CLI TUI output can be truncated; task reply files and canonical events preserve
  evidence. Structured result files are part of this workflow contract, not transcript scraping.
- Never mistake Herdr idle/done or an old visible "passed" line for a matching task result.
- Git worktree at HEAD does not contain uncommitted candidate changes automatically.
- Do not introduce npm or a launcher app to solve a skill discovery problem.
- Permission instructions alone are not a sandbox. Disclose weak enforcement and ask
  before granting additional access; never relay approval between agents as user consent.
- Keep Windows-native tasks native and WSL tasks Linux-local. No shared active task writers.
- Never inject source/comment HTML as markup or silently exclude significant changes.

## First release status

Treat this version as an evaluated draft: deterministic helpers and mock workflow tests
are testable without live model sessions. Do not claim real TUI/WSL end-to-end certification
unless a real authorized task has been run and recorded. Read evaluation.md for the matrix.
