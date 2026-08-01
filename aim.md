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
