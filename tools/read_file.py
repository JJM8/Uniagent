"""Reads a file off the disk with line numbers, so edit_file has something
exact to match against."""

from pathlib import Path

NAME = "read_file"
DESCRIPTION = ("Read a text file from the user's computer, with line numbers. Use this before "
               "editing a file, so you know exactly what is in it.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "read_file".

Arguments:
- path:   the file to read. A relative path is from the project root.
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

ROOT = Path(__file__).parent.parent
MAX_LINES = 2000
MAX_CHARS = 60000  # a whole context window is not worth one runaway file

# For native provider tool-calling.
SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description":
            "The file to read. A relative path is from the project root."},
        "offset": {"type": "integer", "description":
            "Optional, first line to read, counting from 1. Defaults to 1."},
        "limit": {"type": "integer", "description":
            "Optional, how many lines to read from there. Defaults to 2000."},
    },
    "required": ["path"],
}


def run(path, offset=1, limit=MAX_LINES):
    target = Path(path) if Path(path).is_absolute() else ROOT / path

    if not target.exists():
        return "ERROR: there is no file at " + str(target)
    if target.is_dir():
        listing = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return (str(target) + " is a folder, not a file. It contains:\n"
                + "\n".join(listing))

    try:
        lines = target.read_text(errors="replace").split("\n")
    except OSError as e:
        return "ERROR: could not read " + str(target) + ": " + str(e)

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
