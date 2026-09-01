"""Cancellation for one running turn - what /stop actually acts on.

Stopping used to be a flag the turn checked in exactly two places (between
streamed chunks, and at the top of each tool-loop pass), so /stop was only ever
noticed as fast as the slowest blocking call sitting between those two checks:
a provider that hadn't sent its first byte yet, the safety check's own model
round-trip, a tool part-way through running. Nothing was wrong with any one of
those waits; the problem was that stop-awareness was opt-in, so every blocking
call written since simply didn't have it.

This module inverts that. Three pieces:

  * a CONTEXT per running turn, reachable from anywhere in the call tree
    through current() - not a `should_stop` argument threaded down through
    signatures. Code written later is cancellable without its author having to
    know this file exists.
  * a REGISTRY of the blocking things a turn owns - a live HTTP response, a
    subprocess - closed the moment that turn is cancelled, so a thread parked
    in a socket read wakes up NOW rather than whenever the next chunk happens
    to arrive. This is the difference between "stops in ten seconds" and
    "stops immediately".
  * an ABANDONMENT contract. A cancelled context is dead permanently, and every
    write path guards on it (see guard()). The thread that hasn't noticed yet
    cannot stream to a page, cannot touch a transcript, cannot report a result
    - so ownership of the chat can be handed to the next turn straight away
    instead of waiting for that thread to unwind. Worst case a doomed thread
    burns a little CPU until its socket errors out; it can never corrupt
    anything, which is what makes abandoning it safe as a standing policy
    rather than a trick.

Deliberately imports nothing else from Uniagent: main, provider,
tool_processor and the tool modules all import THIS, so anything it imported
back would be a cycle.
"""

import threading
from contextlib import contextmanager


class Stopped(Exception):
    """Raised inside a turn that has been cancelled, at whatever point it next
    touches something cancellable. It means "this turn is over and its work is
    being thrown away" - never an error to report to the user or feed back to
    the model, since /stop is exactly what they asked for. Callers that catch
    broad exceptions must let this one through (see main.run)."""


# How a registered object is made to give up NOW, tried in this order and
# stopping at the first one it actually has - a Popen has kill(), a Future has
# cancel(), a socket has close().
#
# These run on the STOPPING thread while another thread is still inside the
# object, so anything whose teardown takes a lock that the blocked thread holds
# must pass its own closer to register() instead of relying on this list. A
# requests Response is exactly that case and does (see provider._break_open):
# its close() waits on the buffered reader the stopped thread is sitting in, so
# calling it here would hang the stop on the very read it means to interrupt.
_CLOSERS = ("kill", "close", "cancel", "terminate", "shutdown")


def _closer_for(obj):
    for method in _CLOSERS:
        fn = getattr(obj, method, None)
        if callable(fn):
            return fn
    return None


class TurnContext:
    """One running turn's cancellation state, and enough of its in-progress
    work for whoever cancels it to close the transcript out properly.

    `kind` is "turn" for a chat's own turn (the thing /stop abandons and hands
    the chat straight on from), "subagent" for a subagent's, "compaction" for a
    /compact - which is one request to the model with no safe point to give up
    at, so it holds a chat but is never cancelled.

    `text`, `turns`, `partial`, `thinking` and `phases` are the turn's working
    state, published here for whoever stops it: the message being answered, the
    live turns list, whatever the current response has streamed so far, what it
    was thinking while it did, and the clock running on it. They are what lets
    the STOPPING thread write the stopped transcript itself instead of waiting
    for the abandoned worker to come back and do it - see main.request_stop().

    The last two are here for the same reason `partial` is. A stopped response
    really happened: it waited, it thought, it wrote some of an answer, and all
    of that is as true and as worth keeping as the words it managed. Without
    them a stop threw away the thinking and every measurement of the response
    it interrupted, so the transcript redrawn a second later was missing both -
    which reads as though the turn had never thought at all."""

    def __init__(self, key, kind="turn"):
        self.key = key
        self.kind = kind
        self.event = threading.Event()
        self._closeables = []
        self._lock = threading.Lock()
        # The turn's own working state, published for the stopper. `text` is set
        # before the turn starts, so even a stop landing in the moment between
        # taking the chat and building the turns list still knows what was
        # asked. The other two are read under no lock and only ever replaced
        # wholesale, never mutated in place, so a reader always sees one
        # consistent version.
        self.text = None
        self.turns = None
        self.partial = ""
        self.thinking = ""
        self.phases = None
        # What this turn is running on, published for the same reason the rest
        # of the working state is: whoever writes this turn's ending may not be
        # this thread, and a turn that dies on the provider has to be filed with
        # the pair it actually died on rather than whatever the chat says by
        # then - the model can be changed while the doomed request is still in
        # flight. See main.append_error() and main.model_switch().
        self.provider = None
        self.model = None

    @property
    def cancelled(self):
        return self.event.is_set()

    def register(self, obj, closer=None):
        """Track something blocking, so cancel() can break it. Returns `obj`,
        so it drops into an assignment. Registering into an ALREADY cancelled
        context closes the thing immediately rather than tracking it - a turn
        that opened a socket a moment after being stopped must not be left
        holding it open."""
        with self._lock:
            if not self.event.is_set():
                self._closeables.append((obj, closer))
                return obj
        _close(obj, closer)
        return obj

    def unregister(self, obj):
        with self._lock:
            self._closeables = [pair for pair in self._closeables if pair[0] is not obj]

    def cancel(self):
        """Give up now. True if this call is the one that did it, False if the
        context was already cancelled - which is what keeps a second /stop, or
        a stop racing the turn's own end, from finalising the chat twice.

        The flag is set BEFORE anything is closed, on purpose: the thread woken
        by its socket dropping then finds a cancelled context and treats it as
        a stop, rather than reporting a connection error the user never had."""
        with self._lock:
            if self.event.is_set():
                return False
            self.event.set()
            doomed, self._closeables = self._closeables, []
        for obj, closer in doomed:
            _close(obj, closer)
        return True

    def check(self):
        if self.event.is_set():
            raise Stopped(self.key)


