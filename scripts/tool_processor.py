"""Loads the tools, spots a tool call in the model's reply, and runs it."""

import importlib
import inspect
import json
import pkgutil
import re
import sys
from pathlib import Path

import provider

TOOLS_DIR = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

# Skills live in their own folder, NOT in tools/. They are a different kind of
# thing - knowledge to read, not code to run - and mixing them in one folder
# meant every scan of tools/ had to sort out which was which, while a skill's
# folder sat among the .py files looking like a Python package that had lost
# its __init__. Nothing is imported from here: skills are markdown, so this
# folder is deliberately never added to sys.path the way tool_dirs() adds
# tools/ and its subfolders.
SKILLS_DIR = Path(__file__).parent.parent / "skills"

TOOLS = []
BROKEN = []      # tools that wouldn't load, so a bad one can't stop the agent

# The one tool whose FULL instructions go in the prompt. Every other tool gets
# its name and description listed, and the model reads the rest on demand with
# read_skill. Must stay in step with _discovery.INJECTED.
INJECTED = ("read_skill",)

REQUIRED = ("NAME", "DESCRIPTION", "INSTRUCTIONS", "run")


# A skill's YAML front matter, Claude's format: --- name/description --- then
# the instructions as markdown. Parsed by hand rather than with PyYAML - it's
# two fields, and a skill folder dropped in shouldn't need a new dependency.
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _read_skill(path):
    """One Claude-format SKILL.md as a tool-shaped dict, or None if the file
    isn't one (no front matter, or no description).

    A skill is knowledge, not code: there's nothing to run, so reading it IS
    using it - which is what read_skill already says. It gets listed like any
    other tool, and its markdown body is what read_skill hands back."""
    try:
        text = path.read_text()
    except OSError:
        return None
    m = _FRONT.match(text)
    if not m:
        return None

    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip().strip("\"'")

    description = meta.get("description")
    if not description:
        return None  # nothing to tell the model about it - not a usable skill

    # Claude puts skills in a folder as SKILL.md, so the folder is the real
    # name; front matter still wins when it gives one.
    body = m.group(2).strip()
    return {
        "name": meta.get("name") or path.parent.name,
        "description": description,
        "instructions": body,
        # Called instead of read: hand back the same body rather than erroring.
        "run": lambda _body=body, **_kw: _body,
        "skill": True,
        "path": path.relative_to(SKILLS_DIR).as_posix(),
    }


def find_skills():
    """Every Claude-format SKILL.md under skills/, in path order. Shared with
    _discovery so read_skill sees exactly what the prompt's list does.

    Only files actually named SKILL.md count. Skill bundles ship plenty of
    other markdown - reference pages, templates, and the DESCRIPTION.md blurb
    that describes a whole category folder - and those have front matter with
    a description too, so scanning every .md listed them all as skills. The
    DESCRIPTION.md ones all came through named "DESCRIPTION" with an empty
    body: fifteen identical dead entries in the skills list."""
    found = []
    for path in sorted(SKILLS_DIR.rglob("SKILL.md")):
        if "__pycache__" in path.parts:
            continue
        skill = _read_skill(path)
        if skill:
            found.append(skill)
    return found


def tool_dirs():
    """tools/ and every folder inside it, so tools can be grouped into packages."""
    dirs = [TOOLS_DIR]
    for p in sorted(TOOLS_DIR.rglob("*")):
        if p.is_dir() and p.name != "__pycache__":
            dirs.append(p)
    return dirs


