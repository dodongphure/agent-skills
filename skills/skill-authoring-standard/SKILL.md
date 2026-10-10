---
name: skill-authoring-standard
description: Use when creating, authoring, writing, drafting, restructuring, or refining an agent skill — a SKILL.md file or skill package — including requests phrased as "make a skill", "create a skill", "turn this into a skill", "write me a skill for X", or "improve / rewrite this skill". Enforces a strict authoring standard for agent skills — keep SKILL.md under 500 lines, write the description around user intent with explicit trigger phrasing and a "when NOT to use" clause, move bulk into references/, put deterministic procedures in scripts/ and templates in assets/, always include a gotchas / what-not-to-do section, evaluate the skill with and without on a real task, and refine it from diffs over time. Do not use when merely consuming, invoking, installing, or debugging an existing skill, or for non-skill documentation such as READMEs or runbooks.
compatibility: Markdown-only for the rules. The optional scripts/validate_skill.py requires Python 3.8+ and is read-only.
license: MIT
agent_created: true
---

# Skill Authoring Standard

The single most important idea: **a skill is not written once.** Draft one is always bad. You
refine it over time from real usage. Author with that assumption.

The second most important idea: **the description is the trigger.** A description that explains what
the underlying tool *is* will never fire automatically. Compare:

- ❌ "Powerful asynchronous state management and data fetching for React, Vue and Angular." — reads like a product blurb; says nothing about when to reach for the skill.
- ✅ "Use when the user wants to build or refactor a UI. Covers layout, spacing, colour and typography. Do not use for backend-only or non-UI work." — opens with the trigger and rules out the wrong scenario.

## Non-negotiables

Check every one of these before calling a skill finished. The validator in
`scripts/validate_skill.py` enforces the mechanical ones automatically.

| # | Rule | Why |
|---|------|-----|
| 1 | `name` and `description` in frontmatter are the **only required fields** | They are what the agent reads to decide whether to load the skill |
| 2 | The description is written around **user intent**, in **imperative** phrasing | It is the trigger. Describing what the underlying tool *is* means the skill never fires automatically |
| 3 | The description states **when to use it AND when NOT to** | Prevents the skill being picked up in the wrong scenario |
| 4 | The description stays **under 1024 characters** | It loads for **every** skill, on **every** request |
| 5 | `SKILL.md` stays **under 500 lines** | The whole file loads on every request that uses the skill — bigger means more cost and **worse** results |
| 6 | Bulk material lives in `references/`, not `SKILL.md` | Progressive disclosure: references load only when needed |
| 7 | Repeatable procedures live in `scripts/` | Buys **determinism** — known input, known output — which AI otherwise lacks |
| 8 | Templates and static data live in `assets/` | Templates keep non-deterministic output in a structured format |
| 9 | A **gotchas / what-not-to-do** section exists | The highest-value section, and the one that changes most over time |
| 10 | Include **only information the agent does not already know** | A general model already knows what common tools and formats are. Give it your specifics instead |
| 11 | Include any bookkeeping field your harness requires | Some agents need a flag to keep the skill editable later; other agents ignore unknown fields |
| 12 | The skill has been run **with and without** it on a real task | A skill that triggers but does not help is invisible to review — only a baseline comparison catches it |

Optional frontmatter fields, portable across agents: `license`, `compatibility` (declare environment
requirements), `metadata` (arbitrary key/value — a good place for your version), and `allowed-tools`
(experimental; restricts what the skill may run, to limit its impact — support varies by agent).

## Procedure

### 1. Confirm it should be a skill at all
A skill is warranted when the knowledge is **reusable**, **specific to this user's scenario**, and
not something a general model already knows. If it is a one-off task, just do the task.

### 2. Extract the user's own expertise — do not invent it
The model has general knowledge; the person asking has the specific knowledge about this scenario.
That asymmetry is the entire reason the skill exists, so the specifics must come from them.

Ask for, or dig out of the workspace:
- exactly what the user wants, in their words;
- **why the AI got it wrong in previous attempts** (the richest source of gotchas);
- their docs, existing examples, or past outputs to synthesise from;
- any house conventions, formats, paths, or naming rules.

Never fill these gaps with plausible-sounding invention. A skill full of guessed conventions is
worse than no skill.

### 3. Write the description first — it is the trigger
Do this before the body. Use the pattern `Use when …` / cover what it does, when to use it, and
**when not to**. Check the character count. This is the highest-leverage sentence in the whole
skill.

### 4. Draft the body around the gotchas
Structure that works: purpose → when to use → the procedure → **gotchas / what-not-to-do** →
checklists for multi-step work. Put the failures the user actually experienced in the gotchas
section; that is where the value concentrates.

### 5. Route each piece of content to the right place
See the table below. Do not dump everything into `SKILL.md`.

### 6. Rewrite the draft by hand
Read the generated draft with a fine-tooth comb and modify it drastically, or start again. Expect
draft one to be poor. Deleting generic filler is usually most of the work.

### 7. Validate
Run the deterministic checker, then read the skill once more as if you were a fresh agent with no
context:

