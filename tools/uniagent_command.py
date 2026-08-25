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

/workspace IS ITS OWN TOOL. Moving this chat to another device or directory has
a named tool of its own - "workspace", with an `id` argument - and that is where
the explanation of what a workspace is, and when to move, now lives. The command
still works from here, because every command does, but prefer the tool: it is
one typed argument instead of a string to spell, and its own description already
tells you the part that matters.
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
