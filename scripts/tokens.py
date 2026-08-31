"""How many tokens a piece of context actually costs, per provider.

The context panel needs a number the moment a chat is opened - before any turn
has run, so before any provider has reported real usage. That number has to be
a real count, not chars/4: it sits next to the model's real context window and
a 15% guess against a 200k window is 30k tokens of nonsense.

Real counting means the provider's own tokenizer, and two of them only exist
behind a network call (Anthropic's /v1/messages/count_tokens, Gemini's
:countTokens). Switching chats has to feel instant, so the rule here is: **the
caller is never made to wait on a network tokenizer.** count() answers
immediately from cache or a local tokenizer, and anything that needs the
network is handed to the background worker below, which announces the real
figure through on_settled() when it lands. That is the whole design -
everything else is bookkeeping around it.

Counts are cached by (tokenizer, hash of the text), which is what makes this
cheap in practice: the expensive segments are the shared ones (context/*.md,
the tool list, memories), identical across every chat on that model, so the
second chat you open costs nothing. The cache is written to disk, so a restart
doesn't start from zero either.

Segments are counted separately and summed rather than counted as one blob.
That is a few tokens off per segment boundary (a tokenizer can merge across a
join that we count as two), and worth it: one edited file re-counts one
segment instead of the whole prompt, and the shared segments stay shared
across chats.
"""

import hashlib
import json
import queue
import threading
import time
from pathlib import Path

import requests

import provider

# Where the count cache lives. Beside the chats rather than in them - it's
# derived data about text, keyed by content hash, so it belongs to no single
# chat and any chat may hit an entry another one paid for.
CACHE_FILE = Path(__file__).parent.parent / "chats" / ".token-cache.json"

# Keep the cache from growing without limit - each entry is tiny, but nothing
# ever invalidates one (a hash of text can't go stale), so without a cap it
# only ever grows. Oldest-inserted go first; a dropped entry costs one
# re-count, never a wrong number.
CACHE_MAX = 20000

# Tokenizers that need a request. Everything else is computed in-process.
NETWORK = ("anthropic", "gemini")

# Fallback when nothing better exists yet - only ever shown as an approximation
# (see exact=False in measure()), never dressed up as a real count.
CHARS_PER_TOKEN = 4


