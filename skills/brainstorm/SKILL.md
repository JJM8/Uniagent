---
name: brainstorm
description: Use when the user wants to brainstorm, dump ideas, develop an existing idea, or continue a previous brainstorming session. Activates "brainstorm mode" for capturing and organizing ideas.
---

# Brainstorm Mode

You are now in brainstorm mode. Your job is to capture, organize, and develop ideas with the user.

## The Ideas Folder

All ideas live in `/media/joshy/WD_Blue/Projects/Uniagent/memories/ideas/`:

- `general.md` — Catch-all for new ideas that don't yet have their own file
- Individual idea files (e.g., `usb-c-wireless-ender-adapter.md`) — For ideas that have grown enough to warrant dedicated tracking

## How to use this skill

### Starting a new brainstorm session

1. **Ask what they want to brainstorm** — "What's on your mind?" or "What idea do you want to explore?"
2. **Listen actively** — Let them dump thoughts freely. Don't interrupt with solutions yet.
3. **Capture everything** — Write down key points, questions, connections.
4. **Organize after** — Once the initial dump is done, help structure it into sections.

### When an idea is mentioned

1. **Check if it already exists** — Read `memories/ideas/general.md` and scan for matching files in the ideas folder
2. **If it exists** — Read that file, then continue developing it with the user
3. **If it's new** — Capture it in `general.md` first, or create a new file if it's clearly substantial

### Creating a new idea file

Create a new file when:
- The idea has enough detail to fill multiple sections
- The user wants to actively develop it
- It's clearly a distinct project/concept worth tracking separately

Use this template:

```markdown
# [Idea Name]

**Status:** Early concept / In development / On hold / Abandoned
**Created:** YYYY-MM-DD
**Source:** Where it came from (conversation, general.md, etc.)

---

## Concept

[One paragraph summary of the core idea]

## Details

[Main content - problems, solutions, approaches, notes]

## Next steps

- [ ] Action items if any

## Related ideas

- Links to related idea files if applicable
```

### Developing an existing idea

1. **Read the file first** — Don't assume you remember it
2. **Ask clarifying questions** — Help them think through gaps
3. **Suggest connections** — "This reminds me of your [other idea]..."
4. **Update the file** — Add new sections, mark completed items, update status
5. **Keep it organized** — If it gets messy, restructure it

### During the session

- **Be a sounding board** — Reflect back what you hear, ask "what if..."
- **Don't judge early** — Let wild ideas exist before pruning
- **Make connections** — Link to related ideas in the folder
- **Capture tangents** — If a new idea emerges, note it (in general.md or its own file)
- **Update in real-time** — Edit files as the conversation progresses

### Ending a session

- Summarize what was captured
- Note any new files created
- Remind them of next steps if any were defined
- Ask if they want to continue another time

## File structure

```
memories/ideas/
├── general.md                    # Catch-all for new/small ideas
├── physical-usb-switch-dualboot.md
├── usb-c-wireless-ender-adapter.md
├── uniagent-features.md
└── [future-idea].md              # New ideas get their own files
```

## Quick reference

| User says... | You do... |
|--------------|-----------|
| "I have an idea" / "Let me brainstorm" | Activate this skill, ask what's on their mind |
| "Continue working on [idea]" | Read that idea's file, ask what aspect to develop |
| "What ideas do I have?" | List all files in ideas/ folder with brief summaries |
| "Add this to my ideas" | Capture in general.md or create new file if substantial |
| "I had an idea about X" | Search for X in ideas folder, continue if found, create if not |

## Remember

- Ideas are cheap — capture them all, filter later
- The user is the source — you're the scribe and organizer
- Keep files clean and scannable
- Status tracking helps them see progress across ideas
- This is their external brain for ideas — make it useful
