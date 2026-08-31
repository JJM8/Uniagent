# Prompt: implement infinite chat

Hand this whole file to a fresh Claude Code session in the Uniagent repo.

---

Implement **infinite chat** in Uniagent: a mode where the user never switches
chats by hand. After each turn finishes, a small checking agent looks at a
parsed version of the recent conversation and decides whether the subject has
really changed. When it has, Uniagent forks a new chat, carries the turns from
where the new subject started, and the browser follows automatically.

Read `one-agent-mode.md` in the repo root first - it is the design discussion
behind this and explains WHY several of the requirements below are the way they
are. Read this prompt in full before writing any code, and ask about anything
underspecified rather than guessing.

## Read before starting

- `one-agent-mode.md` - the design notes
- `scripts/compaction.py` - especially `archive()`, which already copies a
  chat's whole folder before its history is replaced. Reuse it.
- `scripts/main.py` - the `Agent` class (`SETTINGS_KEYS`, `_write_settings`,
  `_read_settings`, ~line 575-800), `new_chat_id()` (~1408), `run()` (~3150)
- `scripts/server.py` - `_run_turn`'s `finally` block, around line 1240-1273,
  where `{"type": "done"}` is broadcast
- `scripts/settings.py` - `DEFAULTS`, and `PAIRS` at lines 39-43
- `web/index.html` - the settings tabs at lines 3476-3488 and the panels below

## What to build

### 1. The checking agent

Keep it very simple. Follow the pattern already used five times in
`settings.py` - ad-hoc keys, not a new abstraction:

- `infini_provider` / `infini_model` - add to `PROVIDER_KEYS` and `PAIRS` the
  same way `verify_` and `sub_` are. Default to a cheap fast model
  (`deepseek` / `deepseek-v4-flash`).
- `infini_prompt` - the instruction, in `DEFAULTS`, editable in the UI like
  `safety_prompt` and `compaction_prompt` are.
- `infini_turns` - how many recent turns it sees. Default 10.
- `infini_temperature` - default 0.

It gets **no tools** and **no chat context/memories** - it is answering one
question about a transcript, nothing more.

**It must only ever see a parsed version of the conversation, never the raw
history.** Build that parsed view in the new module, not in config. It should:

- include user and assistant turns only; **drop tool RESULTS entirely**
- for each tool CALL keep only the tool name and its first path-ish argument -
  file paths are the single strongest signal that the project changed
  (`/Projects/Uniagent/...` -> `/Projects/drone/...`), so do not drop these
- truncate any long message to roughly the first 50 and last 50 characters,
  joined with an ellipsis
- **insert timing gaps between turns** where the gap is significant - a literal
  line like `[3 hours later]`. This is cheap and is one of the strongest
  signals available; a gap plus a subject change is a new session, the same
  words with no gap is a follow-up.
- number the turns, so the model can name a cutoff by index

Log the exact parsed text somewhere inspectable - this needs to be tunable by
eye, and it will be wrong at first.

**What it returns.** Ask for strict JSON with three fields:

```json
{ "verdict": "continue" | "aside" | "new", "cutoff": <turn index>, "why": "..." }
```

- `continue` - same subject, do nothing
- `aside` - a side question, do nothing (this exists so the model has somewhere
  to put "different subject but clearly a quick detour")
- `new` - fork, splitting at `cutoff`

Anything that will not parse, or an unknown verdict, means **do nothing**. This
feature must fail closed: a broken judge response must never fork a chat.

### 2. The fork

When the verdict is `new`:

- **Snap the cutoff to a clean boundary.** Never split between an assistant
  turn holding `tool_calls` and its matching `tool` results - the history is a
  turns list in OpenAI shape and that pair must stay adjacent or providers
  reject the request. Snap backwards to the nearest preceding user turn.
- Only ever fork **between turns**, never mid-stream. Running it from the
  `finally` block in `_run_turn` gives this for free.
- `archive()` the parent first, reusing `compaction.py`'s.
- Mint a new chat with `new_chat_id()` and **copy the turns from the cutoff
  onward into it verbatim.**
