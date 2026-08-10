# Uniagent - aim and session notes

Handoff doc for a fresh session. Written 2026-07-17.

## The aim

A personal AI agent that runs locally on the user's own Linux machine and can actually
do things on it. Provider-agnostic - swap between DeepSeek, Anthropic, OpenAI
and Gemini by changing one variable. Deliberately tiny and readable: no
LangChain, no frameworks, no SDKs, plain `requests` and the standard library.

The guiding constraint is **legibility**. If a thing can't be understood by
reading the file, it doesn't belong. Simplicity beats capability.

## Layout

```
Uniagent/
├── context/               # ALL of it is injected, in leading-number order
│   ├── 1system.md         # identity + the honesty policy
│   └── 2memory.md         # durable facts about the user
├── scripts/
│   ├── main.py            # the loop: prompt -> model -> tool -> repeat
│   ├── provider.py        # one function per LLM vendor
│   ├── settings.py        # model choice, theme - settings.json, read per turn
│   └── tool_processor.py  # loads tools/, finds tool calls, runs them
└── tools/
    ├── use_tool.py        # meta-tool - the ONLY one injected up front
    ├── terminal.py        # run shell commands (approval-gated)
    ├── write_file.py      # write files (approval-gated)
    └── create_tool.py     # guide for the agent writing its own tools
```

Prompt is assembled as: `SYSTEM + MEMORY + TOOLS_TEXT + history + name`.

## Design decisions, and why

**One .py per tool, nothing else.** Each defines `NAME`, `DESCRIPTION`,
`INSTRUCTIONS`, `run()`. Auto-discovered with `pkgutil.iter_modules`. No JSON
schemas, no sidecar markdown, no registration step - dropping a file in `tools/`
is the whole process. `run()`'s parameter names *are* the arg names.

**Text/JSON protocol, not native tool calling.** The model emits
`{"tool": "x", "args": {...}}` as plain text and `tool_processor` parses it.
This keeps `provider.py` dumb and works identically on all four vendors. The
trade is real: native tool calling is more reliable (constrained decoding).
Revisit if malformed calls become common.

**provider.py holds ONLY vendor-specific things.** Transport, auth, request
shape, per-vendor quirks. App policy (what the agent *says*) lives in
`context/`. The one apparent exception - DeepSeek's hardcoded "Always respond
in English" - fits the rule, because it's a workaround for a DeepSeek quirk,
not a statement of policy.

**Progressive disclosure.** Only `use_tool` is injected; it exposes `listTools`
and `getTool` so the model fetches instructions on demand. Copied from
Anthropic's `tool_search_tool` (`defer_loading: true`) and OpenClaw's skills.
Note this does NOT pay off yet - Anthropic's own guidance is that under ~10
tools you should just inject everything. Built early on purpose.

**Tool instructions stay in history where they were fetched.** Do not "promote"
them to a block after history: a block that moves every turn can never be
cached, whereas history is append-only and caches fine. This mirrors what
Anthropic actually does (inline `tool_reference` expansion, prefix untouched).

## Memory design

Two tiers - a working set that's always loaded, and an archive paged in on
demand.

**Main memory** (`context/2memory.md`) - short, high-value, durable facts about
the user and the machine. Injected into every prompt, so it's always in context.
The agent appends to it itself via the terminal (the how-to lives in that file)
when it learns something worth keeping that isn't already there. Kept small on
purpose: everything here costs tokens on every single turn. Append-only for now,
no compaction, so it only grows.

**Long-term memory** (planned, not built yet) - larger, labelled, detailed, and
NOT injected by default. Organised into sections by header/topic. The agent
reaches into it on demand with a tool like `read_long_term_memory` given a
header, which returns that one section in full detail. This keeps the deep stuff
out of the hot context while still being reachable.

Long-term memory also gets a **search function**: the agent can search across
sections for a term rather than needing to know the exact header, so it can find
relevant detail even when it doesn't know where it lives.

The split is working-set vs archive: main memory is what's always true and
always needed; long-term is the detailed store you page in only when a task
calls for it.

## Hard-won knowledge from this session

**Every "the model is stupid" moment was actually the tool lying.** This is the
big one. Each time DeepSeek did something dumb, the cause was a tool returning
misleading text:
- `"(no output, exit code 0)"` -> read as failure, so it re-ran the command and
  opened two terminal windows. Mentioning an exit code implies something's wrong.
