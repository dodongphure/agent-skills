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

## Report modes (opt-in)

A v1 config, or a v2 config with `report.mode: "static"`, produces the original one-way
`report.html` — no script, network, server or CDN. The interactive modes are an explicit
opt-in: set `report.mode` to `offline-feedback` or `live` in the frozen config. The mode
comes from `workflow.resolved.json`, never from a CLI flag, and the included template ships
`static` on purpose so nobody gets an interactive page without asking for one.

- `offline-feedback` — the page is still a single self-contained file that works from
  `file://`. The human adds comments on diff lines and replies in review threads, then
  **exports a feedback JSON blob** and hands the path (or the pasted JSON) back in chat.
  The controller imports it with `scripts/feedback.py`. There is no wake-up: the human
  still tells the controller to resume.
- `live` — as above, plus the controller may run the bundled loopback helper
  (`scripts/serve_report.py`) so the open page can POST comments directly. It binds
  `127.0.0.1` only, on the fixed port from the config; a busy port is a hard error, not a
  silent re-pick.

**"Exported" is not "received."** A `file://` page cannot write a sibling file, and no mode
wakes a stopped controller. The browser's local draft cache is best-effort only — never
durable, never canonical. Only the controller's receipt means a comment was imported.

**VS Code Remote / SSH.** The helper runs on the *remote* host and binds loopback *there*;
VS Code forwards that port to your machine, so nothing is exposed to the network. Two
caveats: VS Code may not auto-detect a process started outside its terminal (add the port
by hand in the **Ports** panel), and it may remap the local port, so the served page accepts
any loopback `Host`/`Origin` (`127.0.0.1:*` / `localhost:*`). Without VS Code the equivalent
is `ssh -L <port>:127.0.0.1:<port> <host>`. On the **offline** path over a remote, the
exported JSON lands on your *local* machine while the controller runs remotely, so paste
the JSON into the controller tab or copy the file across first.

**The bundled renderer is required for both interactive modes.** `scripts/render_report.py`
emits the snapshot-bound anchors, the immutable view manifest, the CSP script hash and the
inlined UI script — none of which a hand-written page can reproduce. If no Python 3.10+
interpreter can be run, the controller must stop and ask you to provide one or to set
`report.mode` back to `static`; it must not ship a static page that looks interactive.

**Finding a usable interpreter.** Probe in this order, and accept a candidate only when it
actually prints a `Python 3.10+` version on stdout:

1. `py -3 --version` (the Windows launcher).
2. `python3 --version`, then `python --version`.
3. A project-local virtualenv: `<project>/.venv/Scripts/python.exe` on Windows,
   `<project>/.venv/bin/python` on POSIX — also try `venv/` and `env/`.
4. Everything `where python` / `which -a python python3` lists, skipping the Store stub below.
5. Any interpreter the user or workspace documents (a managed or portable install path).

**The Windows Store stub.** A bare `python`/`python3` that resolves under
`%LOCALAPPDATA%\Microsoft\WindowsApps\` is the Store alias: it opens the Store instead of
running. A probe that hangs, opens a window, or prints nothing means **"stub", not "Python
present"** — skip it and keep looking. This is the usual reason a working interpreter is
declared missing. Do not install Python or packages without consent.

Record the winning absolute path once and reuse it for every bundled helper (`check_config.py`,
`snapshot`, `render_report.py`, `feedback.py`, `serve_report.py`) so one task never mixes
interpreters. Only when every candidate above fails is Python genuinely unavailable.

## TUI behavior

Use the configured CLI already running in tab 1. Its model must match the approved config
or the controller asks. After implementation authorization, tab 1 opens real task tabs in
its current Herdr workspace. Developer is sole source writer; one or more reviewer tabs
verify findings and final snapshot. User remains in tab 1 and reads report.html afterward.

Reload the skill after context compaction if instructions were lost; recover from task
state before dispatching. Retain task tabs and evidence. Do not run two active controllers
against the same project or mix Windows/WSL worker execution automatically.

## Existing plans

Earlier app/runner-based planning HTML is historical, not the installed workflow. The current
skill follows SKILL.md and the included strict, versioned `.agent-pr/workflow.json` schema
(v1, or v2 which adds the `report` policy); the older outputs/ planning example uses a
different proposed schema and must not be silently treated as active config.

## Verification status

Mechanical checks, unit tests and independent instruction review can run locally without
Herdr control. Real paid model review cycles and Linux/WSL runtime behavior require an
explicit authorized toy task inside that environment. Never claim live evaluation from fixtures.

The interactive report modes (`offline-feedback`, `live`) are covered by deterministic tests
— anchor stability, envelope validation, idempotent import, server origin/token/size guards —
but they are **not live-certified**: no real Herdr end-to-end task, no browser round trip on
each target OS, and no host wake API has been exercised. Say so plainly; do not advertise the
interactive path as proven until an authorized task records it.
