"""Loads the tools, spots a tool call in the model's reply, and runs it."""

import ast
import importlib
import inspect
import json
import pkgutil
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import provider
import tool_results
import turnctx
import workspace

# Guarded, and mcp_client is guarded again at the point of use below. This
# module failing to import takes the whole agent down with it, and MCP is an
# optional extra whose SDK a given install may simply not have - the same
# reasoning that makes one unreachable server a skipped tool rather than a
# crash (see mcp_client's own module docstring).
try:
    import mcp_client
except Exception:
    mcp_client = None

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

# Where a switched-off tool or skill is kept. Disabling MOVES the file here,
# which is the whole mechanism: load_tools() and find_skills() below scan
# tools/ and skills/, so something that isn't in either is not imported, not
# in the prompt, not in the schema the provider is sent, and not findable by
# process() - there is no "enabled" flag anywhere to be read, honoured in one
# place and forgotten in another.
#
# Outside tools/ and skills/, deliberately: tool_dirs() walks every folder
# under tools/ and find_skills() rglobs skills/, so a "disabled" folder inside
# either would be scanned like any other subfolder and nothing would actually
# turn off.
#
# NOT unused/ - that folder is the user's own scratch space for dev leftovers,
# and a UI that moved files in and out of it would trample things it did not
# put there.
DISABLED_DIR = Path(__file__).parent.parent / "disabled"
DISABLED_TOOLS = DISABLED_DIR / "tools"
DISABLED_SKILLS = DISABLED_DIR / "skills"

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


def _read_skill(path, root=None):
    """One Claude-format SKILL.md as a tool-shaped dict, or None if the file
    isn't one (no front matter, or no description).

    `root` is the folder its "path" is reported relative to, skills/ by
    default - the tools tab passes disabled/skills/ so a switched-off skill
    can be read the same way without pretending it lives somewhere it doesn't.

    A skill is knowledge, not code: there's nothing to run, so reading it IS
    using it - which is what read_skill already says. It gets listed like any
    other tool, and its markdown body is what read_skill hands back."""
    try:
        text = path.read_text(encoding="utf-8")
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
        "path": path.relative_to(root or SKILLS_DIR).as_posix(),
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


def _scan_files():
    """(Re)read every .py in tools/ and its subfolders, plus the skills, and
    hand back (tools, broken). The expensive half of load_tools(), and the
    half that only changes when a FILE does - which is what lets load_tools()
    below skip it entirely most of the time.

    Deliberately does NOT include the MCP tools. Those come from a live
    catalogue rather than from disk (see load_tools), so they cannot be part
    of anything keyed on file mtimes.

    Builds LOCAL lists and returns them finished. That matters because the
    server is threaded (one thread per turn), so two turns can be in here at
    the same time. Filling the globals as we went meant `TOOLS = []` rebound
    the name mid-scan while the other thread was still appending - and since
    `TOOLS.append` looks the global up afresh on every call, that thread's
    remaining tools landed in the OTHER thread's list. Both passes ended up in
    one list, every tool twice, and a native turn sent two schemas under the
    same name: the provider rejects that outright with 400 "Tool names must be
    unique." Handing back a finished pair, for load_tools() to publish in one
    assignment, means a reader sees the whole old list or the whole new one,
    never a half-merged one."""
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
                    # Also optional, and declared the same way - in the tool's
                    # own file, not in a config somewhere else. See
                    # parallel_safe() for what it means and who reads it.
                    "parallel": bool(getattr(m, "PARALLEL", True)),
                })
            except Exception as e:
                # A broken tool gets skipped, not crashed on. Otherwise one bad
                # file the agent wrote would stop the agent from ever starting.
                broken.append(found.name + ".py - " + type(e).__name__ + ": " + str(e))

    # Skills last, and only under names no .py tool already took - a real tool
    # is code and wins over a markdown file that happens to share its name.
    taken = {t["name"] for t in tools}
    tools.extend(s for s in find_skills() if s["name"] not in taken)

    return tools, broken


