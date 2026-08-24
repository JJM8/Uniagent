"""Reach the tools on Uniagent's configured MCP servers.

NOTE THE FILENAME. This is mcp_tool.py, not mcp.py, even though the tool is
called "mcp". tool_processor puts tools/ on sys.path, so a file here named
mcp.py would be imported as the top-level module `mcp` and shadow the MCP SDK
itself - for this file, for scripts/mcp_client.py, and for anything else that
ever imports it. NAME is what the model sees; the filename is free, so it gets
out of the way.

The heavy lifting is in scripts/mcp_client.py - the event loop, the sessions,
the JSON-RPC. This file is just the tool surface over it.

THIS IS THE FALLBACK PATH, NOT THE MAIN ONE. A server given a "tools"
allowlist in mcp.json has each of those tools attached to the model as a tool
in its own right, carrying the server's own JSON Schema, so the provider
constrains its arguments exactly as it does for every tool in this folder -
see mcp_client.flattened(). What is left for this file is the servers that
were not flattened, where the model still has to ask what's there and then
type the arguments itself, plus "reconnect", which belongs here for all of
them because it is about a server rather than about any one tool.
"""

import sys
from pathlib import Path

# Guarded: _discovery re-imports this module on every scan of tools/, so an
# unconditional insert added another copy of the same path each time and
# sys.path grew without limit.
_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import mcp_client

NAME = "mcp"

# Bring the servers up in the background the first time this file is imported.
# Idempotent inside mcp_client - which matters, because load_tools() re-imports
# this file on every single turn.
mcp_client.start()


def _servers_line():
    """The configured servers, as one line for the description below.

    Built fresh every turn, which works because load_tools() re-reads
    DESCRIPTION after reloading this module - so the model is told what is
    actually connected right now, not what was connected at startup. Reading a
    cache, never waiting on a server: this runs while the prompt is being
    assembled.

    Putting the servers HERE rather than behind an action is the whole reason
    this tool needs only two verbs. A "list the servers" action would cost a
    round trip before the model could do anything at all, every time.

    Servers split in two. One whose tools are flattened AND up has nothing to
    do with this tool - its tools arrived as tools in their own right, so it is
    named only to say so and to stop the model coming through here for them. A
    flattened server that is broken or still connecting belongs with the rest:
    its tools are not attached, and this tool is what remains."""
    summary = mcp_client.summary()
    if not summary:
        return "No MCP servers are configured (see mcp.json). "

    def count(n):
        return str(n) + (" tool" if n == 1 else " tools")

    direct, here = [], []
    for name, s in summary.items():
        if s["state"] == "broken":
            here.append(name + " (UNAVAILABLE: " + s["error"][:80] + ")")
            continue
        if s["state"] != "ready":
            here.append(name + " (" + s["state"] + ")")
            continue

        # A flattened server can appear in BOTH lists, and should: its tools
        # went direct, but its resources and prompts have no such home and are
        # still reached through here.
        if s.get("flat"):
            direct.append(name + " (" + count(s["tools"]) + ")")
        caps = s.get("caps") or {}
        got = ([] if s.get("flat") else [count(s["tools"])])
        if caps.get("resources"):
            got.append("resources")
        if caps.get("prompts"):
            got.append("prompts")
        if got:
            here.append(name + " (" + ", ".join(got) + ")")

    text = ""
    if direct:
        text += ("These servers' TOOLS are ALREADY attached to you as tools of "
                 "their own, named mcp__server__tool, with their arguments on "
                 "them - call those directly and do not come through this tool "
                 "for them: " + ", ".join(direct) + ". ")
    if here:
        text += "Reached through THIS tool: " + ", ".join(here) + ". "
    else:
        text += "Nothing else needs this tool. "
    return text


# The two-step and the reconnect rule are stated HERE, not in INSTRUCTIONS,
# because INSTRUCTIONS is never delivered: tool_processor only injects the full
# instructions of the tools in its INJECTED tuple, and read_skill reads skills
# and refuses tool names. DESCRIPTION and SCHEMA are the whole of what the
# model is told about this tool, so anything it must know goes in one of them.
DESCRIPTION = ("Reach an MCP server - a separate program exposing abilities "
               "Uniagent doesn't have built in. " + _servers_line()
               + "TOOLS: action \"tools\" to see what a server offers and what "
                 "arguments each of its tools takes, then \"call\" to run one. "
                 "You cannot know a tool's arguments before you have listed "
                 "them and a guessed name is rejected, so do not skip the "
                 "listing. RESOURCES, meaning data the server exposes such as "
                 "files, logs or project state: action \"resources\" to list "
                 "them, then \"read\" with the exact uri. PROMPTS, meaning "
                 "ready-made instructions it offers: action \"prompts\" to "
                 "list, then \"prompt\" to load one - what comes back is "
                 "guidance to follow, with nothing to call afterwards. Also "
                 "\"complete\" for the values an argument will accept, "
                 "\"logs\" for what a server has reported about itself, and "
                 "\"reconnect\" if one stops working - do not go looking for "
                 "its process in the terminal.")