def load_tools():
    """(Re)read every .py in tools/ and its subfolders. Safe to call again.

    The scan fills LOCAL lists and only assigns them to TOOLS/BROKEN once it
    is finished. That matters because the server is threaded (one thread per
    turn), so two turns can be in here at the same time. Filling the globals
    as we went meant `TOOLS = []` rebound the name mid-scan while the other
    thread was still appending - and since `TOOLS.append` looks the global up
    afresh on every call, that thread's remaining tools landed in the OTHER
    thread's list. Both passes ended up in one list, every tool twice, and a
    native turn sent two schemas under the same name: the provider rejects
    that outright with 400 "Tool names must be unique." Assigning finished
    lists in one step means a reader sees the whole old list or the whole new
    one, never a half-merged one."""
    global TOOLS, BROKEN
    importlib.invalidate_caches()  # so brand new files are noticed
    tools = []
    broken = []

    for d in tool_dirs():
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))

        for found in pkgutil.iter_modules([str(d)]):
            if found.ispkg or found.name.startswith("_"):
                continue  # a package folder, or a shared helper like _discovery
            try:
                if found.name in sys.modules:
                    m = importlib.reload(sys.modules[found.name])
                else:
                    m = importlib.import_module(found.name)

                missing = [a for a in REQUIRED if not hasattr(m, a)]
                if missing:
                    raise AttributeError("missing " + ", ".join(missing))
                if not callable(m.run):
                    raise TypeError("run is not a function")

                tools.append({
                    "name": m.NAME,
                    "description": m.DESCRIPTION,
                    "instructions": m.INSTRUCTIONS,
                    "run": m.run,
                    # Optional - a tool with no SCHEMA (a skill, or a .py
                    # tool that hasn't been given one) is simply absent from
                    # tools_schema()'s native-calling list, same as it always
                    # was invisible to anything but the prose prompt.
                    "schema": getattr(m, "SCHEMA", None),
                })
            except Exception as e:
                # A broken tool gets skipped, not crashed on. Otherwise one bad
                # file the agent wrote would stop the agent from ever starting.
                broken.append(found.name + ".py - " + type(e).__name__ + ": " + str(e))

    # Skills last, and only under names no .py tool already took - a real tool
    # is code and wins over a markdown file that happens to share its name.
    taken = {t["name"] for t in tools}
    tools.extend(s for s in find_skills() if s["name"] not in taken)

    # Published only now that both lists are complete.
    TOOLS = tools
    BROKEN = broken


def _tools_text():
    """The tool section as prose, built from whatever load_tools() last found.

    Every tool that HAS a SCHEMA is deliberately left out: it goes over the
    wire as the provider's own `tools` array (tools_schema()), carrying the
    same name and description, so listing it here as well would send each one
    twice. What's left is what that array cannot carry - skills, and any .py
    tool without a SCHEMA - which are reached the only way they ever were, by
    reading them with read_skill. So in practice this section IS the skills
    list, and it says so.

    A schema-less tool whose NAME a schema'd one already took is left out as
    well, or that one name arrives twice over with two different descriptions
    - once in the schema array, once here, contradicting it. That is exactly
    edit_file_improved.py, which shares edit_file.py's NAME and is already
    unreachable through dispatch (see its own comment); listing it would be
    advertising the wrong description for the tool that actually runs."""
    named = {t["name"] for t in TOOLS if t.get("schema")}
    listed = "Skills:\n"
    for t in TOOLS:
        if t["name"] in INJECTED or t.get("schema") or t["name"] in named:
            continue
        listed += t["name"] + ": " + t["description"] + "\n"
    if BROKEN:
        listed += ("\nBROKEN - these tool files would not load and cannot be used "
                   "until fixed: " + "; ".join(BROKEN) + "\n")

    # read_skill's own full INSTRUCTIONS are NOT injected, even though it is
    # the one tool in INJECTED. They open with "You MUST read a tool's
    # instructions BEFORE you call that tool", which is both false and
    # expensive now: every argument name, type and requiredness already
    # arrived in the schemas, so obeying it burns a round trip per tool
    # re-fetching what the model was handed. The paragraph below says what is
    # actually true instead.
    text = listed + "\n" + (
        "A skill is knowledge, not a callable tool: load one by reading "
        "it with read_skill, giving the name exactly as listed above - "
        "and reading it IS using it, there is nothing else to call "
        "afterwards.\n\n"
        "Your TOOLS are attached to this request as real schemas, with "
        "every argument name and type on them. Those are authoritative: "
        "never guess an argument, and never write a call out as text in "
        "your reply - a call only counts when it goes through the tools "
        "themselves. read_skill does not read tools - there is nothing "
        "about a tool left to look up.\n\n")

    # Some models (OpenAI's gpt-5.x especially) end their reply on a statement
    # of intent - "I'll inspect the folder and report back." - with no tool
    # call, which ends the whole turn: the promised work never happens. Said
    # here, once, for every model, because it has to sit next to the tool
    # instructions to be read at the moment it matters.
    return text + (
        "NEVER end a reply with only a promise of action - if your reply says "
        "you WILL look at, run, read or check something, it must contain the "
        "tool call that does it. Act first, then report what you found. When "
        "a request is reasonably clear, use the tools rather than asking what "
        "to do; ask only when genuinely stuck or the action is risky.\n")


