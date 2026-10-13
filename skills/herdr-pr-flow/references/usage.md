# Install and use

This is a skill package, not an orchestration app. Keep all resources next to SKILL.md.
No npm, custom launcher, background daemon or new TUI. Existing CLIs retain their own runtimes.

## Controller installation

Claude CLI (native Windows): copy the complete herdr-pr-flow folder into
`%USERPROFILE%/.claude/skills/herdr-pr-flow/` without overwriting another installation.
Claude CLI (WSL/Linux): copy the complete folder into `~/.claude/skills/herdr-pr-flow/`.
WorkBuddy user skills: `~/.workbuddy-ai/skills/herdr-pr-flow/` in this environment.
Other controller CLIs: inspect their installed skill docs/settings, or load the absolute
SKILL.md path explicitly. Do not claim a WorkBuddy directory is automatically seen by OMP.
Workers/reviewers receive their role contract directly; they need no controller skill install.

Installing/discovering the description is not full preload. In Herdr tab 1's Claude session:

```text
/herdr-pr-flow
```

Then request a task, or invoke explicitly with it:

```text
/herdr-pr-flow Implement pagination for the customer list. Preserve filters,
add regression tests, use this workspace's reviewers and return the local HTML.
Do not commit.
```

For a controller without supported slash invocation:

> Read <absolute skill path>/SKILL.md and its configuration reference. Act as the
> Herdr controller for this session and follow the workflow-config gate first.

## First project use

No `.agent-pr/workflow.json` means the skill must ASK before proceeding (the file is not
committed by default, but the workflow cannot run without it). It offers the included
draft; user chooses exact CLI/model selectors and required project check argv. Approve
values, then create the config. This does not authorize source changes until the user
supplies an implement request. Never install/authenticate new providers automatically.

## TUI behavior

Use the configured CLI already running in tab 1. Its model must match the approved config
or the controller asks. After implementation authorization, tab 1 opens real task tabs in
its current Herdr workspace. Developer is sole source writer; one or more reviewer tabs
verify findings and final snapshot. User remains in tab 1 and reads report.html afterward.

Reload the skill after context compaction if instructions were lost; recover from task
state before dispatching. Retain task tabs and evidence. Do not run two active controllers
against the same project or mix Windows/WSL worker execution automatically.

## Existing plans

Earlier app/runner-based planning HTML is historical, not the installed workflow. v1 follows
SKILL.md and the included strict `.agent-pr/workflow.json` schema; the older outputs/ planning
example uses a different proposed schema and must not be silently treated as active config.

## Verification status

Mechanical checks, unit tests and independent instruction review can run locally without
Herdr control. Real paid model review cycles and Linux/WSL runtime behavior require an
explicit authorized toy task inside that environment. Never claim live evaluation from fixtures.
