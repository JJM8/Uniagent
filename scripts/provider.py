"""Dead-simple provider layer: pick a provider + model, get a response back."""

import base64
import functools
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

import filecache
import turnctx
import wires

# --- Fallback defaults. The real choices live with the callers: the main
# agent's model in main.py, the safety-check model in tool_validation.py. These
# are only used if get_response is called without an explicit provider/model. ---
PROVIDER = "local"           # must match a "name" in PROVIDERS below
MODEL = "nanbeige4.2-3b"
TEMPERATURE = 0                 # 0 = most predictable, higher = more random

# Bedrock reads AWS credentials from ~/.aws (or AWS_* env vars); its region
# comes from there too, falling back to London where the account's models live.
BEDROCK_REGION = (os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-2")

# LM Studio's local server - OpenAI-compatible, no API key. Override with
# LMSTUDIO_URL if it's running on another host/port than the default.
LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")

# A capable model will, in one reply, keep going past where it should - making
# up a tool result and then answering its own invented result, or writing the
# user's next message for them. A native tool call needs no stop sequence (the
# provider ends generation itself once it decides to call something), so what
# is left here is only that hallucinated-continuation guard.
STOP = ["\nTool result:", "\nUser:"]

# --- Reply text that is not reply text -------------------------------------
#
# Two things arrive on the content channel that are not the model's answer,
# and both of them used to be shown to the user verbatim as though they were.
#
# THINKING WRITTEN INLINE. A reasoning model is supposed to put its working on
# its own channel (reasoning_content / reasoning, read below), and most do.
# Plenty do not: llama.cpp, Ollama, vLLM and several proxies hand the raw
# generation straight through, tags and all, so the working arrives inside
# `content` wrapped in <think>...</think> - which reached the page as an
# ordinary paragraph, in the middle of the reply, with no way to tell it apart
# from the answer. _ThinkSplit below pulls it back out and sends it where the
# rest of the thinking goes, so ONE code path draws thinking whether the model
# separated it or not.
#
# The spellings are the ones that actually turn up. DeepSeek's DSML build uses
# a fullwidth vertical bar (U+FF5C) inside its tags rather than an ASCII pipe,
# which is why that character is matched explicitly rather than by \W.
# The slash may sit either side of the leading bar - </think> is the ordinary
# spelling, <|/DSML|think|> is DeepSeek's - so it is matched as its own group
# and the group is what says whether a tag opens or closes.
_THINK_TAGS = "think|thinking|thought|reason|reasoning"
_THINK_TAG = re.compile(r"<\s*[|｜]?\s*(/?)\s*[|｜]?\s*"
                        r"(?:DSML\s*[|｜]\s*)?(?:" + _THINK_TAGS
                        + r")\s*[|｜]?\s*(/?)\s*>", re.I)

# A TOOL CALL THE WIRE FAILED TO PARSE. Measured on OpenRouter's DeepSeek-v4
# builds: the model writes its call in DeepSeek's own DSML markup, the endpoint
# strips the opening of that markup while turning it into a structured call,
# and every so often it strips the opening, keeps the closing, and produces no
# structured call at all. What reaches here is a reply whose whole visible text
# is markup rubbish -
#
#     ","command":""}</|DSML|parameter>\n</|DSML|invoke>\n</|DSML|tool_calls>
#
# - with tool_call left empty. Shown as the answer (which is what happened
# before this), the user sees that; the turn is over, and the tool the model
# meant to run never ran. There is not enough left to rebuild the call from -
# the name went with the opening - so this is only ever DETECTED here, and
# main._parse_calls turns the detection into "say that again", the same as any
# other malformed call.
_CALL_MARKUP = re.compile(r"</?[|｜]?\s*(?:DSML[|｜])?\s*"
                          r"(?:tool_calls?|invoke|parameter|antml)\b[^>]*>", re.I)


def looks_like_stray_markup(text):
    """Whether `text` is a mangled tool call rather than a reply - a reply made
    of tag remnants and nothing else.

    Deliberately strict about "and nothing else": a model quoting XML back at
    someone, or explaining this very bug, writes tags inside a sentence, and
    that is a real answer. So markup is only read as a failed call when taking
    it out leaves nothing a person would call prose - and a bare fragment of
    JSON arguments ({"command":"") is not prose, which is the usual remainder."""
    if not text or not _CALL_MARKUP.search(text):
        return False
    rest = _CALL_MARKUP.sub("", text)
    # What is left of a mangled call is the tail of its own arguments. Strip
    # the punctuation those are made of; anything still standing is real text.
    rest = re.sub(r"[\s{}\[\]\"\',:]+", "", rest)
    return len(rest) < 24


def strip_call_markup(text):
    """`text` with tool-call tag remnants taken out. Used on the way to the
    page so a leaked closing tag is not shown, never on the way to a provider -
    history keeps what the model actually wrote."""
    return _CALL_MARKUP.sub("", text)


class _ThinkSplit:
    """Splits a streamed content channel into reply text and inline thinking.

    Fed one delta at a time; answers with the part of it that is reply text,
    and calls `on_think` with the part that was inside a thinking tag. Both may
    be empty, and either may be split across as many deltas as the model likes -
    the tags themselves regularly arrive in pieces ("<th", "ink>"), which is
    why the tail of every delta is held back rather than being tested on its
    own and passed straight through.

    Held back is at most `_HOLD` characters, and only when the tail could still
    become a tag: a delta ending in ordinary prose is never delayed, so this
    costs nothing on the models that already separate their thinking properly.
    close() gives back whatever is still held when the stream ends, so a reply
    that stops inside a half-written tag still shows what it had."""

    # The longest tag this can be part-way through, plus room to spare.
    _HOLD = 24

    def __init__(self, on_think=None):
        self.on_think = on_think
        self._hold = ""
        self._inside = False
        self.saw_tag = False

    def feed(self, piece):
        buf = self._hold + (piece or "")
        self._hold = ""
        out = []
        while buf:
            found = _THINK_TAG.search(buf)
            if found:
                shuts = bool(found.group(1) or found.group(2))
                # A tag that agrees with where we already are is not a
                # boundary: a stray </think> outside a block, or a second
                # <think> inside one, would otherwise flip the stream over and
                # send the whole rest of the answer to the thinking block.
                if shuts == self._inside:
                    self.saw_tag = True
                    head, buf = buf[:found.start()], buf[found.end():]
                    self._emit(head, out)
                    self._inside = not self._inside
                    continue
                # Drop the contradictory tag and carry on as we were.
                head, buf = buf[:found.start()], buf[found.end():]
                self._emit(head, out)
                continue
            # No whole tag in what is left. Hold back only as much of the tail
            # as could still be the front of one, and release the rest.
            keep = min(self._HOLD, len(buf))
            head, self._hold = buf[:len(buf) - keep], buf[len(buf) - keep:]
            if "<" not in self._hold:
                head, self._hold = head + self._hold, ""
            self._emit(head, out)
            break
        return "".join(out)

    def close(self):
        """The end of the stream: give back whatever was still held, as the
        side it was being read as."""
        rest, self._hold = self._hold, ""
        out = []
        self._emit(rest, out)
        return "".join(out)

    def _emit(self, text, out):
        if not text:
            return
        if self._inside:
            if self.on_think:
                self.on_think(text)
        else:
            out.append(text)


# How long claude-subscription waits for the next piece of a reply before
# giving up. Per chunk, not per reply: a long answer that keeps arriving is
# fine, a silent process is not.
CLAUDE_TIMEOUT = 120

ENV_FILE = Path(__file__).parent.parent / ".env"


def _key(name):
    """An API key from the project's .env file - the one place they live, so
    the settings page can show and edit exactly what's actually in use, with
    nothing hiding in the shell environment. Raises a clear error if it's not
    set there, rather than crashing deeper in the request."""
    # Through filecache, not off disk: this is called on the way into every
    # request, several times over, and each call used to read the whole file.
    # The cache re-checks the mtime at most once a second, so an edit still
    # lands on the next turn - by hand, from the settings page, or over
    # Syncthing from another machine.
    for line in filecache.text(ENV_FILE).splitlines():
        line = line.strip()
        if line.startswith(name + "="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    raise RuntimeError(name + " is not set in " + str(ENV_FILE) + " - add it there, or on the settings page.")


def _bearer(key):
    """The Authorization header for an OpenAI-wire endpoint - or no header at
    all when there is no key. A local server (LM Studio, Ollama, vLLM) asks
    for none, and sending a bare "Bearer " with nothing after it is worse than
    sending nothing: some of them reject the malformed header outright where
    they would happily have served an anonymous request."""
    return {"Authorization": "Bearer " + key} if key else {}


class Dropped(Exception):
    """The provider hung up mid-stream before saying anything.

    Distinct from a network error on the way out, and from a stop: the request
    was accepted, the headers came back 200, and then the connection died with
    no reply on it. Free endpoints do this under load. It is separate from
    every other failure because it is the one that can safely be retried -
    nothing was yielded, so re-sending duplicates nothing."""


def _check(r):
    """Raise if the request failed. On failure the providers reply with plain
    text (e.g. '401 Authentication Fails'), which would otherwise blow up as an
    opaque JSONDecodeError - turn it into a clear message.

    The body is decoded here rather than read off r.text, because r.text asks
    requests what the encoding is and requests answers ISO-8859-1 for any
    text/* that named no charset - so an error sentence with a quote mark or an
    accent in it arrived mangled. UTF-8 is what these endpoints actually send;
    "replace" keeps a genuinely undecodable body readable instead of raising a
    second error on top of the one being reported."""
    if r.status_code != 200:
        body = r.content.decode("utf-8", "replace")[:300]
        raise RuntimeError("provider returned HTTP " + str(r.status_code) + ": " + body)


def _pause(seconds):
    """time.sleep, except a /stop cuts it short instead of being waited out.
    Retry backoff is a real part of how long a turn can sit doing nothing, so
    it has to be interruptible like everything else."""
    ctx = turnctx.current()
    if ctx is None:
        time.sleep(seconds)
        return
    if ctx.event.wait(seconds):
        raise turnctx.Stopped(ctx.key)


def _response_socket(r):
    """The raw socket under a streaming requests Response, or None.

    Reached through private attributes because there is no public way down to
    it, and the layers differ by version - so each route is tried and the first
    one that yields something wins, rather than one path being assumed and
    raising on a library upgrade."""
    raw = getattr(r, "raw", None)
    if raw is None:
        return None
    routes = (
        lambda: raw._connection.sock,
        lambda: raw._fp.fp.raw._sock,
        lambda: raw._original_response.fp.raw._sock,
    )
    for route in routes:
        try:
            sock = route()
        except Exception:
            continue
        if sock is not None:
            return sock
    return None


def _break_open(r):
    """Wake a thread blocked reading `r`, from OUTSIDE that thread.

    Deliberately NOT r.close(). Closing a requests Response closes the buffered
    file object the reading thread is sitting inside, and that waits on the very
    lock that thread is holding - so the stopper blocks until the read it is
    trying to interrupt finishes on its own. That is a deadlock, and it is
    precisely the failure this whole change exists to remove: /stop would hang
    for as long as the provider felt like staying silent.

    Shutting the SOCKET down takes no lock at all. The blocked recv returns
    immediately with nothing, the read raises where it stands, and the turn's
    context is already marked cancelled so that raise is read as the stop it is
    (see _sse). Tidying up the Response itself is left to the reading thread,
    which does it on the way out."""
    sock = _response_socket(r)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already gone, which is the outcome wanted anyway


def _stream_post(url, **kwargs):
    """requests.post for a streaming provider call, tied to the turn making it.

    Two things happen here that a bare requests.post can't do. The turn is
    checked first, so a request whose turn was stopped while it queued is never
    sent at all; and the response is handed to the turn's context, so a /stop
    landing while this thread is parked waiting for the model's first token
    closes the connection out from under it and the read raises immediately.
    Without that, /stop could not be noticed until the next chunk arrived -
    which on a slow or thinking model is many seconds away, and is exactly what
    made stopping feel broken.

    Every streaming wire goes through here rather than calling requests
    directly, so a provider added later is cancellable without its author
    having to think about /stop at all."""
    turnctx.check()
    r = requests.post(url, **kwargs)
    ctx = turnctx.current()
    if ctx is not None:
        ctx.register(r, closer=lambda: _break_open(r))
    return r


def _error_text(err):
    """The one sentence in an error payload that a person can act on.

    Endpoints wrap their errors, sometimes twice. LM Studio is the worst of
    them: the sentence that says what actually went wrong arrives as JSON
    embedded in the message string of an outer JSON error -

        {"error": {"message": "Engine protocol predict request returned 400:
         {\\"error\\":{\\"message\\":\\"request (11142 tokens) exceeds the
         available context size (8192 tokens), try increasing it\\"}}"}}

    so this peels: down through "error" keys, and into any JSON object found
    inside a message, until what is left is prose. Bounded, because a payload
    that nests forever must still answer something."""
    for _ in range(8):
        if isinstance(err, dict):
            inner = err.get("error")
            if inner and isinstance(inner, (dict, str)):
                err = inner
                continue
            text = err.get("message") or err.get("detail") or json.dumps(err)
        else:
            text = str(err)
        embedded = re.search(r"\{.*\}", text, re.S)
        if embedded:
            try:
                inner = json.loads(embedded.group(0))
            except ValueError:
                inner = None
            # Only when it is plainly another error envelope. A message can
            # quote JSON for its own reasons ("invalid schema: {...}"), and
            # unwrapping that would throw away the sentence explaining it and
            # hand back the fragment being complained about.
            if isinstance(inner, dict) and (inner.get("error") or inner.get("message")):
                err = inner
                continue
        return text.strip()
    return str(err)


def _sse(r, note=None):
    """The JSON payload of each `data:` line of a Server-Sent Events response.

    OpenAI, DeepSeek, Anthropic and Gemini all stream as SSE, so they share this.
    Blank lines are separators, `event:` lines only name what follows (the same
    name is in the payload), and OpenAI-style APIs end with a literal [DONE].

    An error inside a 200 is raised here rather than passed on. All four wires
    can fail AFTER the headers have gone out - the request was accepted, the
    model was then asked to run it and refused - and they all say so the same
    way, as a frame carrying an "error" object instead of a chunk. No reader
    looks at that key, so an error delivered this way used to be dropped on the
    floor: the stream simply ended, and a turn that failed for a stated reason
    reached the user as "(no reply)". A local server hits this constantly,
    because "this prompt is bigger than the context I loaded the model with" is
    only discovered once the engine has the prompt.

    The read is wrapped in the turn's cancellation watch, so /stop breaks the
    connection rather than waiting politely for the next frame. A cancelled
    turn's socket error is not an error - the context says the turn was
    stopped, so it is re-raised as Stopped and nothing reports a network
    failure the user never had.

    The frames are read as BYTES and decoded here, rather than letting requests
    do it, because letting requests do it was wrong twice over on any endpoint
    that omits a charset - which LM Studio, and every llama.cpp server behind
    it, does: it answers "text/event-stream" with nothing after it.

    Wrong the first time because requests then falls back to ISO-8859-1 for
    any text/* type (RFC 2616's default, which the web has long since stopped
    meaning), so a reply's UTF-8 came back decoded one byte at a time - an
    emoji reaching the user as four characters of mojibake.

    Wrong the second time, and worse, because iter_lines splits a decoded str
    with str.splitlines(), which breaks on far more than "\\n": \\x0b, \\x0c,
    \\x1c-\\x1e, \\x85, and U+2028/U+2029. A mis-decoded emoji produces \\x85
    regularly (it is the last byte of U+1F605 among many others), so a frame
    carrying one was cut in half, failed to parse, and was dropped by the
    handler below as if it were a keep-alive. That silently lost the model's
    text mid-reply, which is the part of this no one could see happening.

    Splitting on b"\\n" alone avoids all of that: a line break is a byte
    boundary, so each frame arrives whole and decodes as the UTF-8 it always
    was. Trailing \\r on a CRLF endpoint is taken off by the strip below.

    A connection that dies PART WAY THROUGH is handled on how much of the reply
    had already made it out, because the two cases want opposite things. Having
    yielded nothing, this raises Dropped and the caller sends the request again
    - no text existed to duplicate. Having yielded something, the reply is
    truncated but real, so the stream simply ends and the turn keeps what it
    got: re-sending there would repeat everything already on screen, and
    raising would throw away a paragraph the user can see.

    That second case is RECORDED, in `note` if a caller passed a dict for it.
    Ending quietly is right for the text; ending quietly and saying nothing was
    not. A reply cut off mid-word is indistinguishable from a finished one once
    it is on the page, and the turn loop would carry on as though the model had
    said its piece - so the fact is written down here, and main.py turns it into
    something the user and the model can both see.
    """
    _check(r)
    sent = False
    with turnctx.watch(r, closer=lambda: _break_open(r)):
        try:
            for raw in r.iter_lines(delimiter=b"\n"):
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    return
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue  # a keep-alive or a partial frame - nothing to read
                if isinstance(event, dict) and event.get("error"):
                    raise RuntimeError(_error_text(event))
                sent = True
                yield event
        except turnctx.Stopped:
            raise
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as e:
            # The socket dying BECAUSE this turn was stopped is the stop
            # working, and check() raises Stopped for it before anything here
            # reads it as the provider's fault.
            turnctx.check()
            if sent:
                if note is not None:
                    note["truncated"] = type(e).__name__
                return  # a short reply beats no reply - see the docstring
            raise Dropped(type(e).__name__ + ": " + str(e))
        except Exception:
            turnctx.check()
            raise


# --- One function per provider. Each takes (model, prompt) and yields the
# reply in pieces as they arrive. get_response joins them back up. ---
#
# `prompt` is either a plain string (a one-shot question with no history - the
# safety check, compaction, a connectivity test - normalized below to a single
# user turn) or a real messages list. main.py's turn loop always sends the
# latter now: a system message (context + memories + tool instructions) plus
# the chat's own `turns` - one real message per turn instead of one flattened
# blob, which is the actual fix (a small local model reads "here is a system
# message, here is what the user said, here is what I said" completely
# differently to one giant wall of text it has to guess the shape of).
#
# turns is stored with tool_calls/tool_call_id on it - OpenAI's own shape -
# and tool calling is NATIVE everywhere: the schemas go over as the API's own
# `tools` array and the call comes back structured, so the stored shape IS
# what the wire wants and _native_messages() sends it through nearly as-is.
# There is no longer a prompted format underneath it - no writing calls out as
# JSON or tags in the reply text and parsing them back - so a model that
# cannot do real function calling cannot call tools here at all. That is the
# trade: one path that every provider takes, instead of two that drift.
#
# `tools` being present is still the signal a provider function switches on,
# because a request can legitimately carry no tools: a one-shot string prompt
# (the safety check, compaction, a connectivity test) has no history to
# preserve and nothing to call, and takes the plain-text path below.
#
# gemini/bedrock/claude-subscription accept `tools` and ignore it -
# claude-subscription genuinely cannot use them (the Agent SDK runs its own
# tool loop, switched off here), so that provider answers with prose and never
# calls a Uniagent tool. See TODO.md.

def _messages(prompt):
    """`prompt` normalized to a real messages list - a plain string becomes
    the one user turn there ever was; a list (main.py's real turn loop)
    passes through untouched."""
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    return prompt


def _compat(messages):
    """(system_text, turns): every message folded to plain "system"/"user"/
    "assistant" - the shape every provider actually gets sent in now. A tool
    turn becomes a user turn prefixed "Tool result:" - the same marker the
    system prompt already teaches the model to recognise. No provider ever
    sees a native "tool" role or a tool_calls field; that only ever existed as
    Uniagent's own bookkeeping in the chat file.

    An assistant turn with a call replays its OWN raw_call text verbatim -
    DSML tags, plain JSON, whatever the model actually wrote that turn - never
    a reconstructed "name(json_args)" text. That reconstruction used to be the
    only option (raw_call didn't exist yet), and it's actively harmful: a
    successful DSML call and a failed made-up-shorthand attempt both collapse
    to the exact same synthetic text, so a model reading back its own history
    can't tell which syntax actually worked - which is precisely what was
    teaching deepseek to keep repeating its mistakes. raw_call is only missing
    on turns saved before this existed, so the reconstruction stays as a
    fallback for old chat files, never for anything new.

    Adjacent turns that end up the same role are merged into one: Anthropic,
    Gemini and Bedrock require strict user/assistant alternation, and OpenAI-
    style providers don't need it but tolerate it fine - and main.py's
    stuck-loop breaker can leave a synthetic "user" turn sitting right before
    the next real one, which this same merge absorbs regardless of provider."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    turns = []
    # Which tool each pending tool_call_id was, but ONLY while the assistant
    # turn above made more than one call. A batch's results are folded into a
    # single user turn by the merge at the bottom of this loop, and unlabelled
    # they arrive as "Tool result: ...\nTool result: ..." with nothing saying
    # which call produced which - so a model that read two files at once has
    # to pair them by position and gets no second chance if it pairs them
    # wrong. One call needs no label and does not get one: that is every
    # transcript written before batching existed, and its wording is what the
    # system prompt teaches.
    named = {}
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            which = named.get(m.get("tool_call_id"))
            marker = "Tool result (" + which + "): " if which else "Tool result: "
            mapped_role, content = "user", marker + (m.get("content") or "")
        elif role == "assistant":
            content = m.get("content") or ""
            if m.get("tool_calls"):
                if "raw_call" in m:
                    content += m["raw_call"]
                else:
                    for call in m["tool_calls"]:
                        fn = call.get("function", {})
                        content += fn.get("name", "") + "(" + fn.get("arguments", "") + ")"
            mapped_role = "assistant"
            calls = m.get("tool_calls") or []
            named = {}
            if len(calls) > 1:
                for call in calls:
                    name = (call.get("function") or {}).get("name")
                    if call.get("id") and name:
                        named[call["id"]] = name
        else:
            mapped_role, content = "user", m.get("content") or ""
        if turns and turns[-1]["role"] == mapped_role:
            turns[-1]["content"] += "\n" + content
        else:
            turns.append({"role": mapped_role, "content": content})
    return system, turns


def _plain_messages(prompt):
    """`prompt` folded to a plain OpenAI-shape messages list - system message
    (if any) followed by user/assistant turns, no tool_calls/tool_call_id/tool
    role. Used by every OpenAI-wire provider (openai, deepseek, local): their
    API supports "system" right inside the messages array, unlike Anthropic/
    Gemini/Bedrock which need it split into its own field via _compat()."""
    system, turns = _compat(_messages(prompt))
    return ([{"role": "system", "content": system}] if system else []) + turns


# Message keys the OpenAI wire actually knows. Uniagent's own bookkeeping keys
# raw_call and raw_calls (see _compat, and main.run for the second) are not
# among them, and an unrecognised key on a message is a 400, so the native path
# copies messages through this filter rather than sending a stored turn
# verbatim.
_NATIVE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")

# DeepSeek's thinking models refuse to replay an assistant turn that made a
# tool call unless that turn also carries back the reasoning_content it
# produced at the time: "The `reasoning_content` in the thinking mode must be
# passed back to the API", HTTP 400. Measured, on deepseek-v4-flash and
# deepseek-v4-pro (deepseek-chat and deepseek-reasoner don't ask for it).
#
# It's DeepSeek-specific, so it lives behind _native_messages' own flag rather
# than in _NATIVE_KEYS - an unrecognised message key is a 400 everywhere else,
# which is the same reason raw_call is filtered out.
#
# Only turns WITH tool_calls need it; a plain assistant turn replays fine
# without. An empty string satisfies the check just as well as the real text
# (also measured), which is what makes the fallback below safe: a chat written
# before this was captured has no reasoning_content to give, and "" replays it
# rather than stranding the chat on a permanent 400.
_REASONING_KEY = "reasoning_content"


def _thought(reasoning, part):
    """One reasoning fragment, collected and (if anyone is watching) forwarded.

    The `reasoning` dict a caller passes in is the channel a thinking model's
    working comes back on. It has always collected that text under "content";
    what it grows here is an optional "on_delta" callback the caller may put in
    it, called with each fragment the moment it lands.

    The callback rides IN the dict rather than arriving as another parameter
    because the dict is already threaded through every provider function, every
    wire and every reader in this file - adding a parameter would mean editing
    a dozen signatures that have no opinion about it, which is exactly the kind
    of churn that makes a thing not worth doing. Callers that want the finished
    text and nothing else pass the same plain dict they always did.

    Nothing here is ever yielded as reply text. Thinking is not the answer -
    see _read_openai's tail for the one exception, a model that puts its whole
    reply in the reasoning field and never sends a content chunk at all."""
    if reasoning is None or not part:
        return
    reasoning["content"] = reasoning.get("content", "") + part
    watching = reasoning.get("on_delta")
    if watching:
        watching(part)


def _native_messages(prompt, reasoning=False):
    """`prompt` in the OpenAI/DeepSeek message shape the chat file already
    stores it in - assistant turns keeping their real `tool_calls`, results
    keeping the `tool` role and the `tool_call_id` tying them back - instead
    of _compat()'s folding to plain text.

    This is the shape the API itself requires once a request declares `tools`
    and the provider is the one generating the calls: a call it made must come
    back as the call it made. The folded text form is right for the prompted
    formats and only for those - there, no provider ever knew a tool call
    happened, so there was nothing to be faithful to.

    Two repairs on the way through, both for histories that are missing a half
    the API treats as mandatory (every tool_calls id must have a `tool`
    message, and vice versa). A turn that failed between making a call and
    storing its result leaves exactly that gap - see main.py's append_error -
    and under the old folding it was invisible, because everything became
    prose. Sending it raw is a hard 400 that would strand the chat: every
    later turn replays the same broken history. So an unanswered call is
    replayed as its raw_call text (what the model wrote, same as _compat
    does), and an unclaimed result becomes a "Tool result:" user turn (the
    marker the system prompt already teaches). Neither drops anything the
    model said.

    `reasoning` keeps DeepSeek's reasoning_content on the turns that made a
    call, filling in "" where a chat pre-dating its capture has none - see
    _REASONING_KEY above. Off for everyone else, where the field is a 400."""
    messages = _messages(prompt)
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    claimed = {c.get("id") for m in messages for c in (m.get("tool_calls") or [])}
    out = []
    for m in messages:
        clean = {k: v for k, v in m.items() if k in _NATIVE_KEYS}
        calls = [c for c in (clean.get("tool_calls") or []) if c.get("id") in answered]
        if clean.get("tool_calls"):
            if calls:
                clean["tool_calls"] = calls
                if reasoning:
                    clean[_REASONING_KEY] = m.get(_REASONING_KEY) or ""
            else:
                del clean["tool_calls"]
                clean["content"] = (clean.get("content") or "") + (m.get("raw_call") or "")
        elif clean.get("role") == "tool" and clean.get("tool_call_id") not in claimed:
            clean = {"role": "user", "content": "Tool result: " + (clean.get("content") or "")}
        out.append(clean)
    return out


def _blocks(prompt, tool_use, tool_result, text_block):
    """The shared body of the content-block history converters below.

    Anthropic and Bedrock both want the same thing structurally - an assistant
    turn's tool call as a block sitting next to its prose, and the result as a
    block on a following user turn - and differ only in what those blocks are
    called and how they spell their ids. So the walking, the gap repair and the
    role merging live here once, and each caller passes three little builders:

      tool_use(id, name, args_dict)  -> that provider's call block
      tool_result(id, text)          -> that provider's result block
      text_block(text)               -> that provider's plain text block

    Returns (system_text, turns), the same pair _compat does, except each
    turn's "content" is a LIST of blocks rather than a string.

    The gap repairs are _native_messages' repairs, for the same reason: a turn
    that died between making a call and storing its result leaves a call with
    no result (or the reverse), and every one of these APIs treats that as a
    hard 400 - which would strand the chat, since every later turn replays the
    same broken history. An unanswered call falls back to its raw_call text,
    an unclaimed result to a "Tool result:" text block. Nothing said is lost.

    Empty text blocks are dropped rather than sent: an assistant turn that was
    nothing but a tool call has content "", and both APIs reject a text block
    with nothing in it. A turn that ends up with no blocks at all is dropped
    whole, for the same reason."""
    messages = _messages(prompt)
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    claimed = {c.get("id") for m in messages for c in (m.get("tool_calls") or [])}

    turns = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue

        if role == "tool":
            if m.get("tool_call_id") in claimed:
                blocks = [tool_result(m["tool_call_id"], m.get("content") or "")]
            else:
                blocks = [text_block("Tool result: " + (m.get("content") or ""))]
            mapped = "user"
        elif role == "assistant":
            text = m.get("content") or ""
            calls = [c for c in (m.get("tool_calls") or []) if c.get("id") in answered]
            if m.get("tool_calls") and not calls:
                text += m.get("raw_call") or ""   # unanswered - replay as prose
            blocks = [text_block(text)] if text.strip() else []
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(tool_use(c.get("id"), fn.get("name") or "", args))
            mapped = "assistant"
        else:
            text = m.get("content") or ""
            blocks = [text_block(text)] if text.strip() else []
            mapped = "user"

        if not blocks:
            continue
        # Same merge _compat does, and needed harder here: Anthropic, Gemini
        # and Bedrock all want strict user/assistant alternation, and a tool
        # result landing as its own user turn right before the next real one
        # is the normal case, not the exception.
        if turns and turns[-1]["role"] == mapped:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": mapped, "content": blocks})
    return system, turns


def _anthropic_messages(prompt):
    """(system, turns) in Anthropic's own content-block shape, for a native
    turn - tool_use blocks on the assistant turn that made the call, tool_result
    blocks on the user turn carrying it back. The folded-to-prose form
    (_compat) is right for a prompted turn and wrong here, for the same reason
    it is on the OpenAI wire: a call the provider itself made has to come back
    as that call, not as a description of one."""
    return _blocks(
        prompt,
        tool_use=lambda id_, name, args: {"type": "tool_use", "id": id_,
                                          "name": name, "input": args},
        tool_result=lambda id_, text: {"type": "tool_result",
                                       "tool_use_id": id_, "content": text},
        text_block=lambda text: {"type": "text", "text": text},
    )


def _bedrock_messages(prompt):
    """(system, turns) in Bedrock converse's content-block shape - the same
    idea as _anthropic_messages, in converse's own camelCase spelling, and
    with a result's text wrapped in its own block list rather than sitting on
    the block as a plain string. converse is one shape for every model family
    Bedrock hosts, so this is not Anthropic-specific even though it reads
    like it."""
    return _blocks(
        prompt,
        tool_use=lambda id_, name, args: {"toolUse": {"toolUseId": id_,
                                                      "name": name, "input": args}},
        tool_result=lambda id_, text: {"toolResult": {"toolUseId": id_,
                                                      "content": [{"text": text}]}},
        text_block=lambda text: {"text": text},
    )


def _gemini_contents(prompt):
    """(system, contents) in Gemini's shape for a native turn.

    Gemini is the odd one of the three. Its parts are functionCall/
    functionResponse rather than tool_use/tool_result, the assistant is called
    "model", and - the part that actually shapes this code - a call carries NO
    id. Gemini matches a response to a call by function NAME, so the ids
    Uniagent stores are used here only to pair a call with its result while
    walking the history, then thrown away. A result whose call is missing
    can't be expressed as a functionResponse at all (there is no name to hang
    it on), so it degrades to the "Tool result:" text part the prompted
    formats have always used."""
    messages = _messages(prompt)
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    # id -> the name it was called with, so a result can name its own function.
    named = {c.get("id"): c.get("function", {}).get("name")
             for m in messages for c in (m.get("tool_calls") or [])}

    contents = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue

        if role == "tool":
            name = named.get(m.get("tool_call_id"))
            text = m.get("content") or ""
            if name:
                parts = [{"functionResponse": {"name": name,
                                               "response": {"result": text}}}]
            else:
                parts = [{"text": "Tool result: " + text}]
            mapped = "user"
        elif role == "assistant":
            text = m.get("content") or ""
            calls = [c for c in (m.get("tool_calls") or []) if c.get("id") in answered]
            if m.get("tool_calls") and not calls:
                text += m.get("raw_call") or ""
            parts = [{"text": text}] if text.strip() else []
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                parts.append({"functionCall": {"name": fn.get("name") or "", "args": args}})
            mapped = "model"
        else:
            text = m.get("content") or ""
            parts = [{"text": text}] if text.strip() else []
            mapped = "user"

        if not parts:
            continue
        if contents and contents[-1]["role"] == mapped:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": mapped, "parts": parts})
    return system, contents


def _flatten(prompt):
    """`prompt` (string or messages list) as one flat text block - system
    text, then one line per turn, "User:"/"Uniagent:" prefixed the same way
    the chat file's own turns are named. Only claude-subscription needs this:
    the Agent SDK's query() takes one string per call, not a messages list."""
    system, turns = _compat(_messages(prompt))
    lines = [system] if system else []
    for t in turns:
        if t["role"] == "assistant":
            lines.append("Uniagent: " + t["content"])
        elif t["content"].startswith("Tool result:"):
            lines.append(t["content"])
        else:
            lines.append("User: " + t["content"])
    return "\n".join(lines)


# --- Dialects: the one part of a wire that cannot be data. ------------------
#
# Everything ABOUT a request - where it goes, what carries the key, which keys
# the body has and in what order - is data now, and lives in wires.json (see
# scripts/wires.py). What is left here is the two halves that are genuinely
# structure rather than arrangement:
#
#   the history    a flat messages list carrying tool_calls (openai), versus
#                  content blocks (anthropic), versus parts holding
#                  functionCall/functionResponse (gemini). These are not one
#                  object with three sets of key names; they nest differently,
#                  which is why the three converters above are three functions.
#   the response   where the text is in each streamed event, where the token
#                  counts are, and whether a tool call arrives as fragments to
#                  be accumulated or whole in a single event.
#
# There are three dialects, and between them they cover essentially every
# endpoint anyone ships - the openai one alone is spoken by OpenRouter, Groq,
# Together, xAI, Mistral, Fireworks, Cerebras, Ollama, vLLM and LM Studio. So
# adding a provider is an entry in wires.json and no Python whatsoever, while
# adding a genuinely new RESPONSE FORMAT is the rare thing it actually is, and
# is honest about needing a reader written here.
#
# A dialect is named by a spec's "dialect" and nothing else. Nothing in this
# file knows the word "openrouter", or "groq", or any other company's name.


def _dialect_turns(dialect, prompt, tools, spec):
    """(system_text, messages) for `prompt` in `dialect`'s own shape.

    `tools` present means this turn is on native tool-calling, so the history
    goes over in the API's own shape - a call the provider itself made has to
    come back as that call. Without tools (a one-shot string prompt: the safety
    check, compaction, a connectivity test) there is no history to be faithful
    to, and the folded plain-text form is right.

    The openai dialect returns "" for the system text because it carries the
    system message inside the messages array like any other turn; anthropic and
    gemini split it out into a field of its own, which is why their body
    templates have a $system and openai's does not."""
    if dialect == "anthropic":
        return _anthropic_messages(prompt) if tools else _compat(_messages(prompt))

    if dialect == "gemini":
        if tools:
            return _gemini_contents(prompt)
        system, turns = _compat(_messages(prompt))
        # Gemini calls the assistant's role "model", not "assistant".
        return system, [{"role": "model" if t["role"] == "assistant" else "user",
                         "parts": [{"text": t["content"]}]} for t in turns]

    # openai. Two spec flags, both DeepSeek's, both expressed as data rather
    # than as an `if wire == "deepseek"` that would have to be edited every
    # time somebody points a card at a DeepSeek-compatible host:
    #
    #   replay_reasoning  its thinking models refuse to replay an assistant
    #                     turn that made a tool call unless that turn carries
    #                     its reasoning_content back - see _REASONING_KEY.
    #   prepend_system    it answers in Chinese without being told not to.
    #
    # Anywhere else, both are absent and this is the plain OpenAI wire.
    if tools:
        messages = _native_messages(prompt,
                                    reasoning=bool(spec.get("replay_reasoning")))
    else:
        messages = _plain_messages(prompt)
    lead = spec.get("prepend_system")
    if lead:
        messages = [{"role": "system", "content": str(lead)}] + messages
    return "", messages


# ---------------------------------------------------------------------------
# Several tool calls in one response
#
# A model may decide to call more than one tool before it stops generating -
# read three files, search two things - and every wire below can carry that.
# What arrives is one call PER SLOT: OpenAI numbers its fragments with an
# `index`, Anthropic and Bedrock open a separate content block per call,
# Gemini sends whole functionCall parts one after another.
#
# The `tool_call` dict a caller passes in is the collector for all of them.
# Slot 0 IS that dict - {"id","name","arguments"} written straight onto it,
# exactly where a single call has always been written - and slots 1..n live in
# tool_call["more"]. Keeping the first call in place is what makes this change
# invisible to everything that only ever expected one: main._parse_calls reads
# the whole list through calls_in(), and nothing else had to move.
#
# Readers never touch the layout themselves; they ask _slot() for the dict to
# write into and it grows the list as needed.


def _slot(tool_call, index=0):
    """The dict parallel call `index` accumulates into, created if new."""
    if index <= 0:
        return tool_call
    more = tool_call.setdefault("more", [])
    while len(more) < index:
        more.append({})
    return more[index - 1]


def calls_in(tool_call):
    """Every finished call in a collector, in order - a list of
    {"id","name","arguments"} dicts, empty if nothing was called.

    A slot with no name is one a reader opened and the stream then ended
    before it said what the call was; it is dropped rather than handed on as
    a nameless call, which is the same thing an empty collector means.

    The one place the layout above is read, so the layout is _slot()'s
    business and nobody else's."""
    if not tool_call:
        return []
    found = []
    for slot in [tool_call] + list(tool_call.get("more") or []):
        if slot.get("name"):
            found.append({"id": slot.get("id"), "name": slot["name"],
                          "arguments": slot.get("arguments", "")})
    return found


class _Show:
    """on_call_delta for a response that may write several calls.

    A single call was shown as "name(" + arguments + ")" with the closing
    bracket put on after the stream ended. With more than one there is a
    second question - when does the PREVIOUS one get its bracket - and the
    answer is "when the next one opens", which is what this exists to track.

    Calls are assumed to be written one after another, which is what all four
    wires actually do. A wire that genuinely interleaved two calls' arguments
    would show them interleaved; what gets PARSED is unaffected either way,
    since that comes off the collector rather than off this text."""

    def __init__(self, on_call_delta):
        self.emit = on_call_delta
        self.open_slot = None

    def opened(self, index, name):
        if not self.emit:
            return
        if self.open_slot is not None:
            self.emit(")\n")
        self.open_slot = index
        self.emit((name or "") + "(")

    def arg(self, text):
        if self.emit and text:
            self.emit(text)

    def done(self):
        if self.emit and self.open_slot is not None:
            self.emit(")")
            self.open_slot = None


def _read_openai(r, usage=None, tool_call=None, reasoning=None, on_call_delta=None):
    """The OpenAI/DeepSeek streamed response, yielding the reply text.

    Two things a thinking model does that a plain one never does are handled
    at the bottom of this function, and both of them used to end as silence:

      - it answers in the wrong field. Some builds stream the whole reply as
        reasoning_content and never send a content chunk at all, so a reader
        that only looks at content sees an empty reply.
      - it thinks until it runs out of room. A small model given a big prompt
        can spend every token it has left on reasoning and stop before writing
        a word of the answer, which the endpoint reports as finish_reason
        "length" and nothing else.

    Neither is a network failure and neither raised anything, which is why a
    local reasoning model reached the user as "(no reply)" - the one message
    that says nothing about what to do next."""
    said = False   # any reply text at all - the difference between the cases below
    thought = ""   # this turn's reasoning, kept only in case nothing else arrives
    cut = False    # the endpoint said the reply stopped at a token limit

    # Thinking the model wrote INLINE, inside the content it was supposed to
    # keep it out of. Pulled off the reply here and sent down the same channel
    # as the reasoning field below, so everything downstream - the page's
    # thinking block, the turn's stored reasoning_content, the clock - sees one
    # kind of thinking and not two. See _ThinkSplit.
    def inline(part):
        nonlocal thought
        thought += part
        _thought(reasoning, part)

    split = _ThinkSplit(on_think=inline)

    # Whether the connection died part-way through. _sse hands back what it had
    # rather than raising once any of the reply is out (see its docstring), so
    # without this the turn simply ends early and nothing anywhere knows the
    # difference between "the model finished" and "the wire dropped mid-word".
    note = usage if usage is not None else {}

    # Brackets and separators for whatever this response turns out to write -
    # see _Show. Made whether or not anything is watching; with no callback it
    # is inert.
    show = _Show(on_call_delta)

    for event in _sse(r, note):
        if usage is not None and event.get("usage"):
            u = event["usage"]
            usage["input_tokens"] = u.get("prompt_tokens")
            usage["output_tokens"] = u.get("completion_tokens")
            # How much of that input was served from the provider's own prompt
            # cache. Nothing here asks for caching - both of these wires do it
            # by themselves and simply report what they hit - so this is
            # observation, not a feature. Two spellings because DeepSeek
            # answers on the OpenAI wire without using OpenAI's field:
            # prompt_cache_hit_tokens is its own. Both are a SUBSET of
            # prompt_tokens above, already counted in it, and recorded
            # separately only because cached input is cheaper than fresh.
            details = u.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens")
            if cached is None:
                cached = u.get("prompt_cache_hit_tokens")
            if isinstance(cached, int):
                usage["cache_read"] = cached
            # How many of those output tokens were thinking rather than reply.
            # Reported by OpenAI's reasoning models and by several local
            # servers; a SUBSET of completion_tokens above, never an addition
            # to it. Recorded so a thinking model's two streams can be given
            # their own honest speeds instead of one figure that is wrong for
            # both - see timing.py's header, and main._split_output.
            out_details = u.get("completion_tokens_details") or {}
            thinking = out_details.get("reasoning_tokens")
            if isinstance(thinking, int):
                usage["reasoning_tokens"] = thinking
        for choice in event.get("choices") or []:
            if choice.get("finish_reason") == "length":
                cut = True
            delta = choice.get("delta", {})
            text = split.feed(delta.get("content"))
            if text:
                said = True
                yield text
            # A thinking model streams its reasoning as its own delta field,
            # alongside (and ahead of) the content and tool_calls fragments -
            # accumulated, never yielded, because it is not part of the reply.
            # It exists only to be handed straight back on the next request
            # (see _REASONING_KEY), and to answer for the reply if no reply
            # ever comes - the two cases at the end of this function.
            # Two spellings, because the local servers disagree and a model
            # that thinks in silence is the whole thing this is here to show.
            # llama.cpp, LM Studio and DeepSeek send reasoning_content;
            # OpenRouter and several vLLM builds send plain `reasoning` on the
            # same wire. Whichever arrives is the same text.
            part = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(part, str) and part:
                thought += part
                _thought(reasoning, part)
            if tool_call is not None:
                # Streamed as fragments: id/function.name arrive once (on the
                # first fragment for that index), function.arguments arrives
                # piecemeal and is concatenated into one JSON-text string,
                # same shape _read_anthropic builds from its own
                # input_json_delta fragments.
                #
                # `index` is the wire's own numbering of PARALLEL calls, and
                # it is what _slot() keys on. Every fragment with an index
                # other than 0 used to be dropped here on the way past, which
                # did not stop a model making three calls - it only stopped
                # two of them ever running, while the model was told the whole
                # turn had been carried out.
                for frag in choice.get("delta", {}).get("tool_calls") or []:
                    slot = _slot(tool_call, frag.get("index", 0))
                    if frag.get("id"):
                        slot["id"] = frag["id"]
                    fn = frag.get("function") or {}
                    # on_call_delta gets the same fragments as readable text as
                    # they land - "name(", then the arguments piecemeal - so a
                    # native call can be WATCHED being written. Without it a
                    # native turn shows nothing at all until the whole stream
                    # ends, since none of this is yielded as reply text. The
                    # closing ")" goes on after the loop.
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                        show.opened(frag.get("index", 0), fn["name"])
                    if fn.get("arguments"):
                        slot["arguments"] = slot.get("arguments", "") + fn["arguments"]
                        show.arg(fn["arguments"])
    # Anything the splitter was still holding when the stream ended - the last
    # few characters of the reply, kept back only in case they turned out to be
    # the front of a tag. Flushed before any of the decisions below, which all
    # depend on whether a reply arrived.
    rest = split.close()
    if rest:
        said = True
        yield rest

    # Closed off once the stream is done, so what was watched being written
    # ends up as the same text the turn is stored and redrawn with - see
    # main.py's _parse_calls.
    called = bool(calls_in(tool_call))
    show.done()

    # The endpoint stopped this reply at a token limit and the reply had
    # already started. Nothing below fires for that case - it is only the
    # empty-reply version, further down, that raises - so a reply that got most
    # of the way and was then cut used to end mid-word with nothing said about
    # it. Recorded the same way a dropped connection is; main.py says so once.
    if cut and (said or called):
        note.setdefault("truncated", "length")

    # A turn that made a tool call is complete with no prose at all - the call
    # IS the turn - so nothing below applies to it.
    if said or called:
        return
    if cut:
        # Nothing was written and the endpoint says it stopped at a limit.
        # Handing over the truncated thinking would be no use to anyone, so
        # what goes back is the reason and the fix. Raised rather than
        # yielded: this is a failed turn, and main.py files a raised message
        # into the history where it can still be read afterwards.
        raise RuntimeError(
            "the model ran out of room before it wrote a reply"
            + (" - it spent the whole response thinking ("
               + str(len(thought)) + " characters of reasoning) and was cut off"
               if thought else "")
            + ". Give it more room to answer in: load it with a longer context "
              "length, /compact this chat, or use a model that thinks less.")
    if thought:
        # The answer is there, just in the wrong field: this model puts
        # everything in reasoning_content and never sends a content chunk.
        # Only ever reached when NO reply text arrived, so a model that thinks
        # and then answers properly is untouched - its thinking stays out of
        # the reply, which is where it belongs.
        #
        # It is therefore no longer thinking, and is un-collected here. Left
        # in, the same words would be stored on the turn twice - once as
        # reasoning_content and once as the reply - and a page that draws a
        # thinking block would show the whole answer inside it and then again
        # underneath. "reclassified" tells the caller the span that was
        # measured as thinking was really the reply being written, so the two
        # can be swapped over (main._stream, timing.Phases.reclassify).
        if reasoning is not None:
            reasoning["content"] = ""
            reasoning["reclassified"] = True
        yield thought


def _anthropic_usage(u):
    """Anthropic's message_start usage block as the shape everything else here
    uses - which needs one correction, not just a rename.

    Anthropic reports `input_tokens` EXCLUSIVE of anything served from cache:
    a request whose whole prompt was a cache hit reports input_tokens: 3 and
    cache_read_input_tokens: 40000. Every other wire reports the inclusive
    figure (OpenAI's cached_tokens is a subset of prompt_tokens, not an
    addition to it). Left as-is, the same conversation would read as 40k of
    input on DeepSeek and 3 on Anthropic, the context-window bar in the panel
    would go quiet the moment caching kicked in, and the usage tab would
    quietly stop counting most of what was sent.

    So the total is put back together here, and the cache figures are kept
    alongside it as the sub-counts they are everywhere else. `input_tokens`
    then means the same thing on every provider: everything that went in."""
    fresh = u.get("input_tokens")
    read = u.get("cache_read_input_tokens")
    written = u.get("cache_creation_input_tokens")
    out = {"output_tokens": u.get("output_tokens")}
    if isinstance(read, int):
        out["cache_read"] = read
    if isinstance(written, int):
        out["cache_write"] = written
    if isinstance(fresh, int):
        out["input_tokens"] = fresh + (read or 0) + (written or 0)
    else:
        out["input_tokens"] = fresh  # not reported at all - stays unreported
    return out


def _read_anthropic(r, usage=None, tool_call=None, reasoning=None, on_call_delta=None):
    """Anthropic's streamed response, yielding the reply text."""
    show = _Show(on_call_delta)
    # Anthropic numbers ALL of a message's content blocks in one sequence -
    # text, thinking and tool_use alike - so a block's own index is not the
    # ordinal of a call. This maps the block index to the slot its call
    # accumulates into, and is what pairs an input_json_delta with the
    # tool_use it belongs to: several calls arrive as several blocks, and
    # without the pairing every fragment landed on whichever call was written
    # onto tool_call last.
    slot_of = {}
    for event in _sse(r):
        etype = event.get("type")
        # Real counts, not an estimate: message_start carries the input side
        # (it's known before a single output token exists), message_delta
        # carries the running output count, updated as the reply grows.
        if usage is not None:
            if etype == "message_start":
                u = event.get("message", {}).get("usage", {})
                usage.update(_anthropic_usage(u))
            elif etype == "message_delta":
                out = event.get("usage", {}).get("output_tokens")
                if out is not None:
                    usage["output_tokens"] = out
        # A tool_use content block starts empty ({"type":"tool_use","id":...,
        # "name":...,"input":{}}) and its `input` arrives afterward as
        # incremental input_json_delta fragments - accumulated into a plain
        # JSON-text string here, the same shape _read_openai builds from
        # OpenAI's own tool_calls fragments, so main.py reads one shape
        # regardless of which provider answered.
        if tool_call is not None and etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                index = len(slot_of)
                slot_of[event.get("index")] = index
                slot = _slot(tool_call, index)
                slot["id"] = block.get("id")
                slot["name"] = block.get("name")
                slot["arguments"] = ""
                show.opened(index, block.get("name"))
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            text = delta.get("text")
            if text:
                yield text
            elif delta.get("thinking"):
                # Extended thinking, which arrives as its own block type and
                # was previously read as nothing at all - the reply simply went
                # quiet for however long the model thought for. Collected on the
                # same channel as every other provider's reasoning, so one
                # shape reaches main.py whoever answered.
                _thought(reasoning, delta["thinking"])
            elif tool_call is not None:
                partial = delta.get("partial_json")
                index = slot_of.get(event.get("index"))
                if partial and index is not None:
                    slot = _slot(tool_call, index)
                    slot["arguments"] = slot.get("arguments", "") + partial
                    show.arg(partial)
    show.done()


def _read_gemini(r, usage=None, tool_call=None, reasoning=None, on_call_delta=None):
    """Gemini's streamed response, yielding the reply text."""
    calls = [0]   # how many functionCall parts have arrived, so far
    for event in _sse(r):
        if usage is not None:
            u = event.get("usageMetadata")
            if u:
                usage["input_tokens"] = u.get("promptTokenCount")
                # Gemini is the odd one out on the output side, the way
                # Anthropic is on the input side: thoughtsTokenCount is NOT
                # part of candidatesTokenCount, it sits beside it. Left alone,
                # a Gemini thinking model's whole reasoning bill went
                # unrecorded - the ledger counted the reply and nothing else,
                # and the tokens you are actually charged for were invisible.
                # So the total is put back together here and the thinking kept
                # alongside it as the sub-count it is everywhere else, which is
                # what makes "output_tokens" mean the same thing on every wire:
                # everything that came out.
                written = u.get("candidatesTokenCount")
                thinking = u.get("thoughtsTokenCount")
                if isinstance(thinking, int):
                    usage["reasoning_tokens"] = thinking
                    usage["output_tokens"] = (written or 0) + thinking
                else:
                    usage["output_tokens"] = written
                # Already part of promptTokenCount, same as OpenAI's - see
                # _anthropic_usage on why Anthropic is the odd one out.
                cached = u.get("cachedContentTokenCount")
                if isinstance(cached, int):
                    usage["cache_read"] = cached
        for candidate in event.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text = part.get("text")
                if text:
                    # Gemini does not give thinking a part type of its own - a
                    # thought is an ordinary text part carrying thought: true.
                    # Untested for, its summarised thinking was yielded INTO
                    # the reply, which is how a Gemini answer could arrive with
                    # its own working printed above it.
                    if part.get("thought"):
                        _thought(reasoning, text)
                    else:
                        yield text
                    continue
                # A functionCall part arrives WHOLE - its args are a real JSON
                # object in one event, not the fragment stream Anthropic and
                # the OpenAI wire both send. So there is nothing to accumulate:
                # the call is complete the moment it appears, and on_call_delta
                # gets it in a single piece rather than character by character.
                # Uniagent's own id, because Gemini doesn't issue one (see
                # _gemini_contents) - main.py needs something to pair the
                # stored call with its result.
                #
                # Gemini puts several calls in a response as several
                # functionCall parts, one after another. They are taken in the
                # order they arrive; the guard here used to be "not
                # tool_call.get('name')", which kept the first and threw the
                # rest away.
                fn = part.get("functionCall")
                if fn and tool_call is not None:
                    slot = _slot(tool_call, calls[0])
                    calls[0] += 1
                    slot["id"] = "call_" + uuid.uuid4().hex[:8]
                    slot["name"] = fn.get("name")
                    slot["arguments"] = json.dumps(fn.get("args") or {})
                    if on_call_delta:
                        on_call_delta((slot["name"] or "") + "("
                                      + slot["arguments"] + ")\n")


READERS = {
    "openai": _read_openai,
    "anthropic": _read_anthropic,
    "gemini": _read_gemini,
}


def _post_stream(url, headers, body):
    """POST a streaming request and hand back the response, having first got
    past the failures that are worth another go.

    All three are recognised by what the endpoint says back, never by which
    provider is being called, so they cost nothing on a wire that never
    produces them and protect a custom wire nobody had thought of yet.

    1. A REJECTED PARAMETER is dropped and the request retried. Newer OpenAI
       models refuse parameters older ones expect - gpt-5.x rejects 'stop', and
       a non-default 'temperature' - with a 400 that names the parameter.
       Keeping a table of which model takes what is unknowable and goes stale,
       so the named parameter is simply removed. Each retry removes exactly
       one, so this terminates. stream_options goes the same way on the local
       servers that don't implement it: usage stays unpopulated for that call
       rather than the turn failing over it.

    2. REASONING REFUSING TOOLS is retried with reasoning switched off.
       OpenAI's reasoning models refuse function tools on /chat/completions and
       say so in as many words: "To use function tools, use /v1/responses or
       set reasoning_effort to 'none'". Rule 1 cannot catch this one, because
       the parameter it names is not in the body - it is the model's own
       default that is the problem, so there is nothing to delete. The trade is
       real: that model does no reasoning on a turn carrying tools. Tools are
       the point of a native turn though, so a thinking model with no tools is
       the worse end of it. Set once, so this terminates.

    3. A FLAKY 401 is retried twice, paced. OpenAI's gpt-5.x endpoints
       intermittently 401 with "insufficient permissions" - measured at roughly
       one request in three on the same key and model that succeed moments
       later. A genuinely bad key fails all three times and still raises, just
       a few seconds later."""
    body = dict(body)
    flaky = 0
    while True:
        r = _stream_post(url, headers=headers, json=body, stream=True)
        if r.status_code == 200:
            return r
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        param = err.get("param")
        if err.get("code") in ("unsupported_parameter", "unsupported_value") and param in body:
            del body[param]
            continue
        if (param == "reasoning_effort" and body.get("tools")
                and body.get("reasoning_effort") != "none"):
            body["reasoning_effort"] = "none"
            continue
        if r.status_code == 401 and flaky < 2:
            flaky += 1
            _pause(1.5 * flaky)
            continue
        _check(r)  # not fixable - raise with the endpoint's own message


def _read_retrying(reader, url, headers, body, **kw):
    """Send the request, read the reply, and send it again if the provider
    hangs up before a single frame comes back.

    This is the other half of Dropped. _sse only raises it when nothing was
    yielded, so re-sending here cannot duplicate text the user has already
    seen, and cannot double-count usage or a half-built tool call - the reader
    never got far enough to touch either. A stream that dies after real content
    ends there instead and never reaches this.

    Three attempts, paced, because a free or overloaded endpoint dropping the
    connection is transient by nature; a provider that is genuinely down drops
    all three and raises with the last reason it gave. _pause is used rather
    than sleep so /stop still lands during the wait."""
    for attempt in range(3):
        try:
            yield from reader(_post_stream(url, headers, body), **kw)
            return
        except Dropped as e:
            if attempt == 2:
                raise RuntimeError(
                    "the provider hung up before sending anything, three times "
                    "over - " + str(e))
            _pause(1.5 * (attempt + 1))


def _openai_style(url, headers, body, usage=None, tools=None, tool_call=None,
                  reasoning=None, on_call_delta=None):
    """Send an OpenAI-wire request and read the reply. The spec path builds its
    own body and calls the two halves directly; this is kept because it is the
    smallest possible thing that exercises _stream_post, _sse and a reader
    together, which is exactly what scripts/test_stop.py wants of it."""
    body = dict(body)
    if tools:
        body["tools"] = tools
    if usage is not None:
        body["stream_options"] = {"include_usage": True}
    yield from _read_retrying(_read_openai, url, headers, body, usage=usage,
                              tool_call=tool_call, reasoning=reasoning,
                              on_call_delta=on_call_delta)


# ---- prompt caching ---------------------------------------------------------
# Every provider worth caching against does the same thing: it keeps the front
# of a prompt it has already processed, and charges a tenth of the price for
# the part of the next prompt that matches it byte for byte. What differs is
# only WHO ASKS.
#
#   automatic  the endpoint caches by itself and reports what it hit. Nothing
#              to send. OpenAI, DeepSeek, Grok, Gemini - and so most of what
#              OpenRouter forwards to.
#   explicit   nothing is cached unless the request marks where the reusable
#              part ends. Anthropic, and Anthropic through OpenRouter, which
#              passes the marks along.
#   none       no cache, or none it will admit to - a local llama.cpp, the
#              Claude CLI. Say nothing rather than guess.
#
# The minimum is not a detail: a prefix shorter than it caches nothing at all,
# silently, marks or no marks. And it is NOT monotonic across model
# generations - 512 on the newest Anthropic models, 4096 on Opus 4.6 - so it
# is worth reading off the model rather than assumed.
CACHE_NONE = {"mode": "none"}

# What OpenRouter's own upstreams do, by the vendor prefix its model ids carry
# ("anthropic/claude-opus-5"). This is the only place the vendor half of an
# OpenRouter id means anything, and it means it because caching is the
# upstream's behaviour, not the router's.
_OR_EXPLICIT = ("anthropic/",)

# Anthropic's minimum cacheable prefix, by model. Keyed on a substring of the
# id so it works for the bare id and for OpenRouter's "anthropic/..." spelling
# alike, longest match first.
_ANTHROPIC_MIN = (
    ("claude-opus-4-6", 4096),
    ("claude-opus-4-5", 4096),
    ("claude-haiku-4-5", 4096),
    ("claude-opus-4-7", 2048),
    ("claude-opus-5", 512),
    ("claude-fable-5", 512),
    ("claude-mythos-5", 512),
)
_ANTHROPIC_MIN_DEFAULT = 1024


def _anthropic_min(model):
    for needle, floor in _ANTHROPIC_MIN:
        if needle in model:
            return floor
    return _ANTHROPIC_MIN_DEFAULT


# The endpoints whose caching behaviour is actually known, by host. Keyed on
# the HOST and not on the wire, because the wire is not the answer: an
# OpenRouter card is normally a plain "openai" wire pointed at openrouter.ai,
# and so is a llama.cpp on localhost - the same wire, one caching and one not.
# The host is what says which service is really on the other end.
_CACHE_HOSTS = {
    "openrouter.ai": "openrouter",
    "api.openai.com": "openai",
    "api.deepseek.com": "deepseek",
    "generativelanguage.googleapis.com": "gemini",
    "api.anthropic.com": "anthropic",
}


def _cache_service(name):
    """Which of the known services `name` actually talks to, or "" when it is
    not one of them. The wire is asked first for the wires that ARE a service
    (a card genuinely on the anthropic wire), then the host.

    "" is the honest answer for a local server, a proxy, or anything new: no
    claim is made about a cache nobody here knows the rules for."""
    wire = wire_of(name)
    if wire in ("openrouter", "anthropic", "deepseek", "gemini"):
        return wire
    p = custom_provider(name)
    url = custom_base_url(p) if p else wire_default_url(wire)
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    for known, service in _CACHE_HOSTS.items():
        if host == known or host.endswith("." + known):
            return service
    return ""


def cache_spec(name, model):
    """How `name`'s endpoint caches `model`, as
    {"mode", "min_tokens", "ttl_seconds", "marks"}.

    "marks" is whether this code has to put cache_control breakpoints in the
    request itself (mode "explicit") - the one thing that changes what gets
    SENT rather than only what gets reported.

    Deliberately conservative outside the services actually known: anything
    else gets CACHE_NONE, so nothing is ever warned about a cache that may not
    exist. A wrong "this will be cached" is worse than no answer, because the
    number it puts on screen is one somebody would plan around - and the case
    that makes this matter is a local llama.cpp, which sits on the same wire
    as OpenAI and caches nothing."""
    service = _cache_service(name)
    if service == "openrouter":
        # The router does not cache; whoever it forwards to does. So the id's
        # vendor half is the question ("anthropic/claude-opus-5"), and an id
        # with no vendor in it is something we cannot answer for.
        if any(model.startswith(v) for v in _OR_EXPLICIT):
            return {"mode": "explicit", "min_tokens": _anthropic_min(model),
                    "ttl_seconds": 300, "marks": True}
        if "/" not in model:
            return dict(CACHE_NONE)
        return {"mode": "automatic", "min_tokens": 1024,
                "ttl_seconds": 300, "marks": False}
    if service == "anthropic":
        return {"mode": "explicit", "min_tokens": _anthropic_min(model),
                "ttl_seconds": 300, "marks": True}
    if service in ("openai", "deepseek", "gemini"):
        return {"mode": "automatic", "min_tokens": 1024,
                "ttl_seconds": 300, "marks": False}
    return dict(CACHE_NONE)


# Where the breakpoints go, and how many. Two is the useful number and four is
# the limit: one at the end of the system message, which covers the tool
# schemas too (they render ahead of it), and one at the end of the newest turn,
# which covers the whole conversation so far. Anything more is breakpoints
# spent on boundaries that move every turn anyway.
def _mark_openai_blocks(messages, spec):
    """`messages` with cache_control on the system message and on the last
    turn, in the shape OpenRouter forwards to Anthropic: a message's plain
    string content becomes a one-element list of parts, and the mark rides on
    the part.

    A copy - the caller's list and its dicts are left alone, because they are
    the turn's own working history and a mark written into that would be
    stored in the chat file and replayed for ever.

    Only turns whose content is a plain string are marked. A tool call has no
    text block to hang a mark on, and one whose content is already a list came
    from somewhere that knows more about its shape than this does."""
    if not spec.get("marks") or not messages:
        return messages
    out = [dict(m) for m in messages]
    at = []
    for i, m in enumerate(out):
        if m.get("role") == "system":
            at.append(i)
            break
    for i in range(len(out) - 1, -1, -1):
        if isinstance(out[i].get("content"), str) and out[i]["content"]:
            if i not in at:
                at.append(i)
            break
    for i in at:
        text = out[i].get("content")
        if not isinstance(text, str) or not text:
            continue
        out[i]["content"] = [{"type": "text", "text": text,
                              "cache_control": {"type": "ephemeral"}}]
    return out


def wire_segments(name, model, prompt, tools=None):
    """Every distinct piece of THIS request, in the order the endpoint reads
    it, as strings - the tool schemas first, then one string per message.

    Not for sending: for comparing. Caching is a prefix match on the exact
    bytes, so the only way to say honestly whether the next request will hit
    the cache is to render what it would send and diff it against what was
    sent last time. Anything less - digesting the turns list before the
    dialect has had its way with it - misses the changes the dialect itself
    makes, and those are real: a tool call whose result has gone missing is
    replayed as prose rather than as a call, and that rewrites a message
    sitting in the middle of the prefix.

    Rendered through the same _dialect_turns every real request goes through,
    for exactly that reason."""
    spec = wires.spec_for(wire_of(name)) or {}
    dialect = spec.get("dialect") or "openai"
    system, messages = _dialect_turns(dialect, prompt, tools, spec)
    parts = []
    if tools:
        parts.append(json.dumps(tools, sort_keys=True, ensure_ascii=False))
    if system:
        parts.append(str(system))
    for m in messages:
        parts.append(json.dumps(m, sort_keys=True, ensure_ascii=False))
    return parts


def _spec_wire(wire):
    """The provider function for a wire described in wires.json.

    This is the whole of what used to be five near-identical functions. It
    builds the turn's values, hands them to the spec to be rendered into a
    request in the order that spec asks for, sends it, and reads the answer
    with the dialect's reader. Nothing in it is specific to any endpoint.

    The spec is re-read per call rather than captured, so editing a wire on the
    settings page takes effect on the very next turn - no restart, and no stale
    copy held by a provider object that was built when the server started."""
    def call(model, prompt, temperature=TEMPERATURE, usage=None, tools=None,
             tool_call=None, reasoning=None, on_call_delta=None,
             base_url="", key="", setup=None, provider_name=""):
        spec = wires.spec_for(wire)
        if not spec:
            raise RuntimeError("no wire called " + wire + " - it was removed "
                               "from wires.json while a provider still used it.")
        dialect = spec.get("dialect") or "openai"
        reader = READERS.get(dialect)
        if reader is None:
            raise RuntimeError(wire + ' names dialect "' + str(dialect)
                               + '", which Uniagent cannot read. Known: '
                               + ", ".join(READERS) + ".")

        system, messages = _dialect_turns(dialect, prompt, tools, spec)
        # Ask for caching, where asking is what it takes. The openai dialect
        # carries the system message inside `messages`, so both breakpoints go
        # in there; nothing is marked at all on a wire that caches by itself,
        # which is most of them.
        if dialect == "openai":
            messages = _mark_openai_blocks(messages, cache_spec(provider_name, model))
        url, headers, body = wires.build(spec, {
            "model": model,
            "messages": messages,
            "system": system,
            "temperature": temperature,
            "tools": tools,
            "stop": STOP,
            "max_tokens": spec.get("max_tokens"),
            # Without this the OpenAI wire never reports usage at all: the real
            # counts arrive in one extra final SSE event, which the endpoint
            # only sends when asked. Absent when nobody wants counts, which
            # prunes the whole stream_options object out of the body.
            "want_usage": True if usage is not None else None,
            "key": key,
            "base_url": base_url,
            "setting": setup or {},
        })

        # A router serving this model from several companies, and a choice of
        # which one recorded against the pair (see set_model_route). Merged in
        # rather than templated into the body above, so a wire that CAN route
        # still sends exactly what it always sent for every model nobody has
        # picked an endpoint for - which is nearly all of them.
        route = model_route(provider_name, model) if provider_name else ""
        if route:
            extra = wires.route_body({**spec, "routes": routes_spec_for(provider_name)},
                                     route)
            body = {**body, **extra}

        yield from _read_retrying(reader, url, headers, body, usage=usage,
                                  tool_call=tool_call, reasoning=reasoning,
                                  on_call_delta=on_call_delta)
    return call


def _bedrock_client(service, base_url=None, setup=None):
    """A boto3 client for `service`, using this provider's own AWS settings
    where it has them and this machine's where it doesn't.

    Every argument is only passed when it has a value, because boto3's own
    fallback chain - ~/.aws/credentials, the AWS_* variables, an instance
    role - is better than anything reimplemented here, and passing None
    explicitly is not the same as not passing it.

    base_url is still read as a region when nothing else names one: that is
    what Bedrock's URL box used to mean, before AWS_REGION on the setup form
    gave it a proper home, and a provider set up the old way must keep working.
    """
    import boto3
    setup = setup or {}
    region = (setup.get("AWS_REGION") or "").strip() \
        or (base_url or "").strip() or BEDROCK_REGION
    args = {"region_name": region}
    if setup.get("AWS_PROFILE"):
        # A profile is its own complete set of credentials, so it goes through
        # a Session rather than sitting alongside loose keys on the client.
        session = boto3.Session(profile_name=setup["AWS_PROFILE"].strip(),
                                region_name=region)
        return session.client(service)
    if setup.get("AWS_ACCESS_KEY_ID") and setup.get("AWS_SECRET_ACCESS_KEY"):
        args["aws_access_key_id"] = setup["AWS_ACCESS_KEY_ID"].strip()
        args["aws_secret_access_key"] = setup["AWS_SECRET_ACCESS_KEY"].strip()
        if setup.get("AWS_SESSION_TOKEN"):
            args["aws_session_token"] = setup["AWS_SESSION_TOKEN"].strip()
    return boto3.client(service, **args)


def _bedrock(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
             reasoning=None, on_call_delta=None,
             base_url=None, key=None, setup=None, provider_name=""):
    # base_url/key are here so this is callable exactly like every other wire
    # (see _wire_call). Bedrock has no URL to point at and no key to send - it
    # signs with the AWS credentials on this machine - so a provider's base URL
    # is read as a REGION override instead, which is the one part of "where
    # does this go" that Bedrock does let you choose. Its key box is unused.
    # Bedrock authenticates with AWS SigV4, not a bearer token, so this goes
    # through boto3 - it picks up ~/.aws credentials and the region on its own.
    # converse_stream is Bedrock's unified streaming API: every model family
    # (Anthropic, Nova, Llama, ...) takes the same shape - message content is
    # always a list of blocks, never a bare string. The old code called
    # invoke_model_with_response_stream with a hand-rolled Anthropic-only body
    # (content as a plain string), which is exactly why Nova models rejected it
    # ("expected type: JSONArray, found: String"). converse_stream fixes that
    # for every model at once, with no per-model branching needed.
    client = _bedrock_client("bedrock-runtime", base_url, setup)

    # With `tools`, the history goes over as converse's toolUse/toolResult
    # blocks; without them it's a prompted turn and plain text blocks are
    # right. Same switch every other native provider makes.
    if tools:
        system, turns = _bedrock_messages(prompt)
        messages = turns
    else:
        system, turns = _compat(_messages(prompt))
        messages = [{"role": t["role"], "content": [{"text": t["content"]}]} for t in turns]

    inference_config = {
        "temperature": temperature,
    }
    # Qwen models on Bedrock don't support the stopSequences field
    # Temporarily disabling stopSequences for all Bedrock models
    # if not model.startswith("qwen."):
    #     inference_config["stopSequences"] = STOP
    kwargs = {
        "modelId": model,
        "messages": messages,
        "inferenceConfig": inference_config,
    }
    if system:
        kwargs["system"] = [{"text": system}]
    if tools:
        kwargs["toolConfig"] = {"tools": tools}

    # Bedrock hosts many model families and tool support is NOT uniform across
    # them - the Anthropic and Nova ones take toolConfig, google.gemma-3 does
    # not, and qwen/zai vary by build. A model that doesn't support it fails
    # the whole request with a ValidationException rather than ignoring the
    # field, so one dropped and retried without it - the same defensive move
    # _openai_style makes for a parameter a newer OpenAI model rejects.
    #
    # The turn then runs with NO tools at all, which is worth being clear
    # about: the prompt's own tool section only ever lists skills now (see
    # tool_processor._tools_text), so the model has nothing to call and will
    # answer in prose instead of using a tool. On a model whose Bedrock
    # profile refuses toolConfig outright, that is simply the ceiling of what
    # that model can do here.
    try:
        r = client.converse_stream(**kwargs)
    except Exception as e:
        if not tools or "toolConfig" not in str(e):
            raise
        kwargs.pop("toolConfig")
        # The blocks are Anthropic-shaped either way; re-fold to plain text so
        # a model that can't read toolUse still sees the history it made.
        system, plain = _compat(_messages(prompt))
        kwargs["messages"] = [{"role": t["role"], "content": [{"text": t["content"]}]}
                              for t in plain]
        r = client.converse_stream(**kwargs)

    show = _Show(on_call_delta)
    # contentBlockIndex numbers every block in the message, text included, so
    # it is not the ordinal of a call - same arrangement, and same reason, as
    # _read_anthropic's map of the identical wire.
    slot_of = {}

    for event in r["stream"]:
        # converse_stream sends one "metadata" event, usually last, carrying
        # the real token counts - separate from the contentBlockDelta events
        # that carry the actual text.
        if usage is not None and "metadata" in event:
            u = event["metadata"].get("usage", {})
            usage["input_tokens"] = u.get("inputTokens")
            usage["output_tokens"] = u.get("outputTokens")
            # Bedrock reports cache tokens on the side, like Anthropic does -
            # but inputTokens here is already the inclusive total, so these are
            # recorded and not added on (see _anthropic_usage for the wire
            # where that isn't true).
            for field, key in (("cacheReadInputTokens", "cache_read"),
                               ("cacheWriteInputTokens", "cache_write")):
                value = u.get(field)
                if isinstance(value, int):
                    usage[key] = value
        # A tool call opens with contentBlockStart carrying its id and name,
        # then its input arrives as contentBlockDelta toolUse.input fragments -
        # a JSON-text string built up piece by piece, exactly like Anthropic's
        # partial_json and OpenAI's function.arguments. Same accumulated shape
        # lands in tool_call for main.py, whichever provider answered.
        start = event.get("contentBlockStart", {}).get("start", {}).get("toolUse")
        if start and tool_call is not None:
            index = len(slot_of)
            slot_of[event.get("contentBlockIndex")] = index
            slot = _slot(tool_call, index)
            slot["id"] = start.get("toolUseId")
            slot["name"] = start.get("name")
            slot["arguments"] = ""
            show.opened(index, start.get("name"))
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if text:
            yield text
        # converse's own name for the same thing Anthropic calls a thinking
        # block. Nested one level deeper and otherwise identical.
        thinking = (delta.get("reasoningContent") or {}).get("text")
        if thinking:
            _thought(reasoning, thinking)
        use = delta.get("toolUse")
        if use and tool_call is not None:
            partial = use.get("input")
            index = slot_of.get(event.get("contentBlockIndex"))
            if partial and index is not None:
                slot = _slot(tool_call, index)
                slot["arguments"] = slot.get("arguments", "") + partial
                show.arg(partial)

    # Bedrock's qwen builds send a MALFORMED input fragment: the opening `{"`
    # is missing, so read_file's arguments arrive as
    #     path": "README.md"}
    # instead of {"path": "README.md"}. Measured off the raw converse_stream
    # events on qwen.qwen3-32b-v1:0 - the event itself is broken, nothing here
    # dropped it. Unrepaired it is a dead end rather than a bad turn: the
    # arguments don't parse, main.py's _parse_calls asks for the call again,
    # and the model re-emits the identical broken fragment until the stuck-loop
    # breaker gives up.
    #
    # So: only when the accumulated text doesn't parse, and only when putting
    # those two characters back makes it parse, is it repaired. Anything else
    # is left exactly as it arrived for _parse_calls to reject in the normal
    # way - a genuinely garbled call must still read as one.
    #
    # Every call in the response is checked, not just the first: the fault is
    # per-fragment, so a response carrying three calls can have any of them
    # arrive broken.
    for slot in [tool_call] + list((tool_call or {}).get("more") or []):
        if not (slot and slot.get("arguments")):
            continue
        try:
            json.loads(slot["arguments"])
        except json.JSONDecodeError:
            patched = '{"' + slot["arguments"]
            try:
                json.loads(patched)
                slot["arguments"] = patched
            except json.JSONDecodeError:
                pass

    show.done()


# What each kind of Agent SDK failure means in terms of something you can
# actually go and do about it. The SDK reports these as a word on the message
# or inside the CLI's error text, so both paths below look them up here.
CLAUDE_ERRORS = {
    "authentication_failed": "the Claude Code login has expired - run: claude login "
                             "(or, headless: claude setup-token)",
    "rate_limit": "the Claude subscription's usage limit is spent - subscriptions "
                  "refill on a rolling 5-hour window, so this clears on its own; "
                  "until then use an API-key provider",
    "billing_error": "the Claude subscription is not covering this - check the plan "
                     "at claude.ai/settings/billing",
    "invalid_request": "the Claude Code CLI rejected the request - the model id may not exist",
}


def _claude_cli(setup=None):
    """Where the claude CLI is, or None.

    CLAUDE_CLI_PATH on the provider first, then PATH. The setting exists
    because PATH is the thing that differs between "works in my terminal" and
    "the server can't find it": a systemd user service starts with a minimal
    PATH that leaves out ~/.local/bin, which is exactly where npm and pipx put
    the CLI. Being able to name the file directly is the fix that doesn't
    involve editing a unit file."""
    import shutil
    named = ((setup or {}).get("CLAUDE_CLI_PATH") or "").strip()
    if named:
        path = Path(named).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which("claude")


def _claude_subscription(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
                         reasoning=None, on_call_delta=None,
                         base_url=None, key=None, setup=None, provider_name=""):
    """base_url/key are accepted and ignored, so this is callable exactly like
    every other wire (see _wire_call). This one drives the Claude Code CLI,
    which owns its own login - there is no endpoint to point it at and no key
    to hand it, so both boxes on its card do nothing.

    THIS IS THE ONE-SHOT PATH ONLY. A chat turn on this provider does not come
    here at all: main.run() sends it to claude_session.py instead, which keeps
    a Claude Code session alive per chat and lets it run its own tool loop
    under Uniagent's approval gate. What still arrives here is everything that
    asks this provider ONE question and wants one block of text back - the
    safety check, compaction, a connectivity test - none of which have tools,
    a conversation, or anything to keep warm afterwards.

    tools/tool_call are accepted and not used for exactly that reason: nothing
    that reaches this function passes any. A stray ToolUseBlock is logged and
    ignored further down, since with no tools configured there should never be
    one.

    Claude Code's own runtime, driven through the Agent SDK, signed in as the
    Claude subscription already logged in on this machine. Every other provider
    here is an HTTP API billed per token against a key in .env; this one spends
    the subscription's usage window instead and needs no key at all. It is also
    the only sanctioned way to spend a subscription from code - the alternative
    of lifting the OAuth token and calling api.anthropic.com with it is not.

    The SDK's whole purpose is to run an *agent*: its own tools, its own loop,
    its own system prompt. All of that is switched off below, because none of
    it belongs on a one-shot question - this has to be nothing more than the
    plain text-completion endpoint every other function in this file is.

    TEMPERATURE is the one thing the other providers get that this cannot: the
    SDK exposes no way to ask for it, so a chat's temperature setting means
    nothing here and the wire's card says so. Stop sequences are missing too
    and no longer matter - they existed to halt generation at a tool call, and
    nothing that reaches this function has any tools to call.
    """
    # Imported here, not at module scope, so the SDK is only needed if you
    # actually use this provider - same as boto3 in _bedrock above.
    import asyncio
    import queue
    import threading

    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ClaudeSDKClient, ResultMessage,
                                      StreamEvent, ToolUseBlock)
    except ImportError:
        raise RuntimeError("claude-subscription needs the Agent SDK - "
                           "run: pip install --user claude-agent-sdk")
    cli = _claude_cli(setup)
    if not cli:
        raise RuntimeError(
            "claude-subscription can't find the Claude Code CLI. If `which claude` works "
            "in your terminal but this doesn't, it's PATH: put the full path in this "
            "provider's CLAUDE_CLI_PATH box on the providers tab. Otherwise install the "
            "CLI, then run: claude login")
    timeout = CLAUDE_TIMEOUT
    try:
        timeout = int(float((setup or {}).get("CLAUDE_TIMEOUT") or CLAUDE_TIMEOUT))
    except (TypeError, ValueError):
        pass        # a typo'd timeout falls back rather than failing the call

    pieces = queue.Queue()
    done = object()          # end of a good reply, as opposed to an exception
    abandoned = threading.Event()

    async def read():
        options = ClaudeAgentOptions(
            # No system prompt. Uniagent hands this file one flat string with
            # its instructions and history already in it, and every other
            # provider here sends that as a lone user message - so this does
            # too. None is also the quietest setting the SDK has: it leaves
            # Claude Code's own system prompt out entirely, where passing a
            # string still gets a short "you are a Claude agent" preamble
            # stuck in front of it. Measured, not assumed.
            system_prompt=None,
            # No tools, from three directions: no tool set, nothing
            # pre-approved, and a permission mode that refuses anything not
            # pre-approved rather than blocking on a prompt nobody can answer.
            tools=[],
            allowed_tools=[],
            permission_mode="dontAsk",
            # And no MCP servers or filesystem config either - without these
            # the CLI inherits whatever is set up in ~/.claude for the human
            # using this machine, which has nothing to do with Uniagent.
            mcp_servers={},
            strict_mcp_config=True,
            setting_sources=[],
            # One turn: Uniagent's loop does the iterating.
            max_turns=1,
            model=model,
            env={"API_TIMEOUT_MS": str(timeout * 1000),
                 "CLAUDE_CODE_MAX_RETRIES": "2"},
            # Named outright rather than left to the SDK's own PATH search,
            # which is the whole point of the CLAUDE_CLI_PATH box - see
            # _claude_cli().
            cli_path=cli,
            include_partial_messages=True,
        )
        # The client rather than the one-shot query(): leaving a reply
        # half-read is the normal case here, not the exceptional one, and only
        # this has a teardown that survives it. Breaking out of query()'s
        # generator instead leaves the CLI process unreaped and prints a
        # "generator is already running" traceback over the top of whatever
        # the agent was saying. `async with` closes the transport on the way
        # out, however the loop below ends.
        async with ClaudeSDKClient(options=options) as client:
            await client.query(_flatten(prompt))
            async for message in client.receive_response():
                if abandoned.is_set():
                    break  # consumer stopped reading - see the finally below
                if isinstance(message, StreamEvent):
                    # The raw Anthropic stream events, identical in shape to
                    # the ones _anthropic parses - the text arrives here, and
                    # so does usage: same message_start/message_delta shape,
                    # same real (not estimated) token counts.
                    event = message.event
                    etype = event.get("type")
                    if usage is not None:
                        if etype == "message_start":
                            u = event.get("message", {}).get("usage", {})
                            usage.update(_anthropic_usage(u))
                        elif etype == "message_delta":
                            out = event.get("usage", {}).get("output_tokens")
                            if out is not None:
                                usage["output_tokens"] = out
                    if etype == "content_block_delta":
                        text = event.get("delta", {}).get("text")
                        if text:
                            pieces.put(text)
                elif isinstance(message, AssistantMessage):
                    # Already streamed above, so this is only read for what
                    # the deltas don't carry: how it went, and whether the
                    # model somehow reached for a tool despite having none.
                    if message.error:
                        raise RuntimeError(CLAUDE_ERRORS.get(
                            message.error, "the Claude Code CLI failed: " + message.error))
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            print("\nprovider: claude-subscription tried to call the "
                                  "built-in tool " + block.name + " - it should have "
                                  "none; ignoring it.", file=sys.stderr)
                elif isinstance(message, ResultMessage) and message.is_error:
                    raise RuntimeError("the Claude Code CLI failed: " + "; ".join(
                        message.errors or [message.subtype]))

    def pump():
        """Run the SDK's async loop on its own thread and post what it says
        back to the generator. The SDK is async and everything that calls this
        file is not, so the two meet at this queue."""
        try:
            asyncio.run(read())
        except Exception as e:
            pieces.put(_claude_error(e))
        else:
            pieces.put(done)

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            try:
                piece = pieces.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("claude-subscription sent nothing for "
                                   + str(timeout) + "s - giving up")
            if piece is done:
                return
            if isinstance(piece, Exception):
                raise piece
            yield piece
    finally:
        # Reached when the consumer stops early too - _stream does exactly
        # that at the first complete tool call. Setting this ends the loop in
        # read(), which lets asyncio.run tear the CLI process down instead of
        # leaving it running out a reply nobody will read.
        abandoned.set()


