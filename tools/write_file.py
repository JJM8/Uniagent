"""Writes a whole file in one call, so code never has to be built up with
shell echo chains.

Writes into the chat's workspace, which may be a folder on this machine or one
on another machine over ssh - see scripts/workspace.py."""

from _workspace import here as _here

NAME = "write_file"
DESCRIPTION = ("Write text to a file, creating it or replacing what's there. Use this for "
               "code, notes, config - anything text. Far more reliable than echoing "
               "lines into a file with the terminal.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "write_file". Do not explain what you are writing first.

Arguments:
- path:    where to write it. A relative path is from the workspace root, so
           "tools/weather.py" means the tools folder. Missing parent folders
           are created for you.
- content: the WHOLE file. You must send all of it - this replaces the file,
           it does not append or patch.

USE THIS INSTEAD OF THE TERMINAL FOR WRITING FILES.
Never build a file with `echo 'line' >> file` chains. That means escaping every
quote in every line, inside a shell command, inside a JSON string - it breaks,
and it has broken before. One write_file call with the whole file is always
the right way. If a file is long, that is still one call, not many.

WRITING THE CONTENT STRING:
Escape it however the call syntax you were given requires. Keep it to one
write per file and you will not have trouble.

EDITING SOMETHING THAT ALREADY EXISTS:
This OVERWRITES without warning. To change part of an existing file, use
edit_file instead - it swaps one piece and leaves the rest alone. Only use
write_file on an existing file when you genuinely mean to replace all of it,
and read_file it first so you know what you are destroying.

Returns "(wrote N lines to <path>)". That means it is really on disk - you do
not need to cat it afterwards to check.

If the user refuses you get back a string starting with "DENIED" and nothing
was written - do not immediately retry the same write, ask what they'd prefer."""

# For native provider tool-calling.
SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description":
            "Where to write it. A relative path is from the workspace root. "
            "Missing parent folders are created for you."},
        "content": {"type": "string", "description":
            "The WHOLE file - this replaces the file, it does not append or patch."},
    },
    "required": ["path", "content"],
}


def run(path, content, workspace=None):
    # workspace comes from tool_processor, never from the model - see the note
    # in read_file, and its absence from SCHEMA above.
    ws = workspace or _here()
    target = ws.resolve(path)
    lines = len(content.split("\n"))

    print("\n[write_file] writing " + str(lines) + " lines to:")
    print("    " + target + (" (" + ws.where + ")" if ws.is_remote else ""))
    try:
        if ws.exists(target):
            print("    (this file EXISTS and is being replaced)")
        # Approval is handled centrally in main.py (safety validation + y/n), so
        # by the time we get here the write has already been cleared. Doing our
        # own input() here would also hang cron jobs, which run with nobody
        # watching.
        ws.write_text(target, content)
    except OSError as e:
        return "ERROR: could not write " + target + ": " + str(e)
    except Exception as e:
        return "ERROR: " + str(e)   # workspace unreachable - the message says why
    return ("(wrote " + str(lines) + " lines to " + target
            + (" " + ws.where if ws.is_remote else "") + ")")