# NOT injected into any prompt - see the comment above DESCRIPTION. Kept
# because every tool file must define it, and because it is the readable
# account of how this tool works for whoever opens the file.
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "mcp".

An MCP server can expose four kinds of thing, and there is an action for each:
TOOLS to run, RESOURCES to read, PROMPTS to follow, and COMPLETIONS telling you
what an argument will accept. A server offers some or all of them; this tool's
description says which, per server, so you never have to probe for it.

Arguments:
- action:  "tools"/"call" for tools, "resources"/"read" for resources,
           "prompts"/"prompt" for prompts, "complete" for suggested argument
           values, "logs" for what a server has reported, "reconnect" to
           re-dial a server whose connection has broken.
- server:  which server, named exactly as listed in this tool's description.
- tool:    which tool on that server. Required for "call". With "tools" it is
           optional and narrows the listing to that one tool.
- uri:     which resource, for "read", "watch" and "unwatch" - exactly as the
           listing gave it. Also the URI template being completed, for
           "complete".
- name:    which prompt, for "prompt". Also the prompt being completed, for
           "complete".
- args:    an object of arguments - for the tool being called, or for the
           prompt being loaded. Use the exact names the listing gave you.
           Omit it if there are none.
- argument/value: for "complete" - which argument you want values for, and
           what you have typed of it so far (value may be omitted).
- level:   for "logs", OPTIONAL - debug, info, notice, warning, error,
           critical, alert or emergency. Sets how much the server sends from
           now on; omit it to just read what has arrived.
- timeout: OPTIONAL, seconds to wait for a result. Defaults to 60. Raise it
           for something genuinely slow.

TWO STEPS, NOT ONE. You do not know a server's tools or their arguments until
you ask. Run "tools" first and read the argument names off the schema it hands
back; then "call". Guessing an argument name wastes the call - the server
rejects what it doesn't recognise. Once you have listed a server's tools in
this conversation you already have them, so go straight to "call" from then on.

NOT EVERY SERVER COMES THROUGH HERE. A server given a "tools" allowlist in
mcp.json has those tools attached to you directly instead, named
mcp__server__tool, each carrying the server's own argument schema - no listing
step, and nothing to guess. This tool is for the servers that were not
flattened that way, and for "reconnect", which works on any of them.

A server with many tools is a lot to read at once. If you already know the name
of the tool you want, give it as `tool` alongside action "tools" and you get
just that one.

RESOURCES ARE READ, NOT CALLED. A resource is data the server exposes behind a
uri - a file, a log, some project state. List them with "resources", then
"read" the uri you want, exactly as given. Some are URI TEMPLATES with {braces}
in them: fill the braces in yourself and read the result the same way, and use
"complete" if you don't know what goes in one.

A PROMPT IS KNOWLEDGE, NOT AN ACTION. "prompt" hands back instructions to
follow, exactly like read_skill does for a skill - reading it IS using it, and
there is nothing to call afterwards. A prompt may take arguments of its own;
"prompts" lists them and says which are required.

WHAT THIS IS NOT: this does not talk to another AI, and it is not a way to
send a message somewhere. An MCP server is a program that offers a fixed set
of tools, resources and prompts - this is how you reach them.

IF A SERVER STOPS WORKING: use action "reconnect" on it. Do NOT go hunting
through the terminal for the server's process, and do NOT run python against
scripts/mcp_client.py to reset it - a script run from the terminal is a
SEPARATE process with its own connections, so it will cheerfully report the
server healthy while this one stays broken. "reconnect" is the only thing that
touches the connection you are actually using. Killing the server's process by
hand is worse than useless: it breaks the connection that was about to be
re-dialled for you automatically.

Example, listing one server's tools:
  action "tools", server "kicad"

Example, listing just one of them:
  action "tools", server "kicad", tool "run_drc"

Example, running it:
  action "call", server "kicad", tool "run_drc",
  args {"project_file": "/home/you/boards/amp.kicad_pro"}

Example, reading a resource:
  action "resources", server "unity"          (to see what there is)
  action "read", server "unity", uri "unity://tests"

Example, loading a prompt:
  action "prompts", server "unity"
  action "prompt", server "unity", name "gameobject_handling",
  args {"target": "Player"}

