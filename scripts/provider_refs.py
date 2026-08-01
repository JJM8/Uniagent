"""Carry a provider's NAME across a rename, everywhere the name is stored.

A provider object has a permanent id (provider.py), so everything Uniagent
records ABOUT a provider - its learned models, its cached catalogue - survives
being renamed without anyone doing anything. This file is the other half:
the places that record which provider to USE, which store the name because a
name is what a person reads, types after /model, and writes in cron.json.

Those are:

  settings.json          the four provider settings (settings.PROVIDER_KEYS)
  chats/**/settings.json a chat pinned to a provider, and its subagents' _meta
  cron.json              a job's "provider" field

Storing ids there instead was the alternative, and it is the wrong trade: a
cron job that said `"provider": "p-4f2a91c07d3e"` would be unreadable and
unwritable by hand, and cron.json is a file the user edits. So the name stays
the handle and
a rename updates the handle - which is what a rename means anyway.

Nothing here ever runs on a DELETE. Deleting a provider is meant to orphan
what pointed at it: that is how those settings fall back to a working default
instead of silently moving to some other provider the user never chose.
"""

import json
from pathlib import Path

import settings

ROOT = Path(__file__).parent.parent
CHATS = ROOT / "chats"
CRON_FILE = ROOT / "cron.json"


def rename_everywhere(old, new):
    """Point everything that named provider `old` at `new` instead. Returns
    {what: how many} for the caller to report, counting only what it changed.

    Best-effort per target: a chat whose settings.json is unreadable is
    skipped rather than taking the whole rename down with it. The rename
    itself has already happened in .env by the time this runs, so a failure
    here costs a stale pin, not a broken provider."""
    old = (old or "").strip().lower()
    new = (new or "").strip().lower()
    if not old or not new or old == new:
        return {}
    counts = {}
    for label, n in (("settings", _rename_in_settings(old, new)),
                     ("chats", _rename_in_chats(old, new)),
                     ("subagents", _rename_in_subagents(old, new)),
                     ("cron jobs", _rename_in_cron(old, new))):
        if n:
            counts[label] = n
    return counts


def _rename_in_settings(old, new):
    """The main settings.json - the same in-place edit every other target here
    gets, rather than a trip through settings.save().

    Both halves of that matter. Read raw, because load() heals a setting that
    names a provider which isn't there, and by the time this runs the provider
    HAS stopped existing under its old name - so the healed view would report
    a fallback and this would find nothing to rename, while the file still
    said `old`. Written raw, because save() rewrites the whole file from the
    settings it can currently make sense of, and a rename must change one word
    and touch nothing else - not the model paired with it, which is still
    perfectly good and belongs to the same provider it always did."""
    hits = []

    def edit(data):
        for key in settings.PROVIDER_KEYS:
            if data.get(key) == old:
                data[key] = new
                hits.append(key)
        return bool(hits)

    _rewrite_json(settings.SETTINGS_FILE, edit)
    return len(hits)


def _rewrite_json(path, edit):
    """Read a JSON object, let `edit` change it in place, write it back if it
    said it changed anything. Any unreadable or non-object file is left alone -
    this walks hundreds of chat folders and one bad file must not stop it."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or not edit(data):
        return False
    try:
        path.write_text(json.dumps(data, indent=2))
        return True
    except OSError:
        return False


def _rename_in_chats(old, new):
    """Every chat pinned to `old` - ordinary chats, cron runs and subagent
    transcripts alike, which is why this globs at any depth under chats/.

    Two different things in the same file, and both have to move. The pin
    ("provider") is which provider to USE. The token keys are which
    provider/model a recorded count was TAKEN ON - and a count is only trusted
    when that still matches what the chat runs on now (see main.stored_usage),
    because a number counted with another model's tokenizer, against another
    model's window, says nothing about this one. A rename is the one case
    where the string changes and the model behind it does not, so leaving
    those behind threw away every count in every chat over a name: the panel
    had nothing to show the moment you opened one, and had to recount from
    scratch to get back exactly the number it already had."""
    def edit(data):
        hit = False
        if data.get("provider") == old:
            data["provider"] = new
            hit = True
        for key in ("tokens_model", "context_model"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith(old + "/"):
                data[key] = new + value[len(old):]
                hit = True
        return hit

    return sum(_rewrite_json(p, edit) for p in CHATS.rglob("settings.json"))


def _rename_in_subagents(old, new):
    """A subagent remembers what it last ran on in its chat's _meta.json, and
    follows that on the next call when the call names nothing - so it has to
    move too, or a renamed provider silently demotes every existing subagent
    to the default."""
    def edit(data):
        hit = False
        for entry in data.values():
            if isinstance(entry, dict) and entry.get("provider") == old:
                entry["provider"] = new
                hit = True
        return hit

    return sum(_rewrite_json(p, edit) for p in CHATS.rglob("_meta.json"))


def _rename_in_cron(old, new):
    """The "provider" of any job in cron.json.

    A field lookup, not a search: nothing in a job's prompt - which is free
    text, often several hundred words, and none of this code's business - can
    be touched by it, and a provider called "local" cannot rewrite a job that
    merely mentions the word."""
    hits = []

    def edit(data):
        jobs = data.get("jobs")
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict) and job.get("provider") == old:
                job["provider"] = new
                hits.append(job.get("name"))
        return bool(hits)

    _rewrite_json(CRON_FILE, edit)
    return len(hits)
