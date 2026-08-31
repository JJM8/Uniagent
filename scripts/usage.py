"""Every request this app pays for, written down as it happens.

One line per request, appended to `usage/YYYY-MM.jsonl`. Nothing here decides
anything or feeds back into a turn - it is a ledger, read by the /usage command
and the settings page's usage tab, and by nothing else.

**Why a line per request and not a running total.** Three separate processes
make requests: the web server, the cron watcher, and the terminal. A shared
JSON object holding totals would have all three doing read-modify-write on the
same file, and the arithmetic of two of them landing together is one of the
increments silently disappearing. An append of one short line has no such race:
the file is opened O_APPEND and written in a single write() call, so the worst
two simultaneous writers can do is order their lines unexpectedly. Totals are
computed at read time instead, which costs a parse and buys correctness.

**Why the numbers are labelled.** Providers disagree about what they tell you.
Anthropic reports input tokens before the first output token exists and updates
the output count as the reply grows; the OpenAI wire reports both, but only if
asked (stream_options.include_usage) and only in a final event; plenty of local
servers report nothing at all. On top of that, a turn that calls a tool stops
reading the stream the moment the call is complete (see main.py's _stream), so
the provider's final usage event frequently never arrives - on exactly the
turns that did the most work.

So every count carries its source:

    "reported"  the provider said so
    "estimate"  the provider didn't, so tokens.estimate() measured the text
                with a local tokenizer - an average, not this model's own
    "unknown"   neither was possible: there is no number and none is invented

A total is therefore always presentable honestly - "1.2M tokens, 340k of them
estimated" - and a request nobody can account for is visible as unknown instead
of quietly counting as zero. Nothing in this file ever turns an estimate into a
reported figure, and nothing ever fills an unknown with a guess.

**Why recording happens on a worker thread.** Estimating means tokenizing the
whole prompt, which for a long chat is tens of milliseconds, and it happens on
the pass of a tool loop that is about to make another request. record() is
therefore a queue put and nothing else; the worker does the tokenizing and the
appending. A turn is never made slower, and never fails, because of
bookkeeping.
"""

import atexit
import json
import os
import queue
import threading
import time
from pathlib import Path

import tokens

# One file per calendar month, in local time - the same month boundary the
# summaries below group by, so a shard is never half in and half out of a
# range. Monthly rather than one file forever because the whole shard is
# re-parsed the first time it is read, and a single file would grow without
# limit; monthly rather than daily because 12 open-and-parse calls a year is
# nothing and 365 is a directory nobody wants to look at.
DIR = Path(__file__).parent.parent / "usage"

# The kinds of request that get recorded. Not enforced - an unknown kind logs
# and displays fine - but these are the ones that exist today, and the reason
# the field exists at all: a safety check fires on EVERY tool call, so knowing
# what share of the bill is the safety model rather than the chat is the single
# most useful thing this ledger can say.
KINDS = ("turn", "safety", "compact", "speak", "infini")

# What the ranges in summary() mean, in days back from today. "all" is
# everything on disk.
RANGES = {"today": 1, "7d": 7, "30d": 30, "all": None}


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _shard(t):
    return DIR / (time.strftime("%Y-%m", time.localtime(t)) + ".jsonl")


# --- Writing -----------------------------------------------------------------

_work = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def record(kind, provider, model, chat=None, usage=None, prompt_text=None,
           reply_text=None, ms=None, ok=True, error=None):
    """Write down one request. Returns immediately; never raises.

    `usage` is the dict provider.stream_response() filled in - any of
    "input_tokens", "output_tokens", "cache_read", "cache_write" may be missing
    or None, which is the normal case rather than an error. `prompt_text` and
    `reply_text` are what was actually sent and received, used ONLY to estimate
    the counts the provider didn't report; pass them and a missing number
    becomes an estimate, omit them and it stays unknown. Neither is stored -
    the ledger holds counts, never conversation text.

    `ok`/`error` record a request that failed. A failed request still costs
    input tokens at most providers, so it is written down rather than dropped,
    and marked so the tab can show it apart from the rest."""
    try:
        _work.put((time.time(), kind, provider, model, chat, dict(usage or {}),
                   prompt_text, reply_text, ms, bool(ok),
                   str(error) if error else None))
        _start_worker()
    except Exception:
        pass  # bookkeeping must never take a turn down with it


