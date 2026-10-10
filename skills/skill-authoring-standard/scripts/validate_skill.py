#!/usr/bin/env python3
"""Validate a skill directory against the authoring standard.

Deterministic checker for the mechanical rules: frontmatter is valid YAML and
has the right shape, description trigger quality and length, the 500-line
budget, presence of a gotchas section, and that every relative link in
SKILL.md resolves.

Usage:
    python validate_skill.py <skill-directory | path/to/SKILL.md>

Exit code 0 = all checks passed (warnings allowed).
Exit code 1 = at least one error.
"""

from __future__ import annotations

import os
import re
import sys

DESC_LIMIT = 1024
NAME_LIMIT = 64
LINE_BUDGET = 500

# ---------------------------------------------------------------- frontmatter

def parse_frontmatter(text: str):
    """Return (frontmatter_dict, body, error). Minimal YAML subset:
    simple `key: value` pairs plus indented continuation / block scalars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text, "SKILL.md does not start with a '---' frontmatter block"

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text, "frontmatter block is never closed with '---'"

    fm_lines = lines[1:end]
    body = "\n".join(lines[end + 1:])

    data: dict[str, str] = {}
    key = None
    for raw in fm_lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^\s", raw) and key:
            data[key] = (data[key] + " " + raw.strip()).strip()
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|", ">-", "|-"):
            val = ""
        val = val.strip().strip('"').strip("'")
        data[key] = val
    return data, body, None


# YAML indicators that are unsafe at the start of an unquoted scalar value.
# '{' and '[' are excluded: they start valid flow collections.
YAML_LEAD = ("*", "&", "!", "%", "@", "`", "?", "- ")

# Fields whose value is expected to be a nested block, not a scalar.
NESTED_FIELDS = {"metadata"}


def raw_frontmatter_lines(text: str):
    """Return the raw lines between the opening and closing '---', or None."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return None


def check_yaml_safety(fm_lines):
    """Catch values that satisfy a regex but are invalid YAML.

    The most common failure by far is an unquoted ': ' inside a description.
    A naive parser reads it as one string; a real YAML parser reads the colon
    as the start of a nested mapping and rejects the whole file. Strict
    validators (the official skills-ref among them) then refuse the skill.
    """
    errors, warnings = [], []
    for n, raw in enumerate(fm_lines, start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if not val or val in (">", "|", ">-", "|-", ">+", "|+"):
            continue
        if val[0] in ('"', "'"):
            continue
        if key in NESTED_FIELDS:
            continue
        if re.search(r":\s", val) or val.endswith(":"):
            errors.append(
                f"line {n}: unquoted ':' inside the '{key}' value - YAML reads it as a "
                "nested mapping and strict parsers reject the file. Reword the value, "
                "or wrap it in quotes"
            )
        elif val.startswith(YAML_LEAD):
            warnings.append(
                f"line {n}: '{key}' value starts with the YAML indicator "
                f"'{val[:2].strip()}' - quote the value"
            )
        if re.search(r"\s#", val):
            warnings.append(
                f"line {n}: unquoted ' #' inside the '{key}' value starts a YAML "
                "comment - quote the value"
            )
    return errors, warnings


# ------------------------------------------------------------------- checks

def check_relative_links(body: str, skill_dir: str):
    """Return (errors, warnings) for markdown links that should resolve on disk."""
    errors, warnings = [], []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", body):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        clean = target.split("#")[0].strip()
        if not clean:
            continue
        if os.path.isabs(clean):
            warnings.append(f"absolute path in a link, prefer relative: {target}")
            continue
        if not os.path.exists(os.path.join(skill_dir, clean)):
            errors.append(f"relative link does not resolve: {target}")
        depth = clean.replace("\\", "/").count("/")
        if depth > 1:
            warnings.append(
                f"deeply nested reference link ({depth} levels): {target} "
                "- keep references flat and top-level"
            )
    return errors, warnings


def validate(path: str):
    errors, warnings = [], []

    if os.path.isdir(path):
        skill_dir = path
        skill_md = os.path.join(path, "SKILL.md")
    else:
        skill_md = path
        skill_dir = os.path.dirname(os.path.abspath(path)) or "."

    dir_name = os.path.basename(os.path.abspath(skill_dir))

    if not os.path.isfile(skill_md):
        return [f"SKILL.md not found at {skill_md}"], [], dir_name

    with open(skill_md, encoding="utf-8") as fh:
        text = fh.read()

    # --- frontmatter must be valid YAML, not merely regex-shaped ---
    fm_lines = raw_frontmatter_lines(text)
    if fm_lines is not None:
        yaml_errors, yaml_warnings = check_yaml_safety(fm_lines)
        errors.extend(yaml_errors)
        warnings.extend(yaml_warnings)

    fm, body, err = parse_frontmatter(text)
    if err:
        return errors + [err], warnings, dir_name

    # --- name ---
    name = fm.get("name", "")
    if not name:
        errors.append("frontmatter is missing the required field: name")
    else:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            errors.append(f"name is not kebab-case: '{name}'")
        if len(name) > NAME_LIMIT:
            errors.append(
                f"name is {len(name)} chars, over the {NAME_LIMIT}-character limit"
            )
        if name != dir_name:
            warnings.append(
                f"name '{name}' does not match the directory name '{dir_name}'"
            )

    # --- description ---
    desc = fm.get("description", "")
    if not desc:
        errors.append("frontmatter is missing the required field: description")
    else:
        n = len(desc)
        if n > DESC_LIMIT:
            errors.append(f"description is {n} chars, over the {DESC_LIMIT} limit")
        elif n > DESC_LIMIT * 0.95:
            warnings.append(f"description is {n} chars, very close to the {DESC_LIMIT} limit")

        low = desc.lower()
        trigger = any(k in low for k in ("use when", "use this", "use for", "use whenever"))
        if not trigger:
            errors.append(
                "description has no trigger phrasing ('Use when ...') - "
                "describe user intent, not what the tool IS"
            )
        negative = any(
            k in low for k in ("do not use", "don't use", "not for", "avoid using this when")
        )
        if not negative:
            warnings.append(
                "description does not say when NOT to use this skill "
                "(the 'impeccable' example includes one)"
            )

    # --- line budget ---
    body_lines = len([l for l in body.splitlines() if l.strip()])
    if body_lines > LINE_BUDGET:
        errors.append(
            f"SKILL.md body is {body_lines} non-blank lines, over the {LINE_BUDGET} budget "
            "- move bulk into references/"
        )
    elif body_lines > LINE_BUDGET * 0.8:
        warnings.append(f"SKILL.md body is {body_lines} lines, approaching the {LINE_BUDGET} budget")

    # --- gotchas ---
    headings = re.findall(r"^#{1,6}\s+(.*)$", body, flags=re.MULTILINE)
    if not any(
        re.search(r"gotcha|what not to do|common mistake|pitfall|anti-pattern|avoid",
                  h, re.IGNORECASE)
        for h in headings
    ):
        errors.append(
            "no gotchas / what-not-to-do section found - it is the most valuable section"
        )

    # --- links ---
    link_errors, link_warnings = check_relative_links(body, skill_dir)
    errors.extend(link_errors)
    warnings.extend(link_warnings)

    return errors, warnings, dir_name


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2

    errors, warnings, dir_name = validate(argv[1])

    print(f"Validating skill: {dir_name}")
    print("-" * 60)
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    if not errors and not warnings:
        print("  OK    all checks passed")
    print("-" * 60)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
