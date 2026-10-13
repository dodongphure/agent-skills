# Workflow configuration and first-use consent

Use `<project>/.agent-pr/workflow.json`. It is not committed to the repository by
default, but the workflow cannot run without it: when it is absent this skill must
stop and ask the user to configure it before doing anything. Resolve the project
explicitly; never search all home directories or choose another workspace's
configuration. A user-specified path must be approved and recorded. There is no
inherited global default in v1.

## Absent file

The file is not committed by default, yet it is required to run. When it is missing,
ask before creating any task files, launching agents or editing source:

> This workspace has no `.agent-pr/workflow.json`. Configure the controller,
> developer, at least one reviewer, and project checks before this workflow runs.
> Shall I create a draft from the included template for you to review?

Wait. If consent is granted, collect CLI/model choices for each role and project checks.
The template has null models/checks intentionally. Never replace them with plausible
model IDs or fabricated test commands. Request confirmation of completed values before
writing an active config. Configuration assistance does not authorize implementation.

## Schema version 1 (strict keys)

- `schema_version`: exactly 1.
- `roles.controller`, `roles.developer`: objects with `cli`, `model`, `extra_args`.
- `roles.reviewers`: nonempty array; each has unique `id`, `cli`, `model`,
  `extra_args`, `focus` (array of strings). IDs use lowercase letters/digits/hyphens;
  reserve controller and developer; do not use numeric tab indices for identity.
- `cli`: claude, opencode, omp or codex in the initial adapter set. Verify current
  Herdr kind support and native CLI help; this list can be extended deliberately.
- `model`: nonempty string, never null, whitespace, angle-bracket placeholder or
  shell metacharacter payload. Use the native model selector; OpenCode and OMP use
  exact provider/model IDs. Claude aliases are permitted but not immutable IDs.
- `extra_args`: argv string array; no shell command string, model override, --print,
  permission bypass, nested background session, worktree creation or provider secrets.
  V1 permits only --no-lsp and --hide-thinking; all other extra flags require a deliberate
  audited extension. Still verify flag support on that CLI. Default to empty; --no-lsp
  is useful for OMP reviews. This does not establish project command safety.
- `workflow.max_fix_attempts_per_finding`, `max_task_minutes`, `wait_timeout_ms`:
  positive integers (bool is not an integer). Suggested 3, 120, 300000 respectively.
  `wait_timeout_ms` bounds each `herdr agent prompt --wait --timeout`. On Herdr 0.9.0 that
  flag accepts any value and `agent wait` waits indefinitely when omitted; only
  `agent start` caps at 300000 ms (default 30000). 300000 is a conservative default that
  also stays within the `agent start` ceiling.
- `workflow.model_diversity`: warn or require_distinct. Distinctness uses known actual
  model provenance, not CLI names. Under require_distinct, unverified identities block.
- `verification.required_checks`: nonempty array of objects `{id, argv}` with unique IDs
  and nonempty argv arrays. Examples depend on the project; ask the user. The helper
  NEVER runs these commands. Only execute reviewed commands within the task scope.
  Use native executable paths/arguments appropriate to Windows or WSL. No shell operators,
  pipe chains, secret arguments or git mutation commands; such operations need separate
  explicit authorization and must not be hidden inside workflow checks.
- `safety.allow_stage`, `allow_commit`, `allow_push`, `allow_merge`: all exactly false.
  Any true flag invalidates this workflow; it does not grant permission to commit.

Unknown keys fail validation to catch typos. All configured reviewers are required;
there is no approval quorum toggle or silently disabled reviewer. No credentials in JSON.

## Config changes

Freeze the approved JSON under the task as `workflow.resolved.json`. New settings apply to
new tasks; a current controller's config model does not remotely change its live model.
Ask before switching the running model. Keep machine-specific paths in separately approved
configs, not automatic cross-environment overlays. Use forward slashes where accepted and
native APIs for resolution; do not translate C:/ paths to /mnt/c by guesswork.

## Optional read-only helper

`check_config.py --workspace <project>` checks only `.agent-pr/workflow.json` and returns
JSON. Exit 3 CONFIG_REQUIRED means the file is absent, so stop and ask the user to
configure it; exit 2 INVALID_CONFIG means repair with consent; exit 0 CONFIG_VALID means
structural checks passed. It does not create files,
inspect live Herdr, validate native auth, execute checks or load skill instructions.
`--config <explicit-path>` supports an already authorized alternate path.
