"""Reach the tools on Uniagent's configured MCP servers.

NOTE THE FILENAME. This is mcp_tool.py, not mcp.py, even though the tool is
called "mcp". tool_processor puts tools/ on sys.path, so a file here named
mcp.py would be imported as the top-level module `mcp` and shadow the MCP SDK
itself - for this file, for scripts/mcp_client.py, and for anything else that
ever imports it. NAME is what the model sees; the filename is free, so it gets
out of the way.

The heavy lifting is in scripts/mcp_client.py - the event loop, the sessions,
the JSON-RPC. This file is just the tool surface over it.
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
    round trip before the model could do anything at all, every time."""
    summary = mcp_client.summary()
    if not summary:
        return "No MCP servers are configured (see mcp.json)."

    parts = []
    for name, s in summary.items():
        if s["state"] == "ready":
            parts.append(name + " (" + str(s["tools"]) + " tools)")
        elif s["state"] == "broken":
            parts.append(name + " (UNAVAILABLE: " + s["error"][:80] + ")")
        else:
            parts.append(name + " (" + s["state"] + ")")
    return "Servers: " + ", ".join(parts) + "."


DESCRIPTION = ("Use the tools on an MCP server - separate programs that expose "
               "extra abilities Uniagent doesn't have built in. " + _servers_line()
               + " Call with action \"tools\" to see what a server offers and what "
                 "arguments each of its tools takes, then \"call\" to run one.")

INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "mcp".

Arguments:
- action:  "tools" to see what a server offers, "call" to run one of them,
           "reconnect" to re-dial a server whose connection has broken.
- server:  which server, named exactly as listed in this tool's description.
- tool:    which tool on that server. Required for "call". With "tools" it is
           optional and narrows the listing to that one tool.
- args:    an object holding the arguments for the tool you are calling. Use
           the exact argument names "tools" gave you. Omit it if the tool
           takes none.
- timeout: OPTIONAL, seconds to wait for a result. Defaults to 60. Raise it
           for something genuinely slow.

TWO STEPS, NOT ONE. You do not know a server's tools or their arguments until
you ask. Run "tools" first and read the argument names off the schema it hands
back; then "call". Guessing an argument name wastes the call - the server
rejects what it doesn't recognise. Once you have listed a server's tools in
this conversation you already have them, so go straight to "call" from then on.

A server with many tools is a lot to read at once. If you already know the name
of the tool you want, give it as `tool` alongside action "tools" and you get
just that one.

WHAT THIS IS NOT: this does not talk to another AI, and it is not a way to
send a message somewhere. An MCP server is a program that offers a fixed set
of tools, exactly like the tools you already have - this is how you reach them.

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
  args {"project_file": "/home/you/boards/amp.kicad_pro"}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["tools", "call", "reconnect"],
            "description":
            "\"tools\" to see what a server offers and what arguments each of "
            "its tools takes. \"call\" to run one of them. \"reconnect\" to "
            "drop and re-dial a server whose connection has broken."},
        "server": {"type": "string", "description":
            "Which MCP server, named exactly as listed in this tool's description."},
        "tool": {"type": "string", "description":
            "Which tool on that server. Required for \"call\"; optional with "
            "\"tools\", where it narrows the listing to that one tool."},
        "args": {"type": "object", "description":
            "The arguments for the tool being called, using the exact names "
            "given by \"tools\". Omit if the tool takes no arguments."},
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


def run(action, server, tool=None, args=None, timeout=60):
    if action == "tools":
        found, error = mcp_client.catalogue(server, tool)
        if error:
            return "ERROR: " + error
        return ("Tools on the \"" + server + "\" MCP server. Call one with "
                "action \"call\", giving these exact argument names in `args`.\n\n"
                + _listing(found))

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

    if action == "call":
        if not tool:
            return ("ERROR: action \"call\" needs a `tool` as well - which tool on "
                    "\"" + server + "\" to run. Use action \"tools\" to see them.")
        if args is not None and not isinstance(args, dict):
            return ("ERROR: `args` must be an object of argument names to values, "
                    "not " + type(args).__name__ + ".")
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = 60
        return mcp_client.call(server, tool, args or {}, timeout)

    return ("ERROR: action must be \"tools\" (see what a server offers) or "
            "\"call\" (run one of them), not \"" + str(action) + "\".")
