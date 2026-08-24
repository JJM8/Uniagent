---
name: create-skill
description: How to add a new Claude-format skill under skills/ in this project. Use when the user asks to create, add, or write a new skill.
---

# Creating a Skill

A skill here is knowledge, not code - just a `SKILL.md` file. There's nothing
to run: `read_skill` hands the file's body straight to the model, and that
markdown body becomes the instructions the agent follows.

## Steps

1. Make a folder under `skills/`, named for the skill in kebab-case, e.g.
   `skills/my-new-skill/`.
2. Inside it, create `SKILL.md` with YAML front matter followed by the
   instructions:

   ```markdown
   ---
   name: my-new-skill
   description: One line saying what this covers and when to use it. Start with "How to ..." or "Use when ...".
   ---

   # Title

   Instructions go here: commands to run, steps to follow, things to watch
   out for. Write it the way you'd brief someone who has no other context.
   ```

3. That's it - no registration step. `tool_processor.find_skills()` scans
   `skills/` fresh on every call and picks up any `SKILL.md` with front
   matter and a `description`. Skills live in `skills/`, NOT in `tools/` -
   `tools/` is only for .py tools, which are a different thing (code to run,
   with an argument schema).

## Rules that matter

- **`description` is required.** Without it the file is silently skipped -
  it won't show up in the skills list at all.
- **The folder name is the default `name`.** If the file is literally named
  `SKILL.md`, its parent folder's name is used unless `name:` in the front
  matter overrides it. Keep them in sync to avoid confusion.
- **Description is what triggers use.** The model only sees the name and
  description in the skills list; it has to decide from that alone whether to
  read the skill. Write it like `pdf-notes` or `frontend-design` do:
  say what the skill covers and name the situations ("Use when...") that
  should trigger it.
- **The body is the whole payload.** There's no separate "run" behavior -
  whatever the body says is exactly what the agent will do. Keep it
  concrete: real commands, real file paths, not vague advice.
- **Files starting with `_` are ignored**, so never prefix a skill folder or
  its `SKILL.md` with an underscore.

## Example skills already in this project

- `skills/pdf-notes/SKILL.md` - short and single-purpose (one command to
  remember).
- `skills/frontend-design/SKILL.md` - long-form guidance, no commands at all.
- `skills/open_claude_code/SKILL.md` - numbered steps with shell commands.

Look at whichever is closest to what's being asked for and match its length
and style rather than always writing a long one.