```bash
python scripts/validate_skill.py <path/to/skill-directory>
```

It verifies that the frontmatter is valid YAML (an unquoted `:` inside a value silently breaks
parsing), name shape, length and directory agreement, description length and trigger phrasing, the
500-line budget, presence of a gotchas section, and that every relative link in `SKILL.md` resolves.

### 8. Evaluate — with and without the skill
Never ship a skill you have only "tried once." Run the same realistic task twice — once with the
skill installed, once without — and compare the outputs. If the skill did not change the outcome, it
is not earning the context it costs.

Two things to measure, because they fail differently:

- **Does it trigger?** Write ~20 realistic prompts — 8–10 that should fire the skill and 8–10
  near-misses that should not. Run each 3× (model behaviour is nondeterministic) and record the
  trigger rate. The near-misses matter most: a prompt that shares keywords but needs something else
  is what proves the description is precise rather than merely broad.
- **Does it help?** Compare output quality with and without the skill, not just whether it ran.

Every failure is one of three: it **does not trigger when it should**; it **triggers when it
shouldn't**; it **triggers but does not help**. The third is invisible to review and the most common.

Full mechanics — eval files, assertions, grading, the iteration loop — are in
[references/evaluating-skills.md](references/evaluating-skills.md).

### 9. Refine forever — and automate the loop
A skill that has never been refined is still a draft. There are two levels.

**Manually.** When the skill underperforms, do not patch blindly. Keep the original output and your
hand-edited version side by side, diff them, and promote the **recurring** differences into rules —
most belong in the gotchas section. Discard one-offs; they are noise.

**Automated.** The higher-leverage version is a **second skill whose only job is to refine the
first**. It:

1. reads the original AI output and the hand-edited version of that same output;
2. works out what you changed, and why;
3. separates genuine rules from one-off edits;
4. writes those rules back into the first skill.

That converts every correction you make into a permanent improvement, instead of a fix you have to
repeat every time. This is the highest-value step in the whole standard, and the one most often
skipped.

## Before you finish — self-check

The validator covers the mechanical rules. These are the judgement calls it cannot make — read the
finished skill once more and confirm each one:

- [ ] The description says what the skill is **for**, not what the underlying tool **is**
- [ ] Every gotcha came from a **real failure**, not from imagination
- [ ] Nothing in the body is general knowledge the agent already has
- [ ] The body reads as **instructions for an agent**, not a tutorial for a human
- [ ] A reader with zero context could follow the procedure without asking a question
- [ ] The skill has been **used once on a real task** and refined from that experience
- [ ] The skill was run **with and without** on that task, and the comparison showed a difference

That last box is the one that matters most, and the one most often skipped.

## Where each piece of content lives

| Content | Goes in | Loaded |
|---|---|---|
| Trigger description, core procedure, gotchas | `SKILL.md` | Every request that uses the skill |
| Long docs, schemas, API references, deep rationale | `references/*.md` | Only when the agent needs it |
| Anything repeatable that must be deterministic | `scripts/` | Executed, not necessarily read |
| Templates, boilerplate, lookup tables, static data | `assets/` | Used in the output |

Link to reference files with **normal Markdown links, kept at the top level** of `SKILL.md` — no
deeply nested structures, so the agent can find them. Reference files need no frontmatter.

## Files in this skill

- [assets/skill-template.md](assets/skill-template.md) — copy this to start a new skill with the correct shape
- [references/evaluating-skills.md](references/evaluating-skills.md) — how to test whether a skill triggers and whether it helps
- `scripts/validate_skill.py` — deterministic checker for everything mechanical above

## Gotchas — what not to do

- **Do not describe what a tool IS in the description.** This is the single most common failure.
  A description that reads like a product blurb means the agent will never auto-select the skill.
- **Do not put an unquoted `:` inside a frontmatter value.** `description: Handles X: does Y` is
  invalid YAML — a parser reads that colon as the start of a nested mapping and rejects the whole
  file, so the skill never loads in any strict client. It looks perfectly fine in a text editor,
  which is why it survives review; it only surfaces when someone else's tooling parses your file.
  Reword the value, or wrap it in quotes.
- **Do not write the body as a tutorial for a human.** It is instructions for an agent. Skip
  preamble, motivation, and "in this section we will…".
- **Do not restate general knowledge.** Explaining what a PDF, HTTP, or React is wastes the context
  budget on every single request.
- **Do not let `SKILL.md` grow past 500 lines "just this once."** Move the detail to `references/`.
- **Do not nest reference folders deeply.** Flat and top-level is findable; nested is not.
- **Do not write a skill for a task you have never done.** Without hands-on experience the gotchas
  section — the most important part — will be empty or fabricated.
- **Do not stop at draft one.** A skill that has never been refined is still a draft.
- **Do not ship a skill that triggers but does not help.** It looks correct in review, costs context
  on every request, and changes nothing. Only a with-and-without comparison catches it.
- **Do not duplicate content** between `SKILL.md` and a reference file. It lives in one place.