def _claude_error(e):
    """An Agent SDK or CLI failure, restated as something actionable. The SDK
    doesn't type these apart - an expired login and a spent usage window are
    both just a failed process - so the text is what there is to go on."""
    from claude_agent_sdk import CLINotFoundError
    if isinstance(e, CLINotFoundError):
        return RuntimeError("claude-subscription needs the Claude Code CLI on PATH - "
                            "install it, then run: claude login")
    if isinstance(e, RuntimeError):
        return e  # already translated inside read()
    text = str(e)
    low = text.lower()
    for kind, meaning in CLAUDE_ERRORS.items():
        if kind.replace("_", " ") in low or kind in low:
            return RuntimeError(meaning)
    if "usage limit" in low or "rate limit" in low:
        return RuntimeError(CLAUDE_ERRORS["rate_limit"])
    if "login" in low or "unauthorized" in low or "authentication" in low:
        return RuntimeError(CLAUDE_ERRORS["authentication_failed"])
    return RuntimeError("claude-subscription failed: " + text[:300])


def _piper(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
           reasoning=None, on_call_delta=None, base_url=None, key=None, setup=None,
           provider_name=""):
    """Piper is a text-to-speech engine, not a chat model - it has no way to
    answer a prompt. This exists so a piper provider is a real, selectable
    provider (it shows up on the voice tab and can be picked as the speaker)
    without pretending it can hold a conversation. If it is ever chosen as a
    chat provider, this is the clear, immediate answer rather than a hang or a
    confusing empty reply."""
    raise RuntimeError("piper is a text-to-speech engine, not a chat model - "
                       "it can read replies out loud but cannot answer them. "
                       "Pick a chat provider on the providers tab.")