def _close(obj, closer=None):
    """Best effort, and it has to be: whatever this is, it is being torn down
    from a thread that doesn't own it, so half of these raise on principle. The
    turn is over either way - an exception here would only replace a clean stop
    with a crash."""
    fn = closer or _closer_for(obj)
    if fn is None:
        return
    try:
        fn()
    except Exception:
        pass


# The context of the turn running on THIS thread. Thread-local rather than a
# contextvar because turns run on plain worker threads (and a new thread starts
# with an empty context, so contextvars would not carry across the spawn) -
# same reason main.py keeps _turn_chat this way.
_local = threading.local()

# Live contexts by stop key: a chat's flat id for its own turn, a subagent's
# thread tag for a subagent's. Only turns that have ACTUALLY STARTED are in
# here - a turn queued behind another one for the same chat has no entry yet,
# which is what makes /stop unambiguous when two are lined up: it stops the one
# that is running, and the queued one goes on to run normally afterwards.
_registry = {}
_registry_lock = threading.Lock()


def publish(ctx):
    """Make `ctx` the context /stop finds under its key. Called once the turn
    owns whatever it is going to own, never while it is still queueing."""
    with _registry_lock:
        _registry[ctx.key] = ctx
    return ctx


def unpublish(ctx):
    """Drop `ctx` from the registry - but only if it is still the current one
    under that key. A turn abandoned by /stop is unpublished by the stopper and
    the next turn may already have published its own; the zombie calling this
    on its way out must not delete the live one."""
    with _registry_lock:
        if _registry.get(ctx.key) is ctx:
            del _registry[ctx.key]


def get(key):
    with _registry_lock:
        return _registry.get(key)


def bind(ctx):
    """Make `ctx` the context this thread's code runs under."""
    _local.ctx = ctx
    return ctx


def unbind():
    _local.ctx = None


def current():
    """The context of the turn on this thread, or None when there isn't one -
    a request handler, the cron watcher, a tool called from a test."""
    return getattr(_local, "ctx", None)


def cancelled():
    """Whether the turn on this thread has been stopped. False when there is no
    turn on this thread at all, so it is safe to ask from anywhere."""
    ctx = current()
    return ctx is not None and ctx.cancelled


def check():
    """Raise Stopped if this thread's turn has been stopped. The cheap thing to
    call at the top of anything slow."""
    ctx = current()
    if ctx is not None:
        ctx.check()


def guard(ctx, fn):
    """`fn`, wrapped so it does nothing once `ctx` is cancelled. Every callback
    a turn was handed goes through this - streaming text to a page, saving the
    transcript, reporting a tool result - which is what makes abandoning the
    thread safe: it may still be running, but nothing it produces can reach
    anything. None in, None out, so an absent callback stays absent."""
    if fn is None:
        return None

    def guarded(*args, **kwargs):
        if ctx.cancelled:
            return None
        return fn(*args, **kwargs)

    return guarded


@contextmanager
def watch(obj, closer=None):
    """Register `obj` with this thread's turn for as long as the block runs, so
    a /stop landing meanwhile breaks it open instead of waiting it out. A no-op
    when nothing is running on this thread, which is why callers can wrap
    unconditionally."""
    ctx = current()
    if ctx is None:
        yield obj
        return
    ctx.register(obj, closer)
    try:
        yield obj
    finally:
        ctx.unregister(obj)
