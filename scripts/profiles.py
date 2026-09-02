"""Named profiles - which context an agent runs on, and which tools it may use.

WHAT A PROFILE IS
-----------------
A profile is the answer to "which Uniagent am I talking to". It names some
context to load, some memories to index, and which tools and skills are
reachable. `chat` and `assistant` ship as the two examples; a chat picks one
and every turn in that chat is assembled against it.

WHY IT IS NOT A PRESET
----------------------
main.py already has presets, which do a cruder version of this: they MOVE
context/ and memories/ into archive/ and copy another pair back. That works,
but it swaps by moving files, so there is exactly one live set for the whole
install and switching costs a recursive copy of two folders. A profile swaps
by POINTING instead, which is the whole difference - two chats can be two
different profiles at the same time, and switching is a variable assignment.

WHY IT IS READ ON EVERY TURN
----------------------------
Through filecache, exactly like settings.json, .env and wires.json. That looks
like disk I/O in the hot path and isn't: the bytes are held in memory and the
file is re-stat'ed at most once a second. Keeping it that way preserves the
contract every config reader here documents - an edit takes effect on the NEXT
TURN, with nothing restarted, in this process AND in the cron watcher AND when
the file arrives over Syncthing from another machine. Loading it once at
startup would be marginally faster and would break all three.

PATHS ARE FOLDERS OR FILES
--------------------------
"context" and "memories" each take a path or a list of them, relative to the
project root. A FOLDER contributes every .md/.txt under it, in the numeric
order main._context_order defines. A FILE contributes just itself. Order
follows the list as written, so a profile says what comes first by saying it.

Anything resolving outside the project root is dropped - same guard as
main.preset_path, for the same reason: this is a config file, and a config
file must not be able to name ../../.ssh.

FAILING SOFT IS THE POINT
-------------------------
A missing, unparseable or nonsense profiles.json leaves BUILTIN in charge, and
BUILTIN is exactly today's behaviour: context/, memories/, every tool. So the
worst a broken file can do is turn the feature off, never take the agent down
and never silently narrow what it can reach.
"""

import json
import threading
from pathlib import Path

# filecache ONLY. This module is imported by both main and tool_processor, so
# anything heavier here would close an import cycle between them.
import filecache

ROOT = Path(__file__).parent.parent
PROFILES_FILE = ROOT / "profiles.json"

# What a context/memories entry may be. Kept in step with main.CONTEXT_SUFFIXES
# by hand rather than imported, for the import-cycle reason above - it is two
# strings that have not changed in the life of the project.
SUFFIXES = (".md", ".txt")

# The profile everything falls back to: today's behaviour, exactly. Used when
# profiles.json is missing, broken, or names a profile that isn't there.
BUILTIN_ID = "assistant"
BUILTIN = {
    "label": "Assistant",
    "context": ["context"],
    "memories": ["memories"],
    "tools": {"allow": "*"},
    "skills": {"allow": "*"},
}

_held = {}          # filecache signature -> parsed file
_lock = threading.Lock()


def _parse():
    """profiles.json as {"default": id, "profiles": {id: {...}}}, or the
    builtin stand-in. Never raises."""
    try:
        data = json.loads(filecache.text(PROFILES_FILE, default="{}"))
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    found = data.get("profiles")
    if not isinstance(found, dict) or not found:
        return {"default": BUILTIN_ID, "profiles": {BUILTIN_ID: dict(BUILTIN)}}
    clean = {pid: p for pid, p in found.items() if isinstance(p, dict)}
    if not clean:
        return {"default": BUILTIN_ID, "profiles": {BUILTIN_ID: dict(BUILTIN)}}
    fallback = data.get("default")
    if fallback not in clean:
        fallback = BUILTIN_ID if BUILTIN_ID in clean else sorted(clean)[0]
    return {"default": fallback, "profiles": clean}


def load():
    """The whole file, memoised against the filecache signature - so it is
    re-parsed exactly when the bytes change and not once otherwise. Same
    arrangement as settings.load()."""
    stamp = filecache.signature()
    with _lock:
        held = _held.get("data")
        if held is not None and held[0] == stamp:
            return held[1]
    data = _parse()
    with _lock:
        _held["data"] = (stamp, data)
    return data


def ids():
    """Every profile id, the default first and the rest alphabetical - the
    order the picker and /profile list them in."""
    data = load()
    rest = sorted(p for p in data["profiles"] if p != data["default"])
    return [data["default"]] + rest


def default_id():
    return load()["default"]


def exists(pid):
    return pid in load()["profiles"]