def prompt_text():
    """The tool section for this turn's prompt, rebuilt from the folder.

    There is no call syntax to teach: every turn is native, so the provider
    enforces the call shape itself via the real tools schema sent alongside
    the prompt (see provider.py). Telling the model to write JSON or tags in
    its reply would be actively wrong, not merely redundant, which is why the
    prompted formats are gone rather than kept as a fallback.

    Rescanned every turn rather than cached from startup, so a tool or skill the
    agent just wrote shows up in the very next turn - which is the whole point of
    it being able to write skills mid-task."""
    load_tools()
    return _tools_text()


# Which wire shape a provider's `tools` array wants, keyed by WIRE rather than
# by provider name - provider.wire_for() maps a name to one, answering with
# the name itself for a built-in, so the built-ins are still keyed by their
# own names here. Kept in this file rather than provider.py because it is a
# fact about the schema format, and both callers that need it (main.py's turn
# loop and its token panel) already import this module. Anything unlisted gets
# "openai" - the shape openai/deepseek/local share, and the one a custom
# OpenAI-compatible endpoint wants.
SCHEMA_SHAPES = {
    "anthropic": "anthropic",
    "bedrock": "bedrock",
    "gemini": "gemini",
}


def shape_for(provider_name):
    """The tools_schema() shape `provider_name` speaks."""
    return SCHEMA_SHAPES.get(provider.wire_for(provider_name), "openai")


# Gemini's functionDeclarations take a SUBSET of JSON Schema (an OpenAPI 3.0
# cut), not the whole thing, and it rejects the request outright over a keyword
# it doesn't know rather than ignoring it. These are the ones it accepts.
_GEMINI_KEYS = ("type", "format", "title", "description", "nullable", "enum",
                "properties", "required", "items", "minItems", "maxItems",
                "default")


def _gemini_schema(node):
    """One tool SCHEMA reduced to what Gemini will actually accept.

    Two things happen. Any keyword outside _GEMINI_KEYS is dropped, and a
    oneOf/anyOf union is COLLAPSED onto its first branch rather than dropped -
    dropping it would leave a property with a description and no type at all,
    which Gemini rejects in its own right. Our unions are all of the shape
    "a string, or a list of strings" (email_send's to/cc/bcc, email_manage's
    uid), so the first branch is the plain string - the form the tool handles
    anyway, and the narrower of the two. The model loses the option of passing
    a list to those arguments on Gemini specifically; every other provider
    still gets the real union, because only this function rewrites anything."""
    if isinstance(node, list):
        return [_gemini_schema(n) for n in node]
    if not isinstance(node, dict):
        return node

    union = node.get("oneOf") or node.get("anyOf")
    merged = dict(node)
    if isinstance(union, list) and union and isinstance(union[0], dict):
        merged.pop("oneOf", None)
        merged.pop("anyOf", None)
        # The branch fills in what the union was standing in for (type, items);
        # anything the node said itself - the description - wins over it.
        merged = {**union[0], **merged}

    out = {}
    for key, value in merged.items():
        if key not in _GEMINI_KEYS:
            continue
        # "properties" is a MAP whose keys are the tool's own argument names -
        # path, offset, limit - not schema keywords, so the allowlist must not
        # be applied to them. Only their values are schemas. Filtering here
        # like everywhere else is what quietly emptied every tool's arguments:
        # the schema survived, its properties didn't, and Gemini would have
        # been handed sixteen tools that all take nothing.
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _gemini_schema(sub) for name, sub in value.items()}
        else:
            out[key] = _gemini_schema(value)
    return out