# --- Providers.
#
# There is no built-in provider, and nothing here creates one. Every provider
# is one object in .env's LLM_PROVIDERS - a name, a wire, a base URL, a key and
# an optional model list - and every one of them can be renamed, repointed,
# re-keyed or deleted from the settings page. The code below supplies WIRES
# (how to talk to a shape of API, most of them described in wires.json rather
# than written here) and nothing else; the list of providers is whatever the
# user has put in .env, up to and including nothing at all.
#
# They live in ONE .env variable as a JSON list, exactly like EMAIL_ACCOUNTS
# (tools/_email.py) and for the same reason: a flat NAME=value file cannot
# express "two of the same thing with different keys" without inventing a
# suffix convention, and two DeepSeek keys is the case this exists for. A name
# here is a first-class provider name everywhere else - settings, /model, cron
# jobs, subagents - so "deepseek-work" and "deepseek-personal" are simply two
# providers that happen to point at the same host with different credentials.
CUSTOM_VAR = "LLM_PROVIDERS"

# Which function drives a provider, chosen by its "wire" - the protocol its
# endpoint speaks, not the company running it.
#
# ALMOST EVERY WIRE IS DATA. wires.json describes them - endpoint, auth, the
# body template and the order of it - and _spec_wire() turns any such
# description into a provider function on demand. Adding OpenRouter, Groq,
# xAI, Together, an in-house gateway or a model server nobody has built yet is
# an entry in that file and no code at all.
#
# What is hardcoded below is the two wires that are not HTTP-and-JSON, and so
# have no request to describe: bedrock signs each call with AWS SigV4 through
# boto3, and claude-subscription drives the Claude Code CLI as a subprocess.
# They are wires like any other to everything outside this module - nameable,
# movable and deletable as providers - they simply cannot be expressed as a
# template, and wires.json marks them "native": true so the settings page
# explains that instead of offering an editor that could not work.
NATIVE_WIRES = {
    "bedrock": _bedrock,
    "claude-subscription": _claude_subscription,
    "piper": _piper,
}