# How long the tool snapshot is trusted before the folder is fingerprinted
# again. The same second filecache uses, for the same reason: far shorter than
# a turn, so "a tool the agent just wrote shows up in the very next turn" -
# which is the whole point of scanning at all - still holds.
TOOLS_RECHECK = 1.0

_snap_lock = threading.Lock()
# The file-based half of the tool list, and the fingerprint it was built from.
# `checked` is when that fingerprint was last taken, which is what the recheck
# window is measured against.
_snapshot = {"fingerprint": None, "tools": [], "broken": [], "checked": 0.0}


def _fingerprint():
    """(path, mtime_ns, size) for every file _scan_files() reads, as a tuple.

    mtime_ns and size together rather than mtime alone: two writes inside one
    filesystem timestamp tick are rare, and one that also lands on the same
    byte count is not a case worth re-importing every tool to catch.

    A file that vanishes simply stops appearing here, so a deleted or disabled
    tool moves the fingerprint exactly as an edited one does."""
    marks = []
    for d in tool_dirs():
        try:
            entries = sorted(d.glob("*.py"))
        except OSError:
            continue
        for path in entries:
            try:
                st = path.stat()
            except OSError:
                continue
            marks.append((str(path), st.st_mtime_ns, st.st_size))
    try:
        skills = sorted(SKILLS_DIR.rglob("SKILL.md"))
    except OSError:
        skills = []
    for path in skills:
        try:
            st = path.stat()
        except OSError:
            continue
        marks.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(marks)


def refresh_tools(force=False):
    """Re-fingerprint tools/ and skills/, and rescan only if something moved.

    Returns True when the snapshot was actually rebuilt. Safe to call from
    anywhere; the background refresher calls it on its own thread, which is
    what keeps even the fingerprint off the request path."""
    fingerprint = _fingerprint()
    with _snap_lock:
        unchanged = fingerprint == _snapshot["fingerprint"]
        _snapshot["checked"] = time.monotonic()
        if unchanged and not force:
            return False
    # The scan itself runs OUTSIDE the lock. It imports arbitrary tool code,
    # which can take as long as it likes and must never be able to block a
    # reader that only wants the list as it currently stands.
    tools, broken = _scan_files()
    with _snap_lock:
        _snapshot["fingerprint"] = fingerprint
        _snapshot["tools"] = tools
        _snapshot["broken"] = broken
    return True


def load_tools(force=False):
    """Publish TOOLS/BROKEN for this turn, rescanning the folder only when a
    file in it has actually changed.

    This used to re-import every .py in tools/ on every call, and it is called
    three times per pass of the tool loop - so the same sixteen modules were
    read off disk and re-executed six times to answer one message, which
    measured at roughly 750ms of a message's ~960ms of preparation. Two of
    those modules call provider.chat_providers() at import, which is what made
    them cost 200ms of every 250ms scan.

    The promise that made it rescan - a tool the agent writes mid-task is
    usable on the very next turn - is kept by the fingerprint instead: any
    edit, addition, deletion or disable moves it, and the rescan follows.
    `force` skips the window for the tools panel's own refresh button, which
    exists precisely to ask for a re-read on demand.

    The MCP half is rebuilt on EVERY call regardless. Those tools come from a
    live catalogue rather than from disk (a server that drops out simply stops
    contributing), so nothing keyed on file mtimes could ever notice them
    changing - and at 0.4ms they are not worth trying to cache."""
    global TOOLS, BROKEN
    with _snap_lock:
        due = force or (time.monotonic() - _snapshot["checked"]) >= TOOLS_RECHECK
    if due:
        refresh_tools(force)
    with _snap_lock:
        tools = list(_snapshot["tools"])
        broken = list(_snapshot["broken"])

    # Reading the cache only, never dialling - see flattened(). A failure here
    # is reported like a broken tool file instead of being raised: MCP going
    # wrong must not cost the model the twenty tools that have nothing to do
    # with it.
    if mcp_client is not None:
        try:
            taken = {t["name"] for t in tools}
            tools.extend(e for e in mcp_client.flattened() if e["name"] not in taken)
        except Exception as e:
            broken.append("MCP tools - " + type(e).__name__ + ": " + str(e))

    # Published only now that both lists are complete.
    TOOLS = tools
    BROKEN = broken


