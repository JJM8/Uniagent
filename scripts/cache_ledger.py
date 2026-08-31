"""What the last request to a provider looked like, so the next one can be
told whether it will hit the provider's prompt cache - and how much it will
cost if it doesn't.

Every provider worth caching against works the same way: it remembers a prompt
it has already processed and charges about a tenth of the price for however
much of the NEXT prompt matches it byte for byte, from the beginning. So the
whole question is where the two prompts stop being the same, and everything in
this file is one answer to it: keep a fingerprint of what went out, diff the
next one against it, and count the tokens from the first difference onward.

That number is the useful one. "This chat is 51,000 tokens" says nothing about
what the next turn costs; "47,200 of those 51,000 will be charged at full
price because the tool list changed" says all of it, and names the cause while
it is still cheap to fix.

WHY A FINGERPRINT AND NOT THE TEXT. A digest per segment is a few dozen bytes
where the segment is often tens of kilobytes, and the only operation ever
performed on it is equality. Keeping the text would mean a second copy of
every chat on disk to answer a question that a hash answers exactly as well.

WHAT THIS CANNOT KNOW. Whether the provider still HAS the entry. Caches expire
(five minutes is the usual promise) and are evicted early under load, and
neither of those is visible from here. So a prediction is a prediction, and it
says "should hit" rather than "will hit" - and every prediction is settled
afterwards by what the provider actually reported, which is the number the
panel shows once it exists. See record_reported().

WHERE IT LIVES. One JSON file per chat, beside its history. Not a global
index: a ledger is only ever read for the chat it belongs to, and a chat being
deleted should take its ledger with it rather than leaving a row behind in a
file nobody prunes.
"""

import hashlib
import json
import threading
import time
from pathlib import Path

LEDGER_FILE = "cache.json"

# Read-through cache of what is on disk, by chat id. A chat's ledger is
# rewritten after every request and read on every context poll (which is every
# couple of seconds, per open window), and going to disk for both is a lot of
# I/O for a file that only this process writes.
_lock = threading.Lock()
_mem = {}


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def fingerprint(segments):
    """`segments` - provider.wire_segments()'s output - as the list of digests
    this module compares. Short digests on purpose: this is change detection
    between two prompts in one conversation, not a security boundary, and 64
    bits of it is already far past the point where a collision is less likely
    than the disk being wrong."""
    return [_digest(s) for s in segments]


def _path(chat_dir):
    return Path(chat_dir) / LEDGER_FILE


