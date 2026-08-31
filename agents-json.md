# agents.json - PARKED

An idea, deliberately on hold. Do not build this before the infinite chat
feature works (see `infinite-chat-prompt.md`). Written down so the reasoning
isn't lost.

## The observation

Uniagent already has five agents. Each one was added as a loose set of keys in
`scripts/settings.py` rather than as an object:

| Agent | Keys it has today |
|---|---|
| main | `provider`, `model`, `temperature` |
| subagents | `sub_provider`, `sub_model` |
| cron | `cron_provider`, `cron_model`, `cron_safety_threshold`, `cron_safety_extra` |
| safety check | `verify_provider`, `verify_model`, `safety_prompt`, `safety_threshold`, whitelist, blacklist |
| speak summary | `speak_summary_provider`, `speak_summary_model`, `speak_summary_temperature`, `speak_summary_prompt` |

`settings.py:39-43` is a hand-maintained `PAIRS` map saying which model key
belongs to which provider key. That table exists ONLY because these are loose
pairs instead of objects. With an agents file it deletes itself.

The models tab (`web/index.html:3493`) is already three-quarters of an agents
tab, flattened into a single column.

So the case for the file is not "the new judge needs somewhere to live". It is
five existing things that should have been one thing. The judge is then nearly
free to add as the sixth.

## The one exception to preserve

Compaction deliberately has NO model of its own. It runs on the chat's own
model so the request stays byte-identical to a normal turn and lands on the
prompt cache the chat has already paid to build (`compaction.py:12-21`).

The schema has to be able to SAY that - `"model": "inherit"` - rather than
treating it as a gap to fill in. Giving compaction a model of its own would be
a regression; the old version did exactly that and every compaction was a cold
cache on a model the chat wasn't even using.

## Proposed shape

Only what is genuinely shared by all six:

```json
{
  "id": "judge",
  "label": "drift judge",
  "description": "decides when a chat has changed subject",
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "temperature": 0,
  "prompt": "<settings key, or a file in context/>",
  "tools": [],
  "context": { "files": [], "memories_index": false },
  "enabled": true
}
```

`"tools"` accepts `[]`, a list of names, or `"*"`.

`file attachments / permanent skills` fold into `context` - the same mechanism
as the always-injected `context/` files, scoped per agent instead of globally.

## What does NOT go in the file

**Context-assembly behaviour stays in the code that uses the agent.**

The rule: **config is the knobs, code is the machinery.** If it is a value a
human would plausibly change in a text box, a slider or a checkbox, it is
config. If expressing it needs an `if` or a loop, it is code.

"Last N turns, drop tool results, keep tool name + first path argument,
truncate long messages, inject timing gaps" is a PIPELINE. Putting it in JSON
means inventing a small config language with exactly one consumer, forever -
and every field is then something to write UI for, validate, migrate and
document.

The test: **would a second agent ever use this field?** The main agent does not
want turn-truncation. Neither does the safety check. So it is not config, it is
the judge's own code.

The NUMBERS inside the pipeline are a different matter - window size,
truncation length, confidence threshold. Those are knobs, and they go where
`safety_threshold` already goes: `settings.py` DEFAULTS.

**File layout does not have to match UI layout.** The agents tab can render the
shared object and that agent's specific knobs on one page.

## The only field needing real new code

`tools`. Today tools are global - `tool_processor` scans `tools/` and every
agent gets the lot, plus per-chat pins. "This agent gets NO tools" is a new
capability, not just plumbing. Easy (the tools array is assembled per request
already) but not free.

## The agents tab

- list left, detail right
- main agent pinned top, not deletable
- per agent: model, tool count, **last run**, **spend** - `usage.py` already
  tracks cost, and a cost column would show a runaway judge on day one instead
  of at the end of the month
- **no user-created agents at first.** A new agent needs a call-site in Python,
  and there is no way to author one from a web form. A UI that looks extensible
  but isn't is worse than an honest fixed list.

## The underrated cost: migration

Every chat folder and every cron job on disk was written with the old keys.
`tool_validation.py`'s docstring says outright that its back-compatibility is
load-bearing, and `cron.json` lets individual jobs override the model. So
`agents.json` must be generated from the old keys on first run, or read through
to them as a fallback. Plan for one release where both work.

This is more work than the file itself.

## Why it is parked

It is a refactor of working code in service of a feature that does not exist
yet. Build the judge with ad-hoc keys first, matching the pattern already there
five times over. Prove drift detection is worth having. THEN fold all six into
one file at once, with the judge as the sixth rather than the excuse.

If the judge turns out to be a bad idea, nothing was refactored for it. If it
is a good one, the schema will be written knowing what it actually needs to
hold - whether `tools: []` and `model: "inherit"` are the right fields, having
needed them.