def parallel_safe(name):
    """Whether that tool may run at the same time as other tools.

    A model can ask for several tools in one response, and main._batches uses
    this to decide which of them go off together and which get the floor to
    themselves. True is the default and the common case: a tool that reads a
    file, searches, or fetches a page has nothing to say to the one running
    beside it, and running them together is most of what makes a batch faster
    than the same calls made one at a time.

    A tool opts out by declaring `PARALLEL = False` at the top of its own
    file - the same way it declares NAME and SCHEMA, and for the same reason:
    a fact about a tool belongs in the tool. Two kinds of tool should:

      - one holding something there is only one of. The terminal keeps a
        single shell per chat, so two commands at once would interleave into
        each other's output and neither would be read correctly.
      - one whose ORDER matters against the calls around it. Two edits to the
        same file, or an edit and the read that checks it, mean something
        different depending on which lands first, and a tool that runs alone
        keeps the order the model actually asked for.

    An unknown tool is treated as unsafe. It is about to come back "there is
    no tool called ..." from _run anyway, and guessing generously about
    something we cannot see is the wrong way round."""
    t = _find(name)
    return bool(t and t.get("parallel", True))


def source_meta(text):
    """A tool's NAME and DESCRIPTION read out of its source WITHOUT importing it.

    Parsed, never executed. A disabled tool is code the user switched off, and
    importing one just to find out what to call it in a list would run its
    module body - top-level side effects, imports of packages that may not be
    installed, and all - which is exactly what switching it off was meant to
    stop. It is also what lets a BROKEN tool still be listed by its real name:
    a file that raises on import has no module to ask, but its source still
    says what it meant to be called.

    A tool whose NAME is built rather than written as a literal simply has no
    name here, and inventory() falls back to the filename.

    Takes the SOURCE, not a path, so the marketplace can read the same two
    fields out of a file it has downloaded but not yet written anywhere."""
    meta = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return meta
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in ("NAME", "DESCRIPTION"):
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue  # computed, not a literal - nothing to read
            if isinstance(value, str):
                meta[target.id.lower()] = value
    return meta


def _tool_files(root):
    """Every .py under `root` that is a tool rather than a helper - the same
    files the loaders would consider: no __pycache__, no _-prefixed helpers
    like _discovery.py, no __init__.py."""
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.startswith("_"):
            continue
        out.append(path)
    return out


def _broken_reasons():
    """{file stem: why it wouldn't load} from the last load_tools(), so the
    inventory can mark a tool that is present but not running."""
    reasons = {}
    for note in BROKEN:
        name, _, why = note.partition(" - ")
        reasons[name.removesuffix(".py")] = why or "would not load"
    return reasons


