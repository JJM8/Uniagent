"""The tools tab's marketplace: browse tools and skills on GitHub, install one.

Nothing is curated here and nothing is bundled. The list comes from whichever
repositories the "market_repos" setting names ("anthropics/skills" to begin
with), read live off GitHub - so a skill added upstream this morning is in the
list this afternoon without this file being touched.

Two requests' worth of work per repository, not one per entry:

  1. the git trees API, ONCE, recursively - every path in the repo in a single
     response, which is where the entries themselves come from;
  2. the first few KB of each entry's own file, from raw.githubusercontent.com,
     for its description. That host serves plain files and is not the rate-
     limited API, and only the head of each file is read, since a description
     lives in the first lines of a SKILL.md or the top of a .py.

The whole catalogue is cached for CACHE_SECONDS, so opening the tab twice, or
two windows opening it at once, is one set of requests and then none. The
unauthenticated API allows 60 requests an hour and this spends one per repo -
put GITHUB_TOKEN in .env and it spends one of 5000 instead.

What is installed lands ENABLED, in tools/ or skills/ proper, and is part of
the agent from its very next turn. Worth knowing, since a tool is code that
runs on this machine with everything the agent can reach and a skill is
instructions the model will follow: what you install from someone else's
repository is live, and the switch in the list above is how you take it back
out without deleting it.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import settings
import tool_processor

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

# GitHub refuses a request with no User-Agent outright, so this is not optional.
AGENT = "Uniagent"

TIMEOUT = 15

# How long a browsed catalogue is good for. Long enough that opening the tab,
# closing it and opening it again costs nothing; short enough that a skill
# published upstream shows up the same day without a restart.
CACHE_SECONDS = 600

# Guard rails on what one install may pull down: a skill bundle is a handful
# of markdown files and the odd script, so anything past these is not a skill
# bundle any more and is not being fetched onto the user's disk by accident.
MAX_BUNDLE_FILES = 60
MAX_FILE_BYTES = 2_000_000

# How much of a file is read just to find out what it is. Front matter is the
# first thing in a SKILL.md; NAME and DESCRIPTION are at the top of a tool.
PEEK_BYTES = 6000

# The description in a skill's front matter, without pulling in a YAML parser
# for two fields - the same shape tool_processor._read_skill() parses.
_FRONT = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

ENV_FILE = Path(__file__).parent.parent / ".env"

_cache = {}          # "owner/repo/path" -> (fetched_at, [entry, ...])
_cache_lock = threading.Lock()


def _token():
    """A GitHub token from .env, or "" for anonymous browsing.

    Optional on purpose: the marketplace works with no token at all, it just
    shares the anonymous hourly allowance with everything else on the network
    address. Read the same way provider.py reads its keys - straight out of
    .env - except that a missing one is a normal state here, not an error."""
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return os.environ.get("GITHUB_TOKEN", "")


def _get(url, limit=None):
    """`url`'s body as text, at most `limit` bytes of it. Raises urllib's own
    errors - callers turn those into something a person can read, because the
    two failures worth telling apart (a repo that isn't there, an allowance
    that has run out) are both HTTPError and only the code says which."""
    request = urllib.request.Request(url, headers={
        "User-Agent": AGENT,
        "Accept": "application/vnd.github+json",
    })
    token = _token()
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(limit) if limit else response.read(MAX_FILE_BYTES)
    return raw.decode("utf-8", "replace")


def _split(spec):
    """"owner/repo/some/folder" -> ("owner", "repo", "some/folder"). The folder
    is optional and is a filter, not a root: a repo that keeps its skills in
    skills/ can be named as "owner/repo/skills" so the rest of it is ignored."""
    parts = [p for p in (spec or "").strip().strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1], "/".join(parts[2:])


def _tree(owner, repo):
    """Every path in the repository's default branch, in one request.

    HEAD rather than a branch name because "main" and "master" are both still
    out there and asking which one this repo uses would cost another request."""
    data = json.loads(_get(API + "/repos/" + owner + "/" + repo
                           + "/git/trees/HEAD?recursive=1"))
    return data.get("tree") or [], bool(data.get("truncated"))


def _entries_from_tree(owner, repo, sub, tree):
    """What in this repo can be installed: every skill bundle (a folder with a
    SKILL.md in it) and every standalone tool (a .py at the top of the repo, or
    at the top of the named subfolder).

    A .py deeper than that is deliberately NOT offered. Skill bundles carry
    their own scripts/ - helper programs the skill's instructions tell the
    model to run - and those are not tools: they have no NAME, no DESCRIPTION
    and no run(), and listing them as installable would fill the marketplace
    with dozens of fragments of other people's skills."""
    entries = []
    prefix = (sub + "/") if sub else ""
    for node in tree:
        if node.get("type") != "blob":
            continue
        path = node.get("path") or ""
        if prefix and not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if rest.endswith("SKILL.md") and "/" in rest:
            entries.append({
                "kind": "skill",
                "name": rest.rsplit("/", 1)[0].rsplit("/", 1)[-1],
                # The bundle folder, which is what install() copies wholesale.
                "folder": path.rsplit("/", 1)[0],
                "path": path,
                "repo": owner + "/" + repo,
                "description": "",
            })
        elif rest.endswith(".py") and "/" not in rest and not rest.startswith("_"):
            entries.append({
                "kind": "tool",
                "name": rest[:-3],
                "folder": "",
                "path": path,
                "repo": owner + "/" + repo,
                "description": "",
            })
    return entries


