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

DESCRIPTION = "Execute Uniagent slash commands: /model, /stop, /history, /chats, /load, /new, /help"

INSTRUCTIONS = """
HOW TO CALL: use the tool-call syntax already given to you, with tool name "uniagent_command". Do not explain what you are doing first.

Arguments:
- command: the slash command to execute, e.g. "/model bedrock haiku",
           "/history", "/stop".

Supported commands:
- /model [provider] [model] - switch model
- /history - show chat history
- /chats - list saved chats
- /load <chat> - load a chat
- /new - start new chat
- /stop - stop current turn
- /help - show help
"""

# For native provider tool-calling.
SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description":
            "The slash command to execute, e.g. \"/model bedrock haiku\", \"/history\", \"/stop\"."},
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
    # /model and /stop here mean the chat the model is actually in. Commands
    # answer as (reply, goto); nothing can navigate from inside a tool call -
    # there is no window here to move - so only the reply is passed back.
    chat = main.chat(main.turn_chat())
    reply, _ = command_processor.process(command, chat)
    return reply