def wire_call(wire):
    """The function that drives `wire`, or None if no such wire exists.

    Looked up per call rather than held in a dict built at import, because
    wires.json is editable while the server runs: a wire added on the settings
    page has to be callable on the very next turn, and one whose body template
    was just corrected has to send the corrected body."""
    if wire in NATIVE_WIRES:
        return NATIVE_WIRES[wire]
    if wire in wires.specs():
        return _spec_wire(wire)
    return None


def wire_names():
    """Every wire that exists - shipped, then yours. The settings page's wire
    dropdown, in the order it shows them."""
    return wires.names()


UNKNOWN_ICON = "/icons/unknown.svg"


def wire_default_url(wire):
    """Where `wire` points when a provider names no base URL of its own.

    LMSTUDIO_URL is honoured over the local wire's shipped default, because
    that variable is how someone with LM Studio on another machine has always
    pointed Uniagent at it, and a refactor must not quietly stop reading it."""
    if wire == "local" and os.environ.get("LMSTUDIO_URL"):
        return os.environ["LMSTUDIO_URL"]
    return str(wires.spec_for(wire).get("default_base_url") or "")


def wire_icon(wire):
    """The picture a provider on `wire` shows when it hasn't got one of its
    own, or None. Anything unlisted falls through to the question mark, which
    is the honest answer: the openai wire is spoken by a dozen companies and
    guessing a logo for it would be wrong more often than right."""
    return wires.spec_for(wire).get("icon") or None


def suggested_models(wire):
    """Model ids to offer for `wire` before its endpoint has been asked.

    Suggestions ONLY - never a whitelist. Nothing rejects a model for being
    absent: the settings page's model box is free text with these as
    autocomplete hints, and the model is passed to the provider exactly as
    typed, so a model released after this was written works with no change.

    ORDER MATTERS in one narrow way: the first entry is that wire's default -
    what cron falls back to when a job names a provider but no model, and what
    a blank model setting is filled in with.

    Keyed by WIRE and by nothing else. It used to be keyed by provider name,
    which quietly made a handful of names privileged: a provider called
    "openai" inherited the list and had its own shadowed by it, while renaming
    it to "openai-work" took the list away. A wire is a property no rename can
    touch - see floor_models()."""
    got = wires.spec_for(wire).get("suggested_models")
    return [str(m) for m in got] if isinstance(got, list) else []


# --- Per-wire setup forms.
#
# Every provider gets two boxes: a base URL and an API key. That is the whole
# shape of nearly every wire, so nearly every wire says nothing about it -
# openai, deepseek, anthropic and gemini are all "point it at a host, hand it a
# bearer token" and name no fields at all.
#
# A wire that authenticates some OTHER way needs different boxes and says so in
# its own entry in wires.json: which fields, what each is called, what it
# means, and crucially the ENVIRONMENT VARIABLE NAME each corresponds to.
#
# That name is doing real work. It is what the page labels the box with, so
# what you type into "AWS_REGION" is obviously the same thing as the AWS_REGION
# you might already export - and it is what provider_setting() falls back to
# reading out of the real environment when a provider leaves the box empty. So
# a machine with ~/.aws set up keeps working untouched, and a provider that
# fills the boxes in gets its own credentials instead.


def template_error():
    """Whichever wires file last failed to parse, as a sentence for the
    settings page, or None when both are fine."""
    return wires.error()


def template_for(wire):
    """`wire`'s whole spec - its form, and for a spec wire its request too."""
    return wires.spec_for(wire)


def template_fields(wire):
    """The extra boxes `wire` asks for, each settled into a full dict so
    neither the page nor provider_setting() has to guess at missing keys."""
    return wires.fields(wires.spec_for(wire))


def wants_key(wire):
    """Whether this wire's form has an API key box. A spec says key: false for
    the wires that don't authenticate with one; everything else does."""
    return template_for(wire).get("key", True) is not False


def base_url_label(wire):
    """What this wire calls its base-URL box, or None when it hasn't got one.

    The counterpart of wants_key. A spec says base_url: false for the wires
    that authenticate without a URL at all (bedrock signs with the AWS
    credentials on the machine, claude-subscription drives a CLI that owns its
    own login), or names the box when "base URL" would be the wrong words for
    it - a local model server's is a plain server address."""
    value = template_for(wire).get("base_url", True)
    if value is False:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else "base URL"


def wire_label(wire):
    """The human name for a wire - "Amazon Bedrock" rather than "bedrock" -
    falling back to the wire's own name when its spec names none."""
    return str(template_for(wire).get("label") or wire)


def wants_base_url(wire):
    """Whether this wire's form has a base URL box, and what to call it - True
    for the usual "base URL", a string to rename it, False for a wire with
    nowhere to point."""
    return template_for(wire).get("base_url", True)


def provider_setting(p, env, fallback_env=True):
    """One templated setting for provider `p`, by its variable name.

    Three places, in order: what the provider itself has saved, then the real
    environment variable of that name, then the template's default. The
    environment step is what keeps a machine that already exports AWS_REGION,
    or has ~/.aws set up, working with nothing typed into any box - and it is
    why the boxes are labelled with variable names rather than prose.

    fallback_env=False stops at the provider, for the callers that need to know
    whether THIS provider was configured rather than what it would end up
    using."""
    value = (p.get("config") or {}).get(env)
    if value:
        return str(value).strip()
    if fallback_env:
        value = os.environ.get(env)
        if value:
            return value.strip()
    for f in template_fields(p["wire"]):
        if f["env"] == env:
            return f["default"]
    return ""

# A provider name has to survive being a JSON key, a settings value, a chat
# folder's settings file and something typed after /model, so it is kept to
# the shape all of those already agree on.
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_custom_error = None      # set by custom_providers() when LLM_PROVIDERS is malformed


def _env_value(name):
    """The value of `name` in .env, or "" when it isn't set. _key() with the
    exception swapped for an empty string, for the variables whose absence is
    ordinary rather than something to fail a request over."""
    try:
        return _key(name)
    except RuntimeError:
        return ""


def new_provider_id():
    """A fresh provider id. Random rather than derived from anything on the
    provider, because every field on a provider is editable - a id derived
    from the name would change with the name, which is the whole thing this
    exists to avoid."""
    return "p-" + uuid.uuid4().hex[:12]