def read(chat_id, chat_dir):
    """This chat's ledger, or {} when it has none. Shape:

        {"model": "<wire>/<model>", "at": 1724763..., "digests": [...],
         "tokens": [...], "reported": {"cache_read": n, "cache_write": n}}

    A file that will not parse is treated as absent rather than repaired. It
    is a cache of a cache: the cost of losing one is a single turn that says
    "nothing to compare against yet", which is exactly what a chat's first
    turn says anyway."""
    with _lock:
        held = _mem.get(chat_id)
    if held is not None:
        return held
    try:
        data = json.loads(_path(chat_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    with _lock:
        _mem[chat_id] = data
    return data


def record(chat_id, chat_dir, model_key, segments, tokens):
    """Write down what has just been sent. `tokens` is one count per segment,
    in the same order - the caller has them already (it counted the same
    strings to draw the context bar) and counting them twice would mean
    running a tokenizer over the whole conversation on every request.

    A mismatch between the two lists is treated as no counts at all rather
    than as a reason to refuse: the digests still answer "did this change",
    which is most of the value, and only the size of the answer is lost."""
    digests = fingerprint(segments)
    if not isinstance(tokens, list) or len(tokens) != len(digests):
        tokens = []
    data = {"model": model_key, "at": int(time.time()),
            "digests": digests, "tokens": tokens, "reported": {}}
    _save(chat_id, chat_dir, data)
    return data


def record_reported(chat_id, chat_dir, usage):
    """What the provider said actually happened, once the response is in -
    cache_read and cache_write off the usage dict.

    This is the half that makes the other half honest. A prediction of "should
    hit" that comes back with cache_read at zero is not a rounding error, it
    is a silent invalidator nobody has found yet, and the only way to see one
    is to keep both numbers next to each other."""
    if not isinstance(usage, dict):
        return
    got = {k: usage[k] for k in ("cache_read", "cache_write")
           if isinstance(usage.get(k), int)}
    if not got:
        return
    data = dict(read(chat_id, chat_dir))
    if not data:
        return          # nothing was recorded for this request - nothing to settle
    data["reported"] = got
    _save(chat_id, chat_dir, data)


def _save(chat_id, chat_dir, data):
    with _lock:
        _mem[chat_id] = data
    try:
        p = _path(chat_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass    # losing a ledger costs one uninformed prediction, never a turn


def forget(chat_id):
    """Drop the in-memory copy - a chat being deleted, or its folder going
    away under us. The file goes with the folder."""
    with _lock:
        _mem.pop(chat_id, None)


# Where the two prompts first differ, named for a person. The indices are
# positions in wire_segments()' list, which is tools, then system, then one
# per message - so index 0 and 1 have names worth using and everything after
# them is "a turn".
def _cause(at, n_before, tools_first):
    if tools_first and at == 0:
        return "the tool list changed"
    system_at = 1 if tools_first else 0
    if at == system_at:
        return "the system prompt changed"
    if at >= n_before:
        return None     # nothing changed; the new prompt only grew
    return "the history was rewritten"


def predict(chat_id, chat_dir, model_key, segments, tokens, spec, tools_first=True):
    """What the NEXT request will and won't be charged full price for, as

        {"mode", "uncached", "total", "reason", "verified", "at"}

    "uncached" is the tokens from the first difference to the end - what the
    provider has to read fresh. "total" is the whole prompt. "reason" is a
    sentence saying why, or None when the prefix is intact and fresh.

    Four things can make a prompt uncached, and they are worth telling apart
    because three of them are bugs and one is just time passing:

      the tool list changed     something wrote a tool mid-conversation, and
                                tools render at byte zero
      the system prompt changed a context file or a memory was edited
      the history was rewritten a turn already sent went back and changed -
                                the expensive one, and the easiest to do by
                                accident
      the cache expired         nothing is wrong; it has simply been a while

    A model this chat was not last measured on gets the whole prompt counted
    as uncached, and says so: caches are per model, and a Sonnet entry is no
    use to Opus however identical the text.
    """
    total = sum(tokens) if isinstance(tokens, list) else 0
    mode = (spec or {}).get("mode", "none")
    # ttl and at travel with the answer so a reader can redo the "has it
    # expired yet" part of this on its own clock. The context panel is fetched
    # when something CHANGES, not on a timer, and expiry is the one cause here
    # that arrives without anything changing - so the page recomputes that
    # half locally rather than sitting on a stale reassurance for ten minutes.
    out = {"mode": mode, "uncached": total, "total": total,
           "reason": None, "verified": None, "at": None,
           "ttl": (spec or {}).get("ttl_seconds") or 0}
    if mode == "none":
        out["reason"] = None
        return out

    floor = (spec or {}).get("min_tokens") or 0
    if total < floor:
        out["reason"] = ("too short to cache on this model - it caches from "
                         + format(floor, ",") + " tokens")
        return out

    held = read(chat_id, chat_dir)
    if not held or not held.get("digests"):
        out["reason"] = "nothing sent on this chat yet"
        return out
    if held.get("model") != model_key:
        out["reason"] = "this chat's last request was on another model"
        return out

    out["verified"] = (held.get("reported") or {}).get("cache_read")
    out["at"] = held.get("at")

    old = held["digests"]
    new = fingerprint(segments)
    at = 0
    while at < len(old) and at < len(new) and old[at] == new[at]:
        at += 1

    if at >= len(new):
        # The new prompt is a prefix of the old one, or the two are identical.
        # Either way nothing before `at` has to be read again.
        at = len(new)

    reason = _cause(at, len(old), tools_first) if at < len(old) else None
    uncached = sum(tokens[at:]) if isinstance(tokens, list) and len(tokens) == len(new) else total
    out["uncached"] = uncached

    ttl = (spec or {}).get("ttl_seconds") or 0
    stale = ttl and held.get("at") and (time.time() - held["at"]) > ttl
    if reason is None and stale:
        mins = int((time.time() - held["at"]) // 60)
        reason = ("the cache has probably expired - "
                  + (str(mins) + " minutes" if mins else "over "
                     + str(ttl // 60) + " minutes") + " since the last request")
        out["uncached"] = total
    elif stale:
        out["uncached"] = total
    out["reason"] = reason
    return out