- `&` + `capture_output=True` -> reported a timeout for an app that opened fine.
- `"(the command FAILED, exit code 1)"` for `grep`/`pkill` finding nothing ->
  the model started appending `; echo 'Done!'` to force exit 0, which then made
  it *tell the user it had closed an app that was never running*.

Fix the tool's output, not the prompt. Check this first when behaviour is odd.

**Non-zero exit codes don't mean error.** `grep`, `pkill`, `diff` and `test` all
return 1 for "nothing matched" - an answer, not a failure. Only exit code 0
reliably means anything ("it worked").

**`capture_output=True` + `&` hangs.** The backgrounded app inherits the pipe
and holds it open; `subprocess.run` waits for EOF, not for the shell to exit.
Silence is not EOF. Fix: `Popen` + `DEVNULL` + `start_new_session=True` for
commands ending in `&`. `&` alone never helps.

**A broken tool .py can brick the whole agent** - and BOTH loaders need
try/except, because `use_tool` imports its siblings independently of
`tool_processor`. Hardening only one leaves `listTools` fatal. Found by testing.

**DeepSeek V4 Flash is NOT deterministic at temperature 0.** Verified: same
prompt gave "Giraffe" then "Platypus". It's a MoE model (284B total / 13B
active) and expert routing shifts with server batching. Temperature only
removes sampling randomness. Don't expect determinism.

**DeepSeek defaults to Chinese** without a system message. Language-matching
also beats the instruction - Chinese input still gets Chinese back.

**DESCRIPTION is load-bearing under deferral.** `listTools` shows *only* the
description, so if it's weak the tool never gets fetched at all. An early
description said terminal "returns output", so the model concluded it couldn't
open a window and refused. With everything injected this wouldn't have mattered.

**Prompt caching dictates layout:** static -> semi-static -> volatile. Anything
above a change invalidates everything below it. Only append-only content caches.

**Past tool calls in history act as few-shot examples.** This is why the model
skips `getTool` and guesses the JSON from a pattern it's seen. Guard: any tool
that gets called should have its instructions present (calling loads it).

## Known problems, roughly in priority order

- **History is a plain string, so there are no real roles.** The model can write
  the scaffolding itself - `Tool result:` was seen in its own output once, which
  is a route to fabricating tool results. Also means a user typing
  `Assistant: ...` injects straight into the transcript.
- **No compaction.** History grows forever; every turn re-sends everything until
  the context limit errors.
- **Main-memory writes are agent-driven, not a tool.** The agent appends to
  context/2memory.md via the terminal (instructions live in that file) rather than a
  dedicated memory tool. No compaction, so main memory only grows; long-term
  memory + search (see Memory design) isn't built yet.
- **API key is hardcoded** in `provider.py` (`DEEPSEEK_API_KEY`, env-overridable).
  Fine while untracked; move it before this ever goes near git.
- **`write_file` has no path containment.** `"../../.bashrc"` resolves outside
  the project. Only the approval gate stops it.
- **`EOFError` traceback on Ctrl+D.** Cosmetic.
- Instructions in tools are shouty ALL-CAPS. They work, but 700 chars of DO NOTs
  against a small system prompt made the agent robotic. Tone down once stable.

## Conventions that matter

- Tools return **strings, always** - including errors ("ERROR: ..."), never
  raise. The model reads its own return value, so a readable error is fixable.
- Anything destructive asks for approval with `input()` inside `run()`.
- Denials return a `DENIED` string, not an exception, so the loop survives.
- The inner tool loop is capped at 10 iterations - each one is a paid API call.

## Planned: boards - several chats, co-present

Written 2026-08-09. Design notes, nothing built.

### The problem being solved

Uniagent already runs many agents at once. Every chat is its own `Agent` with
its own `TurnSlot`, so two chats genuinely run in parallel; cron jobs are
agents; subagents are agents. The web UI shows exactly one of them. `currentChat`
is a single string, and `events.onmessage` throws away every event whose `chat`
field isn't it (index.html:4638).

So the shape of the UI contradicts the shape of the system. The sidebar is a
filing cabinet - one drawer open at a time - for something that behaves like a
room full of people working. You cannot watch two agents at once, cannot ask
three the same question, and two chats that both want the same fact each have
to be told it separately.

A **board** is a named view holding several chats side by side, plus the wiring
between them. Drag a chat from the sidebar onto the board and it joins; it is
the same chat, still openable full-screen, still in the sidebar. Nothing is
copied or moved. A board is a small JSON file: which chats, where they sit,
what is wired to what.

### Naming - "workspace" is taken

