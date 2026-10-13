# Herdr compatibility and direct TUI dispatch

## Runtime discovery

Require HERDR_ENV=1 before live inspection/control. Confirm inherited workspace/tab/pane
IDs and cwd belong to the intended project. Do not inspect another focused session from
outside Herdr. Read `herdr --version`, `herdr --help`, relevant `--help` and bundled
`herdr --skill` unless its guidance is already loaded. Version 0.9.0 help was inspected
when authoring; never treat that as a permanent minimum/maximum or compatibility guarantee.

The installed binary and current server are authority. `herdr status` is a read-only check
inside the authorized session if a method/version mismatch occurs. A missing method means
stop and explain, not update, install integrations, restart servers or kill sessions.

## Command pattern (replace placeholders; no guessed tool parameters)

Create a real tab using the caller's explicit workspace, project/developer cwd or approved
review copy cwd, and preserve focus:

```text
herdr tab create --workspace <caller-workspace-id> --cwd <authorized-path> --label <task-role> --no-focus
```

Parse `.result.tab.tab_id` and `.result.root_pane.pane_id`. A pane is not an agent; start
only when its shell owns the prompt:

```text
herdr agent start <unique-name> --kind <verified-kind> --pane <returned-pane-id> -- <native-argv>
```

Name must satisfy installed syntax, e.g. lowercase <=32 chars on 0.9.0. Store live handles,
server/session context and native session provenance in state. Never derive IDs from TUI order.
Use agent name or pane ID, not raw kind name (claude is not a unique agent identity).

Native interactive model arguments confirmed from local help:
- Claude: `--model <model-id-or-approved-alias>`.
- OpenCode: `--model <provider/model>`.
- OMP: `--model <provider/model-id>`; avoid fuzzy matching and legacy --provider.
- Codex: confirm installed `--model` or `-m` before use.

No `--print`, `--dangerously-skip-permissions`, bypass modes or arbitrary shell concatenation.
Do not inject task text into shell commands. Pass the prompt through Herdr's agent prompt.
Where Bash/PowerShell string quoting differs, use the caller's actual shell syntax.

Prompt an idle actor with a short task/artifact contract:

```text
herdr agent prompt <stored-target> <prompt-text> --wait --timeout <bounded-ms>
herdr agent get <stored-target>
herdr agent read <stored-target> --source recent-unwrapped --lines 120
```

`herdr agent get <stored-target>` returns a read-only `agent_session` object when the CLI
integration reported a native session reference; record it as that actor's native session
provenance. A completed `agent start`, `agent prompt --wait` or `agent wait` also returns
`completion_seq`, which identifies a finished turn independently of who has viewed it — use
it to match a reply to the turn that produced it.

For independent parallel reviews, submit to every reviewer (without waiting for the first
review to finish) and then use bounded waits. Never queue a new turn while the actor is
already working: waits do not identify individual turns. Maintain one request ID per
outstanding actor assignment. `blocked` requires user permission/dialog handling; the
controller must not pretend it is the user or blindly send Enter.

Prompt timeout/stall can happen AFTER delivery; inspect actor and matching outbox before
retry. Idle/done are ready-for-input signals, not proof checks passed. Match accepted reply
by task, request, actor and snapshot. Keep original files and copied canonical evidence.

## Fresh session and state monitoring

A Herdr tab keeps its interactive CLI session across tasks, so a retained tab may still hold
a previous task. Before reusing one, inspect it:

```text
herdr agent get <stored-target>                                       # state + agent_session
herdr agent read <stored-target> --source recent-unwrapped --lines 40  # is an old task loaded?
herdr agent list                                                      # every agent's state
```

If it still holds another task's session, reset it with that CLI's own new-session command
(`/new`; some CLIs spell it `/clear`), then wait for it to settle:

```text
herdr agent prompt <stored-target> /new                 # no --wait: a reset produces no turn
herdr agent wait <stored-target> --until idle --timeout <bounded-ms>
```

Confirm the reset with `agent get`/`read` (new `agent_session`, empty composer) and append a
`reset` event before dispatching. Never type `/new` into a tab that is still `working`.

Monitor completion with bounded waits; `--until` may be repeated for several states:

```text
herdr agent wait <stored-target> --until idle --until done --until blocked --timeout <bounded-ms>
```

States are idle, working, blocked, done, unknown. Without `--until`, a wait matches idle, done
or blocked; without `--timeout` it waits indefinitely, so always pass a bounded timeout here.
idle/done mean ready-for-input, NOT that checks passed — match the reply by task, request,
actor, snapshot and `completion_seq`. Use `herdr agent explain` to diagnose a misdetected agent.

## Instruction delivery

First tab loads this skill explicitly via native skill invocation or reads its absolute
SKILL.md path. No custom launcher required. Pass developer/reviewer contracts as assignment
files or fully quoted prompt text. Workers do not need to discover this controller skill.
The developer is the only source writer; reviewer outboxes are writable outside source.

Structured result artifacts are part of this workflow's explicit contract, not a passive
Herdr transcript export. If a TUI read truncates, request the full result in the assigned
outbox; never infer hidden content. Do not rewrite the bundled generic Herdr skill.

## Windows and WSL

Use native Windows Herdr + Windows CLIs for native tasks. In WSL use Linux Herdr + Linux
CLIs and native Linux paths. Install the skill in each controller environment separately.
Do not call Windows CLI wrappers from a Linux task or vice versa as an automatic fallback.
Herdr owns ConPTY/Unix PTY details; no custom terminal driver needed. Helpers require only
stdlib Python 3.10+, but native agent CLIs retain their own runtime requirements.

Keep task records repository-relative; handle spaces with native argv/quoting. Preserve
server/session environment. Never mutate the same task from both Windows and WSL. If the
user wants mixed cross-environment tabs, treat that as a separate unimplemented feature.

## Sources

- https://herdr.dev/docs/agent-automation/
- https://herdr.dev/docs/cli-reference/
- https://herdr.dev/docs/windows-beta/
- https://code.claude.com/docs/en/cli-reference
- https://opencode.ai/docs/cli/
- https://github.com/can1357/oh-my-pi

Live docs may describe features newer than installed help; do not assume them available.
