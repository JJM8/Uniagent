# One agent mode

Design notes. Nothing implemented yet.

## The idea

One chat you never leave. When the agent works out that the subject has really
changed - a different project, a different feature - it forks a new chat by
itself, carries the relevant tail of the conversation across, compacts what it
left behind, saves anything worth keeping into memories, and the UI follows.
No switching chats by hand, ever.

## Retroactive forking - the load-bearing idea

The moment you DECIDE to fork and the point you SPLIT AT are two different
points in time.

Deciding on the first message of a new subject is impossible: "how do I do X in
Rust" is either a two-line aside or the start of a week's work, and nothing in
the message says which. But if the split is retroactive, waiting costs nothing.
Wait until it is obvious, then cut backwards to where the subject actually
turned and hand those turns to the new chat.

Every hard part of this design gets easier once the decision is allowed to lag
the split.

## Detection

Three layers, cheapest first.

**1. Heuristic prefilter, no model call.** Most turns trip none of these and
cost nothing:

- workspace changed
- tool calls started touching a different project root
  (`/Projects/Uniagent/` -> `/Projects/drone/`)
- long idle gap since the last turn
- near-zero vocabulary overlap with the running topic

**2. A cheap judge model,** only when a heuristic trips. Returns three things:

- continuation / aside / new topic
- a confidence
- **the turn index where the new topic began** - it has already read those
  turns to make the call, so it may as well name the split point

**3. Hysteresis.** Fork only when "new topic" holds for K consecutive user
turns. Any turn that returns to the old work decays the score back to zero.
This is the "it was only a side question" filter and it is the difference
between this feeling like magic and feeling broken.

The judge stays OUT of the main model's prompt. The tempting alternative is a
`new_thread` tool the main model calls itself, but that hands a policy decision
to a model that is mid-task and biased toward carrying on, and it pays prompt
tokens on every turn to describe a tool it almost never uses. Judging
out-of-band costs the main chat's cache nothing.

## Keeping the judge cheap

The saving is not really in truncation, it is in NOT RE-SENDING.

- A sliding 10-turn window changes its prefix every turn, so it never hits a
  prompt cache. Instead keep a running one-paragraph **topic state** and send
  only the turns since the last judgement, plus that state.
- Judge on **user turns only**, and only when a heuristic has tripped.
- Truncate long messages: first ~50 and last ~50 characters.
- **Do not drop tool calls.** File paths are the strongest signal there is that
  the project changed. Keep tool NAME + FIRST PATH ARGUMENT, drop the results.
  Cheaper than prose and more informative.
- **Inject timing gaps** between turns - `[3 hours later]`. Nearly free and
  probably the best feature-per-token in the whole design: a gap plus a subject
  change is a new session, the same words with no gap is a follow-up.

Net: a few hundred tokens, a few times an hour.

## The split

Two hard constraints from the existing code:

- **Snap to a clean boundary.** Never split between an assistant tool call and
  its tool result - history is a turns list in OpenAI shape and that pair has
  to stay adjacent or providers reject it. Snap back to the nearest preceding
  user turn.
- **Only between turns**, never mid-stream.

**Copy, do not move.** Carry the turns into the new chat verbatim, then compact
them out of the parent leaving a stub:
`[continued in chat-a1b2c3d4: "ESP32 wake word tuning"]`.
`compaction.archive()` already copies the whole chat folder before replacing
history, so reusing it gives the undo safety net for free. Moving is cleaner in
theory, but when the judge is wrong it has amputated context from a chat you
are still working in.

**Carry the per-chat settings too** - workspace, model, pins, context
selections, and the one-agent-mode toggle itself. Otherwise the seam shows up
the first time a tool resolves a path in the wrong workspace.

## Compaction and memory extraction are two steps, not one

Different prompts, because they answer different questions:

- **Compaction** summarises the parent's tail *for that chat's own future* -
  state, decisions, what is half-finished.
- **Extraction** writes durable facts *for every other chat* - into
  `memories/<topic>.md`, or `context/2memory.md` for general facts.

Mashed together you get mush: too specific to be a memory, too lossy to resume
from.

**Memory bloat is the real risk.** `memories_text()` injects every memory
file's name and first line on every turn, so the folder is a standing cost.
Something that appends automatically on every fork will produce fifteen
near-duplicate files about one project inside a month. So:

- extraction **proposes** by default - a pending queue the UI surfaces - with a
  setting to promote it to fully automatic once trusted
- the extractor reads the existing index and **targets an existing file** unless
  nothing fits

## UI

Start as a **toggle in a normal chat**. It rides along with the settings copy,
so a forked chat inherits it and the mode continues.

Seamless should mean **no action required, not invisible**. A chat that swaps
silently underneath you loses you the thread of where things went.

- the browser **follows the fork automatically**
- carried turns appear in the new chat under a thin labelled divider -
  "split from *Uniagent wake word*"
- an **Undo** on that divider merges it back, for a few minutes
- the sidebar **nests the child under the parent**
- auto-title at fork time - stops being a nicety and becomes required once
  chats are machine-made
- **no live drift meter.** Watching a bar creep up while you type is stressful
  and invites you to fight it.

## Build order

1. **Manual `/fork [n]`** in `command_processor.py`. No judging, no models: mint
   a chat, carry the last n turns, copy settings, archive, stub the parent, link
   both ways. That is the whole mechanic, testable by hand. If forking turns out
   to feel bad, that is a day found out instead of a week.
2. Heuristic prefilter + judge + hysteresis, calling into step 1.
3. Compaction of the parent, then memory extraction as proposals.
4. UI: toggle, seam, undo, nesting.

## Open questions

- **Compact the parent on fork at all?** If the drift was real its tail is dead
  weight, but compacting blows the parent's prompt cache and you might be back
  in five minutes. Possibly better to leave it and only compact if it goes
  untouched for a while.
- **The inverse - auto-resume.** True one-agent mode means never switching
  *back* either: returning to an old subject should pull that chat's summary in
  rather than forking forward. Harder half, defer it - but it decides whether
  forks need to record enough metadata to be found again later.
- **Exclusions.** Cron chats and subagent chats should never auto-fork.

## Where it touches

- `scripts/command_processor.py` - `/fork`
- `scripts/compaction.py` - `archive()`, and the parent-tail summary
- `scripts/main.py` - chat creation, per-chat settings, `memories_text()`
- `scripts/server.py` - sidebar nesting, SSE for the automatic switch
- `scripts/settings.py` - the toggle and thresholds