def _front_matter(block):
    """The front matter's fields as a dict, block scalars included.

    Two fields are wanted (name, description) and PyYAML is not a dependency
    of this project, so this parses by hand like the rest of the skill
    handling does - but unlike tool_processor's version it also understands
    `description: |-` followed by indented lines, which is how several of the
    skills in anthropics/skills are actually written. Without that, the whole
    description reads as "|-" in the marketplace list."""
    fields = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        key, sep, value = lines[i].partition(":")
        i += 1
        if not sep or key != key.strip() or not key.strip():
            continue  # an indented continuation line, or not a field at all
        key = key.strip().lower()
        value = value.strip()
        if value.startswith("|") or value.startswith(">"):
            # A block scalar: every following line indented under this key.
            gathered = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
                gathered.append(lines[i].strip())
                i += 1
            value = " ".join(g for g in gathered if g)
        fields[key] = value.strip().strip("\"'")
    return fields


def _describe(entry):
    """Fill in one entry's description from the head of its own file, in place.

    Failure is silent and leaves the description empty: a marketplace that
    refuses to list anything because one file 404ed is worse than one that
    lists an entry with nothing written under it."""
    url = (RAW + "/" + entry["repo"] + "/HEAD/" + entry["path"])
    try:
        head = _get(url, PEEK_BYTES)
    except Exception:
        return
    if entry["kind"] == "skill":
        match = _FRONT.match(head)
        if not match:
            return
        fields = _front_matter(match.group(1))
        entry["name"] = fields.get("name") or entry["name"]
        entry["description"] = fields.get("description", "")
    else:
        # A tool's whole file, not the peek: ast.parse needs source that ends
        # where a statement does, and 6KB in is usually the middle of one.
        try:
            meta = tool_processor.source_meta(_get(url))
        except Exception:
            return
        entry["name"] = meta.get("name") or entry["name"]
        entry["description"] = meta.get("description", "")


def _fetch(spec):
    """One repository's installable entries, described. Network, no cache."""
    owner_repo_sub = _split(spec)
    if not owner_repo_sub:
        raise ValueError('"' + spec + '" is not owner/repo')
    owner, repo, sub = owner_repo_sub
    tree, truncated = _tree(owner, repo)
    entries = _entries_from_tree(owner, repo, sub, tree)
    entries.sort(key=lambda e: e["name"].lower())
    if truncated:
        # Enormous repo: GitHub gave a partial tree. Say so rather than
        # presenting a short list as if it were the whole thing.
        entries.append({"kind": "note", "name": "", "folder": "", "path": "",
                        "repo": owner + "/" + repo, "description":
                        "this repository is too big for one listing - GitHub "
                        "returned only part of it. Name a subfolder "
                        "(owner/repo/folder) to narrow it down."})
    # Descriptions in parallel: each is its own small request to a different
    # file, and doing sixty of them one after another is the difference between
    # a tab that opens and a tab that hangs.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_describe, [e for e in entries if e["kind"] != "note"]))
    return entries


def _installed_names():
    """What is already on disk, enabled or not, as {("tool"|"skill", name)} -
    so the marketplace can say "installed" instead of offering it again.

    Matched on the name the item goes by, which is what a second copy would
    collide with: two tools cannot both be `web_search` however their files
    are called."""
    return {(i["kind"], i["name"].lower()) for i in tool_processor.inventory()}


def catalogue(refresh=False):
    """Everything installable from every configured repository:
    {"entries": [...], "notes": [...], "fetched": epoch-seconds}.

    Served from the cache unless it has aged out or `refresh` says otherwise.
    A repository that can't be read becomes a note and does not stop the
    others - one bad name in the list must not empty the whole marketplace."""
    entries = []
    notes = []
    fetched = time.time()
    for spec in settings.get("market_repos") or []:
        spec = (spec or "").strip()
        if not spec:
            continue
        with _cache_lock:
            cached = _cache.get(spec)
        if cached and not refresh and time.time() - cached[0] < CACHE_SECONDS:
            entries.extend(cached[1])
            fetched = min(fetched, cached[0])
            continue
        try:
            found = _fetch(spec)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                notes.append(spec + ": no such repository (or it is private).")
            elif e.code in (403, 429):
                notes.append(spec + ": GitHub is rate-limiting this address. "
                             "It resets within the hour - or put a GITHUB_TOKEN "
                             "in .env to browse on your own allowance.")
            else:
                notes.append(spec + ": GitHub said " + str(e.code) + ".")
            continue
        except Exception as e:
            notes.append(spec + ": " + type(e).__name__ + " - " + str(e))
            continue
        with _cache_lock:
            _cache[spec] = (time.time(), found)
        entries.extend(found)

    have = _installed_names()
    for entry in entries:
        entry["installed"] = (entry["kind"], entry["name"].lower()) in have
    entries.sort(key=lambda e: (e["kind"] != "note", e["name"].lower()))
    return {"entries": entries, "notes": notes, "fetched": fetched}