`workspace` already means a directory root plus an optional ssh destination
(scripts/workspace.py) - *where* a chat's tools do their work, and on which
machine. Reusing the word for *a group of chats on screen* would collide in the
settings UI, in `/workspace`, and in every conversation about it afterwards.

Board is the working name here. Room, table and canvas all fit too. Decide
before writing any code, because the word ends up in the file format, the route
names and the tool description.

### What makes it unique

Panes side by side is not new - tmux, VS Code, three browser tabs. The part
nobody has is that these particular agents are **co-present**: they share one
machine, they can share a workspace in the ssh sense, and they can be wired to
each other. The board is where that wiring becomes something you can see and
drag, instead of a config file nobody reads.

Uniagent already does agent-to-agent messaging, but only downward and only
inside one chat: the `subagent` tool spawns children, and their reports come
back through `main.notify` under the parent's turn lock. Boards make that
**lateral** - peer to peer between chats you already have, using the same
delivery path that is already proven to be safe.

Four things a board can do that the single-chat window structurally cannot:

**Broadcast.** One message box at the bottom of the board sends to every chat
on it. They all answer in parallel, each on its own model, each with real tools
on the real machine. Same question to DeepSeek, Claude and Gemini, three live
panes, no copy-paste. This is the demo that sells the feature.

**@mention routing.** Inside a board, `@researcher` in one chat delivers that
text into the chat named researcher, which answers in its own pane. Same
mechanism as a subagent report - hand it to the registered turn runner, it
waits for that chat's `TurnSlot`, it lands when the chat is idle.

**Wires.** Drag a line from chat A's edge to chat B's: "when A finishes a turn,
send A's final answer to B as a message." Visible, hoverable, deletable. Chain
three and you have a pipeline; point two at one and you have a reducer. The
wiring is the feature - it is a thing you built by dragging, not a workflow
YAML.

**A shared note.** One markdown file per board, injected into every chat on
that board, writable by all of them. The blackboard pattern: agents coordinate
by reading and writing shared state instead of by messaging each other. It fits
the existing context system exactly - it is one more injected file, just scoped
to a board rather than global.

Optionally, roles: a board can give each pane a standing instruction (planner /
critic / builder) without editing that chat's own context, so the same chat is
a critic on one board and nothing special everywhere else.

### Why this is cheaper to build than it looks

- **The server already broadcasts everything.** `_broadcast` puts each event on
  every open `/stream` queue, and every event already carries a `chat` field
  (server.py:432-490). The front end is what narrows it to one. Viewing many
  chats live needs no server work at all - it needs `currentChat` to become a
  set and the filter at index.html:4638 to route by `chat` into the right pane.
- **Parallelism is real already.** `TurnSlot` is per-agent, not global. Six
  panes running six turns is what the code does today when six clients poke six
  chats.
- **Delivery into a busy chat is solved.** `main.notify` + the turn lock is
  exactly the "message arrives while it is thinking" problem, already handled
  for subagent reports. Wires and @mentions reuse it rather than inventing a
  queue.
- **Boards are additive.** A board file that names chats. Delete the file and
  nothing else changes; every chat is untouched and still works alone.

### What will bite

- **Approvals.** `terminal` and `write_file` gate on an approval that the UI
  shows as one modal for the whole window (there is already a comment at
  index.html:3441 about approving a background chat's question by accident).
  Six panes means approvals must be per-pane, attached to the pane that asked.
  This is the first thing to fix and it is not optional - a modal that could
  approve the wrong agent's `rm` is worse than no board.
- **Cost, visibly.** Broadcast to six chats is six paid calls, and a wire that
  fires on every turn is a call the user did not type. The board needs a
  running count of what it just spent, and wires should be obvious enough that
  nobody builds an accidental loop. Two chats wired at each other will ping-pong
  forever - detect the cycle when the wire is drawn, refuse it there.
- **DOM weight.** index.html is one 8.4k-line file. Six live transcripts
  streaming markdown at once needs the panes to render only what is on screen,
  and probably a shorter scrollback per pane than the full-screen view.
- **Not every agent should be draggable at first.** Cron jobs and subagents are
  `Agent`s too, so a board could watch a cron run live - which is genuinely
  useful, and also a much bigger surface. Chats only for v1.

### Build order

1. Panes: `currentChat` becomes a set, events route by `chat`, drag from the
   sidebar, board JSON persists. Read-only multi-view, no wiring.
2. Per-pane approvals. Blocks everything after it.
3. Broadcast box.
4. @mention routing between panes.
5. Shared note.
6. Wires, with cycle refusal at draw time.

Stop after any step and the thing still makes sense on its own.
