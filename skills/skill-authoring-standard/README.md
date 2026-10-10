# skill-authoring-standard

An opinionated authoring standard for agent skills — the rules, a starting template, and a
deterministic validator.

Most skills fail for one of two reasons: the `description` explains what the underlying tool *is*
instead of when to use the skill (so it never fires), or `SKILL.md` grows into a document that is
loaded on every request and quietly degrades output. This skill exists to prevent both.

## Install

```bash
npx skills add dodongphure/agent-skills --skill skill-authoring-standard
```

This skill lives at `skills/skill-authoring-standard/` inside the
[dodongphure/agent-skills](https://github.com/dodongphure/agent-skills) monorepo, so `--skill` selects it by name.
Add `-g` to install it globally instead of into the current project.

Confirm the repo is discoverable before you rely on it:

```bash
npx skills add dodongphure/agent-skills --list
```

If that reports no skills, check that `SKILL.md` sits in a directory under `skills/` and its
frontmatter has both `name` and `description`.

## Contents

| Path | Purpose |
|---|---|
| `SKILL.md` | The rules, the authoring procedure, the pre-flight self-check, and the gotchas |
| `assets/skill-template.md` | Copy this to start a new skill with the correct shape |
| `scripts/validate_skill.py` | Deterministic checker for the mechanical rules |

## What it enforces

- The `description` is the **trigger** — written around user intent, imperative, and stating **when
  not to** use the skill. Hard limit: 1024 characters.
- `SKILL.md` stays **under 500 lines**. Bulk material moves to `references/`.
- Repeatable procedures go in `scripts/` (determinism); templates and static data go in `assets/`.
- A **gotchas / what-not-to-do** section is mandatory — it is the highest-value section and the one
  that changes most over time.
- Only information the agent does not already know. No restating general knowledge.
- Skills are **refined over time** — diff what the skill produced against what you actually wanted,
  and promote the recurring differences into rules. The pattern can be automated with a second skill
  whose only job is to refine the first.

## The validator

```bash
python scripts/validate_skill.py <path/to/skill-directory>
```

It checks that the frontmatter parses as valid YAML (an unquoted `:` inside a value breaks it),
`name`/directory agreement, description length and trigger phrasing, the 500-line budget, the
presence of a gotchas section, and that every relative link in `SKILL.md` resolves. Exit code `1`
if any error is found.

```
Validating skill: my-skill
------------------------------------------------------------
  OK    all checks passed
------------------------------------------------------------
0 error(s), 0 warning(s)
```

## Safety

`scripts/validate_skill.py` is **read-only**. It imports only the Python standard library
(`os`, `re`, `sys`), makes no network calls, spawns no subprocesses, and writes nothing — it reads
the target skill directory and prints a report. As with any script you install, read it before you
run it.

Requires Python 3.8+ for the validator. The rules themselves are markdown-only.

## License

MIT — see [LICENSE](LICENSE).
