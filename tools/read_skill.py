"""Loads one skill so you can follow it. Skills are knowledge rather than
code, so reading one IS using it - there is nothing to call afterwards.

Replaces read_tool, which did this and doubled as the way to fetch a .py
tool's call instructions. That second job is gone under native tool-calling:
every tool arrives with a real argument schema, so there is nothing left to
look up. read_tool.py now sits in unused/ - see TODO.md.
"""

import profiles
import tool_processor

NAME = "read_skill"
DESCRIPTION = ("Load one skill - guidance to follow, not an action. Returns the skill's "
               "full text; read it and then do what it says.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "read_skill".

Arguments:
- name: the skill you want, spelled exactly as it appears in the skills list.

A skill is knowledge, not a call format - it comes back as instructions to
follow, so READING IT IS USING IT. There is no second call to make afterwards.
Load one when the thing you are about to do is what that skill covers, and do
it the way the skill describes rather than your own way.

This reads SKILLS ONLY, never tools - a tool name here is an error, not a
lookup. (Where a tool's own arguments come from depends on how this turn was
sent, and the tool section of your instructions says so; it is never this.)"""

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description":
            "The skill you want, spelled exactly as it appears in the skills list."},
    },
    "required": ["name"],
}


def run(name, profile=None):
    # `profile` is filled in by tool_processor._run for any tool that declares
    # it, the same way chat_id and workspace are - the model never supplies
    # it. Without it this tool would be the hole in the profile's skill list:
    # every other route to a skill is filtered, and this one hands back the
    # full text of whatever it is asked for.
    #
    # Straight from the folder every time, never a cached copy, so a skill
    # just written or edited is served as it is NOW - same reason _discovery
    # reloads rather than reusing an import.
    skills = [s for s in tool_processor.find_skills()
              if profile is None or profiles.allows(profile, "skill", s["name"])]
    for skill in skills:
        if skill["name"] == name:
            return skill["instructions"]
    known = ", ".join(s["name"] for s in skills) or "(none)"
    return ("ERROR: there is no skill called " + name + ". You have: " + known
            + ". Note this reads SKILLS only - a tool's arguments are already "
              "in the schema you were sent.")
