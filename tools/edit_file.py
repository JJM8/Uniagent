"""Swaps one exact piece of text in a file, so changing three lines doesn't
mean resending the whole thing."""

from _workspace import here as _here

NAME = "edit_file"
DESCRIPTION = ("Change part of an existing file by swapping one exact piece of text for "
               "another. Leaves the rest of the file untouched. Use this rather than "
               "rewriting a whole file to change a few lines.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "edit_file".

Arguments:
- path: the file to change. A relative path is from the workspace root.
- old:  the exact text to find, copied character for character from the file.
        (any argument name containing "old", e.g. old_text, also works)
- new:  what to put there instead.
        (any argument name containing "new", e.g. new_text, also works)
- all:  OPTIONAL, false by default. Set it true to replace EVERY occurrence
        instead of requiring exactly one.

READ THE FILE FIRST. Use read_file, then copy `old` out of what it showed you.
Do not type it from memory and do not guess at the indentation - it has to
match the file exactly, including spaces and tabs. Strip off the "   12| " line
number prefix that read_file adds; that part is not in the file.

`old` MUST APPEAR EXACTLY ONCE, or the edit is refused and nothing changes.
If it appears several times you get told how many - include more surrounding
lines to make it unique, or pass "all": true if you really do mean all of them.
This is a safety feature: it stops you changing a line you never looked at.

The old text must be different from the new text, or there is nothing to do.

WHEN TO USE WHICH TOOL:
- changing part of a file that exists    -> edit_file (this one)
- making a new file, or replacing it all -> write_file
- seeing what is in a file               -> read_file

Returns "(replaced N occurrence(s) in <path>)" once it is really on disk. You
do not need to read the file back to check it worked."""


# For native provider tool-calling. run()'s own fuzzy old/new matching (any
# kwarg name merely CONTAINING "old"/"new" also works, see below) only
# matters for the old text-embedded-call path, where a model could invent a
# slightly different name; a native call sends exactly what's declared here,
# so "old"/"new" are the names that matter now.
# Runs on its own, never alongside another tool call in the same batch.
# An edit means something different depending on what landed before
# it: two edits to one file, or an edit and the read that checks it,
# have to happen in the order the model asked for them.
# See tool_processor.parallel_safe().
PARALLEL = False

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description":
            "The file to change. A relative path is from the workspace root."},
        "old": {"type": "string", "description":
            "The exact text to find, copied character for character from the file."},
        "new": {"type": "string", "description": "What to put there instead."},
        "all": {"type": "boolean", "description":
            "Optional, false by default. Set true to replace EVERY "
            "occurrence instead of requiring exactly one."},
    },
    "required": ["path", "old", "new"],
}


def run(path, all=False, workspace=None, **kwargs):
    old = None
    new = None
    for key, value in kwargs.items():
        lower = key.lower()
        if old is None and "old" in lower:
            old = value
        elif new is None and "new" in lower:
            new = value
    if old is None or new is None:
        return ("ERROR: could not find old/new text in the arguments. Pass `old` and "
                "`new` (any argument name containing 'old' or 'new' also works, e.g. "
                "old_text/new_text).")

    # workspace comes from tool_processor, never from the model - see the note
    # in read_file, and its absence from SCHEMA above.
    ws = workspace or _here()
    target = ws.resolve(path)

    try:
        if not ws.exists(target):
            return ("ERROR: there is no file at " + target + " (" + ws.where + ")"
                    + ". To make a new file, use write_file.")
        if old == new:
            return "ERROR: `old` and `new` are identical, so this edit would change nothing."
        text = ws.read_text(target)
    except OSError as e:
        return "ERROR: could not read " + target + ": " + str(e)
    except Exception as e:
        return "ERROR: " + str(e)   # workspace unreachable - the message says why

    count = text.count(old)
    if count == 0:
        return ("ERROR: that exact text is not in " + target + ". Read the file "
                "with read_file and copy `old` from what it shows you - it has to match "
                "character for character, including indentation.")
    # Refusing an ambiguous match is the whole point: replacing the first of
    # several occurrences silently changes a line the model never looked at.
    if count > 1 and not all:
        return ("ERROR: that text appears " + str(count) + " times in " + target
                + ", so it is ambiguous and nothing was changed. Either include more "
                'surrounding lines in `old` to pin down which one you mean, or pass '
                '"all": true to replace all ' + str(count) + " of them.")

    try:
        ws.write_text(target, text.replace(old, new))
    except OSError as e:
        return "ERROR: could not write " + target + ": " + str(e)
    except Exception as e:
        return "ERROR: " + str(e)
    return ("(replaced " + str(count) + " occurrence(s) in " + target
            + (" " + ws.where if ws.is_remote else "") + ")")
