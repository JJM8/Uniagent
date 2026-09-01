"""The handful of files that used to be read on the way into every request,
kept in memory instead.

WHY THIS EXISTS
---------------
Several small files sit underneath everything a turn does before it can ask
the model anything: .env (the provider list and every key), wires.json and
wires_custom.json (how each provider's wire is spoken), settings.json (the
model, the safety rules, the compaction rules). None of them is big. All of
them were read, or at least stat()ed, over and over on the way into a single
request - provider._key() re-read the whole of .env per call, wires._read()
stat()ed its two files per call - and the callers above them are loops, so
"per call" turned into hundreds of times per message.

Measured before this existed: one settings.load() cost 351 stat() calls and
40ms, one chat_providers() 27ms, and a single message spent about a second of
its life in syscalls before a byte went to the provider. On a spinning disk a
stat is ~86us against ~2us on an SSD, so the machine this was written on paid
34x for every one of them.

WHAT IT DOES NOT CHANGE
-----------------------
The contract every one of those readers documents: an edit takes effect on the
next turn, with nothing restarted, in this process AND in the separate cron
watcher AND from another machine over Syncthing. That is why none of them
cached in the first place, and it is worth keeping - so this is a cache with
an expiry, not a value read once at startup.

An entry is re-checked at most once every RECHECK seconds. Within that window
a reader gets the value already in hand and touches no disk at all; after it,
one stat decides whether the text is re-read. A second is far shorter than a
turn, so "the next turn sees it" still holds for a file changed by anything,
including a process this one knows nothing about.

A write made THROUGH this process does not wait even that long: settings.save()
and provider.set_env() call forget() on the file they just wrote, so the next
read is guaranteed to see it. The expiry is only there for writers this
process cannot hear.

WHAT IS CACHED IS THE BYTES, NOT THE MEANING
--------------------------------------------
text() answers with the file's contents and nothing more. Whether that JSON is
valid, what the values mean, which of them are usable - all of that stays with
the caller that already knew how to decide it, so this module has no opinion
about any file it holds and cannot be wrong about one. The callers that want
to skip re-parsing as well hang their own memo off signature(), which changes
exactly when the bytes do.
"""

import threading
import time

# How long a cached file is trusted before its mtime is checked again. One
# second: short enough that "the next turn picks it up" is true for every
# caller here (a turn takes seconds at minimum, and the check costs one stat),
# long enough that a loop calling the same reader two hundred times in one
# request does one stat instead of two hundred.
RECHECK = 1.0

_lock = threading.Lock()
# path -> [checked_at, stamp, text]. `stamp` is (mtime_ns, size) rather than
# mtime alone: two writes inside one filesystem timestamp tick are rare but a
# size change catches most of them, and a file that is rewritten to the same
# length in the same nanosecond is not a case worth slowing every read for.
_held = {}
# Bumped whenever any cached file is seen to have changed. Callers that build
# something expensive out of these files (the tool snapshot, the system
# prompt) keep the number they last built at, and rebuild when it moves - see
# signature().
_generation = 0


def _stamp(path):
    """(mtime_ns, size) for `path`, or None when it isn't there.

    None is a real answer, not an error: .env may not exist yet on a fresh
    install and wires_custom.json only exists once something has been
    overridden, and both of those readers already treat "missing" as "nothing
    to add"."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def text(path, default=""):
    """`path`'s contents, from memory when they were checked recently enough.

    `default` comes back for a file that isn't there or can't be read, so a
    caller that treats absence as emptiness - which all of them do - needs no
    try/except of its own."""
    global _generation
    now = time.monotonic()
    with _lock:
        held = _held.get(path)
        if held is not None and now - held[0] < RECHECK:
            return held[2] if held[2] is not None else default

    # The stat and the read happen OUTSIDE the lock. Both touch a disk that
    # can be slow, and holding the lock across them would make every other
    # thread that wants any cached file wait for this one file's I/O - which
    # is the stall this module exists to remove, reintroduced in one place.
    stamp = _stamp(path)
    with _lock:
        held = _held.get(path)
        if held is not None and held[1] == stamp:
            held[0] = now          # unchanged - trust it for another window
            return held[2] if held[2] is not None else default

    body = None
    if stamp is not None:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            body = None

    with _lock:
        previous = _held.get(path)
        _held[path] = [now, stamp, body]
        # Only a real change counts. A first read is not a change - nothing
        # was built on the old value, because there was no old value - and
        # counting it would make every consumer rebuild once for nothing on
        # the first request after a restart.
        if previous is not None and previous[1] != stamp:
            _generation += 1
    return body if body is not None else default


def signature():
    """A number that changes whenever any file held here is seen to have
    changed, and never otherwise.

    For callers whose expensive work is built out of these files rather than
    being one of them: hold the signature you built at, compare, and rebuild
    only when it moves. Cheaper and sharper than each of them re-deriving
    "has anything changed" from mtimes of its own."""
    with _lock:
        return _generation


def forget(path=None):
    """Drop what is held for `path` - or everything, with no argument - so the
    very next read goes to disk.

    Called by the writers in this process the moment they finish writing, so a
    setting saved on the settings page is live on the next read rather than up
    to RECHECK seconds later. A writer that forgets to call this is not
    broken, only up to a second stale."""
    global _generation
    with _lock:
        if path is None:
            _held.clear()
        else:
            _held.pop(path, None)
        _generation += 1


def poll():
    """Re-check every held file now, off the request path.

    The background refresher calls this on its own thread, which is what keeps
    even the once-a-second stat from ever landing on a request: by the time a
    turn asks, the check has already happened and the answer is in memory."""
    for path in list(_held):
        with _lock:
            held = _held.get(path)
            if held is not None:
                held[0] = 0.0      # expire it, so text() below re-checks
        text(path)
