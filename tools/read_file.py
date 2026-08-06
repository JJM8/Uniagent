"""Reads a file off the disk with line numbers, so edit_file has something
exact to match against.

Reads it out of the chat's workspace, which may be a folder on this machine or
one on another machine over ssh - see scripts/workspace.py. Nothing in here
knows the difference."""

from _workspace import here as _here

NAME = "read_file"
DESCRIPTION = ("Read a text file from the user's computer, with line numbers. Use this before "
               "editing a file, so you know exactly what is in it.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "read_file".

Arguments:
- path:   the file to read. A relative path is from the workspace root.
- offset: OPTIONAL, first line to read, counting from 1. Use it to page
          through something big.
- limit:  OPTIONAL, how many lines to read from there. Defaults to 2000.

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
MAX_CHARS = 60000  # a whole context window is not worth one runaway file

# For native provider tool-calling.
SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description":
            "The file to read. A relative path is from the workspace root."},
        "offset": {"type": "integer", "description":
            "Optional, first line to read, counting from 1. Defaults to 1."},
        "limit": {"type": "integer", "description":
            "Optional, how many lines to read from there. Defaults to 2000."},
    },
    "required": ["path"],
}


def run(path, offset=1, limit=MAX_LINES, workspace=None):
    # workspace comes from tool_processor, never from the model - the same
    # arrangement chat_id has, and deliberately absent from SCHEMA above. It
    # carries the chat's root and, for a remote workspace, does the reading on
    # the far machine. None means nobody passed one (a direct call, a test), and
    # the install folder is what this tool always used before workspaces.
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
        # A workspace that can't be reached at all - the machine is off, the
        # key isn't set up. Its message already says which and what to do.
        return "ERROR: " + str(e)

    offset = max(1, int(offset))
    chunk = lines[offset - 1:offset - 1 + int(limit)]
    if not chunk:
        return ("(nothing at line " + str(offset) + " - the file only has "
                + str(len(lines)) + " lines)")

    out = ""
    for i, line in enumerate(chunk, start=offset):
        out += str(i).rjust(5) + "| " + line + "\n"
        if len(out) > MAX_CHARS:
            out += ("... TRUNCATED - too big to show at once. Read on by calling "
                    "read_file again with the same path and offset " + str(i + 1) + ".\n")
            return out

    end = offset - 1 + len(chunk)
    if end < len(lines):
        out += ("... " + str(len(lines) - end) + " more lines. Read on with offset "
                + str(end + 1) + ".\n")
    return out
