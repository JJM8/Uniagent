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

NAME = "uniagent_command"

# This DESCRIPTION does the real teaching, not INSTRUCTIONS below it. A tool
# with a SCHEMA is sent to the model as a native tool definition carrying its
# name, this description and its schema - and NOTHING else (see
# tool_processor.tools_schema). The INSTRUCTIONS are only ever seen if something
# goes and reads this file, which for a schema'd tool the model has no reason to
# do.
#
# It used to spend nearly all of that budget teaching /workspace - what a
# workspace is, when to move, that another device is real - because moving was
# only reachable by spelling a slash command into this tool's free-text
# `command` string. It has its own tool now (workspace_tool.py, NAME
# "workspace") with a typed argument and room for that teaching, so this is back
# to being what it says: the way to run a slash command. /workspace still works
# here, as every command does, but the model is pointed at the named tool.
DESCRIPTION = (
    "Run a Uniagent slash command - the same commands the user can type into the "
    "chat box, run against THIS chat. /model [provider] [model] switches the model; "
    "/usage [today|7d|30d|all] answers \"how much have I spent\" from the local "
    "ledger, no provider asked; /history, /chats, /load <chat>, /new manage the "
    "conversation itself; /stop, /help. Pass the whole command as one string, e.g. "
    "\"/model bedrock haiku\". Note WHERE this chat works - which device and "
    "directory its file tools and terminal act in - is the separate `workspace` "
    "tool, not this one.")

INSTRUCTIONS = """
HOW TO CALL: use the tool-call syntax already given to you, with tool name "uniagent_command". Do not explain what you are doing first.

Arguments:
- command: the slash command to execute, e.g. "/workspace pi", "/model bedrock
           haiku", "/history", "/stop".

Supported commands:
- /workspace - list the workspaces and show which one this chat is in
- /workspace <name> - move this chat to that workspace (its id or its name)
- /workspace default - move it back to the default workspace
- /model [provider] [model] - switch model
- /history - show chat history
- /chats - list saved chats
- /load <chat> - load a chat
- /new - start new chat
- /stop - stop current turn
- /usage [today|7d|30d|all] [chat] - tokens and requests spent, from the local
  ledger. Answers "how much have I spent" without asking any provider anything.
- /help - show help

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

You are told at the top of every turn which workspace this chat is in and which
others exist - names, devices and paths. Read that before assuming where you
are. If you are unsure, run "/workspace" with no name and it lists them.

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
user means is not in the list, say so and let them add it on the settings page -
it needs a path and, for another device, ssh access already set up.
"""

# For native provider tool-calling.
SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description":
            "The slash command to execute, e.g. \"/model bedrock haiku\", "
            "\"/usage 7d\", \"/history\", \"/stop\". To change which device and "
            "directory this chat works in, use the `workspace` tool instead."},
    },
    "required": ["command"],
}

def run(command):
    # Blacklist check - block commands containing these words
    BLACKLIST = ["delete", "remove", "destroy", "wipe", "erase"]
    
    command_lower = command.lower()
    for word in BLACKLIST:
        if word in command_lower:
            return "This command is not allowed"
    
    # Parse the command - just ensure it starts with /
    if not command.startswith("/"):
        command = "/" + command
    
    # Against THIS turn's own chat, not whatever the terminal is sitting in:
    # turn_chat() is the conversation the model calling this tool is having, so
    # /model, /workspace and /stop here mean the chat the model is actually in.
    # Commands answer as (reply, goto); nothing can navigate from inside a tool
    # call - there is no window here to move - so only the reply is passed back.
    #
    # by_user=False because this IS the model, not a person typing. The only
    # thing it changes is /workspace: a move the user makes is written into the
    # history as a note for the model to read, and a move made here needs no
    # such note - the reply below is already coming back to it as the result of
    # its own call.
    chat = main.chat(main.turn_chat())
    reply, _ = command_processor.process(command, chat, by_user=False)
    return reply