def _cached_find(repo, path):
    with _cache_lock:
        for _fetched_at, entries in _cache.values():
            for entry in entries:
                if entry["repo"] == repo and entry["path"] == path:
                    return entry
    return None


def _find(repo, path):
    """The catalogue entry `repo` + `path` names, or None.

    Install works off the browsed LISTING rather than off whatever the request
    body says, so the only things installable are things actually seen in a
    repository the user configured - a made-up path in a POST matches nothing
    and downloads nothing.

    A cache miss re-browses rather than failing. The listing is cached per
    process, and the process that serves the install is not necessarily the
    one that served the browse (a restart in between, a second window) - and
    "install" failing with "refresh the list" on an entry that is plainly on
    screen would be nonsense."""
    entry = _cached_find(repo, path)
    if entry:
        return entry
    catalogue(refresh=True)
    return _cached_find(repo, path)


def install(repo, path):
    """Download one catalogue entry into tools/ or skills/, switched on, and
    say what happened: (True, note) or (False, why not).

    Enabled on arrival - see this module's docstring - so it is in the prompt
    and callable from the next turn. The tools tab lists it at the top with
    its switch already on, and switching it off is what moves it aside.

    The DISABLED folders are still checked for a name clash: something you
    installed and then switched off is still installed, and a second copy
    landing live while the first sits disabled would leave two files claiming
    one name, with which of them the agent actually gets decided by which
    folder happened to be scanned.

    A skill arrives as its whole folder: the SKILL.md alone would come with
    every reference and template its instructions point at missing."""
    entry = _find(repo, path)
    if not entry:
        return False, ("that isn't in the marketplace listing any more - "
                       "refresh it and try again.")

    if entry["kind"] == "skill":
        root = tool_processor.SKILLS_DIR / entry["name"]
        other = tool_processor.DISABLED_SKILLS / entry["name"]
    else:
        root = tool_processor.TOOLS_DIR / (entry["name"] + ".py")
        other = tool_processor.DISABLED_TOOLS / (entry["name"] + ".py")
    if root.exists():
        return False, ("there is already a " + entry["kind"] + " called "
                       + entry["name"] + " - remove it first if you want this one.")
    if other.exists():
        return False, ("there is already a " + entry["kind"] + " called "
                       + entry["name"] + ", switched off - turn that one back "
                       "on, or delete it, rather than installing a second copy.")

    if entry["kind"] == "tool":
        try:
            source = _get(RAW + "/" + repo + "/HEAD/" + path)
        except Exception as e:
            return False, "could not download it: " + type(e).__name__ + " - " + str(e)
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
            root.write_text(source)
        except OSError as e:
            return False, "could not save it: " + str(e)
        return True, ("installed " + entry["name"] + " - it is switched on and "
                      "in the agent from its next message.")

    # A skill: every file under its folder, from the tree we already have.
    folder = entry["folder"] + "/"
    owner_repo_sub = _split(repo)
    if not owner_repo_sub:
        return False, "that repository name is not owner/repo."
    try:
        tree, _ = _tree(owner_repo_sub[0], owner_repo_sub[1])
    except Exception as e:
        return False, "could not read the repository: " + type(e).__name__ + " - " + str(e)

    files = [n for n in tree if n.get("type") == "blob"
             and (n.get("path") or "").startswith(folder)
             and (n.get("size") or 0) <= MAX_FILE_BYTES]
    if not files:
        return False, "there are no files in that folder any more."
    if len(files) > MAX_BUNDLE_FILES:
        return False, ("that folder holds " + str(len(files)) + " files, which is "
                       "more than a skill bundle should be - install it by hand "
                       "if you really want it.")

    written = 0
    for node in files:
        rel = node["path"][len(folder):]
        target = root / rel
        try:
            body = _get(RAW + "/" + repo + "/HEAD/" + node["path"])
        except Exception as e:
            # Half a bundle is not a skill. Take the whole folder back out.
            _remove(root)
            return False, ("could not download " + rel + ": " + type(e).__name__
                           + " - " + str(e))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        except OSError as e:
            _remove(root)
            return False, "could not save " + rel + ": " + str(e)
        written += 1

    return True, ("installed " + entry["name"] + " (" + str(written) + " file"
                  + ("" if written == 1 else "s") + ") - it is switched on and "
                  "in the agent from its next message.")


def _remove(path):
    """Undo a half-finished install. Never raises: it runs while something has
    already gone wrong, and the caller has a better error to report than
    whatever this would add."""
    import shutil
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass
