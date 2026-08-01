"""Shared tool discovery for read_skill and the prompt's tool list.

Scans tools/ (and its subfolders) fresh on every call and returns the working
tool modules plus a note on any that wouldn't load. Kept in one place so
read_skill and tool_processor don't each carry a copy - and so it always
reports a tool's CURRENT instructions, even one the agent just wrote or edited
(we reload, rather than hand back a cached import).

The leading underscore marks this as a helper, not a tool: the loaders skip
`_`-prefixed files, so this never shows up as a tool or as a "broken" one.
"""

import importlib
import pkgutil
import sys
import types
from pathlib import Path

import tool_processor

TOOLS_DIR = Path(__file__).parent

# Its full instructions are already injected into every prompt, so re-reading
# it is noise. Must stay in step with tool_processor.INJECTED.
INJECTED = ("read_skill",)

REQUIRED = ("NAME", "DESCRIPTION", "INSTRUCTIONS", "run")


def _dirs():
    """This folder and every package folder inside it."""
    dirs = [TOOLS_DIR]
    for p in sorted(TOOLS_DIR.rglob("*")):
        if p.is_dir() and p.name != "__pycache__":
            dirs.append(p)
    return dirs


def others():
    """(working tool modules, broken-file notes), everything except the
    injected tool. Re-read fresh each call."""
    mods = []
    broken = []
    importlib.invalidate_caches()  # so files just written are seen
    for d in _dirs():
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
        for found in pkgutil.iter_modules([str(d)]):
            if found.ispkg or found.name.startswith("_") or found.name in INJECTED:
                continue
            try:
                if found.name in sys.modules:
                    m = importlib.reload(sys.modules[found.name])
                else:
                    m = importlib.import_module(found.name)
                for attr in REQUIRED:
                    if not hasattr(m, attr):
                        raise AttributeError("missing " + attr)
                mods.append(m)
            except Exception as e:
                # Skip it - one broken file must never take the whole agent down.
                broken.append(found.name + ".py (" + type(e).__name__ + ": " + str(e) + ")")

    # Claude-format skills (.md), from skills/ rather than this folder, so the
    # sidebar's tools & skills list still shows them. They have no module, so
    # stand in a plain object with the attributes that list actually reads.
    taken = {m.NAME for m in mods}
    for skill in tool_processor.find_skills():
        if skill["name"] not in taken:
            mods.append(types.SimpleNamespace(
                NAME=skill["name"],
                DESCRIPTION=skill["description"],
                INSTRUCTIONS=skill["instructions"],
                PATH=skill["path"],
            ))
    return mods, broken