def _normalize_custom(entry):
    """One item out of LLM_PROVIDERS with every field settled, or None if it
    isn't usable at all (no name, a name that isn't a legal one, or a wire
    nothing here can speak). A bad entry is dropped rather than raised on: one
    hand-edited object must not take out every other provider in the list.

    "id" comes out empty when the object hasn't got one yet; custom_providers()
    stamps those and writes them back, so a caller never sees a provider
    without an id."""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip().lower()
    wire = str(entry.get("wire") or "openai").strip().lower()
    if not _NAME_OK.match(name) or wire_call(wire) is None:
        return None
    models = entry.get("models")
    return {
        # The provider's permanent identity, which nothing on the settings page
        # can change. A name is a label the user owns and retypes at will; this
        # is what anything Uniagent stores ABOUT a provider is filed under, so
        # a rename is a rename and not a quiet delete-and-recreate. See
        # _models_key().
        "id": str(entry.get("id") or "").strip(),
        "name": name,
        "wire": wire,
        "base_url": str(entry.get("base_url") or "").strip(),
        "key": str(entry.get("key") or "").strip(),
        # A path to a picture for this provider - a file on this machine, or
        # one of the bundled /icons/... ones. Empty means "whatever my wire
        # looks like", which is what nearly every provider wants; see
        # icon_for().
        "icon": str(entry.get("icon") or "").strip(),
        # This provider's answers to its wire's setup form, keyed by variable
        # name - {"AWS_REGION": "us-east-1", ...}. Empty for the wires whose
        # form is just a URL and a key, which is most of them. Read through
        # provider_setting(), never directly, so the environment fallback
        # applies. Values are kept as strings: they are all headed for an
        # environment variable or a boto3 argument.
        "config": {str(k): str(v) for k, v in (entry.get("config") or {}).items()
                   if isinstance(entry.get("config"), dict) and str(v).strip()},
        # A manual model list, for an endpoint with no catalogue to ask. Most
        # have one, so this is usually empty and known_models() fills itself
        # in from the live fetch instead.
        "models": ([str(m).strip() for m in models if str(m).strip()]
                   if isinstance(models, list) else []),
    }


def custom_providers():
    """Every provider, normalized, in the order they sit in .env. This IS the
    provider list - there is no built-in set behind it and nothing is ever
    added on the user's behalf.

    [] when LLM_PROVIDERS is absent, when it's an empty list, when it's
    malformed (see custom_error() for why), or when every entry is unusable.
    No providers is a legitimate state: a fresh install shows an empty
    providers tab with an add form, not a set of cards someone has to delete.

    Read from .env on every call, never cached: a provider added on the
    settings page has to work on the very next turn, in this process and in
    the cron watcher, with neither restarted - the same contract settings.py
    keeps."""
    global _custom_error
    raw = _env_value(CUSTOM_VAR)
    if not raw:
        _custom_error = None
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _custom_error = (CUSTOM_VAR + " in .env is not valid JSON (" + str(e)
                         + ") - no custom providers are loaded until it is fixed.")
        return []
    if not isinstance(data, list):
        _custom_error = CUSTOM_VAR + " in .env must be a JSON list of provider objects."
        return []
    _custom_error = None
    out, seen, ids, stamped = [], set(), set(), []
    for entry in data:
        p = _normalize_custom(entry)
        if p and p["name"] not in seen:
            seen.add(p["name"])
            # An id has to be unique as well as present - a hand-copied object
            # would otherwise share the one it was copied from, and the two
            # would read each other's model lists.
            if not p["id"] or p["id"] in ids:
                p["id"] = new_provider_id()
                stamped.append((entry, p["id"]))
            ids.add(p["id"])
            out.append(p)
    if stamped:
        _backfill_ids(data, stamped)
    return out


def _backfill_ids(raw, stamped):
    """Write ids onto the objects in .env that arrived without one - a list
    written before ids existed, or hand-edited since. Without this the id a
    caller just got would be a different random one next read, and everything
    filed under it (see _models_key) would be orphaned every time.

    Edits the RAW list rather than saving the normalized one, so an entry this
    code can't read - a typo'd wire, say - keeps sitting there with its key
    intact instead of being quietly dropped on the way past."""
    for entry, pid in stamped:
        if isinstance(entry, dict):
            entry["id"] = pid
    try:
        set_env(CUSTOM_VAR, json.dumps(raw, separators=(",", ":")))
    except Exception:
        pass        # a read-only .env must not make reading providers fail


def custom_error():
    """Whatever is wrong with LLM_PROVIDERS, in one sentence, or None. Without
    this a malformed variable reads on the settings page as "no providers
    added yet", with no hint that several are sitting in .env unread."""
    custom_providers()
    return _custom_error


def custom_provider(name):
    """The provider object called `name`, or None if there isn't one."""
    for p in custom_providers():
        if p["name"] == name:
            return p
    return None


def _models_key(name):
    """What provider `name`'s learned models are filed under in
    models_custom.json, and what its live catalogue is cached under.

    The provider's id when there is a provider by that name - which is the
    point: rename the provider and its models come with it, because the name
    was never what they were filed under.

    Falls back to the name itself for a name with no provider behind it. That
    keeps two things working: entries written before ids existed, and entries
    left behind by a provider that has since been deleted, which come back if
    a provider of that name is ever created again."""
    p = custom_provider(name)
    return p["id"] if p else (name or "").strip().lower()


def wire_of(name):
    """What provider `name` actually IS - "deepseek", "openai", "bedrock" -
    as opposed to what it is called, which is a label someone typed into the
    settings page and can retype tomorrow.

    This is the stable thing to file a model's behaviour under. A model's
    tokenizer and its context window belong to the service serving it, and
    that doesn't change when the card in front of it gets renamed. Ids are
    stable too, but an id is unreadable and is per-card: two cards pointing at
    the same service would share nothing, when in truth they serve the same
    models with the same windows.

    Falls back to the name for a name with no provider behind it - a deleted
    or renamed provider's leftovers are filed under the old name, and treating
    that name as its own wire keeps them matching themselves rather than
    silently answering for some unrelated service."""
    p = custom_provider(name)
    return p["wire"] if p else (name or "").strip().lower()


def icon_for(p):
    """The path to the picture provider `p` should show: its own if it named
    one, otherwise its wire's, otherwise the question mark.

    A path, never image data - the page turns it into a URL it can load (see
    server._icon_url), because a picture living on this machine can't be
    reached from an https: page without coming back through the server."""
    return p.get("icon") or wire_icon(p["wire"]) or UNKNOWN_ICON


def custom_base_url(p):
    """Where `p` actually points - its own base URL, or its wire's default
    host when it named none."""
    return p["base_url"] or wire_default_url(p["wire"])


def custom_key(p):
    """The key `p` authenticates with - its own, and nothing else's.

    Note what this does NOT do: fall back to OPENAI_API_KEY and friends. Those
    variables still exist and are still read, but by things that are not
    provider calls at all - Whisper in voice_input.py, the vision calls in
    view_image.py and screenshot_tool.py, the token counter in tokens.py. A
    provider called "openai" is now just a provider you can rename or delete,
    so tying it to a variable those other callers depend on would mean deleting
    a card could break the microphone."""
    return p["key"]


def save_custom_providers(entries):
    """Write the whole provider list back to .env. Only the id and the four
    editable fields of each are kept, so nothing a hand edit left behind
    survives a save through here.

    The id is carried, never regenerated: it is the one field on a provider
    that must outlive every edit made to it."""
    clean = []
    for p in entries:
        clean.append({"id": p.get("id") or new_provider_id(),
                      "name": p["name"], "wire": p["wire"],
                      "base_url": p["base_url"], "key": p["key"],
                      "icon": p.get("icon", ""), "config": p.get("config") or {},
                      "models": p["models"]})
    set_env(CUSTOM_VAR, json.dumps(clean, separators=(",", ":")) if clean else "")


def save_custom_provider(name, wire="openai", base_url="", key=None, models=None,
                         rename_from=None, icon=None, config=None):
    """Add a provider object, or update the one already called `name`.

    `key=None` means "leave whatever key is saved alone", so a caller that
    isn't editing the key doesn't have to send it back to keep it. Pass "" to
    genuinely clear it. `icon=None` works the same way, and "" means "go back
    to my wire's icon" rather than "no picture".

    `rename_from`, if given, is the name this object had before: the entry is
    found under that and rewritten under the new one, keeping its place in the
    list. Raises ValueError with a sentence worth showing on anything the
    settings page should refuse.

    Nothing here is privileged. Any provider can be renamed, repointed, given
    a different wire or deleted - the only rules are that a name is a legal
    name, that it doesn't collide with another provider's, and that the wire
    is one this code can actually speak."""
    name = (name or "").strip().lower()
    wire = (wire or "openai").strip().lower()
    if not _NAME_OK.match(name):
        raise ValueError("a provider name must start with a letter or digit and use only "
                         "lowercase letters, digits, dots, dashes and underscores - "
                         "so 'deepseek-work', not " + repr(name))
    if wire_call(wire) is None:
        raise ValueError("unknown wire " + repr(wire) + " - it must be one of: "
                         + ", ".join(wire_names()))
    entries = custom_providers()
    old = (rename_from or name).strip().lower()
    if name != old and any(p["name"] == name for p in entries):
        raise ValueError("there is already a provider called " + name + ".")
    found = False
    renamed_id = None
    for p in entries:
        if p["name"] == old:
            # Before the name is overwritten below: anything this provider's
            # models still have filed under the OLD name has to be brought onto
            # its id, or the rename hides it (see _fold_models_onto_id).
            if name != old:
                renamed_id = p.get("id")
            p["name"] = name
            p["wire"] = wire
            p["base_url"] = (base_url or "").strip()
            if key is not None:
                p["key"] = key.strip()
            if icon is not None:
                p["icon"] = icon.strip()
            if config is not None:
                p["config"] = _clean_config(config)
            if models is not None:
                p["models"] = [m.strip() for m in models if m.strip()]
            found = True
            break
    if not found:
        entries.append({"id": new_provider_id(), "name": name, "wire": wire,
                        "base_url": (base_url or "").strip(),
                        "key": (key or "").strip(),
                        "icon": (icon or "").strip(),
                        "config": _clean_config(config),
                        "models": [m.strip() for m in (models or []) if m.strip()]})
    save_custom_providers(entries)
    if renamed_id:
        _fold_models_onto_id(old, renamed_id)
    return custom_providers()


def _fold_models_onto_id(old_name, provider_id):
    """Copy what models_custom.json still has filed under a provider's OLD
    name onto its id, so a rename doesn't strand it.

    A provider's id follows it through a rename and its models are filed under
    that id - that is the whole point of ids (see _models_key). But entries
    written before ids existed are filed under the bare NAME, and _custom_models
    finds those by looking under the provider's CURRENT name. So renaming
    "deepseek" to "dseek" put that config somewhere nothing would look again:
    every chat on the renamed provider lost its models' context_window and drew
    "/ ?" for a window sitting in plain sight in the file.

    Copied, not moved. A chat that pinned the old provider NAME (chat settings
    store the name, not the id) still resolves through it, and this is not the
    place to break those - the stale key costs a few lines in a file nobody
    reads by hand.

    Per setting, and the id half wins: the id is the half being maintained, so
    this only ever fills in what it doesn't already say."""
    old = (old_name or "").strip().lower()
    if not old or not provider_id or old == provider_id:
        return
    data = _custom()
    stale = data.get(old)
    if not isinstance(stale, dict):
        return
    current = data.get(provider_id)
    current = dict(current) if isinstance(current, dict) else {}
    changed = False
    for model, cfg in stale.items():
        have = current.get(model)
        have = have if isinstance(have, dict) else {}
        merged = {**(cfg if isinstance(cfg, dict) else {}), **have}
        if merged != current.get(model):
            current[model] = merged
            changed = True
    if changed:
        data[provider_id] = current
        CUSTOM_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _clean_config(config):
    """A setup form's answers, ready to store: strings, trimmed, and blanks
    dropped entirely rather than saved as "".

    Dropping them matters. An empty box means "I didn't answer this", and
    provider_setting() reads an unanswered field out of the real environment -
    so storing "" would turn leaving a box empty into actively overriding
    AWS_REGION with nothing."""
    if not isinstance(config, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in config.items()
            if str(k).strip() and str(v).strip()}


def remove_custom_provider(name):
    """Forget the provider object called `name`. Anything still pointed at it
    (a setting, a cron job) falls back to its default the next time it is
    read - settings.py refuses a provider that isn't available()."""
    entries = [p for p in custom_providers() if p["name"] != (name or "").strip().lower()]
    save_custom_providers(entries)
    return entries


def resolved_setup(p):
    """{variable name: value} for every field on `p`'s wire's setup form, each
    resolved through provider_setting() - so the wire function receives what it
    should actually use and never has to know that the value might have come
    from the provider, the environment or a default.

    {} for the wires with no form, which is most of them."""
    return {f["env"]: provider_setting(p, f["env"]) for f in template_fields(p["wire"])}


def _custom_call(p):
    """`p`'s provider function: its wire's, with the base URL, the key and
    this provider's own setup answers baked in. The key goes over as a string
    even when it is empty, which is what tells the wire to send no auth header
    rather than reach for a key in .env that this provider never claimed.

    `setup` goes to every wire, not to a list of wires that were known to
    accept it. It used to be the latter, and that was a standing trap: a wire
    that grew a field had to be added to a frozenset in this file too, or its
    boxes silently did nothing. Now a wire declares its fields in wires.json
    and they arrive, which is the whole of adding one - and $setting:NAME in a
    body template is how a spec wire reads them back.

    `provider_name` goes over for the same reason, and is the one thing here
    that is about the CARD rather than about the endpoint: which endpoint a
    router should send a model to is recorded per provider and model (see
    set_model_route), and the wire cannot look that up without knowing whose
    request it is building."""
    return functools.partial(wire_call(p["wire"]),
                             base_url=custom_base_url(p), key=custom_key(p),
                             setup=resolved_setup(p), provider_name=p["name"])


def providers():
    """Every provider that exists right now - one {"name", "call"} dict each,
    which is all stream_response needs of a provider. Recomputed per call for
    the same reason custom_providers() is.

    Every one of them is a provider object, in the order they sit in .env -
    which is the order the settings dropdowns list them in, and which the tab
    can reorder simply by being the thing that writes the file. What a card
    says is what the request uses; there is no second, privileged list behind
    it."""
    return [{"name": p["name"], "call": _custom_call(p), "custom": p}
            for p in custom_providers()]


def provider_names():
    """Every provider name that exists, in .env order."""
    return [p["name"] for p in providers()]


def wire_for(name):
    """Which wire provider `name` speaks - for anything that has to know the
    SHAPE of a provider's API rather than just its name (see
    tool_processor.shape_for). Falls back to the name itself for a provider
    that no longer exists, which is what the shipped shape tables are keyed
    by."""
    p = custom_provider(name)
    return p["wire"] if p else name

def find_model(name):
    """The provider that owns model `name`, or None if nothing has it. Used by
    /model so you can name a model without naming its provider too."""
    for provider_name, models in known_models().items():
        if name in models:
            return provider_name
    return None


# --- Live + learned model lists. The hardcoded MODELS above go stale the day
# they're written, so they are only the floor: each provider that has a
# list-models endpoint is asked for its real catalogue (cached briefly), and
# models proven to work by /model's test call are remembered in
# models_custom.json forever. known_models() merges all three. ---

CUSTOM_FILE = Path(__file__).parent.parent / "models_custom.json"

_live_cache = {}          # provider id -> (fetched_at, [models])
_LIVE_TTL = 600           # seconds; provider catalogues don't churn faster


def _filter_chat(ids):
    """Drop the obviously-not-chat models an unfiltered catalogue includes
    (embeddings, audio, image...). Err on keeping: an unfamiliar id is far
    more likely a new chat model than a new embedding model."""
    NOISE = ("embed", "whisper", "tts", "dall-e", "audio", "moderation",
             "realtime", "transcribe", "image", "davinci", "babbage")
    return [i for i in ids if not any(n in i for n in NOISE)]


def _fetch_live_custom(p):
    """A provider's own catalogue, asked for the way its wire's spec says to
    ask - the endpoint, the auth and where the ids sit in the reply are all in
    wires.json (see wires.models_request/parse_models).

    Nearly every OpenAI-compatible endpoint serves /models, which is what makes
    adding a provider a matter of a URL and a key rather than typing out every
    model id by hand. A wire whose spec names no catalogue endpoint, or one
    whose endpoint answers with something unexpected, simply yields nothing:
    the caller treats that as "no live list" and falls back to the wire's
    suggestions and the provider's own models, which is a working provider with
    a shorter dropdown rather than an error."""
    spec = wires.spec_for(p["wire"])
    ctx = {"model": "", "key": custom_key(p), "base_url": custom_base_url(p),
           "setting": resolved_setup(p)}
    url, headers = wires.models_request(spec, ctx)
    if not url:
        return []
    r = requests.get(url, headers=headers, timeout=5)
    _check(r)
    return wires.parse_models(spec, r.json())


def _fetch_live(name):
    """This provider's own list of its models, straight from its API. Raises
    on any failure - the caller treats that as 'no live list', not an error.

    Keyed off the provider's WIRE, not its name: a provider is whatever its
    card says it is, so "which endpoint do I ask for a catalogue" follows the
    shape it speaks and the host it points at, both of which are on the
    object."""
    p = custom_provider(name)
    if not p:
        raise RuntimeError("no provider called " + name)
    if p["wire"] == "bedrock":
        # The same credentials the actual calls use - a provider pointed at its
        # own AWS account must not list the machine account's models.
        client = _bedrock_client("bedrock", p["base_url"], resolved_setup(p))
        profiles = client.list_inference_profiles()["inferenceProfileSummaries"]
        return sorted(x["inferenceProfileId"] for x in profiles)
    if p["wire"] == "claude-subscription":
        # Nothing to ask - the CLI serves whatever its login is entitled to,
        # with no catalogue endpoint. known_models() falls back to the
        # suggestion table and to the object's own list.
        raise RuntimeError("claude-subscription has no model catalogue to fetch")
    return _fetch_live_custom(p)