def _hash(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


# --- Which tokenizer a model uses -------------------------------------------

def tokenizer(name, model):
    """The tokenizer id for provider `name`'s `model` - the cache key prefix,
    and what decides who does the counting below.

    Only the provider whose OWN endpoint we can call gets that endpoint:
    bedrock and claude-subscription run Claude models, but bedrock takes
    inference-profile ids (`eu.anthropic.claude-sonnet-4-5-20250929-v1:0`)
    that api.anthropic.com will not accept, and claude-subscription
    deliberately holds no API key at all. Both fall through to tiktoken, which
    is approximate for them and is reported as such."""
    if name == "anthropic":
        return "anthropic/" + model
    if name == "gemini":
        return "gemini/" + model
    if name == "openai":
        return "tiktoken/" + _tiktoken_name(model)
    # deepseek, bedrock, claude-subscription, local, and anything new: no
    # tokenizer of their own we can run, so tiktoken's is used as a stand-in.
    return "tiktoken~/" + _tiktoken_name(model)


def _tiktoken_name(model):
    """The tiktoken encoding for `model`, by name. encoding_for_model() knows
    the OpenAI ids; anything else (or a model newer than the installed
    tiktoken) gets o200k_base, which is what current models use."""
    try:
        import tiktoken
        return tiktoken.encoding_for_model(model).name
    except Exception:
        return "o200k_base"


# --- The cache ---------------------------------------------------------------

_cache = None                 # {cache key: token count}, loaded once, lazily
_cache_lock = threading.Lock()
_dirty = False
_last_write = 0.0
_WRITE_EVERY = 10             # seconds; reads are far more frequent than this


def _load():
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        _cache = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _flush(force=False):
    """Write the cache out, at most every _WRITE_EVERY seconds. Losing the
    last few entries to a hard kill costs a re-count, so this never needs to
    be synchronous with the reads."""
    global _dirty, _last_write
    now = time.time()
    if not _dirty or (not force and now - _last_write < _WRITE_EVERY):
        return
    with _cache_lock:
        data = dict(_load())
        if len(data) > CACHE_MAX:  # oldest insertions first - dicts keep order
            data = dict(list(data.items())[-CACHE_MAX:])
            _cache.clear()
            _cache.update(data)
        _dirty, _last_write = False, now
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # a cache that can't be written is still a cache in memory


def _get(key):
    with _cache_lock:
        return _load().get(key)


def _put(key, count):
    global _dirty
    with _cache_lock:
        _load()[key] = count
        _dirty = True
    _flush()


# --- The background worker ---------------------------------------------------
#
# One thread, one queue, started on first need. Everything a network tokenizer
# is asked for lands here instead of blocking the caller; the answer goes into
# the cache and on_settled() says so. `_queued` stops the same text being
# fetched twice while its first request is still in flight - several readers
# (two open pages, a turn starting) can ask for the same uncounted segment
# inside the second or two one request takes.

_work = queue.Queue()
_queued = set()
_worker = None
_worker_lock = threading.Lock()

# Tokenizers that just failed, and when to try them again. Keyed by tokenizer
# rather than by text: the usual failure is "no ANTHROPIC_API_KEY in .env",
# which is not going to come out differently for the next segment, and without
# this every poll would queue every uncounted segment forever. One retry every
# few minutes picks a key up shortly after it's added, and costs nothing in
# between.
_blocked = {}
_BLOCK_FOR = 300

# Called (on the worker thread) whenever a background count lands and turns a
# chars/4 approximation into a real number. server.py sets this so the page can
# be told to redraw the count it drew as "~"; nothing in the terminal or cron
# process sets it, and None simply means nobody is listening.
on_settled = None


def _is_blocked(tok):
    until = _blocked.get(tok)
    return until is not None and time.time() < until


def _block(tok):
    _blocked[tok] = time.time() + _BLOCK_FOR


def _start_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="token-counter",
                                       daemon=True)
            _worker.start()


def _run_worker():
    while True:
        key, name, model, text, tok = _work.get()
        try:
            count = _count_remote(name, model, text)
            if count is not None:
                _put(key, count)
                if on_settled is not None:
                    try:
                        on_settled()
                    except Exception:
                        pass  # a listener that breaks must not stop the counting
            else:
                _block(tok)
        except Exception:
            # No key, no network, a 400 on a model the endpoint doesn't know:
            # the approximation stands, the panel keeps working, and this
            # tokenizer is left alone for a few minutes (see _blocked).
            _block(tok)
        finally:
            with _cache_lock:
                _queued.discard(key)
            _work.task_done()


def _count_remote(name, model, text):
    """The provider's own count for `text`, over the wire. Runs on the worker
    thread only - never call this from a request handler."""
    if name == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages/count_tokens",
            headers={"x-api-key": provider._key("ANTHROPIC_API_KEY"),
                     "anthropic-version": "2023-06-01"},
            json={"model": model, "messages": [{"role": "user", "content": text}]},
            timeout=20,
        )
        provider._check(r)
        return r.json().get("input_tokens")
    if name == "gemini":
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            ":countTokens",
            headers={"x-goog-api-key": provider._key("GEMINI_API_KEY")},
            json={"contents": [{"parts": [{"text": text}]}]},
            timeout=20,
        )
        provider._check(r)
        return r.json().get("totalTokens")
    return None


# --- Counting ----------------------------------------------------------------

