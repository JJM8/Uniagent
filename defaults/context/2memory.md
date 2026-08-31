# Memory

The durable facts about the user, this computer and this environment - the
things worth knowing in every conversation, whatever it happens to be about.
This file is injected into every prompt, so keep it short and keep it general.

RULE: when you learn one of these, write it under "## Facts" below before your
reply ends. Do not wait to be told "remember this".

What belongs here:
- **The user** - a preference, a recurring habit, an account, a person in their
  life, a routine, the way they like something done.
- **This computer and environment** - hardware and monitors, which browser or
  tool to reach for, paths that matter, what is installed and what is not, a
  setting or a workaround you had to find the hard way and would otherwise have
  to find again next time.

What does NOT belong here: anything tied to a single project, person or topic.
That goes in its own file under `memories/`, which is listed for you in the
system message and read only when it's relevant. This file is for what is true
across all of them.

The test: would this matter in a conversation about something else? Yes, it
goes here. No, it goes in a `memories/` file.

Check a fact isn't already below before adding it, even worded differently:
tighten the line that exists rather than adding a second one saying the same
thing. One fact per line, plain and short. This file is yours to maintain -
rewrite a line that turns out to be wrong, and delete one that stops being
true, rather than letting it sit here being believed.

Append with `write_file`, or from the terminal:

    echo "- the new fact, one plain line." >> context/2memory.md

## Facts

(nothing yet)