def _custom():
    """{provider id: {model: {config...}}} - models proven to work by /model's
    test call, each with whatever config it's been given (context_window,
    tool_syntax, injection - all optional, {} is a fine value). Also holds
    one entry that isn't a provider: "default", the injection list any model
    without one of its own falls back to - see default_injection().

    Keyed by provider ID, not name - see _models_key(). Older files are keyed
    by name and still read, because _models_key() falls back to the name."""
    try:
        data = json.loads(CUSTOM_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _custom_models(name):
    """{model: {config...}} recorded for provider `name` - what /model has
    proven works on it, with whatever config each model has been given.

    Reads BOTH keys and merges them: the provider's id, which is where
    everything is written from now on, over anything still filed under the
    bare name. The name-keyed half is what a file written before ids existed
    looks like, and what a deleted provider leaves behind - so recreating a
    provider under a name that was used before picks its old models back up
    instead of starting blank. The id wins on any model both mention, since
    that is the half being maintained.

    The merge is per SETTING, not per model, and that distinction is the whole
    point of it. remember_model() writes {} for a model it has just proven
    works, which is right - it has nothing to say about that model's config -
    but a model taking the id half wholesale meant that empty placeholder
    REPLACED everything recorded under the old name. That is how a model with
    a context_window of 1,000,000 sitting right there in the file came back as
    None, and the token bar read "/ ?" for a window it plainly knew. Setting by
    setting, the id half still wins wherever it actually says something, and
    what only the name half knows survives."""
    data = _custom()
    out = {}
    for key in (name, _models_key(name)):
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        for model, cfg in entry.items():
            out[model] = {**out.get(model, {}),
                          **(cfg if isinstance(cfg, dict) else {})}
    return out


def remember_model(name, model):
    """Record that `model` was tested and worked on provider `name`, so it
    shows up in every list from now on without anyone editing code. Leaves an
    existing model's config (context_window, tool_syntax, injection) alone if
    it already has one - only ever adds the empty placeholder for a genuinely
    new model, never overwrites."""
    key = _models_key(name)
    data = _custom()
    models = data.setdefault(key, {})
    if model not in models:
        models[model] = {}
        CUSTOM_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def note_pick(name, model):
    """Stamp `model` on provider `name` as the one just CHOSEN, so it can float
    to the top of the picker next time - see recent_models().

    A pick, not a use. Every list, dropdown and command that switches a model
    calls this; a turn merely running on a model does not, and that is
    deliberate. Recency is meant to answer "what have I been reaching for
    lately", and a cron job quietly running the same model at 4am all week
    would otherwise own the top of the list without anyone having chosen it
    once.

    Creates the entry if this is a model nothing has recorded yet, and touches
    nothing else about it - remember_model()'s rule, for its reason: what a
    model's config says was set deliberately, and picking it is not an edit."""
    key = _models_key(name)
    data = _custom()
    models = data.setdefault(key, {})
    cfg = models.get(model)
    models[model] = {**(cfg if isinstance(cfg, dict) else {}), "used": int(time.time())}
    try:
        CUSTOM_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass        # a pick that could not be remembered is not a failed pick


def recent_models(limit=5, names=None):
    """The models most recently picked, newest first, as [{"provider",
    "model"}] - the head of every model list on the page.

    Only providers that still exist are offered, and each pair only once: a
    model recorded under a provider's old name and again under its id is one
    model, and a provider that has been deleted is not somewhere you can go
    back to. Nothing is invented for a fresh install - no picks, no list, and
    the picker simply opens on the full catalogue as it always did."""
    usable = list(names if names is not None else available())
    seen = {}
    for name in usable:
        for model, cfg in _custom_models(name).items():
            when = cfg.get("used") if isinstance(cfg, dict) else None
            if isinstance(when, int) and when > 0:
                key = (name, model)
                seen[key] = max(when, seen.get(key, 0))
    newest = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
    return [{"provider": n, "model": m} for (n, m), _ in newest[:max(0, limit)]]


# --- which endpoint of a model to run on ------------------------------------
#
# A router serves one model id from several companies at once (see the "routes"
# block in wires.json). Which of them a model runs on is recorded next to that
# model's other config, so it is chosen once and holds everywhere that pair is
# used - this chat, a subagent, a cron job at 4am - rather than being a fourth
# thing to remember to set per place. "" is the default and means "let the
# router choose", which is what every model does until somebody says otherwise.

# One model's endpoint list, cached for as long as the model catalogues are:
# (provider name, model) -> (fetched_at, [endpoints]). It is a round trip to
# the router, and the answer changes about as often as a price does.
_routes_cache = {}


def routes_spec_for(name):
    """The "routes" block that applies to provider `name`, or {}.

    Its wire's, normally. Falling back to OpenRouter's for a provider POINTED
    AT OPENROUTER on some other wire, which is the ordinary way people set it
    up: the openai wire speaks openrouter.ai perfectly well, so that is what
    most openrouter cards are on, and refusing to route them on a technicality
    would be refusing the one provider this feature exists for. What a card
    points at is what it is."""
    p = custom_provider(name)
    if not p:
        return {}
    block = wires.routes_spec(wires.spec_for(p["wire"]))
    if block:
        return block
    host = urlparse(custom_base_url(p) or "").hostname or ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return wires.routes_spec(wires.spec_for("openrouter"))
    return {}


def routes_supported(name):
    """Whether provider `name` can be pointed at a particular endpoint at all -
    what the picker asks before offering an endpoint box for it."""
    return bool(routes_spec_for(name))


def model_route(name, model):
    """The endpoint provider `name` sends `model` to, or "" for the router's
    own choice."""
    value = model_config(name, model).get("route")
    return value.strip() if isinstance(value, str) else ""


def set_model_route(name, model, route):
    """Record which endpoint `model` runs on, or clear it with "".

    Written the same way and to the same place as everything else a model
    carries, so a route survives a rename (it is filed under the provider's
    id) and reaches every chat, subagent and job on that pair at once."""
    route = (route or "").strip()
    key = _models_key(name)
    data = _custom()
    models = data.setdefault(key, {})
    cfg = models.get(model)
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    if route:
        cfg["route"] = route
    else:
        cfg.pop("route", None)
    models[model] = cfg
    CUSTOM_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return route


def model_routes(names=None):
    """{provider: {model: endpoint}} for every pair that has been given one -
    read straight off models_custom.json, with nothing asked of anybody.

    The page draws the chosen endpoint beside a model everywhere it lists one,
    so this rides along with the settings rather than being a request per row.
    Providers with no routed models are left out entirely, which on almost
    every install is all of them."""
    out = {}
    for name in (names if names is not None else available()):
        chosen = {model: cfg["route"]
                  for model, cfg in _custom_models(name).items()
                  if isinstance(cfg, dict) and isinstance(cfg.get("route"), str)
                  and cfg["route"].strip()}
        if chosen:
            out[name] = chosen
    return out


def route_options(name, model):
    """Every endpoint provider `name` serves `model` from, as
    [{"id", "label", "note"}] - the suggestions under the endpoint box.

    [] for a provider that does not route, for a model the router has never
    heard of, and for a router that cannot be reached right now. All three are
    the same thing to the caller: no suggestions to offer, and a box that is
    still free text - the slug you want is on the model's page on the router's
    own site, and typing it in has to keep working when this fetch does not."""
    block = routes_spec_for(name)
    p = custom_provider(name)
    if not block or not p or not model:
        return []
    now = time.time()
    cached = _routes_cache.get((name, model))
    if cached and now - cached[0] <= _LIVE_TTL:
        return cached[1]
    spec = {**wires.spec_for(p["wire"]), "routes": block}
    ctx = {"model": model, "key": custom_key(p), "base_url": custom_base_url(p),
           "setting": resolved_setup(p)}
    found = []
    try:
        url, headers = wires.routes_request(spec, ctx)
        if url:
            r = requests.get(url, headers=headers, timeout=8)
            _check(r)
            found = wires.parse_routes(spec, r.json())
    except Exception:
        found = []
    _routes_cache[(name, model)] = (now, found)
    return found


def model_config(name, model):
    """This model's own config - context_window, injection - from
    models_custom.json, or {} if it's never been recorded there. Never raises:
    a missing or malformed entry just means "nothing configured".

    A leftover "tool_syntax" is ignored: every turn is native now, so there is
    no syntax left to pick. The key is harmless where it still sits in the
    file."""
    cfg = _custom_models(name).get(model, {})
    return cfg if isinstance(cfg, dict) else {}


def default_injection():
    """The injection list a model falls back to when it has none of its own -
    models_custom.json's top-level "default" entry. [] if that's missing too,
    which main.py treats as "nothing to inject", not an error."""
    default = _custom().get("default", {})
    items = default.get("injection", []) if isinstance(default, dict) else []
    return items if isinstance(items, list) else []


def context_window(name, model):
    """This model's max input context in tokens, or None if it isn't known -
    the UI shows "tokens / ?" for that case rather than a guessed number.

    Asked of the provider first, then of its WIRE (_window_on_wire), then of
    the server itself (_served_window). The middle one is what makes this
    survive the settings page. A provider's name is a label a person typed and
    can change at any moment; its wire is what it actually is. Rename
    "deepseek" to "test" and the card is still a deepseek endpoint serving
    deepseek-v4-flash, with the same tokenizer and the same million-token
    window - so the window has to be found by what the provider IS, not by
    what it is currently called.

    The last one is only for a local server, and it is the only one of the
    three that can be right about a local model: the window there is not a
    property of the model at all, it is whatever number was on the slider when
    somebody loaded it, and it changes every time they load it again."""
    value = model_config(name, model).get("context_window")
    if not isinstance(value, int):
        value = _window_on_wire(wire_of(name), model)
    if not isinstance(value, int):
        value = _served_window(name, model)
    return value if isinstance(value, int) else None


def _window_on_wire(wire, model):
    """The context window recorded for `model` by anything speaking `wire`.
    None if nobody has recorded one, or if the entries on file disagree.

    Scoped to the wire, not the whole file, because that is the honest limit
    of what one entry can say about another. Two cards on the same wire are
    two endpoints of the same kind of service, so a window recorded on one is
    a real answer for the other; two cards on different wires share nothing
    but a string, and "gemma-3-4b" served by LM Studio at 8k has no bearing on
    the same name served anywhere else.

    Every key that wire could be filed under is read: the id of each provider
    speaking it (where everything is written now), each of their names, and
    the wire's own name - which is the legacy case and by far the most common
    one, since a card is usually first called after the thing it points at.
    "deepseek" as a leftover key is exactly that, and it keeps answering for
    the card that has since been renamed to "test".

    The provider's own entry is always asked first (see context_window), so
    this can only fill a blank, never overrule a window set deliberately.

    Disagreement is answered with the SMALLEST, not a vote and not the
    largest. This number is the ceiling a bar is drawn against and the point a
    conversation gets compacted at, so guessing high means sailing past a
    limit that was there all along, and guessing low means being warned early.
    Only one of those loses a turn."""
    data = _custom()
    keys = {wire}
    for p in custom_providers():
        if p["wire"] == wire:
            keys.add(p["id"])
            keys.add(p["name"])
    found = set()
    for key in keys:
        entry = data.get(key)
        cfg = entry.get(model) if isinstance(entry, dict) else None
        window = cfg.get("context_window") if isinstance(cfg, dict) else None
        if isinstance(window, int):
            found.add(window)
    return min(found) if found else None


# A local server's model windows, keyed by the URL asked, as
# (fetched_at, {model id: window}). Short-lived on purpose: context_window()
# is called on every status poll, so this must not become a request per poll,
# and the number itself goes stale the moment somebody reloads a model in
# LM Studio with the slider somewhere else.
_served_cache = {}
_SERVED_TTL = 30

# This machine, or a machine on the same home network - the two places a model
# server can be. Hostname-based rather than resolved, because the question is
# only "is it worth one cheap request to ask this thing a local-server
# question", and a DNS lookup per status poll to answer it would cost more
# than the request would.
_LAN_HOST = re.compile(r"""^(
      localhost | .*\.local |
      127\.\d+\.\d+\.\d+ | 0\.0\.0\.0 | \[?::1\]? |
      10\.\d+\.\d+\.\d+ |
      192\.168\.\d+\.\d+ |
      172\.(1[6-9]|2\d|3[01])\.\d+\.\d+
    )$""", re.X | re.I)


def _on_this_network(url):
    """Whether `url` points at this machine or this LAN - i.e. at something
    somebody is running themselves, rather than at a service on the internet."""
    host = (url or "").split("//")[-1].split("/")[0].rsplit("@", 1)[-1].strip()
    if host.startswith("["):          # [::1]:1234 - an IPv6 literal owns the
        host = host.split("]")[0] + "]"   # colons, so only the brackets end it
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return bool(_LAN_HOST.match(host))


def _served_window(name, model):
    """What the local server says it loaded `model` with, or None.

    This exists because a local model's context window is not knowable from
    the outside. "nanbeige4.2-3b" can be 8k on this machine and 128k on the
    next one - same name, same weights, different slider - so there is nothing
    to hardcode and nothing another provider's entry can tell us. The number
    has to come from the server holding the model.

    LM Studio publishes it on its own REST API, alongside the OpenAI-shaped
    one: /api/v0/models carries max_context_length (what the file supports)
    and loaded_context_length (what it was actually loaded with). The loaded
    figure is the real ceiling and is preferred; max is the fallback for a
    model sitting on disk that nothing has loaded yet.

    Only asked of a provider pointed at this machine or this network, and NOT
    of the "local" wire alone - a card for LM Studio is just as often the
    openai wire with localhost typed into its URL box, because that is the
    same protocol and it works. What decides is where the card points, which
    is also what keeps a doomed request off a paid endpoint on every status
    poll. Never raises: no server, an older LM Studio, Ollama or vLLM in its
    place - all of them mean "not known", which is what context_window()
    already handles by drawing a "?"."""
    p = custom_provider(name)
    if not p or not _on_this_network(custom_base_url(p)):
        return None
    # The /v1 on the end is the OpenAI-compatible API; the native one is a
    # sibling of it, not a child.
    root = custom_base_url(p).rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    hit = _served_cache.get(root)
    if not hit or time.time() - hit[0] > _SERVED_TTL:
        windows = {}
        try:
            r = requests.get(root + "/api/v0/models", timeout=3)
            if r.status_code == 200:
                for entry in r.json().get("data") or []:
                    window = entry.get("loaded_context_length") \
                        or entry.get("max_context_length")
                    if isinstance(window, int) and entry.get("id"):
                        windows[entry["id"]] = window
        except Exception:
            pass  # not LM Studio, or not running - see the docstring
        # Cached even when empty, so a server that will never answer this is
        # asked once every TTL rather than on every poll.
        hit = (time.time(), windows)
        _served_cache[root] = hit
    return hit[1].get(model)


def floor_models(name):
    """Provider `name`'s starting model list. The head entry is that
    provider's default model, so order matters.

    Its own "models" first - a list typed on its card is the user saying what
    this endpoint serves, and it outranks anything guessed here. Then the
    suggested_models() for its wire, but only when it still points at
    that wire's own host: the openai wire is what OpenRouter, Groq, vLLM and
    LM Studio all speak, and offering gpt-4o to a laptop running LM Studio
    would be noise. Those endpoints have live catalogues, which known_models()
    asks for and which are the real answer for them."""
    p = custom_provider(name)
    if not p:
        return []
    out = list(p["models"])
    if not p["base_url"] or p["base_url"] == wire_default_url(p["wire"]):
        for m in suggested_models(p["wire"]):
            if m not in out:
                out.append(m)
    return out


def default_model(name):
    """The model provider `name` runs when nothing names one - its floor's
    head entry, or failing that the first model actually known for it, so a
    provider added with no manual model list still resolves to something real
    off its live catalogue instead of to "". "" only if the provider has no
    models at all, which settings.py treats as unset.

    The catalogue fallback is filtered for chat models first. An endpoint that
    serves embeddings alongside chat models can easily list one of those
    first - LM Studio does - and silently defaulting to an embedding model
    means every message fails on an endpoint that is working perfectly."""
    floor = floor_models(name)
    if floor:
        return floor[0]
    known = known_models([name]).get(name, [])
    return next(iter(_filter_chat(known)), "") or next(iter(known), "")


def known_models(names=None):
    """{provider: [models]} for `names` (default: every provider that exists,
    custom ones included): the floor first (its head entry stays the default),
    then any /model-tested ones, then the provider's live catalogue. Deduped
    in that order. A provider whose live fetch fails (no key, network down, no
    such endpoint) just contributes its floor - never an error."""
    merged = {}
    for name in (names if names is not None else provider_names()):
        key = _models_key(name)
        seen = floor_models(name)
        for extra in _custom_models(name):
            if extra not in seen:
                seen.append(extra)
        now = time.time()
        cached = _live_cache.get(key)
        if cached is None or now - cached[0] > _LIVE_TTL:
            try:
                _live_cache[key] = (now, _fetch_live(name))
            except Exception:
                _live_cache[key] = (now, [])  # remember the failure too - no hammering
        for extra in _live_cache[key][1]:
            if extra not in seen:
                seen.append(extra)
        merged[name] = seen
    return merged


def test_model(name, model):
    """Actually send `model` one tiny request on provider `name`. Returns
    None when it worked, else the provider's own error text - the only
    authority on whether a model id is real."""
    try:
        get_response("Reply with only: OK", provider=name, model=model)
        return None
    except Exception as e:
        return str(e)


# --- speech to text ---------------------------------------------------------
#
# Transcription goes through the same providers chat does. A provider is a
# host, a key and a wire; asking it for words instead of an answer is a
# different endpoint on that same host, so there is nothing here that needs its
# own credentials - which is the point. Pick a provider on the voice tab and
# the microphone follows it everywhere: the web page's button, the hold-to-talk
# key on this machine, and the terminal.
#
# Which wires can do it at all:
#   openai / deepseek / local  POST /audio/transcriptions, OpenAI's own shape,
#                              which is what every third-party endpoint serving
#                              speech models copies (Groq, LM Studio, vLLM).
#   gemini                     no transcription endpoint - its chat models take
#                              audio inline instead and are asked, in words, to
#                              write down what they hear.
#   anthropic / bedrock /      nothing. Claude takes no audio at all, and
#   claude-subscription        Bedrock's transcriber is a separate AWS service
#                              with its own S3-shaped workflow.
STT_WIRES = frozenset({"openai", "deepseek", "local", "gemini"})

# What a provider is offered before its catalogue has been read - and after,
# for the endpoints that don't list their speech models. Suggestions, never a
# whitelist: the box on the voice tab is free text, so a model newer than this
# still works with no code change.
#
# Only used for a provider actually pointed at the wire's own host. An openai
# card pointed somewhere else is somebody else's endpoint (Groq, a local
# server) and suggesting OpenAI's model ids for it would be wrong every time -
# those endpoints list their own, which is what the live fetch below is for.
STT_FLOOR = {
    "openai": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro"],
}

# What a speech model looks like in a catalogue of a hundred chat models.
# Matched on the id because that is all a /models list gives us - no endpoint
# here says what a model is FOR.
_STT_WORDS = ("whisper", "transcribe", "stt", "speech-to-text", "voxtral", "scribe")

# A clip can be a minute of speech over a phone connection, and the model has
# to hear all of it before it says anything - so this is generous next to a
# chat request.
STT_TIMEOUT = 120

# Gemini needs to be told what it is being handed. Keyed by the extension of
# the filename the clip arrives under, which is why voice_input names its clips
# after their real container rather than always saying .wav.
STT_MIME = {"wav": "audio/wav", "webm": "audio/webm", "ogg": "audio/ogg",
            "mp3": "audio/mpeg", "mpga": "audio/mpeg", "mpeg": "audio/mpeg",
            "mp4": "audio/mp4", "m4a": "audio/mp4", "flac": "audio/flac"}

STT_PROMPT = ("Transcribe the speech in this audio exactly, word for word. "
              "Reply with the transcription and nothing else - no quotes, no "
              "preamble, no description of the audio. If there is no speech in "
              "it, reply with nothing at all.")

_stt_cache = {}           # provider id -> (fetched_at, [models])


def _looks_stt(model_id):
    return any(word in model_id.lower() for word in _STT_WORDS)


def _at_wire_host(p):
    """Whether `p` still points where its wire points by default - i.e. it is
    the company the wire is named after, not a third party speaking the same
    protocol."""
    return not p["base_url"] or p["base_url"].rstrip("/") == \
        wire_default_url(p["wire"]).rstrip("/")


def _fetch_stt(p):
    """Provider `p`'s own speech models, off its catalogue. Raises on any
    failure, exactly like _fetch_live - the caller treats that as "no live
    list" rather than as an error, because a provider that can't be asked
    still has its floor and still takes a typed model id."""
    if p["wire"] == "gemini":
        # Gemini has no speech catalogue to filter - transcription is done by
        # the ordinary multimodal models, so the ordinary list IS the answer,
        # minus the ones that plainly aren't (image, tts, embedding).
        return [m for m in _filter_chat(_fetch_live_custom(p)) if m.startswith("gemini-")]
    r = requests.get(custom_base_url(p).rstrip("/") + "/models",
                     headers=_bearer(custom_key(p)), timeout=5)
    _check(r)
    return sorted(m["id"] for m in r.json().get("data", []) if _looks_stt(m["id"]))


def stt_models(name):
    """The speech-to-text models provider `name` can be asked for: its wire's
    floor (when it is pointed at that wire's own host), anything speech-shaped
    in its manual model list, then its live catalogue. [] for a provider on a
    wire that can't transcribe at all.

    Cached for the same few minutes chat catalogues are, and never raises: this
    is drawn on a settings tab, and a provider that is unreachable right now
    must show its suggestions rather than an error."""
    p = custom_provider(name)
    if not p or p["wire"] not in STT_WIRES:
        return []
    out = list(STT_FLOOR.get(p["wire"], [])) if _at_wire_host(p) else []
    for m in p["models"]:
        if _looks_stt(m) and m not in out:
            out.append(m)
    key = _models_key(name)
    now = time.time()
    cached = _stt_cache.get(key)
    if cached is None or now - cached[0] > _LIVE_TTL:
        try:
            _stt_cache[key] = (now, _fetch_stt(p))
        except Exception:
            _stt_cache[key] = (now, [])     # remember the failure too - no hammering
    for m in _stt_cache[key][1]:
        if m not in out:
            out.append(m)
    return out


def _transcribe_openai(p, model, data, filename):
    """OpenAI's /audio/transcriptions, which is the shape every endpoint
    serving speech models copies. `filename` matters: the endpoint reads the
    container off its extension, so it has to match the bytes."""
    r = requests.post(custom_base_url(p).rstrip("/") + "/audio/transcriptions",
                      headers=_bearer(custom_key(p)),
                      files={"file": (filename, data)},
                      data={"model": model}, timeout=STT_TIMEOUT)
    _check(r)
    return (r.json().get("text") or "").strip()


def _transcribe_gemini(p, model, data, filename):
    """Gemini has no transcription endpoint - the audio goes into an ordinary
    generateContent turn as an inline part, with STT_PROMPT asking for the
    words back and nothing else. Inline, not the file API, because a
    hold-to-talk clip is seconds long and an upload step would double the wait
    for no benefit."""
    ext = filename.rsplit(".", 1)[-1].lower()
    body = {"contents": [{"parts": [
        {"text": STT_PROMPT},
        {"inline_data": {"mime_type": STT_MIME.get(ext, "audio/webm"),
                         "data": base64.b64encode(data).decode()}},
    ]}]}
    r = requests.post(custom_base_url(p).rstrip("/") + "/models/" + model
                      + ":generateContent",
                      headers={"x-goog-api-key": custom_key(p),
                               "Content-Type": "application/json"},
                      json=body, timeout=STT_TIMEOUT)
    _check(r)
    parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts).strip()


def transcribe(name, model, clip, filename):
    """The words in `clip`, transcribed by provider `name` on `model`.

    `clip` is bytes or anything with .read(). Raises RuntimeError with a
    sentence meant to be read by a person - voice_input turns that into the
    message the page shows, so it is the only explanation anyone gets when the
    microphone doesn't work."""
    p = custom_provider(name)
    if not p:
        raise RuntimeError("there is no provider called " + name + " any more - "
                           "pick one on the voice tab")
    if p["wire"] not in STT_WIRES:
        raise RuntimeError(name + " speaks " + p["wire"] + ", which has no "
                           "speech-to-text at all - pick a provider on the "
                           + ", ".join(sorted(STT_WIRES)) + " wires")
    if not model:
        raise RuntimeError("no speech-to-text model chosen for " + name
                           + " - set one on the voice tab")
    data = clip.read() if hasattr(clip, "read") else clip
    if p["wire"] == "gemini":
        return _transcribe_gemini(p, model, data, filename)
    return _transcribe_openai(p, model, data, filename)


# Speaking the answer back out loud - the same idea as transcription above, run
# the other way. A provider is a host, a key and a wire; asking it for audio
# instead of words is a different endpoint on that same host, so this needs no
# credentials of its own either. Pick a provider on the voice tab and the
# finished reply is read aloud in the web page.
#
# Which wires can do it at all:
#   openai / deepseek / local  POST /audio/speech, OpenAI's own shape, which is
#                              what third-party endpoints serving speech copy.
#                              DeepSeek's own host serves no audio - the wire is
#                              here because a provider card ON that wire is
#                              often pointed somewhere else that does.
#   gemini                     no speech endpoint - its TTS models are asked
#                              through ordinary generateContent with the answer
#                              requested as audio instead of text.
#   anthropic / bedrock /      nothing. Claude produces no audio, and Polly is
#   claude-subscription        a separate AWS service with its own workflow.
TTS_WIRES = frozenset({"openai", "deepseek", "local", "gemini", "piper"})

# What a provider is offered before its catalogue has been read - and after,
# for endpoints that don't list their speech models. Suggestions, never a
# whitelist: the box on the voice tab is free text, so a model newer than this
# works with no code change. Only used for a provider pointed at its own wire's
# host, for the same reason STT_FLOOR is - see _at_wire_host.
TTS_FLOOR = {
    "openai": ["gpt-4o-mini-tts", "tts-1", "tts-1-hd"],
    "gemini": ["gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"],
}

# What a text-to-speech model looks like in a catalogue of a hundred others.
# "speech" alone is deliberately NOT in here: it matches "speech-to-text", which
# is the opposite job. kokoro and orpheus are the two local TTS models common
# enough in LM Studio/vLLM catalogues to be worth naming.
_TTS_WORDS = ("tts", "text-to-speech", "kokoro", "orpheus")

# Which voice reads it. A setting now - the voice tab picks one - but the same
# rules as the model boxes apply: these lists are suggestions for the dropdown,
# never a whitelist, and a blank setting means "whatever this wire's own default
# is" so an install that has never opened the picker sounds exactly as it did.
#
# OpenAI takes the voice as a plain id on /audio/speech; Gemini as a prebuilt
# voice name inside the speech config.
TTS_VOICE_DEFAULT = {"openai": "alloy", "gemini": "Kore"}

# The voices, each with a few words on how it sounds - "alloy" on its own tells
# you nothing, and the alternative to a description is picking one at random and
# listening to a whole reply to find out. Order is the order they are offered
# in, so the plainest ones come first.
#
# The two halves are NOT sourced the same way, which is worth knowing before
# editing them:
#
#   gemini   Google's own one-word labels, straight from its speech-generation
#            docs. Authoritative - if they change, change these.
#   openai   OpenAI publishes no descriptions at all, only the ids. These are
#            written here from how each one actually reads, so treat them as a
#            rough guide rather than a spec. openai.fm plays samples of all of
#            them if the wording here doesn't match what someone hears.
#
# The four newest OpenAI voices - ballad, verse, marin, cedar - only exist on
# the models that came with them; the older tts-1 pair rejects a request naming
# one, so TTS_VOICES_LEGACY below is what those two are offered instead.
TTS_VOICES = {
    "openai": {
        "alloy": "neutral and even - the default",
        "echo": "calm and measured, lower",
        "sage": "soft and unhurried",
        "ash": "warm, with some expression",
        "nova": "bright and brisk",
        "shimmer": "light and airy",
        "coral": "friendly and clear",
        "fable": "storytelling, faintly British",
        "onyx": "deep and authoritative",
        "ballad": "soft and emotive",
        "verse": "conversational, wide range",
        "marin": "natural and relaxed - one of the two newest",
        "cedar": "natural and grounded - one of the two newest",
    },
    # Gemini's prebuilt voices, as its speech config names them - capitalised,
    # and each one a fixed character rather than a tone you ask for. Style is
    # steered by what you SAY to the model instead; see _speak_gemini.
    "gemini": {
        "Kore": "firm - the default",
        "Puck": "upbeat",
        "Charon": "informative",
        "Zephyr": "bright",
        "Fenrir": "excitable",
        "Leda": "youthful",
        "Orus": "firm",
        "Aoede": "breezy",
        "Callirrhoe": "easy-going",
        "Autonoe": "bright",
        "Enceladus": "breathy",
        "Iapetus": "clear",
        "Umbriel": "easy-going",
        "Algieba": "smooth",
        "Despina": "smooth",
        "Erinome": "clear",
        "Algenib": "gravelly",
        "Rasalgethi": "informative",
        "Laomedeia": "upbeat",
        "Achernar": "soft",
        "Alnilam": "firm",
        "Schedar": "even",
        "Gacrux": "mature",
        "Pulcherrima": "forward",
        "Achird": "friendly",
        "Zubenelgenubi": "casual",
        "Vindemiatrix": "gentle",
        "Sadachbia": "lively",
        "Sadaltager": "knowledgeable",
        "Sulafat": "warm",
    },
}

