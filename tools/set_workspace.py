"""Moves this chat to another workspace - another folder, or another machine.

The list comes from WORKSPACES in .env, which is the same list the settings
page writes and the same one the dropdown in the chat window shows. Nothing
here can invent a workspace: this tool switches between the ones that are
already configured, so "work on the Pi instead" is one call, and "work in /"
is not something it is possible to say.
"""

import sys
from pathlib import Path

# Add scripts/ to the module path so we can import provider and main. Guarded:
# the tool loader re-imports this module on every scan, and an unconditional
# insert would grow sys.path without limit.
_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import main
import provider
import workspace as workspace_mod

NAME = "set_workspace"
DESCRIPTION = ("Move this conversation to a different workspace - a different project "
               "folder, or a different computer over ssh. Call with no arguments to see "
               "which ones exist and which you are in.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "set_workspace".

Arguments:
- name: OPTIONAL, which workspace to move to - its id or its name. Leave it out
        to list the workspaces and see which one you are in now.

WHAT A WORKSPACE IS: a root directory, and optionally a machine. Every file
tool (read_file, write_file, edit_file, ask_file) and the terminal work inside
whichever one this chat is in. A remote workspace means those tools genuinely
run on that machine over ssh - `pwd` in the terminal is a directory over there,
and a file you write lands on that computer, not this one.

WHEN TO USE IT: when the work is somewhere else. "Check the logs on the Pi",
"build the site in my projects folder". Switch once and everything afterwards
happens there - do not switch back and forth mid-task.

WHAT IT CHANGES: this chat only, from the next tool call onwards, and it stays
until it is changed again. Other chats are untouched. The terminal gets a fresh
shell in the new workspace, so anything you had open in the old one - a running
process, an activated venv, a directory you had cd'd into - is left behind
there.

IT CANNOT CREATE ONE. If the workspace you want is not in the list, say so and
let the user add it on the settings page - it needs a path and, for another
machine, ssh access that is already set up."""

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description":
            "Which workspace to move to - its id or its name. Omit to list them."},
    },
    "required": [],
}


def _listing(current_id):
    entries = provider.workspaces()
    if not entries:
        return ("There are no workspaces configured, so this chat works in the "
                "Uniagent folder itself. They are added on the settings page, "
                "under workspaces.")
    lines = []
    for w in entries:
        marks = []
        if w["id"] == (current_id or ""):
            marks.append("CURRENT")
        elif not current_id and w["default"]:
            marks.append("CURRENT - the default")
        elif w["default"]:
            marks.append("default")
        if w["ssh"]:
            marks.append("on " + w["ssh"])
        lines.append("- " + w["id"] + " (" + w["name"] + "): " + w["path"]
                     + (("  [" + ", ".join(marks) + "]") if marks else ""))
    return "Workspaces:\n" + "\n".join(lines)


def run(name=None, chat_id=None, workspace=None):
    # chat_id and workspace are filled in by tool_processor, never by the model
    # - the same arrangement every workspace-aware tool has, and the reason
    # neither is in SCHEMA above.
    current = getattr(workspace, "id", "") or ""

    if not name:
        return _listing(current)

    wanted = str(name).strip().lower()
    entries = provider.workspaces()
    match = next((w for w in entries if w["id"] == wanted), None)
    if match is None:
        match = next((w for w in entries if w["name"].strip().lower() == wanted), None)
    if match is None:
        return ("ERROR: there is no workspace called " + str(name) + ".\n"
                + _listing(current) + "\nThis tool can only switch between the "
                "workspaces above - it cannot create one.")

    if chat_id is None:
        return ("ERROR: this call has no chat behind it, so there is nothing to "
                "move. (Workspaces belong to a conversation.)")

    if match["id"] == current:
        return "Already in " + match["name"] + " (" + match["path"] + ") - nothing to do."

    try:
        chat = main.chat(main.chat_md(chat_id))
        chat.workspace = match["id"]
        chat._write_settings()
    except (OSError, ValueError) as e:
        return "ERROR: could not save the workspace onto this chat: " + str(e)

    # Say whether it is actually reachable now rather than letting the next
    # tool call be the thing that discovers the machine is off.
    ws = workspace_mod.get(match["id"])
    ok, message = ws.check()
    head = ("Moved this chat to " + match["name"] + " - " + ws.where
            + ". Files and terminal commands now happen there.")
    if not ok:
        return (head + "\n\nWARNING: it is not reachable at the moment:\n" + message
                + "\nThe chat has still been moved; tell the user, rather than "
                "retrying blindly.")
    return head + "\n" + message
