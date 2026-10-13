# Local PR report contract

Controller is the sole canonical author. Actors submit results to separate assigned outboxes.
Append their original comments and source/model provenance to the event journal; retain edits,
fix deltas, verification attempts and disagreement. HTML is a derived view, not the database.

`report.json` requires schema_version 1, task_id, title, status, requirements (strings), plan
(string), participants, snapshots, final_snapshot_id (null until final), findings, approvals,
checks, changes, timeline. Empty arrays are allowed before execution. Status values:
PLANNED, IMPLEMENTING, REVIEWING, FIXING, VERIFYING, BLOCKED, READY_FOR_HUMAN.

Use `assets/report.example.json` as a structural example, not a real approval/test record.
Copy and clear its mock data before real use. The rendered example prominently says MOCK.

## Participant

`{id, role, cli, requested_model, observed_model, model_provenance}`.
role controller/developer/reviewer; observed_model null if unavailable. Do not infer actual
backend from a tab title or self-claim. Show requested label and unverified actual identity.
Store native session/tab/pane and timestamps in state/events; summarize replacements in timeline.
Under strict model diversity, unavailable identity blocks even if render schema accepts it.

## Snapshot

`{id, manifest_sha256}`; hash is actual 64-hex digest from scoped file manifest.
Readable S1/S2 labels are not commit hashes. The renderer does not verify source files itself.

## Finding

`{id, owner, severity, path, line, snapshot_id, body, acceptance, status, history}`.
owner is known reviewer participant; severity high/medium/low; status open/awaiting_verification/resolved.
history entries: `{actor, snapshot_id, kind, body, verdict, diff?}`.
kind fix/verification/comment; verification verdict addressed/not_addressed, others null.
Retain all history. The last owner verification must be addressed before resolved; developer
self-approval and wrong-owner verification fail. Later changes still need final full reviews.

## Approval

`{reviewer, snapshot_id, verdict, body}`. verdict approved/changes_requested/cannot_verify.
Append chronologically. Each reviewer's latest approval must approve the SAME final_snapshot_id.
List every configured reviewer participant; omitting one from report must never reduce quorum.
Controller compares report participants against frozen config before rendering.

## Check

`{id, snapshot_id, argv, exit_code, output, required}`. argv is actual command array;
exit_code null if not run. Include timestamps/cwd in output or event evidence.
Each required check's latest evidence must be on final snapshot with exit code 0 for READY.
At least one required check. Controller compares IDs/commands to approved config. Evidence
from actors is not independently true merely because JSON has exit_code 0.

## Changes and timeline

Changes: `{path,diff}` for complete cumulative baseline→candidate text differences.
Finding history includes per-fix deltas. Timeline: `{actor,kind,body,snapshot_id}` with null
snapshot before capture. Include request/session/model changes and pending human actions.
Do not truncate code silently; report binary files with hashes, size and explicit omission.

## Render

`<python> <skill>/scripts/render_report.py --task-dir <project>/.agent-pr/tasks/<id>`

For READY output, the helper requires workflow.resolved.json and compares the exact configured
reviewer IDs and required check IDs/argv to the report; omission cannot shrink the gate.
It also applies require_distinct to available observed model labels. Controller must still
verify provenance and compare live role identities against configuration, not self-claims.
The helper validates records, rejects obvious unsafe task paths/symlinks, escapes comments
and diffs, and writes only task-local report.html via atomic sibling replacement. No script,
network, CDN, server, Git operation or task dispatch. No package dependencies. On unavailable
Python use equivalent escaped static HTML from assets/report-template.html and manually enforce
all gates. Never interpolate actor-produced HTML directly into markup.

READY requires records consistent with configured actors/checks AND actual live final hashes,
authorized boundaries, model-diversity policy and real evidence. Record validator can reject
many stale/self-approved states; it cannot prove model honesty, permissions or the absence
of unlisted source changes. State these limits, never advertise certification from a mock.