# What tts-1 and tts-1-hd take - the nine that predate the newer models. Asked
# for one of the other four they fail the whole request, so the picker has to
# know the difference rather than offering all thirteen everywhere.
TTS_VOICES_LEGACY = ("alloy", "echo", "sage", "ash", "nova", "shimmer",
                     "coral", "fable", "onyx")

# The models that only take those nine, and that cannot be told how to read.
# The voice half of that is enforced by OpenAI - naming one of the newer four
# is a 400 and no audio at all - while the instructions half is not: these two
# accept the field and ignore it, which is worse, because a direction that is
# silently doing nothing looks exactly like one that is working badly.
# Matched on the id because that is all there is to go on.
TTS_LEGACY_MODELS = ("tts-1", "tts-1-hd")


def _tts_legacy(model):
    """True for an OpenAI speech model from before instructions and the newer
    voices existed. Exact ids rather than a prefix match: a third-party endpoint
    serving something called "tts-1-turbo" is not one of these two."""
    return (model or "").strip().lower() in TTS_LEGACY_MODELS


def tts_voices(name, model=""):
    """The voices provider `name` can be asked for on `model`, as a list of
    {"id", "says"} - the name to send and a few words on how it sounds, which
    is what the dropdown puts beside each one. [] for a provider on a wire that
    cannot speak, or one whose wire has no published voice list: a local
    endpoint serving kokoro has its own names and nothing here knows them.

    No network: this is a table lookup, unlike tts_models() above, so the page
    may ask again on every keystroke in the model box without it costing
    anything."""
    p = custom_provider(name)
    if not p or p["wire"] not in TTS_WIRES:
        return []
    if p["wire"] == "gemini":
        said = TTS_VOICES["gemini"]
    elif p["wire"] == "piper":
        # Piper's voices are the .onnx files in its voice directory - read
        # live, since the folder can change while the server runs.
        return _piper_voices(resolved_setup(p))
    # openai/deepseek/local all speak OpenAI's shape, but only a provider
    # actually pointed at OpenAI's own host serves OpenAI's voices - the same
    # reasoning TTS_FLOOR is limited by, see _at_wire_host.
    elif p["wire"] != "openai" or not _at_wire_host(p):
        return []
    elif _tts_legacy(model):
        said = {v: TTS_VOICES["openai"][v] for v in TTS_VOICES_LEGACY}
    else:
        said = TTS_VOICES["openai"]
    return [{"id": v, "says": says} for v, says in said.items()]


def tts_instructable(name, model=""):
    """Whether `model` on provider `name` can be told HOW to speak - tone,
    accent, pace. gpt-4o-mini-tts and its successors take it as a field; Gemini
    has no field for it but can be told the same thing in the prompt (see
    _speak_gemini); the tts-1 pair accepts the field and ignores it, which
    counts as no. So: true for everything that can actually be steered, by
    whichever mechanism."""
    p = custom_provider(name)
    if not p or p["wire"] not in TTS_WIRES:
        return False
    if p["wire"] == "piper":
        # Piper's voices are fixed recordings - there is no way to tell it a
        # tone, accent or pace, so the instructions box is disabled.
        return False
    return p["wire"] == "gemini" or not _tts_legacy(model)

# Synthesis is slower than a chat request - a long answer is a minute of audio
# to generate - but it is not slower than transcription, which has to be waited
# on before anything happens at all.
TTS_TIMEOUT = 120

# The most text sent in one go. OpenAI's /audio/speech refuses anything over
# 4096 characters outright, so this is that limit with room to spare; a reply
# longer than it is cut at the last sentence that fits (see _speakable). Most
# replies are nowhere near it.
TTS_MAX_CHARS = 3800

_tts_cache = {}           # provider id -> (fetched_at, [models])

# Markdown that is punctuation on a screen and noise in your ear: a heading's
# hashes, the asterisks around bold, a bullet's dash, the backticks around a
# name. Stripped as SYNTAX only - every word survives, including the contents
# of code blocks, because what gets read out is meant to be the reply itself
# and not a summary of it.
_MD_FENCE = re.compile(r"^\s*```.*$", re.M)
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_EMPH = re.compile(r"(\*\*|__|`)")


def _speakable(text):
    """`text` with its markdown syntax taken off and cut to something an audio
    endpoint will accept. Words are never dropped, only the characters that
    mark them up - so what you hear is the reply as written.

    The cut, when there has to be one, is made at the last sentence end that
    fits rather than mid-word: a reply that stops on a full stop sounds like it
    finished early, one that stops mid-syllable sounds broken."""
    out = _MD_LINK.sub(r"\1", text)
    out = _MD_FENCE.sub("", out)
    out = _MD_HEAD.sub("", out)
    out = _MD_BULLET.sub("", out)
    out = _MD_EMPH.sub("", out)
    out = out.strip()
    if len(out) <= TTS_MAX_CHARS:
        return out
    cut = out[:TTS_MAX_CHARS]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    return cut[:stop + 1].strip() if stop > TTS_MAX_CHARS // 2 else cut.strip()


def _fetch_tts(p):
    """Provider `p`'s own speech models, off its catalogue. Raises on any
    failure, exactly like _fetch_stt - the caller treats that as "no live list"
    rather than an error, because a provider that can't be asked right now
    still has its floor and still takes a typed model id."""
    if p["wire"] == "gemini":
        # No catalogue flag says what a model is for, so the ids are all there
        # is to go on - and Gemini's TTS models all say so in their name.
        return [m for m in _fetch_live_custom(p) if _looks_tts(m)]
    r = requests.get(custom_base_url(p).rstrip("/") + "/models",
                     headers=_bearer(custom_key(p)), timeout=5)
    _check(r)
    return sorted(m["id"] for m in r.json().get("data", []) if _looks_tts(m["id"]))


def _looks_tts(model_id):
    return any(word in model_id.lower() for word in _TTS_WORDS)


def tts_models(name):
    """The text-to-speech models provider `name` can be asked for: its wire's
    floor (when it is pointed at that wire's own host), anything speech-shaped
    in its manual model list, then its live catalogue. [] for a provider on a
    wire that cannot speak at all.

    Cached for the same few minutes chat catalogues are, and never raises - it
    is drawn on a settings tab, and a provider that is unreachable right now
    must show its suggestions rather than an error."""
    p = custom_provider(name)
    if not p or p["wire"] not in TTS_WIRES:
        return []
    if p["wire"] == "piper":
        # Piper's "models" are its voices - the .onnx stems. Read live from
        # the voice directory, since the folder can change while the server
        # runs and there is no catalogue to cache.
        return [v["id"] for v in _piper_voices(resolved_setup(p))]
    out = list(TTS_FLOOR.get(p["wire"], [])) if _at_wire_host(p) else []
    for m in p["models"]:
        if _looks_tts(m) and m not in out:
            out.append(m)
    key = _models_key(name)
    now = time.time()
    cached = _tts_cache.get(key)
    if cached is None or now - cached[0] > _LIVE_TTL:
        try:
            _tts_cache[key] = (now, _fetch_tts(p))
        except Exception:
            _tts_cache[key] = (now, [])     # remember the failure too - no hammering
    for m in _tts_cache[key][1]:
        if m not in out:
            out.append(m)
    return out


def _speak_openai(p, model, text, voice, instructions):
    """OpenAI's /audio/speech, which is the shape every endpoint serving TTS
    copies. Asks for mp3 because that is the one format every browser plays and
    the smallest thing to send to a phone.

    `instructions` is how gpt-4o-mini-tts is told the tone, accent and pace to
    read in. It is left out of the body when empty, and when the model is one of
    the tts-1 pair: those two take the field and do nothing with it, so sending
    it would only put a setting on the wire that has no effect. The voice tab
    says as much next to the box rather than letting it look like it works."""
    body = {"model": model, "input": text,
            "voice": voice or TTS_VOICE_DEFAULT["openai"],
            "response_format": "mp3"}
    if instructions and not _tts_legacy(model):
        body["instructions"] = instructions
    r = requests.post(custom_base_url(p).rstrip("/") + "/audio/speech",
                      headers=_bearer(custom_key(p)), json=body,
                      timeout=TTS_TIMEOUT)
    _check(r)
    return r.content, "audio/mpeg"


def _speak_gemini(p, model, text, voice, instructions):
    """Gemini has no speech endpoint - audio comes back from an ordinary
    generateContent turn asked to answer in sound instead of words.

    That also means there is no instructions FIELD: how it should read is said
    in the prompt, above the words to be read, which is the way Google
    documents it. The separator matters - without a line marking where the
    direction stops, a short instruction gets read out as part of the text.

    What arrives is headerless 16-bit PCM, which no browser will play on its
    own, so it is wrapped in a WAV container here. The sample rate is read off
    the part's own mime type ("audio/L16;codec=pcm;rate=24000") rather than
    assumed, since that is the one part of it Google has ever varied."""
    said = (instructions.strip() + "\n\nRead this out, and nothing else:\n"
            + text) if instructions.strip() else text
    body = {
        "contents": [{"parts": [{"text": said}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig":
                             {"voiceName": voice or TTS_VOICE_DEFAULT["gemini"]}}},
        },
    }
    r = requests.post(custom_base_url(p).rstrip("/") + "/models/" + model
                      + ":generateContent",
                      headers={"x-goog-api-key": custom_key(p),
                               "Content-Type": "application/json"},
                      json=body, timeout=TTS_TIMEOUT)
    _check(r)
    parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        if not inline.get("data"):
            continue
        pcm = base64.b64decode(inline["data"])
        mime = inline.get("mimeType") or inline.get("mime_type") or ""
        rate = 24000
        for bit in mime.split(";"):
            if bit.strip().startswith("rate="):
                try:
                    rate = int(bit.split("=", 1)[1])
                except ValueError:
                    pass    # keep the default rather than fail over a header
        return _wav(pcm, rate), "audio/wav"
    raise RuntimeError(model + " answered with no audio in it - if it is not a "
                       "text-to-speech model, pick one that is on the voice tab")


def _wav(pcm, rate):
    """Raw 16-bit mono PCM wrapped in a WAV header, in memory. Written by hand
    rather than through the wave module because that wants a file object and
    this is 44 bytes of header in front of bytes we already have."""
    header = b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVEfmt "
    header += (16).to_bytes(4, "little")        # size of this fmt chunk
    header += (1).to_bytes(2, "little")         # 1 = uncompressed PCM
    header += (1).to_bytes(2, "little")         # mono
    header += rate.to_bytes(4, "little")
    header += (rate * 2).to_bytes(4, "little")  # bytes per second, at 2 per sample
    header += (2).to_bytes(2, "little")         # bytes per frame
    header += (16).to_bytes(2, "little")        # bits per sample
    header += b"data" + len(pcm).to_bytes(4, "little")
    return header + pcm


def _piper_binary(setup):
    """The piper executable to run, from the provider's PIPER_PATH box, the
    environment, or PATH. Returns a path string, or None if it can't be found."""
    path = (setup or {}).get("PIPER_PATH") or ""
    if path:
        return path
    # Not on PATH either? shutil.which returns None and the caller says so.
    import shutil
    return shutil.which("piper") or ""


def _piper_voices(setup):
    """The voice names a piper provider can be asked for, as a list of
    {"id", "says"} - the .onnx stem, and a few words on how it sounds.

    Piper voices live as <name>.onnx files (each with a matching .onnx.json) in
    the voice directory. The id sent to piper is the .onnx stem. The description
    is read from the .onnx.json's "audio" object when it has one - piper's own
    metadata - and falls back to a plain "a piper voice" otherwise."""
    import json as _json
    voice_dir = (setup or {}).get("PIPER_VOICE_DIR") or ""
    if not voice_dir:
        return []
    d = Path(voice_dir).expanduser()
    if not d.is_dir():
        return []
    out = []
    for onnx in sorted(d.glob("*.onnx")):
        stem = onnx.stem
        says = "a piper voice"
        meta = onnx.with_suffix(".onnx.json")
        try:
            data = _json.loads(meta.read_text(encoding="utf-8"))
            audio = data.get("audio", {})
            if isinstance(audio, dict):
                says = (audio.get("quality") or audio.get("sample_rate")
                        or "a piper voice")
        except (OSError, ValueError):
            pass
        out.append({"id": stem, "says": says})
    return out


