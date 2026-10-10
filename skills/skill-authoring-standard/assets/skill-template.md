---
name: <kebab-case-skill-name>
description: Use when <the user's intent, in their words — the scenario that should trigger this skill>. <What it does, one clause.> Covers <the specific cases>. Do not use for <the nearest scenario this must NOT fire in>, or for <adjacent non-goal>.
license: <your-license>
agent_created: true
---

# <Skill Title>

<One or two sentences: what this skill is for and the single most important idea behind it.>

Deep detail lives in [references/<topic>.md](references/<topic>.md) — read it when <the specific
condition that makes it relevant>.

## When to use

- <Triggering scenario 1, phrased the way the user would actually ask>
- <Triggering scenario 2>

**Not for:** <the nearest thing that looks similar but should not trigger this skill>.

## Procedure

### 1. <First concrete step>
<Imperative instruction. Only what the agent does not already know.>

### 2. <Second step>
<...>

### 3. Validate
```bash
<exact command>
```

## Where things live

| Content | Location |
|---|---|
| <always-needed content> | `SKILL.md` |
| <bulk docs / schemas> | `references/` |
| <repeatable deterministic steps> | `scripts/` |
| <templates / static data> | `assets/` |

## Gotchas — what not to do

- **<Failure the user actually hit>.** <What to do instead.>
- **<Second failure>.** <What to do instead.>

<!--
Authoring checklist before finishing (see the skill-authoring-standard skill):
  [ ] description states when to use AND when not to use, and is under 1024 chars
  [ ] description is written around user intent, not a description of what the tool IS
  [ ] this file is under 500 lines; anything bulkier moved to references/
  [ ] gotchas section is populated from real failures, not guesses
  [ ] every relative link above resolves to a file that exists
  [ ] no restatement of general knowledge the agent already has
  [ ] agent_created: true is present
  [ ] no unquoted ':' inside a frontmatter value (breaks YAML - quote it or reword)
  [ ] ran: python scripts/validate_skill.py <this-skill-dir>
-->