def inventory():
    """Every tool and skill on disk, switched on or off, as one flat list of
    {kind, name, description, enabled, path, broken} - what the settings
    page's tools tab draws.

    `path` locates the item inside its own root (tools/ or disabled/tools/ for
    a tool, skills/ or disabled/skills/ for a skill), which is what
    set_enabled() takes back to move it. Which root a path is relative to is
    decided by `enabled`, so the two never need to be told apart by string.

    load_tools() is run first so a broken tool is reported as broken here even
    on a server that has not taken a turn yet - it is the same scan every turn
    does anyway."""
    load_tools()
    reasons = _broken_reasons()
    items = []

    for enabled, root in ((True, TOOLS_DIR), (False, DISABLED_TOOLS)):
        for path in _tool_files(root):
            try:
                meta = source_meta(path.read_text(encoding="utf-8"))
            except OSError:
                meta = {}
            stem = path.stem
            items.append({
                "kind": "tool",
                "name": meta.get("name") or stem,
                "description": meta.get("description", ""),
                "enabled": enabled,
                "path": path.relative_to(root).as_posix(),
                # Only a live tool can be broken: the disabled ones are never
                # imported, so "it would fail" is not something we know.
                "broken": reasons.get(stem, "") if enabled else "",
            })

    for enabled, root in ((True, SKILLS_DIR), (False, DISABLED_SKILLS)):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            if "__pycache__" in path.parts:
                continue
            skill = _read_skill(path, root)
            if not skill:
                continue
            items.append({
                "kind": "skill",
                "name": skill["name"],
                "description": skill["description"],
                "enabled": enabled,
                "path": skill["path"],
                "broken": "",
            })

    items.sort(key=lambda i: (i["name"].lower(), i["kind"]))
    return items


def _roots(kind, enabled):
    """(where it is now, where it goes) for something of `kind` currently
    `enabled`. One place decides the four combinations, so a move and the
    listing that offered it can't disagree about which folder is which."""
    if kind == "skill":
        live, off = SKILLS_DIR, DISABLED_SKILLS
    else:
        live, off = TOOLS_DIR, DISABLED_TOOLS
    return (live, off) if enabled else (off, live)