def count(name, model, text):
    """Tokens in `text` for this model. Never blocks on the network, never
    raises - if a real count isn't available right now, an approximation is
    returned and labelled as one.

    Returns (count, state):
      "exact"    this model's own tokenizer produced it
      "approx"   chars/4, and nothing better is on its way: either the model
                 has no tokenizer we can reach at all, or the one it has just
                 failed and is in its retry backoff (see _blocked)
      "pending"  chars/4 for now, real count being fetched in the background -
                 on_settled() fires when it lands, and the next read of this
                 same text gets the real one from the cache

    Nothing but "exact" is ever presented to the user as a real count."""
    if not text:
        return 0, "exact"
    tok = tokenizer(name, model)
    stand_in = tok.startswith("tiktoken~/")  # a tokenizer borrowed from another model
    key = tok + ":" + _hash(text)
    cached = _get(key)
    if cached is not None:
        return cached, "approx" if stand_in else "exact"
    if tok.startswith("tiktoken"):
        n = _count_tiktoken(tok.split("/", 1)[1], text)
        if n is not None:
            _put(key, n)
            return n, "approx" if stand_in else "exact"
        return _approx(text), "approx"  # tiktoken missing entirely - nothing better is coming
    # A network tokenizer with nothing cached: hand it off and answer now.
    if _is_blocked(tok):
        return _approx(text), "approx"  # nothing is coming until the block lifts
    with _cache_lock:
        fresh = key not in _queued
        if fresh:
            _queued.add(key)
    if fresh:
        _start_worker()
        _work.put((key, name, model, text, tok))
    return _approx(text), "pending"


def _count_tiktoken(encoding, text):
    try:
        import tiktoken
        return len(tiktoken.get_encoding(encoding).encode(text, disallowed_special=()))
    except Exception:
        return None


def _approx(text):
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate(name, model, text):
    """A token count for `text` right now, from a local tokenizer only - never
    the network, never queued, never exact.

    count() above is for the context panel, where the number sits beside a real
    context window and being wrong by 30k is a lie worth avoiding; it will
    therefore reach for the provider's own tokenizer and answer "pending" while
    it does. This one is for usage.py, which is writing down what a request
    that has ALREADY happened cost, on a provider that declined to say. There
    is nothing to wait for there - the request is over, a second reply is not
    coming, and a number that lands two minutes later cannot be written into a
    line that was appended two minutes ago. So this answers immediately with
    whatever local tokenizer fits best, and the caller labels the result an
    estimate rather than dressing it up (see usage.py's "estimate" sources).

    The encoding is the model's own where tiktoken knows it and o200k_base
    where it doesn't, which is what makes this an average rather than an
    answer: a local Qwen or a Mistral is being measured with OpenAI's ruler.
    It lands within a few percent for ordinary prose and is the honest best
    available, since the only tokenizer that could do better is the one the
    provider just declined to run.

    Returns 0 for empty text, and falls back to chars/4 if tiktoken isn't
    installed at all - still an estimate, just a coarser one."""
    if not text:
        return 0
    tok = tokenizer(name, model)
    # Anything with a real tiktoken encoding uses it; the network tokenizers
    # (anthropic, gemini) have no local form at all, so they borrow o200k_base
    # the same way an unknown local model does.
    encoding = tok.split("/", 1)[1] if tok.startswith("tiktoken") else "o200k_base"
    key = "est/" + encoding + ":" + _hash(text)
    cached = _get(key)
    if cached is not None:
        return cached
    n = _count_tiktoken(encoding, text)
    if n is None:
        return _approx(text)
    _put(key, n)
    return n


def measure(name, model, segments):
    """Total tokens for a list of text segments, counted one at a time so each
    is cached on its own (see the module docstring on why).

    Returns {"tokens", "each", "exact", "settled"}. `each` is the per-segment
    counts in the order they were given - what cache_ledger needs to say how
    much of a prompt falls after the point two prompts stopped matching, and
    free here because every segment was counted separately anyway. `exact` is
    True only when EVERY segment was counted by this model's own tokenizer -
    one segment falling back to chars/4 (no key, tokenizer down, borrowed
    encoding) makes the whole total approximate, because it is. `settled` is
    False while a background count is still in flight, which is the caller's
    cue that this number will firm up shortly on its own."""
    each, all_exact, settled = [], True, True
    for text in segments:
        n, state = count(name, model, text)
        each.append(n)
        all_exact = all_exact and state == "exact"
        settled = settled and state != "pending"
    return {"tokens": sum(each), "each": each,
            "exact": all_exact, "settled": settled}