def text_of(messages):
    """A request's messages as one string, for estimating what it cost.

    Only ever fed to tokens.estimate(), so this wants to be the same SIZE as
    what went over the wire rather than the same bytes - the exact JSON framing
    a provider wraps around it is a few tokens per message and unknowable from
    here anyway. Content that isn't a plain string (a list of blocks, a tool
    call's arguments) is measured as its JSON, which is close enough for a
    number already labelled an estimate.

    Returns "" for anything unreadable, which record() then treats as unknown -
    an estimate we cannot stand behind is worth less than an honest blank."""
    try:
        parts = []
        for m in messages or []:
            content = m.get("content")
            parts.append(content if isinstance(content, str)
                         else json.dumps(content, default=str))
            calls = m.get("tool_calls")
            if calls:
                parts.append(json.dumps(calls, default=str))
        return "\n".join(p for p in parts if p)
    except Exception:
        return ""


def _start_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="usage-log",
                                       daemon=True)
            _worker.start()


def _run_worker():
    while True:
        job = _work.get()
        try:
            if job is not None:
                _write(_build(*job))
        except Exception:
            pass  # a line that can't be written is a line lost, nothing worse
        finally:
            _work.task_done()


def _build(t, kind, provider, model, chat, usage, prompt_text, reply_text,
           ms, ok, error):
    """One record, with every count resolved to a number and a source."""
    got_in, src_in = _resolve(usage.get("input_tokens"), provider, model, prompt_text)
    got_out, src_out = _resolve(usage.get("output_tokens"), provider, model, reply_text)
    rec = {"t": int(t), "kind": kind, "provider": provider or "", "model": model or "",
           "in": got_in, "in_src": src_in, "out": got_out, "out_src": src_out}
    if chat:
        rec["chat"] = chat
    # Only written when the provider actually reported them, so a zero in the
    # file means "nothing was cached" rather than "this wire doesn't say".
    for field in ("cache_read", "cache_write"):
        value = usage.get(field)
        if isinstance(value, int):
            rec[field] = value
    if ms is not None:
        rec["ms"] = int(ms)
    if not ok:
        rec["ok"] = False
    if error:
        rec["error"] = error[:200]
    return rec


def _resolve(reported, provider, model, text):
    """(count, source) for one side of a request - see the module docstring."""
    if isinstance(reported, int) and reported >= 0:
        return reported, "reported"
    if text:
        return tokens.estimate(provider or "", model or "", text), "estimate"
    return None, "unknown"


def _write(rec):
    """Append one record. One open, one write, one line - see the module
    docstring on why that shape is the whole concurrency story."""
    path = _shard(rec["t"])
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _drain(timeout=5.0):
    """Wait for the worker to finish what's queued, at shutdown or in a test.

    Waits on join() rather than on empty(): the worker takes a job OFF the
    queue and only then tokenizes and writes it, so an empty queue means the
    last record is being written, not that it has been. The difference is one
    lost line every time the process exits - and reliably the most recent one,
    which is the one anybody looking would notice missing.

    Bounded, because a slow tokenizer must not hold the process open. The first
    estimate in a process pays for loading tiktoken's tables, which is seconds
    rather than milliseconds; every one after it is cached. Giving up early
    costs a couple of unrecorded requests and nothing else."""
    if _worker is None:
        return
    try:
        waiter = threading.Thread(target=_work.join, daemon=True)
        waiter.start()
        waiter.join(timeout)
    except Exception:
        pass


atexit.register(_drain)


# --- Reading -----------------------------------------------------------------
#
# Summaries are wanted repeatedly (the tab polls, /usage is cheap to type) over
# a file that only ever grows at the end. So each shard is folded ONCE into
# per-(day, kind, provider, model, chat) buckets and kept; a later read seeks
# to where it stopped and folds only what has been appended since. Buckets
# rather than raw records because that is what collapses: a busy day is
# thousands of requests but a few dozen distinct combinations, so the cache
# stays small no matter how long the ledger runs.

_cache = {}      # path -> {"offset": int, "buckets": {key: counters}}
_cache_lock = threading.Lock()

# The counters a bucket holds. "in"/"out" are the TOTAL tokens on that side,
# with "_est" saying how much of that total came from an estimate rather than
# the provider, and "_unknown" counting the REQUESTS that had no number at all
# (not tokens - there is no token count to add, which is the point).
_ZERO = {"requests": 0, "in": 0, "in_est": 0, "in_unknown": 0,
         "out": 0, "out_est": 0, "out_unknown": 0,
         "cache_read": 0, "cache_write": 0, "errors": 0, "ms": 0}


