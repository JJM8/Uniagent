# Memory

The durable facts about the user and this machine - the things worth knowing in
every conversation, whatever it happens to be about. This file is injected into
every prompt, so keep it short and keep it general.

RULE: when you learn a general fact about the user that is not tied to one
project or one-off task - a preference, a recurring habit, an account, a person
in their life, a routine, the way they like something done - write it under
"## Facts" below before your reply ends. Do not wait to be told "remember
this". Check it isn't already there first, even worded differently: tighten the
line that exists rather than adding a second one saying the same thing. One
fact per line, plain and short.

Append with `write_file`, or from the terminal:

    echo "- the new fact, one plain line." >> context/2memory.md

Anything tied to a single project, person or topic does NOT belong here - it
goes in its own file under `memories/`, which is listed for you in the system
message and read only when it's relevant. This file is for what is true across
all of them.

## Facts

(nothing yet)
