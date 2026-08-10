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

import requests

import turnctx

# --- Fallback defaults. The real choices live with the callers: the main
# agent's model in main.py, the safety-check model in tool_validation.py. These
# are only used if get_response is called without an explicit provider/model. ---
PROVIDER = "deepseek"           # must match a "name" in PROVIDERS below
MODEL = "deepseek-v4-flash"
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
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value:
                    return value
    except OSError:
        pass
    raise RuntimeError(name + " is not set in " + str(ENV_FILE) + " - add it there, or on the settings page.")


def _bearer(key):
    """The Authorization header for an OpenAI-wire endpoint - or no header at
    all when there is no key. A local server (LM Studio, Ollama, vLLM) asks
    for none, and sending a bare "Bearer " with nothing after it is worse than
    sending nothing: some of them reject the malformed header outright where
    they would happily have served an anonymous request."""
    return {"Authorization": "Bearer " + key} if key else {}


def _check(r):
    """Raise if the request failed. On failure the providers reply with plain
    text (e.g. '401 Authentication Fails'), which would otherwise blow up as an
    opaque JSONDecodeError - turn it into a clear message."""
    if r.status_code != 200:
        raise RuntimeError("provider returned HTTP " + str(r.status_code) + ": " + r.text[:300])


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


def _sse(r):
    """The JSON payload of each `data:` line of a Server-Sent Events response.

    OpenAI, DeepSeek, Anthropic and Gemini all stream as SSE, so they share this.
    Blank lines are separators, `event:` lines only name what follows (the same
    name is in the payload), and OpenAI-style APIs end with a literal [DONE].

    The read is wrapped in the turn's cancellation watch, so /stop breaks the
    connection rather than waiting politely for the next frame. A cancelled
    turn's socket error is not an error - the context says the turn was
    stopped, so it is re-raised as Stopped and nothing reports a network
    failure the user never had.
    """
    _check(r)
    with turnctx.watch(r, closer=lambda: _break_open(r)):
        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    return
                try:
                    yield json.loads(body)
                except json.JSONDecodeError:
                    continue  # a keep-alive or a partial frame - nothing to read
        except turnctx.Stopped:
            raise
        except Exception:
            # The socket dying BECAUSE this turn was stopped is not a provider
            # failure - it is the stop working. Anything else is a real error
            # and goes up as itself.
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
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            mapped_role, content = "user", "Tool result: " + (m.get("content") or "")
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


# Message keys the OpenAI wire actually knows. Uniagent's own bookkeeping key
# raw_call (see _compat) is not one of them, and an unrecognised key on a
# message is a 400, so the native path copies messages through this filter
# rather than sending a stored turn verbatim.
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


