# agent-skills

[![skills.sh](https://skills.sh/b/dodongphure/agent-skills)](https://skills.sh/dodongphure/agent-skills)

Agent skills by [@dodongphure](https://github.com/dodongphure) — one repo, one folder per skill.

## Install

```bash
npx skills add dodongphure/agent-skills --skill <skill-name>
```

Add `-g` to install globally instead of into the current project. To see what this repo offers
before installing:

```bash
npx skills add dodongphure/agent-skills --list
```

## Skills

| Skill | What it does |
|---|---|
| [`skill-authoring-standard`](skills/skill-authoring-standard/) | The authoring standard for agent skills: the rules, a starting template, and a deterministic validator |
| [`herdr-pr-flow`](skills/herdr-pr-flow/) | Skill-first Herdr TUI orchestration with workspace configuration, developer/reviewer tabs, sequential fixes and a local PR-style HTML report; no automatic commits. The report is static by default and can optionally be interactive (opt-in config v2) so the human can comment on diff lines and have the controller read the comments back |

## Layout

```
skills/
└── <skill-name>/
    ├── SKILL.md          # required — frontmatter + instructions
    ├── README.md         # optional — for humans browsing the repo
    ├── assets/           # optional — templates, static data
    ├── scripts/          # optional — deterministic procedures
    └── references/       # optional — long docs, loaded on demand
```

Each skill is self-contained: everything it needs sits inside its own folder, so a skill can be
moved, vendored, or removed without touching the others.

## Adding a skill

1. Create `skills/<skill-name>/SKILL.md` with `name` and `description` in the frontmatter.
2. Follow [`skill-authoring-standard`](skills/skill-authoring-standard/), then validate:
   ```bash
   python skills/skill-authoring-standard/scripts/validate_skill.py skills/<skill-name>
   ```
3. Add a row to the table above.

## License

MIT — see the `LICENSE` file inside each skill folder.