Example, asking what an argument accepts:
  action "complete", server "unity", name "gameobject_handling",
  argument "target", value "Pl" """

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
            "enum": ["tools", "call", "resources", "read", "watch", "unwatch",
                     "prompts", "prompt", "complete", "logs", "reconnect"],
            "description":
            "\"tools\" to see what a server offers and what arguments each of "
            "its tools takes, \"call\" to run one. \"resources\" to list the "
            "data a server exposes, \"read\" to fetch one by uri, \"watch\" / "
            "\"unwatch\" to be told when one changes. \"prompts\" to list the "
            "ready-made instructions it offers, \"prompt\" to load one. "
            "\"complete\" for the values an argument accepts. \"logs\" for what "
            "a server has reported about itself, including anything it has "
            "said about watched resources. \"reconnect\" to drop and re-dial a "
            "server whose connection has broken."},
        "server": {"type": "string", "description":
            "Which MCP server, named exactly as listed in this tool's description."},
        "tool": {"type": "string", "description":
            "Which tool on that server. Required for \"call\"; optional with "
            "\"tools\", where it narrows the listing to that one tool."},
        "uri": {"type": "string", "description":
            "Which resource, for \"read\" - exactly as the \"resources\" "
            "listing gave it. For \"complete\", the URI template whose "
            "argument you are completing."},
        "name": {"type": "string", "description":
            "Which prompt, for \"prompt\". For \"complete\", the prompt whose "
            "argument you are completing."},
        "args": {"type": "object", "description":
            "Arguments for the tool being called or the prompt being loaded, "
            "using the exact names the listing gave. Omit if there are none."},
        "argument": {"type": "string", "description":
            "For \"complete\": which argument you want the valid values of."},
        "value": {"type": "string", "description":
            "For \"complete\": what you have of that argument so far, used to "
            "narrow the suggestions. Omit to get them all."},
        "level": {"type": "string",
            "enum": list(mcp_client.LOG_LEVELS),
            "description":
            "For \"logs\", optional: how much the server should send from now "
            "on. Omit to just read what has already arrived."},
        "timeout": {"type": "integer", "description":
            "Optional, seconds to wait for a result. Defaults to 60."},
    },
    "required": ["action", "server"],
}

MAX_CHARS = 40000   # a server's full listing is not worth a whole context window


def _listing(tools):
    """A server's tools as something readable, with each one's arguments named.

    The schema is rendered rather than dumped as raw JSON Schema: the model
    needs the argument names, their types and which are required, and that is
    three lines a tool instead of forty. Anything unusual in the schema
    (nested objects, unions) still comes through as its type name, which is
    enough to know the argument exists and to ask for that one tool on its own
    if the detail matters."""
    out = ""
    for t in tools:
        out += t["name"] + ": " + (t["description"] or "(no description)").strip() + "\n"
        schema = t["schema"] if isinstance(t["schema"], dict) else {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        if not props:
            out += "    (takes no arguments)\n"
        for arg, spec in props.items():
            spec = spec if isinstance(spec, dict) else {}
            kind = spec.get("type") or "any"
            note = " REQUIRED" if arg in required else " optional"
            desc = (spec.get("description") or "").strip().replace("\n", " ")
            out += "    " + arg + " (" + str(kind) + "," + note + ")"
            out += (": " + desc if desc else "") + "\n"
        out += "\n"
        if len(out) > MAX_CHARS:
            return out + ("... TRUNCATED - too many tools to show at once. Ask "
                          "again with `tool` set to the one you want.\n")
    return out


def _entry(item, label):
    """One resource or template as a line and an indented description."""
    out = item[label] + (("  [" + item["mime"] + "]") if item.get("mime") else "")
    if item.get("name") and item["name"] != item[label]:
        out += "  - " + item["name"]
    out += "\n"
    if item.get("description"):
        out += "    " + item["description"].replace("\n", " ") + "\n"
    return out


def _resources_text(items, templates):
    """A server's resources, and separately its URI templates.

    The two are kept apart because they are used differently: a resource's uri
    is read as given, a template's has to be filled in first. Running them
    together in one list invites reading a uri with {braces} still in it."""
    out = ""
    if items:
        out += "Resources - read one with action \"read\" and its exact uri:\n\n"
        for r in items:
            out += _entry(r, "uri")
            if len(out) > MAX_CHARS:
                return out + "\n... TRUNCATED - too many to show at once.\n"
    if templates:
        out += ("\nURI TEMPLATES - fill in the {braces} yourself, then \"read\" "
                "the result. Use action \"complete\" if you don't know what "
                "goes in one:\n\n")
        for t in templates:
            out += _entry(t, "uri")
            if len(out) > MAX_CHARS:
                return out + "\n... TRUNCATED - too many to show at once.\n"
    return out or "This server lists no resources.\n"


def _prompts_text(prompts):
    """A server's prompts, each with its arguments and which are required."""
    out = ("Prompts - load one with action \"prompt\" and its name. What comes "
           "back is instructions to follow, not a result:\n\n")
    for p in prompts:
        out += p["name"] + ": " + (p["description"] or "(no description)") + "\n"
        if not p["arguments"]:
            out += "    (takes no arguments)\n"
        for a in p["arguments"]:
            out += ("    " + a["name"]
                    + (" (REQUIRED)" if a["required"] else " (optional)")
                    + ((": " + a["description"].replace("\n", " "))
                       if a["description"] else "") + "\n")
        out += "\n"
        if len(out) > MAX_CHARS:
            return out + "... TRUNCATED - too many prompts to show at once.\n"
    return out