def _speak_piper(p, model, text, voice, instructions):
    """Piper's local neural TTS, run as a subprocess. No API key, no network -
    the audio is made on this machine by the piper binary.

    `model` is the voice to use - the .onnx stem, e.g. "en_US-lessac-medium" -
    and `voice` is accepted as an alias for it (the voice tab's picker sends
    the same thing). Piper reads the .onnx and its matching .onnx.json from the
    voice directory, and writes 16-bit mono PCM at the model's sample rate,
    which is wrapped in a WAV header here so a browser can play it.

    `instructions` (tone, accent, pace) is not something piper can be told -
    its voices are fixed recordings. It is accepted and ignored, matching how
    the tts-1 pair behaves, so the voice tab doesn't pretend a direction works
    when it can't."""
    import shutil
    import subprocess

    setup = resolved_setup(p)
    binary = _piper_binary(setup)
    if not binary:
        raise RuntimeError("piper can't be found. Install it (pip install piper-tts "
                           "or the piper release for your OS), or put the full path "
                           "in this provider's PIPER_PATH box on the providers tab.")
    voice_dir = (setup or {}).get("PIPER_VOICE_DIR") or ""
    if not voice_dir:
        raise RuntimeError("piper needs a voice model directory - set this "
                           "provider's PIPER_VOICE_DIR box on the providers tab "
                           "to the folder holding your .onnx voices.")
    d = Path(voice_dir).expanduser()
    if not d.is_dir():
        raise RuntimeError("the piper voice directory " + str(d) + " doesn't exist - "
                           "point PIPER_VOICE_DIR at a real folder of .onnx voices.")

    # The voice to use: the model box, or the voice box, or the first .onnx in
    # the directory. Piper needs the .onnx stem, and both boxes carry it.
    stem = (model or voice or "").strip()
    if not stem:
        first = sorted(d.glob("*.onnx"))
        if not first:
            raise RuntimeError("no .onnx voice models in " + str(d) + " - download "
                               "one (e.g. en_US-lessac-medium) and put it there.")
        stem = first[0].stem
    onnx = d / (stem if stem.endswith(".onnx") else stem + ".onnx")
    if not onnx.is_file():
        raise RuntimeError("no piper voice called " + stem + " in " + str(d)
                           + " - pick one from the voice tab's list.")

    # Piper writes to stdout when no output file is given. The sample rate is
    # read from the model's .onnx.json so the WAV header is right.
    rate = 22050
    meta = onnx.with_suffix(".onnx.json")
    try:
        import json as _json
        data = _json.loads(meta.read_text(encoding="utf-8"))
        audio = data.get("audio", {})
        if isinstance(audio, dict) and audio.get("sample_rate"):
            rate = int(audio["sample_rate"])
    except (OSError, ValueError, TypeError):
        pass
    try:
        proc = subprocess.run(
            [binary, "--model", str(onnx), "--output_raw"],
            input=text.encode("utf-8"), capture_output=True, timeout=TTS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("piper took longer than " + str(TTS_TIMEOUT)
                           + "s to read that out - giving up")
    except OSError as e:
        raise RuntimeError("could not run piper at " + binary + ": " + str(e))
    if proc.returncode != 0:
        raise RuntimeError("piper failed: " + (proc.stderr or b"").decode(
            "utf-8", "replace")[:300])
    pcm = proc.stdout
    if not pcm:
        raise RuntimeError("piper produced no audio for that text")
    return _wav(pcm, rate), "audio/wav"


def speak(name, model, text, voice="", instructions=""):
    """`text` read aloud by provider `name` on `model`, as (audio bytes, mime).

    `voice` is a name off tts_voices() and "" means the wire's own default, so
    an install that has never touched the picker sounds unchanged.
    `instructions` says HOW to read - tone, accent, pace - and is passed to
    whichever mechanism the wire has for it, or dropped where there is none.
    Neither is checked against a list here: the voice tab's boxes are
    suggestions, exactly like its model box, and a voice added after this
    release should work without a code change.

    Raises RuntimeError with a sentence meant to be read by a person, exactly
    like transcribe() above - the server puts that straight in front of whoever
    turned this on, and it is the only explanation they get when nothing comes
    out of the speaker."""
    p = custom_provider(name)
    if not p:
        raise RuntimeError("there is no provider called " + name + " any more - "
                           "pick one on the voice tab")
    if p["wire"] not in TTS_WIRES:
        raise RuntimeError(name + " speaks " + p["wire"] + ", which has no "
                           "text-to-speech at all - pick a provider on the "
                           + ", ".join(sorted(TTS_WIRES)) + " wires")
    if not model:
        raise RuntimeError("no text-to-speech model chosen for " + name
                           + " - set one on the voice tab")
    said = _speakable(text)
    if not said:
        raise RuntimeError("nothing to read out")
    if p["wire"] == "gemini":
        return _speak_gemini(p, model, said, voice, instructions)
    if p["wire"] == "piper":
        return _speak_piper(p, model, said, voice, instructions)
    return _speak_openai(p, model, said, voice, instructions)


# The API-key variables in .env. NOT a provider table any more - a provider's
# key lives on the provider - but these are still read by the parts of Uniagent
# that call OpenAI and friends directly rather than through a provider: the
# vision calls in view_image.py and screenshot_tool.py, the token counter in
# tokens.py, and Whisper in voice_input.py when the voice tab has never been
# given a provider of its own. They are edited in .env itself - the settings
# page has no tab for them any more, since everything it configures now names
# a provider instead.
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "gemini": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


def _wire_ready(p):
    """Whether provider `p`'s wire has what it needs, for the wires that
    authenticate without a key. Everything else is judged on what its card
    holds.

    Takes the whole provider, not just the wire, because that is where its
    setup form's answers live: a Bedrock provider carrying its own AWS keys is
    ready on a machine with no ~/.aws at all, and a claude-subscription
    provider that names the CLI's full path is ready even where PATH doesn't
    find it. Anything unanswered falls back to the environment, so a machine
    already set up stays ready with nothing typed in."""
    import importlib.util
    wire = p["wire"]
    if wire == "bedrock":
        setup = resolved_setup(p)
        return bool(setup.get("AWS_PROFILE")
                    or (setup.get("AWS_ACCESS_KEY_ID") and setup.get("AWS_SECRET_ACCESS_KEY"))
                    or (Path.home() / ".aws" / "credentials").exists())
    if wire == "claude-subscription":
        # Needs the SDK and the CLI it drives. Whether that CLI is still
        # *logged in* is the one credential check here that isn't cheap - it
        # costs a round trip, and this runs on every settings page load - so it
        # isn't made. An expired login is caught at request time instead, where
        # _claude_error names the fix.
        return bool(_claude_cli(resolved_setup(p))
                    and importlib.util.find_spec("claude_agent_sdk"))
    if wire == "piper":
        # Needs the piper binary and a voice directory. Both are cheap to check
        # (a PATH lookup and a folder stat), so it's done here rather than at
        # request time - a piper provider with no voices is a dead end on the
        # voice tab, and saying so on the card beats a silent failure later.
        setup = resolved_setup(p)
        binary = (setup.get("PIPER_PATH") or "").strip()
        if not binary:
            import shutil
            binary = shutil.which("piper") or ""
        voice_dir = (setup.get("PIPER_VOICE_DIR") or "").strip()
        return bool(binary and voice_dir and Path(voice_dir).expanduser().is_dir())
    # A wire whose spec says it takes no key is ready on its own: there is no
    # credential to be missing, and its own default URL is somewhere real. This
    # is what lets a local model server card work with every box left empty,
    # and it holds for any keyless wire someone writes later rather than for a
    # list of the ones that existed when this was written.
    return not wants_key(wire)


def keyless_wires():
    """The wires that authenticate without an API key - read off the specs
    rather than listed, so a wire added later is included by saying key: false
    and nothing else."""
    return [w for w in wire_names() if not wants_key(w)]


def available():
    """The providers usable right now - not just listed, but actually holding
    the credentials they need.

    A provider counts as configured once it has a key, or a base URL, or is on
    a wire that authenticates without either (bedrock's AWS credentials,
    claude-subscription's CLI login). The bar is deliberately low: having the
    card at all IS the intent to use it, so a wrong key surfaces as the
    endpoint's own 401 at request time, which says far more than the provider
    quietly missing from a dropdown would.

    This IS enforced as a whitelist: settings.py refuses to save a provider
    that isn't in this list, and cron/subagent reject one too. It exists so
    nothing - human or model - can pick a provider with no working credentials
    and have it fail later, deeper in, for a confusing reason.

    "Configured" here always means credentials exist, never that the provider
    is reachable RIGHT NOW - a dead network doesn't remove one from this list,
    it just fails the next request. An LM Studio card with a base URL and no
    key is listed for exactly that reason: a stopped server is a connection
    error at request time, not a vanished settings option."""
    return [p["name"] for p in custom_providers()
            if p["key"] or p["base_url"] or _wire_ready(p)]


# Wires that cannot hold a conversation, whatever credentials they have. piper
# is a text-to-speech engine and _piper raises rather than answering, so a
# piper provider is real and selectable on the voice tab but must never be
# offered as the thing a chat, a cron job or a subagent RUNS on. A wire spec
# can say "chat": false to join this without any code change here.
_NO_CHAT_WIRES = frozenset({"piper"})


def can_chat(wire):
    """Whether `wire` can answer a prompt at all - the counterpart of
    wants_key/base_url_label, read off the spec the same way."""
    if wire in _NO_CHAT_WIRES:
        return False
    return template_for(wire).get("chat", True) is not False


def chat_providers():
    """available(), minus the providers that cannot answer a prompt.

    What a cron job or a subagent may actually be run ON. available() is
    deliberately the wider list - a TTS card is a real, fully configured
    provider and belongs in the voice tab's dropdown - but naming one as the
    provider for a scheduled run is a guaranteed failure at request time, so
    it is never offered as a choice or accepted as one."""
    return [name for name in available()
            if can_chat(wire_for(name))]


def unusable_reason(name):
    """Why `name` cannot be the provider something RUNS on, as a sentence, or
    None if it can.

    Two different failures used to share one message ("it needs an API key this
    machine doesn't have"), which was simply false for the second: a piper card
    is fully configured and still cannot answer a prompt. cron and subagent
    both reject providers, so the wording lives here rather than in two copies
    that would drift."""
    if not name or not str(name).strip():
        return None
    name = str(name).strip().lower()
    if name in chat_providers():
        return None
    if name in available():
        # The wire is worth naming only when it isn't just the name again -
        # "'piper' is a piper provider" tells nobody anything.
        wire = wire_for(name)
        kind = ("a " + wire + " provider") if wire != name else "set up"
        return ("'" + name + "' is " + kind + " and cannot hold a conversation, "
                "so nothing can be run on it. Retry with one of these:")
    return ("'" + name + "' is not available - it needs an API key/credentials "
            "this machine doesn't have. Retry with one of these:")


def options_text():
    """The providers usable right now, as a short block for a tool's
    instructions or a rejection message. The one place this is spelled out for
    the model - cron and subagent both use it, so there's no second copy to
    drift. Recomputed on each call, so it can't go stale.

    Deliberately lists providers, not models. Only the provider is checked
    (against this list); the model is a free string passed straight through,
    so the model picks any model that provider offers from its own knowledge
    rather than from a hardcoded list that can never be complete. Each
    provider's default is shown for the case where it doesn't care which.

    chat_providers() rather than available(): every caller is offering a
    provider to RUN something on, so listing one that cannot answer a prompt
    would be inviting the exact rejection this text exists to prevent."""
    lines = []
    for name in chat_providers():
        default = next(iter(floor_models(name)), None)
        suffix = " (default: " + default + ")" if default else ""
        lines.append("  " + name + suffix)
    if not lines:
        return "  (none configured on this machine)"
    return "\n".join(lines) + (
        "\nName any model that provider offers - you are not limited to the "
        "defaults above. Give the model id exactly as that provider spells it.")


def key_status(only_set=False):
    """{provider: has a key set in .env} for the BUILT-IN providers with an
    editable API key - not bedrock (AWS credentials, not a single key),
    claude-subscription (the Claude Code CLI owns its own login) or local (LM
    Studio's server wants none). Custom providers are not here either: their
    keys live inside their own objects, not in a variable per provider, and
    the providers tab shows them. Never returns the key itself, only whether
    one is there. only_set=True returns just the names that do, for
    available() to use."""
    status = {}
    for name, env in KEY_ENV.items():
        try:
            _key(env)
            status[name] = True
        except RuntimeError:
            status[name] = False
    return [name for name, ok in status.items() if ok] if only_set else status


# .env is written by the settings page - a provider's key, email account setup,
# the web password - from HTTP threads that can land at the same moment. They
# all go through set_env below under this lock, so one save can't read the file
# while the other is part-way through replacing it.
_env_lock = threading.Lock()

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def env_names():
    """Every variable in .env, in the order the file has them, as
    [{"name": ..., "set": ...}] - the names only, never the values. Same
    contract as key_status() above: what's handed out is which variables are
    configured, not what they're configured to, so a secret written into .env
    has no way back out.

    A name with a blank value counts as not set, matching _key(). Comments and
    anything that isn't a NAME=value line are skipped, and a name a hand edit
    has left on two lines is listed once - set if either line carries a value,
    which is what _key() would find."""
    with _env_lock:
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    found = {}
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if not sep or not _ENV_NAME.match(name):
            continue
        has_value = bool(value.strip().strip("\"'"))
        found[name] = found.get(name, False) or has_value
    return [{"name": name, "set": is_set} for name, is_set in found.items()]


def port(name, default):
    """A port number out of .env, or `default` when it isn't set or isn't a
    usable one. The ports are settable because 8763/8764 are only defaults:
    another program may already hold them, and a machine may simply want
    Uniagent somewhere else.

    Anything unparseable falls back to the default rather than raising. A typo
    in .env should not stop the server from coming up at all - it should come
    up somewhere predictable and say where."""
    try:
        value = int(str(_env_value(name)).strip())
    except (TypeError, ValueError):
        return default
    return value if 1 <= value <= 65535 else default


def set_env(name, value):
    """Write `name=value` into .env - replacing that variable's line if it has
    one, adding it if not, or removing the line entirely when `value` is blank.
    Every other line is left exactly as it was, comments included.

    The one general way to write a secret into .env. Values are checked for
    newlines rather than escaped: a value containing one would silently become
    two variables (or half a variable), and there is no legitimate password or
    key that has a line break in it."""
    if not _ENV_NAME.match(name or ""):
        raise ValueError("not a usable variable name: " + repr(name))
    value = (value or "").strip()
    if "\n" in value or "\r" in value:
        raise ValueError("a value cannot contain a line break")
    with _env_lock:
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        lines = [line for line in lines if not line.strip().startswith(name + "=")]
        if value:
            lines.append(name + "=" + value)
        ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        # Secrets: readable by this user and nobody else. A fresh file would
        # otherwise be born world-readable under the default umask.
        try:
            ENV_FILE.chmod(0o600)
        except OSError:
            pass
    # This process just changed the file, so it must not wait out the cache's
    # own re-check before seeing its own write - see filecache.forget().
    filecache.forget(ENV_FILE)


# --- Workspaces. Where a chat's file and terminal tools do their work, and
# optionally which machine they do it on.
#
# One .env variable holding a JSON list, exactly like LLM_PROVIDERS and
# EMAIL_ACCOUNTS above, for the same reasons: it is the one file that is already
# private, already backed up with the folder, and already the place a person
# looks when they want to edit config by hand.
#
#   WORKSPACES=[{"id":"laptop","name":"Laptop","path":"/home/you/projects","ssh":"","default":true},
#               {"id":"pi","name":"Pi","path":"/home/you","ssh":"you@192.168.1.50"}]
#
# `path` is the root relative paths resolve against. `ssh` blank means this
# machine; anything else is an ssh destination - "user@host", or a Host alias
# out of ~/.ssh/config - and the tools that understand workspaces then do their
# reading, writing and running over there instead of here.
#
# Read from .env on every call and never cached, the same contract the provider
# list keeps: a workspace added on the settings page has to work on the very
# next turn, in this process and in the cron watcher, with neither restarted. ---

WORKSPACE_VAR = "WORKSPACES"

# The workspace that is always there: the Uniagent folder itself. It is not in
# .env, cannot be edited and cannot be removed - it is where the tools worked
# before workspaces existed, and it is what a chat falls back to when whatever
# it was pointing at is gone. Something has to be the floor.
#
# The dot in the id is what keeps it out of reach: ids made from a name go
# through _slug(), which only ever produces letters, digits and dashes, so no
# workspace anyone adds can collide with this one. A hand-edited .env that uses
# it anyway is dropped on the way in (see workspaces()).
BUILTIN_WORKSPACE_ID = "uniagent.root"


def builtin_workspace(is_default=True):
    """The Uniagent folder as a workspace object, shaped like any other."""
    return {"id": BUILTIN_WORKSPACE_ID,
            "name": "Uniagent folder",
            "path": str(Path(__file__).resolve().parent.parent),
            "ssh": "", "port": 0, "key": "",
            "default": bool(is_default),
            # The one flag the others never carry. The settings page reads it
            # to know this row has no remove button and nothing to edit.
            "builtin": True}

# An id is referenced by every chat that sits in the workspace, so it has to
# survive renames and be safe in a filename and a URL. The display name is the
# thing people change; the id is the thing code holds onto.
_WS_ID_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_workspace_error = None


def workspace_error():
    """The last complaint about WORKSPACES, or None. Same idea as
    custom_error(): a malformed list has to say so on the settings page rather
    than silently behaving as though no workspaces were configured."""
    return _workspace_error


def _slug(text):
    """A usable id out of a display name - "Josh's Pi 4" becomes "josh-s-pi-4"."""
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return out or "workspace"


def _normalize_workspace(entry):
    """One entry off .env as a clean object, or None if it isn't usable.

    A workspace with no path is the one genuinely unusable case: every tool
    that takes a workspace resolves against that path, and "" would silently
    mean the process's current directory - which is scripts/, and would put the
    agent's files inside the install."""
    if not isinstance(entry, dict):
        return None
    path = str(entry.get("path") or "").strip()
    if not path:
        return None
    name = str(entry.get("name") or "").strip()
    wsid = str(entry.get("id") or "").strip().lower()
    if not _WS_ID_OK.match(wsid):
        wsid = _slug(name or path.rsplit("/", 1)[-1])
    ssh = str(entry.get("ssh") or "").strip()
    try:
        port = int(entry.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    # The private key file, on THIS machine - it is what this end logs in with,
    # so unlike "path" it is expanded here whether the workspace is remote or
    # not. Empty means "work it out", which is the usual case.
    key = str(entry.get("key") or "").strip()
    return {
        "id": wsid,
        "name": name or wsid,
        # Only expanded for a local workspace: "~" on the far end of an ssh
        # connection is the remote account's home, and expanding it here would
        # quietly rewrite it to this machine's.
        "path": str(Path(path).expanduser()) if not ssh else path,
        "ssh": ssh,
        "port": port if 1 <= port <= 65535 else 0,
        "key": str(Path(key).expanduser()) if key else "",
        "default": bool(entry.get("default")),
    }


def _saved_workspaces():
    """Just the ones out of .env, normalized. The built-in is not among them -
    it is not stored anywhere and never written back."""
    global _workspace_error
    raw = _env_value(WORKSPACE_VAR)
    if not raw:
        _workspace_error = None
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _workspace_error = (WORKSPACE_VAR + " in .env is not valid JSON (" + str(e)
                            + ") - no workspaces are loaded until it is fixed.")
        return []
    if not isinstance(data, list):
        _workspace_error = WORKSPACE_VAR + " in .env must be a JSON list of workspace objects."
        return []
    _workspace_error = None
    out, seen = [], set()
    for entry in data:
        w = _normalize_workspace(entry)
        if not w:
            continue
        # A duplicate id is not a warning, it is a chat pointing at two
        # different machines depending on which one is read first. Later
        # duplicates are dropped, first one wins. The built-in's id is dropped
        # outright: a hand-edited .env must not be able to shadow the floor.
        if w["id"] in seen or w["id"] == BUILTIN_WORKSPACE_ID:
            continue
        seen.add(w["id"])
        out.append(w)
    return out


def workspaces():
    """Every workspace: the built-in Uniagent folder first, then whatever is in
    .env, in the order they sit there.

    Never empty. The built-in is always the first entry, which is what makes
    "there is always somewhere to work" true rather than a special case
    everything downstream has to remember."""
    saved = _saved_workspaces()
    # The built-in is the default exactly when nothing else claims it, so no
    # extra state is needed anywhere to record which one holds it.
    return [builtin_workspace(not any(w["default"] for w in saved))] + saved


def default_workspace():
    """The workspace a chat gets when it has never been given one. Always a
    workspace - the built-in when nothing else is marked."""
    for w in workspaces():
        if w["default"]:
            return w
    return builtin_workspace(True)


def workspace(wsid=None):
    """One workspace by id, or the default when `wsid` is None or unknown.

    Unknown falls back rather than raising on purpose. A chat can outlive the
    workspace it was filed under - deleted on the settings page, or the .env
    copied to a machine that doesn't have that mount - and the chat should
    still open and still answer, in the default place, instead of erroring on
    every tool call until someone edits its settings file by hand."""
    if wsid:
        for w in workspaces():
            if w["id"] == str(wsid).strip().lower():
                return w
    return default_workspace()


def save_workspaces(entries):
    """Write the whole workspace list back to .env, keeping only the fields a
    workspace has. At most one default survives - two would make
    default_workspace() depend on list order, which is not something anybody
    intends when they tick a box."""
    clean, seen_default = [], False
    for w in entries:
        is_default = bool(w.get("default")) and not seen_default
        seen_default = seen_default or is_default
        item = {"id": w["id"], "name": w["name"], "path": w["path"],
                "ssh": w.get("ssh", ""), "default": is_default}
        if w.get("port"):
            item["port"] = int(w["port"])
        # Only written when set, so an .env that never needed one stays as
        # short as it was.
        if w.get("key"):
            item["key"] = w["key"]
        clean.append(item)
    set_env(WORKSPACE_VAR, json.dumps(clean, separators=(",", ":")) if clean else "")


def save_workspace(name, path, ssh="", port=0, wsid=None, default=False, key=""):
    """Add a workspace, or update the one with this id.

    Raises ValueError with a sentence worth showing on anything the settings
    page should refuse. The id is derived from the name on creation and then
    never changes, because chats point at it: renaming "Pi" to "Raspberry Pi"
    must not orphan every chat that was working there."""
    name = (name or "").strip()
    path = (path or "").strip()
    ssh = (ssh or "").strip()
    key = (key or "").strip()
    if not name:
        raise ValueError("a workspace needs a name.")
    if not path:
        raise ValueError("a workspace needs a root directory.")
    if ssh and not re.match(r"^[A-Za-z0-9._@\-]+$", ssh):
        raise ValueError("that does not look like an ssh destination - it should be "
                         "'user@host', a hostname, or a Host alias from ~/.ssh/config.")
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        raise ValueError("the ssh port has to be a number.")
    if port and not 1 <= port <= 65535:
        raise ValueError("a port has to be between 1 and 65535.")
    if port and not ssh:
        raise ValueError("a port only means something with an ssh destination.")
    if key and not ssh:
        raise ValueError("an ssh key only means something with an ssh destination.")
    if key:
        key = str(Path(key).expanduser())
        # Caught here rather than at connection time: a typo in a key path
        # otherwise surfaces as "permission denied" on the next turn, which
        # sends you looking at the far machine for a problem that is on this
        # one.
        if not Path(key).is_file():
            raise ValueError("there is no key file at " + key + ".")
        if key.endswith(".pub"):
            raise ValueError("that is the public half - Uniagent needs the private key, "
                             "which is the same path without the .pub.")

    entries = _saved_workspaces()
    wsid = (wsid or "").strip().lower()
    if wsid == BUILTIN_WORKSPACE_ID:
        raise ValueError("the Uniagent folder is built in - it cannot be edited.")
    if wsid:
        target = next((w for w in entries if w["id"] == wsid), None)
        if target is None:
            raise ValueError("there is no workspace with id " + wsid + ".")
    else:
        base = _slug(name)
        candidate, n = base, 2
        taken = {w["id"] for w in entries}
        while candidate in taken:
            candidate, n = base + "-" + str(n), n + 1
        target = {"id": candidate}
        entries.append(target)

    target["name"] = name
    target["path"] = str(Path(path).expanduser()) if not ssh else path
    target["ssh"] = ssh
    target["port"] = port
    target["key"] = key
    if default:
        for w in entries:
            w["default"] = (w is target)
    # Nothing is forced to be the default here: with none of these claiming it
    # the built-in holds it, so the list is never without one.
    save_workspaces(entries)
    return target


def set_default_workspace(wsid):
    """Which workspace a chat gets when it has never been given one.

    The built-in is chosen by nothing else claiming it, so setting it means
    clearing the flag off every saved workspace rather than writing it
    anywhere."""
    wsid = (wsid or "").strip().lower()
    entries = _saved_workspaces()
    if wsid != BUILTIN_WORKSPACE_ID and not any(w["id"] == wsid for w in entries):
        raise ValueError("there is no workspace called " + repr(wsid) + ".")
    for w in entries:
        w["default"] = (w["id"] == wsid)
    save_workspaces(entries)


def delete_workspace(wsid):
    """Remove a workspace. Chats still pointing at it fall back to the default
    on their next tool call - see workspace()."""
    wsid = (wsid or "").strip().lower()
    if wsid == BUILTIN_WORKSPACE_ID:
        raise ValueError("the Uniagent folder is built in - it cannot be removed. "
                         "It is what a chat falls back to when its own workspace "
                         "is gone, so there is always somewhere to work.")
    entries = _saved_workspaces()
    left = [w for w in entries if w["id"] != wsid]
    if len(left) == len(entries):
        raise ValueError("there is no workspace with id " + repr(wsid) + ".")
    # No need to hand the default on: if the one removed held it, the built-in
    # takes it back simply by nothing else claiming it.
    save_workspaces(left)


# --- mojibake ----------------------------------------------------------------
#
# A reply's UTF-8 decoded one byte at a time, so "\u2192" (E2 86 92) arrives as
# the three characters "\u00e2\u0086\u0092" - which shows as "\u00e2" and two
# invisibles. _sse no longer does that (see its docstring), but the endpoint at
# the far end can: a serving stack that decodes each TOKEN's bytes on its own
# splits every multi-byte character the same way, and the mojibake is then in
# the text it sends, not in anything we did to it. Old transcripts hold plenty
# of it from before the _sse fix, and it is worth repairing on the way in rather
# than being saved a second time.
#
# The lead character of a mis-decoded sequence, then its continuations. Ranges
# are the UTF-8 ones read as Latin-1: C2-F4 leads a 2-to-4 byte sequence, 80-BF
# continues one.
_MOJI = re.compile("[\u00c2-\u00f4][\u0080-\u00bf]{1,3}")
# How many characters the sequence that lead starts should have, all told.
_MOJI_LEN = ((0xc2, 0xdf, 2), (0xe0, 0xef, 3), (0xf0, 0xf4, 4))
# A run at the very end that may only be half-arrived - see _mended.
_MOJI_TAIL = re.compile("[\u00c2-\u00f4][\u0080-\u00bf]{0,2}$")
# How many times over text may have been mangled - see repair_mojibake. Two is
# what actually turns up on disk (saved mangled, read back and mangled again);
# the third is slack.
_MOJI_PASSES = 3


def _wanted(lead):
    """How many characters a mis-decoded sequence starting with `lead` has."""
    for low, high, size in _MOJI_LEN:
        if low <= ord(lead) <= high:
            return size
    return 0


def repair_mojibake(text):
    """`text` with any Latin-1-decoded UTF-8 in it put back, and everything else
    left exactly as it was.

    Run by run, not whole-string, and only where the bytes actually say so: a
    run is repaired when re-encoding it as Latin-1 and decoding that as UTF-8
    succeeds STRICTLY. That test is what makes this safe on ordinary text -
    "cor\u00e7\u00e3o" or "caf\u00e9 \u00bd" is not valid UTF-8 once
    re-encoded, so it fails and is left alone, while "\u00e2\u0086\u0092" is
    and becomes "\u2192". Whole-string repair would also give up entirely on a
    reply that mixes the two - one emoji that came through fine and one arrow
    that didn't - because the good character cannot be Latin-1-encoded at all.

    Text with nothing to repair comes back as the same object, so the common
    case costs one regex scan and no allocation.

    Applied until it stops changing anything, because text can be mis-decoded
    TWICE - saved mangled once, read back and mangled again - and one pass then
    only gets halfway: "\u00c3\u00a2\u00c2\u0086\u00c2\u0092" becomes
    "\u00e2\u0086\u0092", which is still the arrow nobody can read. Each pass
    can only shorten the text, so this converges; the cap is there because a
    loop over model output should have one whatever the maths says."""
    def fix(match):
        run = match.group(0)
        try:
            return run.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return run

    for _ in range(_MOJI_PASSES):
        if not _MOJI.search(text):
            break
        mended = _MOJI.sub(fix, text)
        if mended == text:
            break
        text = mended
    return text


def _mended(pieces):
    """`pieces` with the mojibake repaired ACROSS the joins.

    Repairing each piece on its own would miss the case that matters: the
    endpoints that do this decode one token at a time, so the three characters
    of a broken arrow arrive as three separate pieces and no single piece is a
    repairable run. So a run still short of the length its lead announces is
    held back and carried into the next piece.

    Nothing is held for longer than that. A piece ending on a COMPLETE run goes
    out repaired, a piece ending on nothing repairable goes out untouched, and
    whatever is being held when the stream ends is yielded as it stands - a
    stream that stops mid-character must still show what it had.

    The repair comes BEFORE the hold-back, not after, because a pass can reveal
    an incomplete run that wasn't visible as one: text mangled twice arrives as
    "\u00c3\u00a2" - a complete-looking run - and only once that is mended to
    "\u00e2" is it apparent that two more characters are owed. Repairing first
    means each pass's leftovers are held the same way the original's are.

    The cost of a hold is that a reply ending on an ordinary accented letter -
    "caf\u00e9" - waits for the next piece before it is shown, and for the end
    of the stream if there is no next piece. A few characters late on the last
    word of a reply, and never wrong, is the right side of that trade."""
    hold = ""
    for piece in pieces:
        text = repair_mojibake(hold + piece)
        hold = ""
        tail = _MOJI_TAIL.search(text)
        if tail and len(tail.group(0)) < _wanted(tail.group(0)[0]):
            text, hold = text[:tail.start()], tail.group(0)
        if text:
            yield text
    if hold:
        yield repair_mojibake(hold)


def _guarded(model, prompt, temperature, call, usage=None, tools=None, tool_call=None,
             reasoning=None, on_call_delta=None):
    """Wrap a provider's streaming generator so a stopped turn stops it. Yields
    the pieces through untouched.

    Every provider's stream passes through here, which makes it the one place
    that can guarantee a stopped turn produces nothing further no matter WHICH
    provider answered: the check before each piece gives cooperative
    cancellation to the wires that don't go through _sse (Bedrock's boto3
    stream, claude-subscription's SDK), and it holds for any provider added
    later. _stream_post is what makes it immediate on top of that; this is the
    floor underneath it.

    `usage`, `tool_call` and `reasoning` are passed straight through to the
    provider function, which fills them in place as its own events report them
    - token counts, a native tool call, a thinking model's reasoning_content.

    It is also where mojibake is mended, for the same reason the stop check is
    here: every provider's reply comes through this loop, so a wire that sends
    Latin-1-decoded UTF-8 is repaired whichever one it is and whichever is added
    next. See _mended, which holds a part-arrived character across the join."""
    def pieces():
        for piece in call(model, prompt, temperature, usage=usage, tools=tools,
                          tool_call=tool_call, reasoning=reasoning,
                          on_call_delta=on_call_delta):
            turnctx.check()
            yield piece
        turnctx.check()

    for piece in _mended(pieces()):
        yield piece


def stream_response(prompt, provider=PROVIDER, model=MODEL, temperature=TEMPERATURE,
                     usage=None, tools=None, tool_call=None, reasoning=None,
                     on_call_delta=None):
    """Send prompt to the given provider + model and yield the reply in pieces
    as the model writes it. `prompt` is either a plain string (one throwaway
    question, no history) or a real messages list - main.py's turn loop always
    sends the latter now, see _messages()/_compat() above. Falls back to the
    module defaults only when a caller passes nothing. claude-subscription
    takes `temperature` too (every provider function shares one signature) but
    silently ignores it - see _claude_subscription's docstring for why.

    `usage`, if given, must be a dict - it's mutated in place with whichever
    of "input_tokens"/"output_tokens" the provider actually reports (real
    counts from the provider's own response, never estimated/tokenized here).
    Pass None (the default) to skip asking for it at all.

    `tools`, if given, is a provider-shaped tools array (tool_processor.
    tools_schema()) sent alongside the prompt for real native tool-calling -
    only anthropic/openai/deepseek/local actually use it; every other
    provider function accepts and ignores it, to keep one shared signature.
    `tool_call`, if given, must be a dict - mutated in place with the
    provider's own structured tool calls as they stream in, the same way
    `usage` is. Both are None by default, which is exactly today's behaviour:
    no tools sent, no native call parsed.

    A response may carry SEVERAL calls, and the dict is the collector for all
    of them: the first is written straight onto it as {"id","name",
    "arguments"} - where a single call has always been - and any others go
    into tool_call["more"]. Read them back with calls_in(), which is the only
    thing that should know that layout.

    `reasoning`, if given, must be a dict too - it collects a thinking model's
    reasoning_content under "content" as it streams. That text is never part
    of the reply (it isn't yielded); it is captured only so it can be handed
    straight back on the next request, which DeepSeek's thinking models
    require of any turn that made a tool call - see _REASONING_KEY."""
    for p in providers():
        if p["name"] == provider:
            return _guarded(model, prompt, temperature, p["call"],
                            usage=usage, tools=tools, tool_call=tool_call,
                            reasoning=reasoning, on_call_delta=on_call_delta)
    raise ValueError(f"Unknown provider: {provider}")


def get_response(prompt, provider=PROVIDER, model=MODEL, temperature=TEMPERATURE, usage=None):
    """The whole reply as one string. For callers that have nothing to do with
    the pieces as they arrive - the safety check, mainly."""
    return "".join(stream_response(prompt, provider=provider, model=model,
                                   temperature=temperature, usage=usage))
