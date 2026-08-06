"""Reads a file off the disk with line numbers, so edit_file has something
exact to match against."""

import sys
from pathlib import Path

# Add scripts/ to the module path so we can import main. Guarded: _discovery
# RE-IMPORTS this module every time it scans tools/, so an unconditional insert
# put another copy of the same path on sys.path each scan - it grew without
# limit, and every import in the process got slower with it.
_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import main

from _workspace import here as _here

NAME = "ask_file"
DESCRIPTION = ("SMART file inspector: USE THIS WHEN READING A FILE UNLESS YOU ARE READY TO MAKE VERY SPECIFIC EDITS, USE THIS TO UNDERSTAND FILES OR A CODEBASE - summarization, config values, function purpose, errors, whatever. It uses a subagent to answer from the file so you don't bloat your context with raw content. Saves tokens and keeps you smart. Call this before read_file every time.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "ask_file". Do not explain what you are doing first.

Arguments:
- path:     the file to read. A relative path is from the workspace root.
- offset:   OPTIONAL, first line to read, counting from 1. Use it to page
            through something big.
- limit:    OPTIONAL, how many lines to read from there. Defaults to 2000.
- question: OPTIONAL, a very detailed specific question or thing to look for
            in the file. The subagent will answer that query instead of
            giving a generic summary.

Output comes back as "   12| the text of line twelve". The number and the pipe
are added by this tool to help you count lines - they are NOT in the file. When
you quote text into edit_file, strip them off and use only what came after the
pipe.

THIS IS FOR FILES, NOT FOR TOOLS. To find out how to call another tool, that
is read_skill, which is a completely different thing. This one reads real files
off the disk.

Read a file before you edit it. Guessing at what a file contains and then
trying to edit it wastes a turn when the text you guessed isn't there."""

MAX_LINES = 2000

# For native provider tool-calling.
SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description":
            "The file to read. A relative path is from the workspace root."},
        "offset": {"type": "integer", "description":
            "Optional, first line to read, counting from 1. Use it to page "
            "through something big. Defaults to 1."},
        "limit": {"type": "integer", "description":
            "Optional, how many lines to read from there. Defaults to 2000."},
        "question": {"type": "string", "description":
            "Optional, a very detailed specific question or thing to look "
            "for in the file - the subagent answers that instead of giving "
            "a generic summary."},
    },
    "required": ["path"],
}

search_prompt = "You are a search sub agent. The content below has line numbers (number| text). Summarize the key points, referencing line numbers where helpful.\n"

def _build_prompt(question, file_content):
    if question:
        return f"You are a search sub agent. Answer the query: '{question}' using this file content:\n{file_content}"
    return search_prompt + file_content

def run(path, offset=1, limit=MAX_LINES, question=None, workspace=None):
    # workspace comes from tool_processor, never from the model - see the note
    # in read_file, and its absence from SCHEMA above.
    ws = workspace or _here()
    target = ws.resolve(path)

    try:
        if not ws.exists(target):
            return "ERROR: there is no file at " + target + " (" + ws.where + ")"
        if ws.is_dir(target):
            return (target + " is a folder, not a file. It contains:\n"
                    + "\n".join(ws.listdir(target)))
        lines = ws.read_text(target).split("\n")
    except OSError as e:
        return "ERROR: could not read " + target + ": " + str(e)
    except Exception as e:
        return "ERROR: " + str(e)   # workspace unreachable - the message says why

    offset = max(1, int(offset))
    chunk = lines[offset - 1:offset - 1 + int(limit)]
    if not chunk:
        return ("(nothing at line " + str(offset) + " - the file only has "
                + str(len(lines)) + " lines)")

    out = ""
    for i, line in enumerate(chunk, start=offset):
        out += str(i).rjust(5) + "| " + line + "\n"

    end = offset - 1 + len(chunk)
    if end < len(lines):
        out += ("... " + str(len(lines) - end) + " more lines. Read on with offset "
                + str(end + 1) + ".\n")
    summary = main._stream([{"role": "user", "content": _build_prompt(question, out)}],
                           "bedrock", "eu.anthropic.claude-haiku-4-5-20251001-v1:0", 0, None)
    return "Summary: " + summary + "\n"
