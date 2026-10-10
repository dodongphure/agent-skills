# Evaluating a skill

How to test whether a skill triggers when it should and whether it actually helps. The summary lives
in `SKILL.md`; this file holds the mechanics.

## Two questions, tested separately

A skill can fail in two independent ways, and a single test cannot catch both:

1. **Does it trigger?** The agent loads the skill for prompts that need it, and does not load it for
   prompts that don't.
2. **Does it help?** Given the same task, output with the skill is better than output without it.

A skill that triggers perfectly and helps not at all is the worst case: it looks correct in review,
costs context on every request, and changes nothing.

## Test 1 — trigger rate

Write ~20 realistic prompts, labelled by whether they should fire the skill. Aim for 8–10 positives
and 8–10 negatives.

```json
[
  { "query": "turn my incident notes into a runbook", "should_trigger": true },
  { "query": "whats the quickest way to convert json to yaml", "should_trigger": false }
]
```

**Positives** should vary along four axes:

- *Phrasing* — formal, casual, abbreviated, occasional typos.
- *Explicitness* — some name the domain directly, others describe the need without naming it.
- *Detail* — terse prompts alongside context-heavy ones with file paths and backstory.
- *Complexity* — single-step tasks and multi-step workflows where the relevant step is buried.

**Negatives** matter more than positives, and the valuable ones are **near-misses**: prompts that
share keywords with your skill but need something else. "Write a fibonacci function" tests nothing.
"Update the formulas in my Excel budget" is a strong negative for a CSV-analysis skill — same
concept, different task.

Run each prompt **3 times**. Model behaviour is nondeterministic, so record the **trigger rate** —
the fraction of runs where the skill loaded. A prompt that fires 1 time in 3 is a description
problem, not a pass.

Keep a held-out slice you don't tune against, or you will overfit the description to your test set.

## Test 2 — output quality

Run the same task twice, once with the skill and once without. Compare the outputs.

Store cases in `evals/evals.json` inside the skill directory:

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "a realistic user message",
      "expected_output": "a human-readable description of what success looks like",
      "files": ["evals/files/input.csv"]
    }
  ]
}
```

Start with 2–3 cases. Don't invest in a large suite before you've seen the first round of results.

Organise results so each iteration is comparable:

```
example-skill-workspace/
└── iteration-1/
    ├── eval-first-case/
    │   ├── with_skill/     # outputs/, timing.json, grading.json
    │   └── without_skill/  # outputs/, timing.json, grading.json
    └── benchmark.json      # aggregated across cases
```

Start each run from a **clean context** — no state carried over from the previous run or from the
development session.

Grade against explicit **assertions** rather than a general impression. "The output contains a
labelled bar chart" is checkable; "the output is good" is not.

## Reading execution traces, not just outputs

A correct final answer reached by an unproductive route is still a skill defect. Read the trace and
look for:

- **Instructions too vague** — the agent tries several approaches before one works.
- **Instructions that don't apply** — the agent follows them anyway on the wrong task.
- **Too many options, no default** — the agent dithers instead of proceeding.

These are cheap to fix once seen and invisible in the final output alone.

## The loop

1. Run the eval set, with and without the skill.
2. Read the traces, not just the outputs.
3. Feed back **all** results — successes too, not only failures. Ask what triggered false positives,
   what was missed, what could be cut.
4. Change the skill.
5. Re-run. Compare against the previous iteration.

One pass of execute-then-revise noticeably improves a skill; complex domains need several.
