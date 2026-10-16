# Controller efficiency and timing

## Keep decisions small, not safeguards

For straightforward work, the existing plan needs only the goal, authorized paths,
steps and required checks. Reuse that plan; do not ask another model to plan or generate
a report. Replan when requirements, scope or new evidence invalidates the approach;
complex or uncertain tasks still need analysis. Plan-only authorization stays plan-only.
Read references at the relevant phase, not every reference before each dispatch.

Reuse the verified interpreter path, loaded Herdr guidance and verified CLI syntax within
the same unchanged environment/session. After recovery reconcile state, pending requests,
configuration and identities; after a version/environment change revalidate capabilities.
Caching syntax is not permission to skip config, session reset, provenance or source gates.

Use the existing snapshot and renderer helpers rather than generating temporary replacements.
Keep every accepted receipt durable and keep the current render-after-accepted-replies and
final-handoff checkpoints. Do not remove validation or switch report modes for speed.
A combined bookkeeping helper is deferred until measurements justify its complexity.

## Reviewer fan-out

For initial reviews and final approvals on one frozen candidate:
1. Pause developer writes and prepare each isolated review input and outbox.
2. Persist each dispatch event and send each reviewer's prompt without `--wait`.
3. Only after ALL configured reviewers are dispatched, collect with bounded waits.
4. Validate each matching reply; all reviewers remain required. Keep independent initial
   findings private until all arrive. A timed-out dispatch needs reconciliation, not retry.

Do not fan out sequential fixes or owner rechecks across changing snapshots. If concurrent
submission is unsupported, record that limitation and collect sequentially without claiming
parallel execution. No hidden subagent team, extra daemon or alternative terminal driver.

## Measure commands without adding model turns

After configuration/authorization gates and creation of the task directory, the controller
can wrap an ALREADY APPROVED, finite command with the bundled standard-library helper:

```text
<python> <skill>/scripts/measure.py --task-dir <task> --category render -- <python> <skill>/scripts/render_report.py --task-dir <task>
<python> <skill>/scripts/measure.py --task-dir <task> --category snapshot -- <python> <skill>/scripts/snapshot.py --workspace <project> --task-dir <task> --name <new-label> --paths-json <task>/paths.json
<python> <skill>/scripts/measure.py --task-dir <task> --summary
```

Categories: `preflight`, `agent_wait`, `snapshot`, `bookkeeping`, `render`, `check`.
Wrap existing bounded Herdr waits, not an unbounded interactive CLI or live report server.
The helper invokes argv directly with no shell, inherits cwd/environment and command output,
and returns the child's exit code (127 if launch failed). Use absolute executables where
possible. It does not validate command safety, supply permissions, set a child timeout,
launch actors by itself, retry commands or modify canonical records. Only the controller
runs it; never wrap a command denied by the host or pass secrets as command arguments.

Diagnostics append to `evidence/timings.jsonl`; no argv, prompts or command output is stored
there. The log is not check evidence, an event journal, an approval or a READY requirement.
If logging fails after a child ran, reconcile actual command effects before retrying.
Do not run several timer writers against the same task log concurrently.

Each row has category, OS-derived UTC start/end, monotonic elapsed seconds, exit code and
outcome (`exited` or `launch_error`). Interrupted/killed helpers may leave no row; report
missing coverage rather than invent durations. Summary skips malformed JSON lines and
reports `unreadable_lines`; structurally invalid records still fail validation. Summary
reads do not create files. Link guards cover task metadata; junction detection is available
on Python 3.12+, not an enforcement sandbox on older supported interpreters.

Timing command execution does NOT measure the controller's model thinking. Use host-provided
turn timing when available, or describe the gap between accepted events as an unclassified
wall-clock interval (thinking, human delay, tool overhead and scheduling can all contribute).
Do not equate sequential wait durations with reviewer compute: parallel workers can already
be running before a wait starts. Category sums may overlap and are not task wall time.

Compare similarly scoped runs with the same models, checks, report mode and environment.
Report task wall time, available controller-turn time, command category totals, render count
and measurement coverage separately. Do not claim a speedup from a mock or a single run.
Optimize the measured bottleneck next; changing render frequency or task-record ownership
requires a separate reviewed design and regression tests.