- **Copy, do not move.** Leave the parent's history intact except for a short
  stub appended at the end noting where things continued, e.g.
  `[continued in chat-a1b2c3d4]`. If the judge was wrong, nothing has been
  amputated from a chat the user is still working in.
- **Carry the per-chat settings across**: workspace, model, provider,
  temperature, pins, safety threshold - and the infinite-chat toggle itself, so
  the mode continues in the new chat. Look at `Agent.SETTINGS_KEYS` and carry
  what makes sense; do not carry usage counters.
- Give the new chat a name derived from the new subject. The judge's `why` is a
  reasonable source, or ask for a short title as a fourth JSON field.
- Do not fork cron chats or subagent chats, ever. Guard for this explicitly.
- Do not fork if the cutoff would leave the new chat empty, or would carry the
  entire history (nothing was actually split).

### 3. The per-chat toggle

For now this is **a per-chat setting only** - no global on/off. Add it to
`Agent.SETTINGS_KEYS` and give it a setter that calls `_write_settings()`, the
same shape as the existing per-chat setters around `main.py:740`. Default off.

Expose it in the chat UI wherever the per-chat safety threshold slider already
lives, as a simple toggle.

### 4. The settings tab

Add a new **`infiniagent`** tab to the settings panel, alongside the existing
thirteen (`web/index.html:3476-3488`). It holds everything global about the
feature:

- provider + model pickers for the checking agent, matching the layout of the
  existing model rows in the models tab
- **how many turns it checks** (`infini_turns`) - the user specifically wants
  this adjustable
- temperature
- the prompt, in an editable textarea, with empty meaning "restore the shipped
  default" - exactly how the compaction tab behaves
- ideally: a read-only view of the last parsed transcript that was sent to the
  judge, and its last verdict. This makes the feature tunable instead of
  mysterious.

### 5. Seamless switching

When a fork happens the browser must follow **automatically, with no click**.

- Broadcast a new SSE event from the server carrying the parent id and the new
  chat id. The existing event types are listed around `server.py` - follow the
  same `_broadcast({"type": ...})` shape, and also send the existing `chats`
  event so the sidebar refreshes.
- The page switches itself to the new chat.
- **Seamless means no action required, not invisible.** The carried turns
  should appear in the new chat under a thin labelled divider naming the chat
  they came from, so the user can see what happened and where the rest went.
  Do not silently swap the view with no explanation.
- A client that is looking at a different chat must not be yanked anywhere.

## Explicitly NOT in this piece of work

- `agents.json` and an agents tab - parked, see `agents-json.md`. Use ad-hoc
  settings keys for now, matching the existing pattern.
- Memory extraction into `memories/`
- Compacting the parent chat after a fork
- The heuristic prefilter (workspace change, path change, vocabulary overlap).
  Just run the judge after every turn for now; the whole point of this pass is
  to find out whether the judge is any good.
- Hysteresis / waiting K turns before forking. The `aside` verdict and the
  retroactive cutoff cover most of it for v1. Leave the code shaped so a
  confidence threshold could be added later.
- Auto-resume - noticing the user has returned to an old subject and going back
  to that chat.

## Constraints

- Do not break existing chats. Every chat and cron job on disk was written
  before this existed; a missing setting means "off".
- Do not slow the main turn down. The check runs after `done` is broadcast, not
  before - the user must never wait on the judge.
- A judge failure (provider down, timeout, unparseable) must be logged and
  otherwise invisible. Never let it break or block a turn.
- Follow the house style: thorough module and function docstrings explaining
  WHY, matching `compaction.py` and `tool_validation.py`.

## Done when

- A chat with the toggle on, taken from one subject to a clearly different one,
  forks by itself; the browser lands in the new chat; the carried turns are
  there under a divider; the parent still has its history plus a stub.
- A side question does not fork the chat.
- The toggle, the turn count and the prompt are all editable in the infiniagent
  tab and take effect on the next turn without a restart.
- The toggle off means nothing happens at all - no judge call, no cost.