def _inside(root, rel):
    """`root`/`rel` resolved, or None if that isn't actually inside `root`.
    The path comes off an HTTP request, so "../../.env" has to be refused
    rather than trusted to be one of the names we just listed."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    path = (root / rel).resolve()
    root = root.resolve()
    return path if root in path.parents else None


def set_enabled(kind, rel, enable):
    """Switch one tool or skill on or off by MOVING it, and say what happened:
    (True, note) or (False, why not).

    `rel` is the item's path inside the root it is in NOW - so the caller
    passes what inventory() gave it, and `enable` says which way it is going.

    A skill moves as a whole folder, not as its SKILL.md alone: a skill bundle
    is a folder of reference files and templates that its instructions point
    at, and leaving those behind would half-disable it - the body would come
    back with every link inside it broken.

    Nothing is overwritten. A name already present in the destination stops
    the move and says so, rather than quietly replacing a file the user may
    have edited since."""
    src_root, dst_root = _roots(kind, not enable)
    src = _inside(src_root, rel)
    if src is None or not src.exists():
        return False, "no such " + kind + " - it may already have been moved."

    if kind == "skill":
        # The bundle is the folder holding SKILL.md. A SKILL.md sitting loose
        # at the root of skills/ has no folder of its own, and moving its
        # parent would move every other skill with it.
        src = src.parent
        if src == src_root.resolve():
            return False, ("that skill sits loose in " + src_root.name
                           + "/ rather than in a folder of its own, so there "
                             "is nothing to move without taking the whole "
                             "folder. Put it in its own folder first.")
        rel = src.relative_to(src_root.resolve()).as_posix()

    dst = dst_root / rel
    if dst.exists():
        return False, ("there is already a " + kind + " called " + dst.name
                       + " in " + dst_root.parent.name + "/" + dst_root.name
                       + " - move or delete that one first.")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as e:
        return False, "could not move it: " + str(e)

    # The .pyc left behind in tools/__pycache__ is harmless - the loaders list
    # .py files, not compiled ones - but a re-enabled tool that came back
    # changed would be imported from the stale cache, so it goes with the file.
    if kind == "tool":
        cache = src.parent / "__pycache__"
        if cache.is_dir():
            for stale in cache.glob(src.stem + ".*.pyc"):
                try:
                    stale.unlink()
                except OSError:
                    pass

    # So TOOLS/BROKEN reflect the move immediately rather than at the next turn.
    load_tools()
    return True, (("enabled " if enable else "disabled ") + kind + " "
                  + Path(rel).stem)


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
        # Said because the capability is worth nothing unsaid. Several models
        # will make one call and wait, turn after turn, purely because nothing
        # told them the alternative was open - and every one of those waits is
        # a whole round trip through the prompt. The independence caveat is
        # the load-bearing half: a batch's calls are decided together, before
        # any of their results exist, so anything whose arguments depend on
        # what another call returns has to wait for the next turn.
        "CALL SEVERAL TOOLS AT ONCE when the next step needs more than one and "
        "they do not depend on each other - three files to read, a search and "
        "a fetch. They are sent together and run together, so a batch costs "
        "one round trip where the same calls made one at a time cost one "
        "each. Only what you can decide RIGHT NOW belongs in a batch: if a "
        "call's arguments depend on what another call returns, it waits for "
        "the result. Never guess at an argument to fit more into one batch.\n\n"

        "NEVER end a reply with only a promise of action - if your reply says "
        "you WILL look at, run, read or check something, it must contain the "
        "tool call that does it. Act first, then report what you found. When "
        "a request is reasonably clear, use the tools rather than asking what "
        "to do; ask only when genuinely stuck or the action is risky.\n\n"

        # Said explicitly because NOT saying it cost a whole turn. A model
        # that needs to read a diagram - a pinout, a chart, a screenshot -
        # will keep inventing ways to look at it (open it in the browser,
        # convert it, "let me view the image"), fail at every one, and try the
        # next, because nothing ever told it the capability is absent. That is
        # the loop main._looping now cuts; this is what stops it starting.
        "YOU CANNOT SEE IMAGES. There is no tool that shows you a picture, and "
        "there is no way to make one - opening a file in a browser, rendering "
        "a PDF page, converting a format: none of them let you look at it. If "
        "something you need is only in an image, do not keep trying: get at it "
        "another way (extract the text, read the source data, find the same "
        "figure written down), or say plainly that you cannot see it and ask "
        "the user what it shows. You CAN write an image into a reply for the "
        "USER to look at - ![name](/path/to/file.png) - and you should when a "
        "picture is the answer. That shows it to them, not to you.\n")


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
    which Gemini rejects in its own right. Every union we write puts the
    ordinary form FIRST and the escape hatch second, so collapsing onto branch
    one keeps the case that matters: "a string, or a list of strings"
    (email_send's to/cc/bcc, email's uid) collapses to the single string, and
    "a number, or the empty string that clears it" (cron's temperature and
    safety) collapses to the number. On Gemini the model loses the second form
    of those arguments - it cannot pass a list of uids or clear a cron job's
    temperature back to the default - and their descriptions still mention it,
    which is the honest cost of a wire that will not take a union at all. Every
    other provider gets the real thing, because only this function rewrites
    anything."""
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
    never an attempt, so it must not even trip the nudge.

    Deliberately strict about what counts, because several tools are named
    after ordinary English words - email, terminal, input, printing, cron,
    firefox - and a reply is allowed to use those words in a sentence. Two
    things have to hold, and prose fails both:

      no space before the "(" - a model writing a call writes email({...}),
      never "email (...)", while English puts a space before a parenthesis;

      and the bracket has to open like ARGUMENTS - empty, a JSON object or
      list, a quoted string, a name= keyword, or a single unspaced token -
      rather than like an aside, which is words with spaces between them.

    So "Check your email (Gmail, mail.com)" is left alone and email({"to":
    ...}) still gets caught. Erring towards missing one is right: a missed
    nudge costs a turn, while a false one interrupts an answer that was
    perfectly good and tells the model off for something it never did."""
    text = unfenced(text)
    for t in TOOLS:
        for m in re.finditer(r"\b" + re.escape(t["name"]) + r"\(", text):
            if _args_shaped(text[m.end():]):
                return True
    return False


# What sits just inside the bracket of a real written-out call: nothing at all,
# JSON, a quoted string, a keyword argument, or one unspaced value.
_ARGS_SHAPED = re.compile(r"""\s*(?:
      \)                          # name() - no arguments
    | [{\[]                       # name({...}) / name([...])
    | ["']                       # name("...")
    | [A-Za-z_][\w-]*\s*=         # name(query=...)
    | [^\s()]+\s*\)               # name(value) - one token, no spaces
)""", re.VERBOSE)


def _args_shaped(after):
    """True if the text right after a "name(" opens the way an argument list
    does rather than the way an English aside does."""
    return bool(_ARGS_SHAPED.match(after))


def _find(name):
    for t in TOOLS:
        if t["name"] == name:
            return t
    return None


def process(call, chat_id=None, workspace_id=None):
    """Run the tool the call asks for and return its output as text, clipped
    to a size a conversation can afford to carry.

    Every result in the app comes back through here - run(), the Claude
    session, cron - which is the whole reason the size limit lives at this
    line and not at the two places that append the result to a history. A
    third place to append one gets written eventually; a third place to
    forget the limit should not exist. See tool_results.clamp() for what
    "clipped" means and where the full copy is kept."""
    return tool_results.clamp(_run(call, chat_id, workspace_id),
                              call.get("tool"), chat_id)


def _run(call, chat_id=None, workspace_id=None):
    """Run the tool the call asks for and return its output as text.

    chat_id is the conversation the call came from, and is handed to any tool
    whose run() declares it - that's how the terminal keeps one open shell per
    chat instead of one for everybody. Tools that don't ask for it never see
    it, so nothing else needed changing.

    workspace_id is the same idea for WHERE the call happens: the chat's
    workspace, resolved here into a workspace.Workspace and handed to any tool
    whose run() declares `workspace`. That object knows its own root and, if
    the workspace is a remote one, does its reading, writing and running over
    ssh - so a tool asks it for a file and never has to care which machine the
    file is on.

    A stopped turn never starts a tool. Once one HAS started it is left to
    finish on its own - a command already running cannot be un-run, and killing
    a shared per-chat shell part-way would break the next turn that uses it -
    but its result is discarded, because the context it would be reported
    through is already dead (see turnctx.guard)."""
    turnctx.check()
    t = _find(call["tool"])
    if t is None:
        # Might be one the agent just wrote, so re-read the folder and retry.
        load_tools()
        t = _find(call["tool"])
    if t is None and call["tool"].startswith(getattr(mcp_client, "NAME_PREFIX", "mcp__")):
        # An MCP tool that was attached earlier in this conversation and isn't
        # now: its server has dropped out since. Say that, instead of listing
        # thirty tool names and leaving the model to work out for itself that
        # the one it wants used to be there.
        rest = call["tool"].split("__", 2)
        server = rest[1] if len(rest) > 2 else call["tool"]
        return ("ERROR: " + call["tool"] + " is not available - the \"" + server
                + "\" MCP server is not connected right now. Use the mcp tool "
                  "with action \"reconnect\" and server \"" + server
                + "\", then try this again.")
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
        params = inspect.signature(t["run"]).parameters
        if "chat_id" in params:
            args["chat_id"] = chat_id
        # Same contract as chat_id, one line further on: a tool that declares
        # `workspace` is handed the chat's one, a tool that doesn't is called
        # exactly as before. That is what keeps this cheap - no registry of
        # which tools are workspace-aware, no schema entry for the model to
        # fill in wrongly, and nothing at all to do for the twelve tools that
        # have no business with the filesystem.
        if "workspace" in params:
            args["workspace"] = workspace.get(workspace_id)
    except (TypeError, ValueError):
        pass  # can't read the signature - just call it the plain way
    try:
        return t["run"](**args)
    except turnctx.Stopped:
        # A tool that noticed the stop itself. That is the turn ending, not the
        # tool failing, so it must not be caught below and handed back to the
        # model as an error message about work nobody is waiting for.
        raise
    except Exception as e:
        return "ERROR running " + call["tool"] + ": " + type(e).__name__ + ": " + str(e)
