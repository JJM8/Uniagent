"""Moving this chat between workspaces, as a tool of its own.

The move itself has always existed - it is command_processor's /workspace, a
thing done TO the conversation rather than in it, which is why it is a command
and not a function here. What did NOT exist was a way for the model to reach it
that looked like everything else it can do: it had to go through
uniagent_command and spell "/workspace pi" into a free-text `command` string,
one arm of a grab-bag tool whose description had to teach slash commands AND
what a workspace is in the same few hundred characters.

So this file is a thin front door: a real NAME, a real SCHEMA with one real
argument, and a run() that hands the work straight to the command that already
does it. The model gets a named tool with a typed argument instead of a string
to compose; the token panel gets a "tool schema: workspace" entry it can price;
and nothing about how a move actually happens changed - there is still exactly
one implementation, in command_processor._workspace.

Named workspace_tool.py, not workspace.py: the loader imports a tool file by
its stem, and scripts/workspace.py is already the module `workspace` that this
whole system is built on. Two files claiming that name would have the loader
reload the wrong one on every scan. NAME below is what the model sees, and the
file it lives in has never had to match (see email_tool.py, mcp_tool.py).
"""

import sys
from pathlib import Path

# Guarded: _discovery re-imports this module on every scan of tools/, so an
# unconditional insert added another copy of the same path each time and
# sys.path grew without limit.
_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import command_processor
import main
import workspace

NAME = "workspace"

# This DESCRIPTION does the real teaching, not INSTRUCTIONS below it. A tool
# with a SCHEMA is sent to the model as a native tool definition carrying its
# name, this description and its schema - and NOTHING else (see
# tool_processor.tools_schema). The INSTRUCTIONS are only ever seen if something
# goes and reads this file, which for a schema'd tool the model has no reason to
# do. So everything it must know to move correctly - above all what a workspace
# IS - has to be here, in the few hundred characters it is actually given.
#
# The other half - which workspaces exist right now, with their devices and
# paths - is appended by live_description() below rather than living in the
# system prompt as it used to. Same bytes for every chat, so it does not cost
# the prompt cache anything; see workspace.catalogue for why that matters and
# for how the model finds out which workspace it is actually in.
#
# It is long for a description and it earns that, because this is the one tool
# whose effect lands on every OTHER tool. What was trimmed out of it was only
# what it said twice: the file tools were named one by one where "the file
# tools" does the same job, and the "they act THERE, so use this whenever the
# user means another machine" rule was here AND on `id` below.
DESCRIPTION = (
    "CHANGES WHICH DEVICE AND DIRECTORY THIS CHAT WORKS ON. A workspace is one saved "
    "place to work - a computer, and a root directory on it. The one this chat is in "
    "decides, for EVERY tool call you make, which machine the file tools read and "
    "write on, which machine the terminal's commands actually run on, and what a "
    "relative path means. Another device is reached over ssh and is real: there, `ls` "
    "lists THAT device's files and a file you write lands on THAT device's disk, not "
    "on the machine running Uniagent - and Uniagent's own folder (memories/, context/, "
    "skills/) is not reachable from there. So whenever the user means another of their "
    "devices - \"what's on my phone\", \"check the logs on the Pi\", \"is it still "
    "running on the server\" - move this chat there FIRST and then do the work, rather "
    "than answering from the machine you happen to be on. They need not say the word "
    "\"workspace\". Call it with no argument to list them and see where this chat is. "
    "It only moves between workspaces that already exist - it cannot create one - and "
    "a move affects this chat only, applies from your next tool call, and lasts until "
    "changed.")

INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "workspace". Do not explain what you are doing first.

Arguments:
- id: the workspace to move this chat to - its id or its name, e.g. "pi".
      "default" moves it back to the default workspace. Omit it entirely to
      list the workspaces and see which one this chat is in.

WHAT A WORKSPACE IS - READ THIS, IT DECIDES WHERE YOUR TOOLS ACT.

A workspace is one saved place to work: A DEVICE and A DIRECTORY on it. This
chat is in exactly one of them at a time, and that one workspace decides, for
every tool call you make:
- WHICH COMPUTER read_file, write_file, edit_file and ask_file read and write
  on, and