def run(action, server, tool=None, uri=None, name=None, args=None,
        argument=None, value="", level=None, timeout=60):
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 60
    if args is not None and not isinstance(args, dict):
        return ("ERROR: `args` must be an object of argument names to values, "
                "not " + type(args).__name__ + ".")

    if action == "tools":
        found, error = mcp_client.catalogue(server, tool)
        if error:
            return "ERROR: " + error
        return ("Tools on the \"" + server + "\" MCP server. Call one with "
                "action \"call\", giving these exact argument names in `args`.\n\n"
                + _listing(found))

    if action == "call":
        if not tool:
            return ("ERROR: action \"call\" needs a `tool` as well - which tool on "
                    "\"" + server + "\" to run. Use action \"tools\" to see them.")
        return mcp_client.call(server, tool, args or {}, timeout)

    if action == "resources":
        items, templates, error = mcp_client.resources(server)
        if error:
            return "ERROR: " + error
        return ("Resources on the \"" + server + "\" MCP server.\n\n"
                + _resources_text(items, templates))

    if action == "read":
        if not uri:
            return ("ERROR: action \"read\" needs a `uri` as well. Use action "
                    "\"resources\" on \"" + server + "\" to see what there is.")
        return mcp_client.read(server, uri, timeout)

    if action == "prompts":
        found, error = mcp_client.prompts(server)
        if error:
            return "ERROR: " + error
        if not found:
            return "The \"" + server + "\" server lists no prompts."
        return ("Prompts on the \"" + server + "\" MCP server.\n\n"
                + _prompts_text(found))

    if action == "prompt":
        if not name:
            return ("ERROR: action \"prompt\" needs a `name` as well - which "
                    "prompt to load. Use action \"prompts\" to see them.")
        return mcp_client.prompt(server, name, args or {}, timeout)

    if action == "complete":
        if not argument:
            return ("ERROR: action \"complete\" needs an `argument` - which "
                    "argument you want the valid values of.")
        # Which of the two is being completed is read off whichever target was
        # given, rather than asked for as a third argument that could disagree
        # with it.
        if name and uri:
            return ("ERROR: give either `name` (completing a prompt's argument) "
                    "or `uri` (completing a URI template's), not both.")
        if name:
            return mcp_client.complete(server, "prompt", name, argument,
                                       value or "", timeout)
        if uri:
            return mcp_client.complete(server, "resource", uri, argument,
                                       value or "", timeout)
        return ("ERROR: action \"complete\" needs `name` (the prompt whose "
                "argument you are completing) or `uri` (the URI template).")

    if action in ("watch", "unwatch"):
        if not uri:
            return ("ERROR: action \"" + action + "\" needs a `uri` - which "
                    "resource to watch. Use action \"resources\" to see them.")
        return mcp_client.watch(server, uri, action == "watch", timeout)

    if action == "logs":
        return mcp_client.logs(server, level, timeout)

    if action == "reconnect":
        # In THIS process, which is the whole point. Uniagent's servers belong
        # to the running Uniagent - a python one-liner in the terminal tool
        # gets its own mcp_client, its own event loop and its own child
        # process, reports everything healthy, and changes nothing here.
        mcp_client.drop(server)
        found, error = mcp_client.catalogue(server)
        if error:
            return "ERROR: reconnected \"" + server + "\" and it failed: " + error
        return ("Reconnected to \"" + server + "\". It has " + str(len(found))
                + " tools. Try your call again.")

    return ("ERROR: no such action \"" + str(action) + "\". Use \"tools\" or "
            "\"call\" for tools, \"resources\"/\"read\"/\"watch\" for "
            "resources, \"prompts\" or \"prompt\" for prompts, \"complete\" "
            "for an argument's valid values, \"logs\", or \"reconnect\".")