def _shard_buckets(path):
    """This shard's buckets, folding in whatever is new since the last read."""
    with _cache_lock:
        state = _cache.get(path)
        if state is None:
            state = {"offset": 0, "buckets": {}}
            _cache[path] = state
        try:
            size = path.stat().st_size
        except OSError:
            return {}
        # Shorter than where we stopped means this is not the file we were
        # reading any more (hand-edited, truncated, replaced). Start over
        # rather than carry totals from a file that no longer exists.
        if size < state["offset"]:
            state["offset"], state["buckets"] = 0, {}
        if size > state["offset"]:
            state["offset"] += _fold(path, state["offset"], state["buckets"])
        return dict(state["buckets"])


def _fold(path, offset, buckets):
    """Fold the records after `offset` into `buckets`; return bytes consumed.

    A partial final line is deliberately NOT consumed: a writer may be
    appending as this reads, and half a JSON object is not a record. Leaving
    the offset short means it is read whole on the next pass, a second or two
    later, instead of being lost."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return 0
    end = chunk.rfind(b"\n")
    if end < 0:
        return 0
    for raw in chunk[:end].splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # a torn or hand-mangled line costs one record, not the file
        if isinstance(rec, dict) and rec.get("t"):
            _add(buckets, rec)
    return end + 1


def _add(buckets, rec):
    key = (_day(rec["t"]), rec.get("kind") or "", rec.get("provider") or "",
           rec.get("model") or "", rec.get("chat") or "")
    b = buckets.get(key)
    if b is None:
        b = buckets[key] = dict(_ZERO)
    b["requests"] += 1
    for side in ("in", "out"):
        value, src = rec.get(side), rec.get(side + "_src")
        if isinstance(value, int):
            b[side] += value
            if src == "estimate":
                b[side + "_est"] += value
        else:
            b[side + "_unknown"] += 1
    for field in ("cache_read", "cache_write", "ms"):
        value = rec.get(field)
        if isinstance(value, int):
            b[field] += value
    if rec.get("ok") is False:
        b["errors"] += 1
    return b


def _merge(into, b):
    for k in _ZERO:
        into[k] = into.get(k, 0) + b.get(k, 0)
    return into


def _shards():
    try:
        return sorted(p for p in DIR.glob("*.jsonl") if p.is_file())
    except OSError:
        return []


def _since_day(days):
    """The first day inside a range of `days` days ending today, inclusive -
    so "7d" is today and the six days before it, not the last 168 hours. Day
    boundaries are local midnight, which is what someone reading "today" on
    their own screen means by it."""
    if not days:
        return None
    return _day(time.time() - (days - 1) * 86400)


def summary(range="30d", chat=None):
    """Totals for a range, grouped every way the tab and the command need.

    `range` is a key of RANGES. `chat` limits everything to one chat's own
    requests, which is what /usage chat reports.

    Every group row carries the same counters as the top-level totals, so a
    caller can render any of them with one function. `estimated`/`unknown` ride
    along everywhere rather than being folded away, because a total whose
    provenance has been discarded cannot be labelled honestly later."""
    days = RANGES.get(range, 30)
    since = _since_day(days)
    totals = dict(_ZERO)
    groups = {"model": {}, "kind": {}, "chat": {}, "day": {}}
    first_day = None
    for path in _shards():
        for key, b in _shard_buckets(path).items():
            day, kind, provider, model, chat_id = key
            if first_day is None or day < first_day:
                first_day = day
            if since and day < since:
                continue
            if chat and chat_id != chat:
                continue
            _merge(totals, b)
            _merge(groups["model"].setdefault((provider, model), dict(_ZERO)), b)
            _merge(groups["kind"].setdefault(kind, dict(_ZERO)), b)
            _merge(groups["day"].setdefault(day, dict(_ZERO)), b)
            if chat_id:
                _merge(groups["chat"].setdefault(chat_id, dict(_ZERO)), b)
    return {
        "range": range,
        "since": since,
        "until": _day(time.time()),
        "logging_since": first_day,
        "totals": totals,
        "by_model": _rows(groups["model"], ("provider", "model")),
        "by_kind": _rows(groups["kind"], ("kind",)),
        "by_chat": _rows(groups["chat"], ("chat",)),
        "by_day": sorted(({"day": d} | b for d, b in groups["day"].items()),
                         key=lambda r: r["day"]),
    }


def _rows(group, names):
    """A group dict as a list of rows, biggest first. Sorted by total tokens
    rather than by request count: one enormous request costs more than fifty
    small ones, and this list exists to answer "what is spending the money"."""
    rows = []
    for key, b in group.items():
        values = key if isinstance(key, tuple) else (key,)
        rows.append(dict(zip(names, values)) | b)
    rows.sort(key=lambda r: r["in"] + r["out"], reverse=True)
    return rows