def _anthropic(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
               reasoning=None, on_call_delta=None,
               base_url="https://api.anthropic.com", key=None):
    # `base_url`/`key` default to Anthropic's own endpoint and the key in
    # .env, which is the built-in "anthropic" provider. A custom provider on
    # the anthropic wire (see custom_providers()) passes its own of each -
    # that is the only difference between the two, so they share this body
    # rather than a near-copy of it that would drift.
    #
    # With `tools` the history goes over in Anthropic's own content-block
    # shape, so a tool_use it produced comes back as a tool_use. Without them
    # (a one-shot string prompt - a safety check, compaction) there is no
    # history to preserve and _compat's plain text is right.
    system, turns = (_anthropic_messages(prompt) if tools
                     else _compat(_messages(prompt)))
    body = {
        "model": model,
        "max_tokens": 1024,
        "temperature": temperature,
        "stop_sequences": STOP,
        "stream": True,
        "messages": turns,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    r = _stream_post(
        base_url.rstrip("/") + "/v1/messages",
        headers={
            "x-api-key": _key("ANTHROPIC_API_KEY") if key is None else key,
            "anthropic-version": "2023-06-01",
        },
        json=body,
        stream=True,
    )
    for event in _sse(r):
        etype = event.get("type")
        # Real counts, not an estimate: message_start carries the input side
        # (it's known before a single output token exists), message_delta
        # carries the running output count, updated as the reply grows.
        if usage is not None:
            if etype == "message_start":
                u = event.get("message", {}).get("usage", {})
                usage["input_tokens"] = u.get("input_tokens")
                usage["output_tokens"] = u.get("output_tokens")
            elif etype == "message_delta":
                out = event.get("usage", {}).get("output_tokens")
                if out is not None:
                    usage["output_tokens"] = out
        # A tool_use content block starts empty ({"type":"tool_use","id":...,
        # "name":...,"input":{}}) and its `input` arrives afterward as
        # incremental input_json_delta fragments - accumulated into a plain
        # JSON-text string here, same shape _openai_style builds from
        # OpenAI's own tool_calls fragments, so main.py reads one shape
        # regardless of which provider answered.
        if tool_call is not None and etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_call["id"] = block.get("id")
                tool_call["name"] = block.get("name")
                tool_call["arguments"] = ""
                # Watched being written, same as the OpenAI wire - see
                # _openai_style's on_call_delta comment. Without it a native
                # turn shows nothing at all for its whole length, because a
                # tool_use block is never yielded as reply text.
                if on_call_delta:
                    on_call_delta((block.get("name") or "") + "(")
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            text = delta.get("text")
            if text:
                yield text
            elif tool_call is not None:
                partial = delta.get("partial_json")
                if partial:
                    tool_call["arguments"] = tool_call.get("arguments", "") + partial
                    if on_call_delta:
                        on_call_delta(partial)
    if on_call_delta and tool_call and tool_call.get("name"):
        on_call_delta(")")


def _openai_style(url, headers, body, usage=None, tools=None, tool_call=None, reasoning=None,
                  on_call_delta=None):
    """OpenAI and DeepSeek speak the same wire format, so they share this.

    Newer OpenAI models REJECT parameters older ones expect - gpt-5.x refuses
    'stop' (and non-default 'temperature') with a 400 naming the parameter.
    Rather than keep a list of which model takes what (unknowable, goes
    stale), any such 400 drops the named parameter and retries - so one code
    path serves every generation, current and future. stream_options is
    dropped the same defensive way if a backend rejects it (some local
    servers don't support it) - usage just stays unpopulated for that call,
    it doesn't fail the turn over it."""
    body = dict(body)
    if tools:
        body["tools"] = tools
    if usage is not None:
        # Without this, a streamed response never carries a usage block at
        # all - real counts arrive in one extra final SSE event with an
        # empty choices list, not on every chunk.
        body["stream_options"] = {"include_usage": True}
    flaky = 0
    while True:
        r = _stream_post(url, headers=headers, json=body, stream=True)
        if r.status_code == 200:
            break
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        param = err.get("param")
        if err.get("code") in ("unsupported_parameter", "unsupported_value") and param in body:
            del body[param]  # each retry removes one param, so this terminates
            continue
        # OpenAI's reasoning models refuse function tools on this endpoint
        # while reasoning is on, and say so in as many words: "Function tools
        # with reasoning_effort are not supported for gpt-5.6-terra in
        # /v1/chat/completions. To use function tools, use /v1/responses or
        # set reasoning_effort to 'none'." Measured on gpt-5.6-terra.
        #
        # The retry above can't catch this one: `param` names reasoning_effort,
        # which is NOT in the body - it's the model's own default that's the
        # problem, so there is nothing to delete. Setting it explicitly is the
        # remedy the error itself names, and the cheaper of the two it offers
        # (the other, /v1/responses, is a different API shape end to end).
        #
        # The trade is real and worth knowing: that model does no reasoning on
        # a turn that carries tools. Tools are the whole point of a native turn
        # though, so a thinking model with no tools is the worse end of it.
        # Set once, so this terminates.
        if tools and param == "reasoning_effort" and body.get("reasoning_effort") != "none":
            body["reasoning_effort"] = "none"
            continue
        # OpenAI's gpt-5.x endpoints intermittently 401 with "insufficient
        # permissions" - measured at roughly 1 request in 3 on the same key
        # and model that succeed moments later. A couple of paced retries turn
        # that from a dead turn into a pause. A genuinely bad key fails all
        # three times and still raises, just a few seconds later.
        if r.status_code == 401 and flaky < 2:
            flaky += 1
            _pause(1.5 * flaky)
            continue
        _check(r)  # not fixable - raise with the provider's own message
    for event in _sse(r):
        if usage is not None and event.get("usage"):
            u = event["usage"]
            usage["input_tokens"] = u.get("prompt_tokens")
            usage["output_tokens"] = u.get("completion_tokens")
        for choice in event.get("choices") or []:
            text = choice.get("delta", {}).get("content")
            if text:
                yield text
            # A thinking model streams its reasoning as its own delta field,
            # alongside (and ahead of) the content and tool_calls fragments -
            # accumulated, never yielded, because it is not part of the reply.
            # It exists only to be handed straight back on the next request:
            # see _REASONING_KEY.
            if reasoning is not None:
                part = choice.get("delta", {}).get("reasoning_content")
                if part:
                    reasoning["content"] = reasoning.get("content", "") + part
            if tool_call is not None:
                # Streamed as fragments: id/function.name arrive once (on the
                # first fragment for that index), function.arguments arrives
                # piecemeal and is concatenated into one JSON-text string,
                # same shape _anthropic builds from its own input_json_delta
                # fragments. index != 0 is a second parallel call - ignored,
                # same one-call-per-turn rule the text-embedded path already
                # enforces (see _span()'s "first call only" comment above).
                for frag in choice.get("delta", {}).get("tool_calls") or []:
                    if frag.get("index", 0) != 0:
                        continue
                    if frag.get("id"):
                        tool_call["id"] = frag["id"]
                    fn = frag.get("function") or {}
                    # on_call_delta gets the same fragments as readable text as
                    # they land - "name(", then the arguments piecemeal - so a
                    # native call can be WATCHED being written, the way a
                    # text-embedded one always could. Without it a native turn
                    # shows nothing at all until the whole stream ends, since
                    # none of this is yielded as reply text. The closing ")"
                    # goes on after the loop.
                    if fn.get("name"):
                        tool_call["name"] = fn["name"]
                        if on_call_delta:
                            on_call_delta(fn["name"] + "(")
                    if fn.get("arguments"):
                        tool_call["arguments"] = tool_call.get("arguments", "") + fn["arguments"]
                        if on_call_delta:
                            on_call_delta(fn["arguments"])
    # Closed off once the stream is done, so what was watched being written
    # ends up as the same text the turn is stored and redrawn with - see
    # main.py's _parse_call.
    if on_call_delta and tool_call and tool_call.get("name"):
        on_call_delta(")")


def _openai(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
            reasoning=None, on_call_delta=None,
            base_url="https://api.openai.com/v1", key=None):
    # Same switch _deepseek makes: `tools` present means this turn is on native
    # tool-calling, so the history goes over in the API's own shape rather than
    # folded to plain text - a call the provider itself made has to come back
    # as that call. Without tools it's a prompted-format turn, unchanged.
    #
    # NOT reasoning=True. That is DeepSeek's own requirement (its thinking
    # models 400 unless a calling turn replays its reasoning_content); here the
    # key is simply unrecognised, and an unrecognised message key is itself a
    # 400 - see _REASONING_KEY. `reasoning` is still forwarded below, because
    # capturing what a model streams costs nothing and the dict is only ever
    # replayed by the provider that asks for it.
    messages = _native_messages(prompt) if tools else _plain_messages(prompt)
    yield from _openai_style(
        base_url.rstrip("/") + "/chat/completions",
        # key=None means "the built-in openai provider" and reads .env; a
        # custom provider always passes its own, and passes "" when it has
        # none - which _bearer turns into no auth header at all rather than
        # borrowing OPENAI_API_KEY, which would be nobody's intention.
        _bearer(_key("OPENAI_API_KEY") if key is None else key),
        {
            "model": model,
            "temperature": temperature,
            "stop": STOP,
            "stream": True,
            "messages": messages,
        },
        usage=usage, tools=tools, tool_call=tool_call, reasoning=reasoning,
        on_call_delta=on_call_delta,
    )


def _deepseek(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
              reasoning=None, on_call_delta=None,
              base_url="https://api.deepseek.com", key=None):
    # `tools` present means this turn is on native tool-calling, so the
    # history goes over in the API's own shape (_native_messages) rather than
    # folded to plain text - the call the provider itself made has to come
    # back as that call. Without tools it's a prompted-format turn and nothing
    # changes. _openai and _local share this wire and now make the same switch;
    # what stays DeepSeek-only is the reasoning_content round-trip below.
    #
    # reasoning=True is DeepSeek-only: its thinking models demand their own
    # reasoning_content back on any turn that made a call (_REASONING_KEY).
    messages = (_native_messages(prompt, reasoning=True) if tools
                else _plain_messages(prompt))
    yield from _openai_style(
        base_url.rstrip("/") + "/chat/completions",
        _bearer(_key("DEEPSEEK_API_KEY") if key is None else key),
        {
            "model": model,
            "temperature": temperature,
            "stop": STOP,
            "stream": True,
            # DeepSeek defaults to Chinese without this - its own leading
            # system message, since it's a DeepSeek quirk, not something
            # every provider needs.
            "messages": [{"role": "system", "content": "Always respond in English."}]
                        + messages,
        },
        usage=usage, tools=tools, tool_call=tool_call, reasoning=reasoning,
        on_call_delta=on_call_delta,
    )


def _local(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
           reasoning=None, on_call_delta=None):
    # A local server on the user's own machine, not a paid API - no key needed,
    # and whatever model is loaded in LM Studio right now is what answers,
    # named exactly as LM Studio shows it.
    #
    # Native tool-calling, same switch as _openai/_deepseek - LM Studio serves
    # the OpenAI wire, tools array included, for any loaded model whose
    # template supports it. Whether the model actually USES it is the model's
    # business: a small one that ignores the schemas and writes a call as
    # prose simply doesn't call anything, and main.py nudges it (see
    # tool_processor.looks_like_call). There is no prompted fallback to put it
    # on any more - a model that can't do native tool calls can't use tools.
    #
    # reasoning is forwarded, not replayed. Locally-run thinking models
    # (deepseek-r1 builds especially) do stream reasoning_content, so capturing
    # it is worth it - but no reasoning=True on _native_messages: LM Studio
    # never demands the round-trip, and handing an unknown key to whichever
    # backend is loaded is a needless way to fail a turn.
    messages = _native_messages(prompt) if tools else _plain_messages(prompt)
    yield from _openai_style(
        LMSTUDIO_URL + "/chat/completions",
        {},
        {
            "model": model,
            "temperature": temperature,
            "stop": STOP,
            "stream": True,
            "messages": messages,
        },
        usage=usage, tools=tools, tool_call=tool_call, reasoning=reasoning,
        on_call_delta=on_call_delta,
    )


def _gemini(model, prompt, temperature=TEMPERATURE, usage=None, tools=None, tool_call=None,
            reasoning=None, on_call_delta=None,
            base_url="https://generativelanguage.googleapis.com/v1beta", key=None):
    # alt=sse is what makes Gemini stream as Server-Sent Events; without it the
    # streaming endpoint sends one big JSON array instead.
    #
    # With `tools`, the history goes over as functionCall/functionResponse
    # parts (_gemini_contents) rather than _compat's plain text. Gemini's
    # tools array is ALREADY one Tool object holding every declaration - it
    # refuses several non-search Tool objects in one request - so it goes on
    # the body as-is; see tool_processor.tools_schema.
    if tools:
        system, contents = _gemini_contents(prompt)
    else:
        system, turns = _compat(_messages(prompt))
        # Gemini calls the assistant's role "model", not "assistant".
        contents = [{"role": "model" if t["role"] == "assistant" else "user",
                     "parts": [{"text": t["content"]}]} for t in turns]
    body = {
        "generationConfig": {"temperature": temperature, "stopSequences": STOP},
        "contents": contents,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = tools
    r = _stream_post(
        base_url.rstrip("/") + f"/models/{model}:streamGenerateContent?alt=sse",
        headers={"x-goog-api-key": _key("GEMINI_API_KEY") if key is None else key},
        json=body,
        stream=True,
    )
    for event in _sse(r):
        if usage is not None:
            u = event.get("usageMetadata")
            if u:
                usage["input_tokens"] = u.get("promptTokenCount")
                usage["output_tokens"] = u.get("candidatesTokenCount")
        for candidate in event.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text = part.get("text")
                if text:
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
                fn = part.get("functionCall")
                if fn and tool_call is not None and not tool_call.get("name"):
                    tool_call["id"] = "call_" + uuid.uuid4().hex[:8]
                    tool_call["name"] = fn.get("name")
                    tool_call["arguments"] = json.dumps(fn.get("args") or {})
                    if on_call_delta:
                        on_call_delta(tool_call["name"] + "(" + tool_call["arguments"] + ")")


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
             base_url=None, key=None, setup=None):
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

    for event in r["stream"]:
        # converse_stream sends one "metadata" event, usually last, carrying
        # the real token counts - separate from the contentBlockDelta events
        # that carry the actual text.
        if usage is not None and "metadata" in event:
            u = event["metadata"].get("usage", {})
            usage["input_tokens"] = u.get("inputTokens")
            usage["output_tokens"] = u.get("outputTokens")
        # A tool call opens with contentBlockStart carrying its id and name,
        # then its input arrives as contentBlockDelta toolUse.input fragments -
        # a JSON-text string built up piece by piece, exactly like Anthropic's
        # partial_json and OpenAI's function.arguments. Same accumulated shape
        # lands in tool_call for main.py, whichever provider answered.
        start = event.get("contentBlockStart", {}).get("start", {}).get("toolUse")
        if start and tool_call is not None:
            tool_call["id"] = start.get("toolUseId")
            tool_call["name"] = start.get("name")
            tool_call["arguments"] = ""
            if on_call_delta:
                on_call_delta((start.get("name") or "") + "(")
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if text:
            yield text
        use = delta.get("toolUse")
        if use and tool_call is not None:
            partial = use.get("input")
            if partial:
                tool_call["arguments"] = tool_call.get("arguments", "") + partial
                if on_call_delta:
                    on_call_delta(partial)

    # Bedrock's qwen builds send a MALFORMED input fragment: the opening `{"`
    # is missing, so read_file's arguments arrive as
    #     path": "README.md"}
    # instead of {"path": "README.md"}. Measured off the raw converse_stream
    # events on qwen.qwen3-32b-v1:0 - the event itself is broken, nothing here
    # dropped it. Unrepaired it is a dead end rather than a bad turn: the
    # arguments don't parse, main.py's _parse_call asks for the call again,
    # and the model re-emits the identical broken fragment until the stuck-loop
    # breaker gives up.
    #
    # So: only when the accumulated text doesn't parse, and only when putting
    # those two characters back makes it parse, is it repaired. Anything else
    # is left exactly as it arrived for _parse_call to reject in the normal
    # way - a genuinely garbled call must still read as one.
    if tool_call and tool_call.get("arguments"):
        try:
            json.loads(tool_call["arguments"])
        except json.JSONDecodeError:
            patched = '{"' + tool_call["arguments"]
            try:
                json.loads(patched)
                tool_call["arguments"] = patched
            except json.JSONDecodeError:
                pass

    if on_call_delta and tool_call and tool_call.get("name"):
        on_call_delta(")")


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
                         base_url=None, key=None, setup=None):
    """base_url/key are accepted and ignored, so this is callable exactly like
    every other wire (see _wire_call). This one drives the Claude Code CLI,
    which owns its own login - there is no endpoint to point it at and no key
    to hand it, so both boxes on its card do nothing.

    tools/tool_call accepted, not used: the Agent SDK runs its OWN tool loop
    (see the options below, all of which switch that loop OFF) - fighting it
    with a second, custom tools schema would conflict rather than add to it.
    A stray ToolUseBlock is already logged and ignored further down for the
    same reason. Left on the old prompted-text path deliberately.

    Claude Code's own runtime, driven through the Agent SDK, signed in as the
    Claude subscription already logged in on this machine. Every other provider
    here is an HTTP API billed per token against a key in .env; this one spends
    the subscription's usage window instead and needs no key at all. It is also
    the only sanctioned way to spend a subscription from code - the alternative
    of lifting the OAuth token and calling api.anthropic.com with it is not.

    The SDK's whole purpose is to run an *agent*: its own tools, its own loop,
    its own system prompt. All of that is switched off below, because Uniagent
    is the agent - it runs its own loop and executes its own tools, and needs
    this to be nothing more than the plain text-completion endpoint every other
    function in this file talks to.

    Two things the other providers get that this one cannot, because the SDK
    exposes no way to ask for them: TEMPERATURE and STOP. The missing stop
    sequences are the one that costs something. They exist so generation halts
    at the first tool call instead of barrelling on inventing tool results, and
    without them this model does barrel on. It stays affordable only because
    _stream in main.py breaks at the first complete call - which closes this
    generator, which kills the CLI process mid-sentence. So the tail is cut,
    just a beat later than a stop sequence would cut it.
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
                            usage["input_tokens"] = u.get("input_tokens")
                            usage["output_tokens"] = u.get("output_tokens")
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


# --- Providers.
#
# There is no built-in provider, and nothing here creates one. Every provider
# is one object in .env's LLM_PROVIDERS - a name, a wire, a base URL, a key and
# an optional model list - and every one of them can be renamed, repointed,
# re-keyed or deleted from the settings page. The code below supplies WIRES
# (how to talk to a shape of API) and nothing else; the list of providers is
# whatever the user has put in .env, up to and including nothing at all.
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
# endpoint speaks, not the company running it. "openai" is the OpenAI
# /chat/completions shape, which is what nearly every third-party endpoint
# serves (OpenRouter, Groq, Together, xAI, Mistral, Ollama, vLLM, LM Studio).
# "deepseek" is that same wire plus DeepSeek's own two quirks - its leading
# "answer in English" system message and the reasoning_content round-trip its
# thinking models demand, see _deepseek - so a DeepSeek key wants this rather
# than plain "openai", which would 400 on deepseek-reasoner.
#
# bedrock and claude-subscription are wires like any other here, so they can be
# named, moved and deleted like any other provider. What they ignore is the key
# and the URL: bedrock signs with the AWS credentials on this machine (and
# reads its base URL box as a region), claude-subscription drives the Claude
# Code CLI, which owns its own login.
WIRES = {
    "openai": _openai,
    "deepseek": _deepseek,
    "anthropic": _anthropic,
    "gemini": _gemini,
    "bedrock": _bedrock,
    "claude-subscription": _claude_subscription,
    # Byte for byte the openai wire. It exists as a separate name only so that
    # provider_Request_Template.json has somewhere to say "this one is a server
    # address and nothing else" - a local model server takes no key, and a key
    # box on its form is a box that can only be filled in wrongly. Same
    # function, so nothing about the request differs.
    "local": _openai,
}

# Where each wire points when a provider names no base URL of its own. These
# MUST match the same-named defaults on the functions above. The two that
# authenticate without a URL have nothing to put here.
WIRE_DEFAULT_URL = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "bedrock": "",
    "claude-subscription": "",
    "local": LMSTUDIO_URL,
}

# Wires that don't authenticate with a key. A provider on one of these counts
# as configured on its own credentials rather than on anything typed into its
# card - see available().
KEYLESS_WIRES = frozenset({"bedrock", "claude-subscription", "local"})

# The picture a provider shows on the settings page when it hasn't been given
# one of its own. Keyed by wire for the same reason WIRE_MODELS is: a wire is
# the one thing about a provider that survives every edit made to it, so a
# card renamed from "deepseek" to "cheap-and-fast" keeps looking like what it
# actually talks to.
#
# These are paths, exactly like a provider's own "icon" - they name files in
# web/icons/ that the server hands out under /icons/. Anything on a wire not
# listed here falls through to the question mark, which is the honest answer:
# the openai wire is spoken by a dozen companies and guessing a logo for it
# would be wrong more often than right.
WIRE_ICONS = {
    "openai": "/icons/openai.svg",
    "deepseek": "/icons/deepseek.svg",
    "anthropic": "/icons/claude.svg",
    "claude-subscription": "/icons/claude.svg",
    "bedrock": "/icons/bedrock.svg",
}

UNKNOWN_ICON = "/icons/unknown.svg"

# --- Per-wire setup forms.
#
# Every provider gets two boxes: a base URL and an API key. That is the whole
# shape of nearly every wire here, so nearly every wire says nothing about it -
# openai, deepseek, anthropic and gemini are all "point it at a host, hand it a
# bearer token" and are deliberately absent from the file below.
#
# A wire that authenticates some OTHER way needs different boxes, and needs to
# say so somewhere the settings page can read: Bedrock signs with AWS
# credentials and has no URL or key at all, claude-subscription drives a CLI
# and needs to be told where that CLI is. provider_Request_Template.json is that
# somewhere - which fields, what each is called, what it means, and crucially
# the ENVIRONMENT VARIABLE NAME each one corresponds to.
#
# That name is doing real work. It is what the page labels the box with, so
# what you type into "AWS_REGION" is obviously the same thing as the
# AWS_REGION you might already export - and it is what provider_setting()
# falls back to reading out of the real environment when a provider leaves the
# box empty. So a machine with ~/.aws set up keeps working untouched, and a
# provider that fills the boxes in gets its own credentials instead.
TEMPLATE_FILE = Path(__file__).parent.parent / "provider_Request_Template.json"

_templates_cache = (None, None)     # (mtime, data)


def templates():
    """{wire: template} out of provider_Request_Template.json.

    Re-read whenever the file changes, cached otherwise: this is asked for on
    every settings page load and on every Bedrock call, and it is a file that
    changes about once a year.

    Never raises. A missing file means "every wire uses the default form",
    which is true and is the state most installs are in; a malformed one is
    reported through template_error() and otherwise treated the same, because
    a typo'd bracket must not stop every provider from working."""
    global _templates_cache, _template_error
    try:
        mtime = TEMPLATE_FILE.stat().st_mtime
    except OSError:
        _template_error = None
        return {}
    if _templates_cache[0] == mtime:
        return _templates_cache[1]
    try:
        data = json.loads(TEMPLATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _template_error = (TEMPLATE_FILE.name + " could not be read (" + str(e)
                           + ") - every wire is using the default form until it is fixed.")
        _templates_cache = (mtime, {})
        return {}
    _template_error = None
    clean = {}
    if isinstance(data, dict):
        for wire, entry in data.items():
            # "_readme" and anything else that isn't a wire this code speaks is
            # skipped rather than refused - the file is meant to be read by a
            # person, and notes in it are welcome.
            if wire in WIRES and isinstance(entry, dict):
                clean[wire] = entry
    _templates_cache = (mtime, clean)
    return clean


_template_error = None


def template_error():
    templates()
    return _template_error


def template_for(wire):
    """The setup form for `wire` - {} for the wires that use the default one."""
    return templates().get(wire, {})


def template_fields(wire):
    """The extra boxes `wire` asks for, each settled into a full dict so
    neither the page nor provider_setting() has to guess at missing keys. A
    field with no "env" is dropped: the variable name is the one thing that
    cannot be defaulted, since it is what the value is looked up by."""
    out = []
    for f in template_for(wire).get("fields") or []:
        if not isinstance(f, dict):
            continue
        env = str(f.get("env") or "").strip()
        if not env:
            continue
        out.append({
            "env": env,
            "label": str(f.get("label") or env),
            "help": str(f.get("help") or ""),
            "secret": bool(f.get("secret")),
            "required": bool(f.get("required")),
            "default": str(f.get("default") or ""),
            "placeholder": str(f.get("placeholder") or ""),
        })
    return out


def wants_key(wire):
    """Whether this wire's form has an API key box. Templates say key: false
    for the wires that don't authenticate with one; everything else does."""
    return template_for(wire).get("key", True) is not False


def base_url_label(wire):
    """What this wire calls its base-URL box, or None when it hasn't got one.

    The counterpart of wants_key. A template says base_url: false for the wires
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
    falling back to the wire's own name when no template names it."""
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
    if not _NAME_OK.match(name) or wire not in WIRES:
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
    return p.get("icon") or WIRE_ICONS.get(p["wire"]) or UNKNOWN_ICON


def custom_base_url(p):
    """Where `p` actually points - its own base URL, or its wire's default
    host when it named none."""
    return p["base_url"] or WIRE_DEFAULT_URL[p["wire"]]


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
    if wire not in WIRES:
        raise ValueError("unknown wire " + repr(wire) + " - it must be one of: "
                         + ", ".join(sorted(WIRES)))
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
        CUSTOM_FILE.write_text(json.dumps(data, indent=2) + "\n")


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


# The wire functions that take a `setup` argument - the ones with a form in
# provider_Request_Template.json to fill it from. Kept as an explicit set rather
# than read off the template file, because it describes these functions'
# SIGNATURES: a wire listed here that doesn't accept setup= would raise on
# every call, and that is a code fact, not a configuration one.
SETUP_WIRES = frozenset({"bedrock", "claude-subscription"})


def _custom_call(p):
    """`p`'s provider function: its wire's, with the base URL and the key
    baked in. The key goes over as a string even when it is empty, which is
    what tells the wire function to send no auth header rather than reach for
    a key in .env that this provider never claimed.

    The wires with a setup form also get it resolved and baked in, so a
    Bedrock provider carrying its own AWS credentials calls with those and one
    carrying none falls through to the machine's."""
    call = functools.partial(WIRES[p["wire"]],
                             base_url=custom_base_url(p), key=custom_key(p))
    if p["wire"] in SETUP_WIRES:
        call = functools.partial(call, setup=resolved_setup(p))
    return call


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

# Suggestions ONLY - never a whitelist. Nothing rejects a model for being
# absent from here: the settings page's model box is free text with these as
# autocomplete hints, and the model is passed to the provider exactly as
# typed. So a model released after this was written works with no code change,
# which is the point - these lists cannot be kept complete (nobody can list
# every OpenAI or Bedrock model) and must not be treated as if they were.
#
# Keyed by WIRE and by nothing else. It used to be keyed by provider name,
# which quietly made a handful of names privileged: a provider called
# "openai" inherited this list and had its own model list shadowed by it,
# while renaming that provider to "openai-work" took the list away. A wire is
# a property of the object that no rename can touch, so keying on it is what
# makes a name genuinely free to change - see floor_models().
#
# ORDER MATTERS in one narrow way: the first entry is a provider's default -
# what cron falls back to when a job names a provider but no model, and what
# a blank model setting is filled in with. Add below the first entry unless
# you mean to change that default.
WIRE_MODELS = {
    "anthropic": [
        "claude-opus-4-8",
        "claude-fable-5",
        "claude-sonnet-5",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "o3",
        "o3-mini",
        "o4-mini",
    ],
    "deepseek": [
        "deepseek-v4-flash",
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "gemini": [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ],
    # Bedrock takes inference-profile ids, which depend on what's enabled in
    # the account and region (BEDROCK_REGION above), so this is only the two
    # known to work rather than a guess at the full catalogue.
    "bedrock": [
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ],
    # The Claude Code CLI takes these aliases as well as full model ids, and
    # the aliases are the better default: they follow whatever the
    # subscription currently entitles you to without this list going stale.
    # sonnet leads deliberately - a subscription pays in usage window rather
    # than per token, and opus empties that window several times faster.
    "claude-subscription": [
        "sonnet",
        "opus",
        "haiku",
    ],
}


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
    """A custom provider's own catalogue, asked for the way its wire asks.
    Nearly every OpenAI-compatible endpoint serves /models, which is what
    makes adding one a matter of a URL and a key rather than also typing out
    every model id by hand - the object's own "models" list is the fallback
    for the few that don't."""
    base = custom_base_url(p).rstrip("/")
    key = custom_key(p)
    if p["wire"] in ("openai", "deepseek", "local"):
        r = requests.get(base + "/models", headers=_bearer(key), timeout=5)
        _check(r)
        return _filter_chat(sorted(m["id"] for m in r.json().get("data", [])))
    if p["wire"] == "anthropic":
        r = requests.get(base + "/v1/models",
                         headers={"x-api-key": key,
                                  "anthropic-version": "2023-06-01"}, timeout=5)
        _check(r)
        return [m["id"] for m in r.json().get("data", [])]
    r = requests.get(base + "/models", headers={"x-goog-api-key": key}, timeout=5)
    _check(r)
    return [m["name"].removeprefix("models/") for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]


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
        data = json.loads(CUSTOM_FILE.read_text())
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
        CUSTOM_FILE.write_text(json.dumps(data, indent=2) + "\n")


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

    Asked of the provider first, then of its WIRE (_window_on_wire), and that
    second half is what makes this survive the settings page. A provider's
    name is a label a person typed and can change at any moment; its wire is
    what it actually is. Rename "deepseek" to "test" and the card is still a
    deepseek endpoint serving deepseek-v4-flash, with the same tokenizer and
    the same million-token window - so the window has to be found by what the
    provider IS, not by what it is currently called."""
    value = model_config(name, model).get("context_window")
    if not isinstance(value, int):
        value = _window_on_wire(wire_of(name), model)
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


def floor_models(name):
    """Provider `name`'s starting model list. The head entry is that
    provider's default model, so order matters.

    Its own "models" first - a list typed on its card is the user saying what
    this endpoint serves, and it outranks anything guessed here. Then the
    WIRE_MODELS suggestions for its wire, but only when it still points at
    that wire's own host: the openai wire is what OpenRouter, Groq, vLLM and
    LM Studio all speak, and offering gpt-4o to a laptop running LM Studio
    would be noise. Those endpoints have live catalogues, which known_models()
    asks for and which are the real answer for them."""
    p = custom_provider(name)
    if not p:
        return []
    out = list(p["models"])
    if not p["base_url"] or p["base_url"] == WIRE_DEFAULT_URL.get(p["wire"]):
        for m in WIRE_MODELS.get(p["wire"], []):
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
        WIRE_DEFAULT_URL.get(p["wire"], "").rstrip("/")


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
TTS_WIRES = frozenset({"openai", "deepseek", "local", "gemini"})

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

# Which voice reads it. Not a setting: the voice tab is a provider and a model
# and nothing else, and every endpoint here has a working default anyway - this
# just has to be a name each one recognises. OpenAI takes it as a plain voice
# id, Gemini as a prebuilt voice name.
TTS_VOICE = "alloy"
TTS_VOICE_GEMINI = "Kore"

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


def _speak_openai(p, model, text):
    """OpenAI's /audio/speech, which is the shape every endpoint serving TTS
    copies. Asks for mp3 because that is the one format every browser plays and
    the smallest thing to send to a phone."""
    r = requests.post(custom_base_url(p).rstrip("/") + "/audio/speech",
                      headers=_bearer(custom_key(p)),
                      json={"model": model, "input": text, "voice": TTS_VOICE,
                            "response_format": "mp3"},
                      timeout=TTS_TIMEOUT)
    _check(r)
    return r.content, "audio/mpeg"


def _speak_gemini(p, model, text):
    """Gemini has no speech endpoint - audio comes back from an ordinary
    generateContent turn asked to answer in sound instead of words.

    What arrives is headerless 16-bit PCM, which no browser will play on its
    own, so it is wrapped in a WAV container here. The sample rate is read off
    the part's own mime type ("audio/L16;codec=pcm;rate=24000") rather than
    assumed, since that is the one part of it Google has ever varied."""
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig":
                             {"voiceName": TTS_VOICE_GEMINI}}},
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


def speak(name, model, text):
    """`text` read aloud by provider `name` on `model`, as (audio bytes, mime).

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
        return _speak_gemini(p, model, said)
    return _speak_openai(p, model, said)


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
    return False


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


def options_text():
    """The providers usable right now, as a short block for a tool's
    instructions or a rejection message. The one place this is spelled out for
    the model - cron and subagent both use it, so there's no second copy to
    drift. Recomputed on each call, so it can't go stale.

    Deliberately lists providers, not models. Only the provider is checked
    (against this list); the model is a free string passed straight through,
    so the model picks any model that provider offers from its own knowledge
    rather than from a hardcoded list that can never be complete. Each
    provider's default is shown for the case where it doesn't care which."""
    lines = []
    for name in available():
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
            lines = ENV_FILE.read_text().splitlines()
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
            lines = ENV_FILE.read_text().splitlines()
        except OSError:
            lines = []
        lines = [line for line in lines if not line.strip().startswith(name + "=")]
        if value:
            lines.append(name + "=" + value)
        ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))
        # Secrets: readable by this user and nobody else. A fresh file would
        # otherwise be born world-readable under the default umask.
        try:
            ENV_FILE.chmod(0o600)
        except OSError:
            pass


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
    - token counts, a native tool call, a thinking model's reasoning_content."""
    for piece in call(model, prompt, temperature, usage=usage, tools=tools,
                      tool_call=tool_call, reasoning=reasoning,
                      on_call_delta=on_call_delta):
        turnctx.check()
        yield piece
    turnctx.check()


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
    provider's own structured tool call ({"id", "name", "arguments"}) as it
    streams in, the same way `usage` is. Both are None by default, which is
    exactly today's behaviour: no tools sent, no native call parsed.

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
