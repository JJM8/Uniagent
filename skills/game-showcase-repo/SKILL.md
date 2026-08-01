---
name: game-showcase-repo
description: How to structure a public GitHub repo for a game you plan to sell commercially. Use when setting up a repo that needs to show enough to interest people without exposing the full source code.
---

# Game Showcase Repo

When you want a public GitHub presence for a game you intend to sell, you need to show enough that people understand what it is and get excited, without giving away the whole codebase. This is how you set that up.

## What goes in

| What | Include? | Why |
|------|----------|-----|
| README.md | Yes | Main pitch. Describe the game, features, current state, screenshots |
| Scripts/ | Selective | Include your best, most interesting scripts to show competence |
| Images/ | Yes | Screenshots of gameplay, editor views, concept art |
| Videos/ | Optional | Embed a YouTube or GIF highlight in the README if you have one |
| Full Unity project | No | That's your source IP. Keep it local |
| Large art assets | No | They bloat the repo and you probably don't own all the licenses |

## The README

Write it like a person, not a product page. Avoid:

- Em dashes
- "Revolutionary", "cutting-edge", "robust"
- Over-explaining
- Buzzwords

Do include:

- What the game actually is (one sentence)
- Current features, listed straight
- A few specific technical details that show depth
- Screenshots if you have them
- What you're planning next
- A note if it's not playable yet

Put URLs and repo references in plain text or brackets, not markdown links on headings. Keep it scannable.

## Choosing scripts to include

Pick scripts that demonstrate the most technically interesting parts of your game. Good candidates:

- Physics systems (flight model, vehicle handling, environmental simulation)
- AI behaviour trees or state machines
- Custom tools or editors you built
- Systems that are hard to get right (radar, damage models, netcode)

Scrub any comments containing business logic, pricing, server endpoints, or API keys. A comment that says "// TODO: connect to license server" is a leak.

## Screenshots and media

Put images in an `images/` folder in the repo root. Reference them in the README with standard markdown:

```
![caption](images/screenshot.png)
```

Keep images under 1MB each. If you have video, host it on YouTube and link it, don't push video files to the repo.

## .gitignore

Use a standard Unity .gitignore so you don't accidentally commit Library/, Temp/, or .csproj files. The one from github/gitignore works fine. If you're only committing a subset of files (not the full project), a simple .gitignore that ignores nothing is fine too.

## License

If you're planning to sell the game, do NOT put an open-source license on the repo. Either omit the license file entirely (all rights reserved by default) or add a custom LICENSE that says something like:

> Source code shown here is for demonstration purposes only. All rights reserved. Contact for licensing.

This keeps you legally covered while still showing your work.

## The goal

Someone who finds the repo should be able to tell in 30 seconds whether they care about the game. If they do care, they should find enough detail to stay interested. If theyre a potential publisher or employer, they should see clean, well-structured code that shows you know what you're doing. They should NOT be able to rebuild your game from what you gave them.