def resolve(pid=None):
    """`pid`'s profile with every key filled in, falling back to the default
    profile and then to BUILTIN. Always returns a usable dict, so no caller
    needs to handle "no such profile"."""
    data = load()
    found = data["profiles"].get(pid)
    if not isinstance(found, dict):
        found = data["profiles"].get(data["default"])
    if not isinstance(found, dict):
        found = BUILTIN
    merged = dict(BUILTIN)
    merged.update(found)
    return merged


def label(pid=None):
    """What to call this profile on screen. Its own label, else its id."""
    return resolve(pid).get("label") or (pid or default_id())


def _safe(rel):
    """`rel` resolved under the project root, or None if it points outside it.
    A config file must never be able to name a path above ROOT."""
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        return None
    path = (ROOT / rel).resolve()
    root = ROOT.resolve()
    if path != root and root not in path.parents:
        return None
    return path


def roots(pid=None, kind="context"):
    """The folders and files `pid` draws its `kind` ("context"/"memories")
    from, in the order the profile lists them, dropping anything that escapes
    the project root. Entries that don't exist yet are KEPT - a profile whose
    folder hasn't been made is not an error, it just contributes nothing, and
    dropping it here would hide it from the settings page too."""
    spec = resolve(pid).get(kind)
    if isinstance(spec, str):
        spec = [spec]
    if not isinstance(spec, list):
        spec = BUILTIN.get(kind, [])
    out = []
    for entry in spec:
        path = _safe(entry)
        if path is not None and path not in out:
            out.append(path)
    return out


def write_root(pid=None, kind="memories"):
    """Where something NEW of this kind gets written - the first entry that is
    a directory, or the first entry at all. A profile can list several places
    to READ from; it has to name exactly one to write to, or the model is left
    guessing which of them a new memory belongs in.

    None when the profile lists NOTHING of this kind, which is the whole point
    of an empty list: a profile with no memories has nowhere to put one, and
    the caller drops the memory instructions entirely rather than writing them
    somewhere. This used to fall back to ROOT/kind, and that quietly undid the
    empty list - a deliberately bare profile was still told to save its
    memories into the default profile's folder, so the one thing it was set up
    not to touch was the one thing it wrote to."""
    found = roots(pid, kind)
    for path in found:
        if path.is_dir():
            return path
    for path in found:
        if path.suffix.lower() not in SUFFIXES:
            return path
    return found[0] if found else None


def _list(spec, key):
    value = spec.get(key)
    if isinstance(value, str):
        return value if value == "*" else [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


def allows(pid, kind, name):
    """Whether `pid` may use the tool or skill called `name`. `kind` is "tool"
    or "skill" - they are separate lists because they are separate kinds of
    thing in separate folders, and one shared namespace would let a tool and a
    skill of the same name resolve each other's rules silently.

    Deny beats allow, always. An absent or "*" allow list means everything,
    which is what makes a deny-only profile the easy thing to write."""
    spec = resolve(pid).get("skills" if kind == "skill" else "tools")
    if not isinstance(spec, dict):
        return True
    deny = _list(spec, "deny")
    if isinstance(deny, list) and name in deny:
        return False
    if deny == "*":
        return False
    allow = _list(spec, "allow")
    if allow is None or allow == "*":
        return True
    return name in allow


def unknown_names(pid=None, known=None):
    """Names this profile lists that match nothing currently loadable, as
    [(kind, name)]. For the settings page to warn with.

    Worth surfacing because there are two ways to be wrong and they look
    identical from here: a typo, and a tool that is globally switched off (the
    tools tab moves those into disabled/, so they are not loaded at all and an
    allow list naming one silently does nothing).

    `known` is {"tool": {names}, "skill": {names}} from the caller that
    actually has the tool list - passed in rather than imported, to keep this
    module free of tool_processor."""
    if not known:
        return []
    profile = resolve(pid)
    missing = []
    for kind, key in (("tool", "tools"), ("skill", "skills")):
        spec = profile.get(key)
        if not isinstance(spec, dict):
            continue
        for field in ("allow", "deny"):
            listed = _list(spec, field)
            if not isinstance(listed, list):
                continue
            for name in listed:
                if name not in known.get(kind, ()) and (kind, name) not in missing:
                    missing.append((kind, name))
    return missing


def save(data):
    """Write the whole file. Returns what is now on disk.

    Whole-file rather than key-merged, unlike settings.save(): the profiles
    page edits a tree, and merging a tree key by key cannot express deleting a
    profile or removing one name from an allow list."""
    with _lock:
        _held.clear()
    PROFILES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # Written by us, so don't wait out the recheck window - forget() bumps the
    # signature, which drops the memo above with it.
    filecache.forget(PROFILES_FILE)
    return load()