def tools_schema(shape="openai"):
    """Every tool that has a SCHEMA, as a provider-shaped `tools` array for
    native tool-calling - what a "tool_syntax": "native" turn sends alongside
    the prompt instead of (or as well as) prompt_text()'s prose. A tool with
    no SCHEMA (a skill, or a .py tool that hasn't been given one) is simply
    left out, same as it was always invisible to anything but the prompt.

    Four shapes, all carrying the same three facts - name, description, and
    the tool's own JSON-Schema object - under whatever keys that provider's
    API happens to call them:

      openai     {"type": "function", "function": {name, description,
                 parameters}}  - openai/deepseek/local share one wire, see
                 provider.py's _openai_style.
      anthropic  {name, description, input_schema}
      bedrock    {"toolSpec": {name, description, inputSchema: {"json": ...}}}
                 - converse_stream's own wrapper; the caller puts the list
                 under {"toolConfig": {"tools": [...]}}.
      gemini     ONE {"functionDeclarations": [...]} holding every tool, not
                 one entry each: Gemini refuses a `tools` array with several
                 non-search Tool objects in it. The schema is also rewritten
                 on the way through - see _gemini_schema.

    Rescanned fresh via load_tools() every call, same as prompt_text() - a
    tool the agent just wrote shows up in the very next turn."""
    load_tools()
    usable = [t for t in TOOLS if t.get("schema")]
    if shape == "anthropic":
        return [{"name": t["name"], "description": t["description"],
                  "input_schema": t["schema"]} for t in usable]
    if shape == "bedrock":
        return [{"toolSpec": {"name": t["name"], "description": t["description"],
                              "inputSchema": {"json": t["schema"]}}} for t in usable]
    if shape == "gemini":
        return [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": _gemini_schema(t["schema"])} for t in usable]}]
    return [{"type": "function", "function": {"name": t["name"],
              "description": t["description"], "parameters": t["schema"]}}
            for t in usable]


def schema_entries(shape="openai"):
    """tools_schema(shape) as [(tool name, the exact JSON sent for it)] - one
    entry per TOOL, whatever the shape nests it in.

    For the token panel, which asks "which tools does this model have, and what
    does each cost". Gemini is the reason this exists rather than the panel
    picking the name out itself: its whole array is a single object holding
    every declaration, so there is no per-tool entry to read a name off. The
    text is the real wire JSON, never re-indented - the panel's counts are
    taken straight off it (see main.py's injection_breakdown)."""
    array = tools_schema(shape)
    if shape == "gemini":
        decls = array[0]["functionDeclarations"] if array else []
        return [(d["name"], json.dumps(d)) for d in decls]
    if shape == "bedrock":
        return [(t["toolSpec"]["name"], json.dumps(t)) for t in array]
    if shape == "anthropic":
        return [(t["name"], json.dumps(t)) for t in array]
    return [(t["function"]["name"], json.dumps(t)) for t in array]


load_tools()


_FENCE = re.compile(r"```.*?```", re.DOTALL)


def unfenced(text):
    """`text` with every fenced ``` code block removed, plus anything after
    an unterminated opening fence dropped too (still-streaming or the model
    just never closed it). A tool call written inside a fence - a whole file
    quoted back, an example buried in the middle of a longer block, or just
    "you'd write something like `web_search({...})`" - is documentation, not
    an attempt, and must never be mistaken for one, no matter how tightly or
    loosely the fence sits around it."""
    text = _FENCE.sub("", text)
    open_at = text.find("```")
    return text if open_at == -1 else text[:open_at]


def looks_like_call(text):
    """True if a REAL tool's name is immediately followed by "(" outside any
    fence - a model WRITING a call as prose instead of making one.

    Every turn is native now: a real call arrives on the provider's own
    structured channel, never as text, so any call-shaped text in the reply is
    by definition a call that didn't happen. That is worth catching rather
    than accepting as a final answer - a model that types `web_search({...})`
    and stops has silently done nothing, and the turn ends looking like it
    answered. main.py sends this back as a nudge to use the real tools.

    Checked on the UNFENCED text only: something call-shaped sitting inside a
    ``` block (a tool's source quoted back, a pinned skill's content) was
    never an attempt, so it must not even trip the nudge."""
    text = unfenced(text)
    return any(re.search(r"\b" + re.escape(t["name"]) + r"\s*\(", text) for t in TOOLS)


def _find(name):
    for t in TOOLS:
        if t["name"] == name:
            return t
    return None


def process(call, chat_id=None):
    """Run the tool the call asks for and return its output as text.

    chat_id is the conversation the call came from, and is handed to any tool
    whose run() declares it - that's how the terminal keeps one open shell per
    chat instead of one for everybody. Tools that don't ask for it never see
    it, so nothing else needed changing."""
    t = _find(call["tool"])
    if t is None:
        # Might be one the agent just wrote, so re-read the folder and retry.
        load_tools()
        t = _find(call["tool"])
    if t is None:
        known = ", ".join(x["name"] for x in TOOLS)
        msg = "ERROR: there is no tool called " + call["tool"] + ". You have: " + known
        if BROKEN:
            msg += ". These tool files are broken and were skipped: " + "; ".join(BROKEN)
        return msg
    # Copied, then chat_id set LAST so it overwrites: if the model puts a
    # chat_id in its own args - by mistake or on purpose - it is discarded and
    # the real one wins, so it can never reach into another chat's terminal.
    args = dict(call.get("args", {}))
    try:
        if "chat_id" in inspect.signature(t["run"]).parameters:
            args["chat_id"] = chat_id
    except (TypeError, ValueError):
        pass  # can't read the signature - just call it the plain way
    try:
        return t["run"](**args)
    except Exception as e:
        return "ERROR running " + call["tool"] + ": " + type(e).__name__ + ": " + str(e)