- WHICH COMPUTER the terminal's commands actually run on, and
- WHICH DIRECTORY a relative path like "notes.md" or "src/" means.

A workspace on another device is reached over ssh and is completely real: with
this chat in it, `ls` lists that device's files, a file you write lands on that
device's disk, and a process you start runs on that device. Nothing you do
touches the machine Uniagent itself is running on. The reverse is just as true -
while this chat is on another device, Uniagent's own folder (memories/,
context/, skills/, tools/) is NOT reachable, because that folder is on the
Uniagent machine. To read or write a memory from elsewhere, move to the Uniagent
folder workspace, do it, and move back.

Which workspaces EXIST is listed in this tool's own description, every turn.
Which one this chat is in right now is NOT: you are told that when it changes -
the user moving this chat says so in the conversation, and a move you make comes
back as your own result - and a chat nobody has moved is in the default
workspace, on the machine running Uniagent. If you are unsure where you are,
call this with no id before you touch a file; it says which one you are in.

WHEN TO MOVE. Whenever the user means another one of their devices, move there
first and then do the work. "What's in my downloads on the phone", "check the
logs on the Pi", "is the server still running on the NAS", "build the site in my
projects folder" - each of those is a workspace, and the user does NOT have to
say the word "workspace" for it to be one. Naming any device they have saved is
the request to work on it. Do not answer such a question from the device you
happen to be on, do not guess what is on the other machine, and do not tell the
user to go and look themselves - move and find out.

HOW MOVING BEHAVES. It takes effect from your very next tool call, it affects
this chat only, and it stays until changed again - so move once, do the work
there, and move back when the work is genuinely somewhere else again. Do not
flip back and forth mid-task. The terminal gets a fresh shell in the new place,
so anything open in the old one (a running process, an activated venv, a
directory you had cd'd into) is left behind there. The reply tells you whether
the device is actually reachable; if it is not, say so plainly rather than
retrying blindly.

IT CANNOT CREATE ONE. Only the workspaces already saved exist. If the place the
user means is not in the list, say so and let the user add it on the settings
page - it needs a path and, for another device, ssh access already set up.
"""

# For native provider tool-calling. One optional argument: with it this moves,
# without it it lists. The parameter is `id` rather than `workspace` on purpose
# - tool_processor.process() fills in an argument BY THAT NAME on any tool whose
# run() declares it, handing over the chat's Workspace object, which would
# silently overwrite whatever the model asked for here.
# Runs on its own, never alongside another tool call in the same batch.
# This one moves the ground every other tool stands on: it changes
# which machine and which root the chat's tools work in. Nothing else
# may be in flight while that happens.
# See tool_processor.parallel_safe().
PARALLEL = False

SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description":
            "The workspace to move this chat to - its id or its name. "
            "\"default\" moves back to the default workspace. Omit it to list "
            "the workspaces instead."},
    },
    "required": [],
}


def live_description():
    """DESCRIPTION with the workspaces that currently exist appended.

    A hook rather than a constant because DESCRIPTION is captured once, when
    tool_processor scans this folder, and that scan only reruns when a file
    under tools/ moves - so a workspace added on the settings page would not
    show up here until something unrelated was edited. This is called per
    request instead. See tool_processor._described.

    workspace.catalogue() is memoised on the filecache signature, so the cost
    per call is a dict lookup, and it deliberately says nothing about which
    workspace THIS chat is in - that would make the schema differ per chat,
    and the schemas are the head of the cached prefix."""
    return DESCRIPTION + workspace.catalogue()


def run(id=None):
    # Against THIS turn's own chat, not whatever the terminal is sitting in:
    # turn_chat() is the conversation the model calling this tool is having, so
    # the move lands on the chat the model is actually in. Commands answer as
    # (reply, goto); nothing can navigate from inside a tool call - there is no
    # window here to move - so only the reply is passed back.
    #
    # by_user=False because this IS the model, not a person typing. The only
    # thing it changes is that a move the USER makes is written into the history
    # as a note for the model to read, and a move made here needs no such note -
    # the reply below is already coming back to it as the result of its own call.
    arg = (id or "").strip()
    chat = main.chat(main.turn_chat())
    reply, _ = command_processor.process(
        "/workspace" + (" " + arg if arg else ""), chat, by_user=False)
    return reply
