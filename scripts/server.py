"""The web front-end: serves web/index.html and bridges it to main.py.

    python3 server.py

ONE endpoint takes all user input (POST /input). A line starting with "/" is a
command - command_processor answers it directly and the model never sees it.
Anything else is a turn of the CURRENT chat, run on a worker thread while
GET /stream carries the reply to the page as it's written, the same way the
terminal shows it arriving. GET /history hands the page the transcript to
draw on open, and again after each turn, so what's shown is always what's
actually in the history.

Chats run independently: every stream event is tagged with the chat it belongs
to, and the page shows only the loaded chat's - so one chat can work through a
tool loop or take a subagent's report in the background while another is
talked to. A report always lands in the chat that spawned the subagent.

The settings page is served from here too: GET/POST /settings for the model
choices, the look of the page and who transcribes speech, GET/POST /env for
every variable in .env (no tab draws these any more - each thing that used to
live there now belongs to the tab that owns it - but the routes stay, since
UNIAGENT_PASSWORD has nowhere else to be changed from), GET/POST /context to
read and
edit the prompt files, GET/POST /cron to read and edit cron.json, and POST
/restart to bring this process (and the cron watcher) back on new code.

HOST is "0.0.0.0" - reachable from any machine on the network, which is what
makes it work from a phone. Everything behind that is gated: every route except
the login page needs the session cookie auth.py issues, because what's back
there is a shell on this machine. See auth.py for the password itself, and
_serve_https() for why the app is https-only.
"""

import datetime
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import auth
import command_processor
import compaction
import main
import market
import provider
import provider_refs
import settings
import tokens
import tool_processor
import update
import workspace
import turnctx
import voice_input
# Not "from tools import _discovery": tools/ was never on sys.path as a
# package (there's no tools/__init__.py) - it's tool_processor's own import
# above that puts the tools/ DIRECTORY on sys.path, the same way every tool
# module reaches _discovery too (see read_skill.py).
import _discovery as _tool_discovery
# Same reason: tools/ is on sys.path by the time this runs, so the email
# helper is importable here by its bare name. The /email routes below are the
# ONLY way an account's password gets written - deliberately not a tool, so a
# password never has to be said to the model to be saved.
import _email

HOST = "0.0.0.0"
PORT = provider.port("UNIAGENT_PORT", 8763)

# Where the app actually lives. PORT above serves nothing but a redirect here
# (see _serve_redirect), because a password and a session cookie crossing the
# network in the clear would undo the point of having them - and because a
# browser only hands a page the microphone on a "secure origin", so hold-to-talk
# never worked over plain http://<lan-ip> anyway.
#
# The certificate is self-signed and generated here on first run, because there
# is no public domain to get a real one for and getting one would mean an
# account and a domain purchase. A browser therefore shows its "not private"
# interstitial the first time: click Advanced, then proceed, ONCE per device,
# and from then on it behaves like any https site. Chrome on Android does offer
# that click-through. Android's WebView does NOT - it refuses a self-signed cert
# outright - so the Capacitor app can't reach this without being rebuilt to
# trust the cert.
HTTPS_PORT = provider.port("UNIAGENT_HTTPS_PORT", 8764)
CERTS = Path(__file__).parent.parent / "certs"
CERT_FILE = CERTS / "uniagent.crt"
KEY_FILE = CERTS / "uniagent.key"
NAMES_FILE = CERTS / "names.txt"  # what the current cert was made to cover
CERT_DAYS = 3650  # long, so clicking through the warning is a once-ever thing

# How long a client gets to finish the TLS handshake, and then how long any one
# read or write on the connection may stall. Both are waited for on the
# connection's OWN thread (see TLSServer), so a client that hangs costs one
# thread rather than the server - but a port reachable from the internet
# collects enough half-open probes that they still need an upper bound.
# REQUEST_TIMEOUT is comfortably above the 15s keepalive _stream() sends, so a
# quiet event stream is never mistaken for a stalled one.
HANDSHAKE_TIMEOUT = 10
REQUEST_TIMEOUT = 30

# POST /voice: the biggest clip we'll take, and how a browser's Content-Type
# becomes the file extension Whisper needs (it identifies the format from the
# name, not the bytes). Which one you get depends on the browser - Chrome and
# Firefox record webm/opus, Safari mp4 - so all of them are listed rather than
# assuming one. 25MB is Whisper's own per-file limit; it's about 25 minutes of
# opus, far longer than anything you'd hold a button down for.
MAX_CLIP = 25 * 1024 * 1024
CLIP_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
            "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
            "audio/aac": "aac", "audio/flac": "flac"}

ROOT = Path(__file__).parent.parent
PAGE = ROOT / "web" / "index.html"
LOGIN_PAGE = ROOT / "web" / "login.html"
CRON_FILE = ROOT / "cron.json"

# The two routes that answer without a session, and the only two. Everything
# else - including GET / itself - is refused until the cookie is there, so the
# 66KB app page never reaches someone who hasn't got past the password.
OPEN_ROUTES = ("/login",)
COOKIE = "uniagent_session"

# Every open /stream connection has a queue here; broadcasting an event puts
# it on all of them, so a desktop window and a phone can watch the same turn.
_streams = []
_streams_lock = threading.Lock()


def _broadcast(event):
    data = json.dumps(event)
    with _streams_lock:
        for q in _streams:
            q.put(data)


# A chat id a client is allowed to CREATE by naming it: 'chat-' and eight hex
# digits, exactly what main.new_chat_id() mints, and nothing else. Loading is
# looser (a cron job is 'cron/<name>'), but that path requires the chat to
# already exist. Creating does not, so the shape is the only thing standing
# between "name a chat that isn't there yet" and "write a folder into
# chats/subagents/ or chats/deleted_chats/ and confuse everything that reads
# them".
_NEW_CHAT_ID = re.compile(r"chat-[0-9a-f]{8}")


# The chat the last message from a browser went to. Not "the current chat" -
# nothing is drawn from it and no request is answered against it. It exists for
# one caller: the hold-to-talk key, which is a physical key on the machine
# running this and so has no window behind it to say which chat it means. "The
# one you were last typing into" is the honest answer for it; before chats were
# per window, it got that by reading the global everything else used too.
_last_input_chat = None
_last_input_lock = threading.Lock()


def _voice_chat():
    """Where a clip from the hold-to-talk key goes: the chat last spoken to
    from a browser, or the terminal's if this process hasn't seen one yet."""
    with _last_input_lock:
        c = _last_input_chat
    return c if c is not None else main.current


def _chat_named(cid, create=False):
    """The Agent for a chat id off the wire, or None if it names nothing real.

    Every route that used to mean "whatever chat the server has loaded" now
    takes its chat from the request, because there is no server-wide loaded
    chat any more - each browser window carries its own and sends it. This is
    the one place that turns what a client said into an Agent, so it is also
    the one place that has to assume the id is hostile: the slug is checked,
    the path is built here rather than taken from anyone, and it must land
    inside chats/ or nothing comes back.

    `create` allows an id that has no folder yet - a chat the client minted
    (POST /new) and hasn't written to. It has to be allowed: a new chat exists
    only in the browser until its first message, which is exactly what stops
    every page load from leaving an empty chat on disk. It is also the only
    way a client can bring a folder into being, hence the tighter
    _NEW_CHAT_ID shape on top of the checks below."""
    cid = (cid or "").strip().removesuffix(".md").removesuffix(".json")
    # Up to three parts: an ordinary chat is one ('chat-1bb28a87'), one run of a
    # cron job is three ('cron/ai-brief/003').
    if not re.fullmatch(r"[\w-]+(/[\w-]+){0,2}", cid):
        return None
    path = main.chat_md(cid).resolve()
    if main.CHATS.resolve() not in path.parents:
        return None
    if not path.exists():
        if not (create and _NEW_CHAT_ID.fullmatch(cid)):
            return None
    return main.chat(path)


def _stem_of(named):
    """The flat chat id behind a route id off the wire, or None if it isn't a
    valid one. 'cron/ai-brief/003' -> 'ai-brief-003', 'chat-1bb28a87' -> itself.

    For the two routes that key off files named after the FLAT id - the
    validation logs and the subagent transcript folders - while the page names
    every chat by its route. Those used to take the flat id only, so a cron
    chat's request came in as 'cron/ai-brief', failed the slug check and 404'd:
    the safety rows silently never appeared, because the page treats a failed
    fetch as "this chat has no log". The slug check stays - this is still a
    filename being built from something a client said."""
    named = (named or "").strip().removesuffix(".md").removesuffix(".json")
    if not re.fullmatch(r"[\w-]+(/[\w-]+){0,2}", named):
        return None
    return main.chat_id(main.chat_md(named))


def _chat_of(handler, create=False, mint=False):
    """The chat a request is about, read off its ?chat= parameter.

    Three cases, and the difference between the first two matters:

      no ?chat= at all   the terminal's chat (main.current). Input that
                         arrives with no window behind it to say where it
                         belongs - the hold-to-talk key, and nothing else.
      ?chat= (empty)     a window that hasn't got a chat yet. With `mint` it
                         gets a fresh one, which is how the first message of a
                         new conversation costs no extra round trip and writes
                         no empty folder in advance - the id comes back in the
                         reply. Without it, None: reading the history or status
                         of a chat that doesn't exist yet is simply nothing,
                         and minting one per poll would fill main._open with
                         Agents nobody ever talks to.
      ?chat=<id>         that chat, or None if it names nothing real.

    `create` is different from `mint`: it allows an id the client already holds
    but hasn't written to yet, rather than inventing a new one.
    """
    # keep_blank_values, because "?chat=" with nothing after it is a real case
    # here and parse_qs drops blank values by default - without it "I have no
    # chat yet" would be indistinguishable from "didn't say", and the first
    # message of every new conversation would land in the terminal's chat.
    q = parse_qs(urlparse(handler.path).query, keep_blank_values=True)
    if "chat" not in q:
        return main.current
    named = q.get("chat", [""])[0]
    if not named:
        return main.chat(main.chat_md(main.new_chat_id())) if mint else None
    return _chat_named(named, create=create)


def _asks_blank(handler):
    """Whether this request says "I have no chat yet" - ?chat= with nothing
    after it, the middle case of _chat_of above. Both that and an id naming
    nothing real come back as None from _chat_of, and the two want different
    answers on /injection: a window in a brand-new conversation has a real
    context to show (the injection every chat starts with), a made-up id has
    nothing."""
    q = parse_qs(urlparse(handler.path).query, keep_blank_values=True)
    return "chat" in q and not q["chat"][0]


# The stand-in for a chat that doesn't exist yet, so a new conversation's
# panel can show what it would run with. Deliberately NOT main.chat(): that
# registers the agent, and a page polling with no chat would fill main._open
# with agents nobody ever talks to. Nothing is written for it either - see
# context_usage(record=False) in _injection - so a window sitting on a new
# chat still leaves no folder behind.
_blank_chat_agent = None
_blank_chat_lock = threading.Lock()


def _blank_chat():
    global _blank_chat_agent
    with _blank_chat_lock:
        if _blank_chat_agent is None:
            _blank_chat_agent = main.Chat(main.chat_md(main.new_chat_id()))
        return _blank_chat_agent


# The right-hand context panel is push-driven: nothing on the page asks "has
# anything changed yet?" on a timer. These are the two "it changed" signals,
# broadcast at the moment the change actually happens - a pin lands, a turn
# ends, a tool file is written. They carry no payload on purpose: resolving an
# injection re-reads every context file and comes to tens of KB, so pushing it
# unasked to every connected page would be the old poll in a different coat.
# The page answers a signal by fetching the one thing that moved (GET
# /injection, GET /tools), and only when the panel is actually open.
def _broadcast_context(chat=None):
    # Tagged with the chat it describes, so a page can tell "this chat changed"
    # from "something moved in a chat I'm not looking at" - and ignore the
    # second, which since every window sits in its own chat is now the common
    # case rather than the rare one. Untagged (chat=None) means "this affects
    # every chat" - a context file saved, a tool added, the settings model
    # changed - and every panel redraws.
    #
    # .route, not .id: the page compares this against the id IT holds, and for
    # a cron job those are two different strings (see main.chat_route).
    _broadcast({"type": "context", "chat": chat.route if chat else None})


def _broadcast_tools():
    _broadcast({"type": "tools"})


def _broadcast_chats():
    """Tell every page the chat LIST moved - a chat appeared, was deleted,
    renamed, or started/stopped working.

    This replaces the sidebar's old two-second poll of GET /chats, which was
    by far the most expensive thing the server did: building that list reads
    and JSON-parses every chat's history to find its label, so with 500 chats
    on disk it was ~22MB of reading and an 82KB response EVERY TWO SECONDS,
    per open page, almost always to conclude nothing had changed. The list
    genuinely changes a handful of times an hour, so it is sent as a signal at
    those moments instead, and the page fetches only then - the same
    push-not-poll shape the context panel already uses above."""
    _broadcast({"type": "chats"})


# How often to look for chat-list changes this process didn't make. Only cron
# does that: the watcher is a SEPARATE process (see cron.py), so its jobs'
# chats appear and grow with nothing here to notice it and say so.
_CHATS_WATCH_SECONDS = 30


def _chats_signature():
    """Something cheap that changes whenever the chat list would look
    different: how many chats there are and the newest mtime among them.

    stat() only - no reading, no parsing. Over ~500 chats this is about 2ms,
    against ~100ms and 22MB of reading to build the list itself, which is
    exactly why the list is fetched on a signal and this is what watches for
    one."""
    try:
        files = list(main.CHATS.glob("chat-*/" + main.HISTORY_FILE)) \
            + list((main.CHATS / "cron").glob("*/*/" + main.HISTORY_FILE))
        return len(files), max((p.stat().st_mtime for p in files), default=0)
    except OSError:
        return None


def _watch_chats():
    """Broadcast a chats signal when the list changes underneath us.

    Everything the web front-end does to the list says so directly (see
    _broadcast_chats's callers); this is only here for the cron watcher, which
    is another process entirely and can't. Half a minute of staleness on a
    scheduled job's row is fine - it's a job nobody is sitting and watching -
    and this costs a couple of milliseconds to check."""
    last = _chats_signature()
    while True:
        time.sleep(_CHATS_WATCH_SECONDS)
        now = _chats_signature()
        if now is not None and now != last:
            last = now
            _broadcast_chats()


# A network tokenizer (anthropic, gemini) answers on tokens.py's background
# worker, a moment after the panel has already drawn a chars/4 approximation
# for that segment. That used to firm up on "whichever poll comes next"; with
# no poll, the worker says so itself (tokens.on_settled, wired up in serve()).
# Counts land one per segment, in bursts, so they're coalesced into a single
# redraw instead of one per segment.
_settle_timer = None
_settle_lock = threading.Lock()
_SETTLE_DEBOUNCE = 0.5


def _on_tokens_settled():
    global _settle_timer
    with _settle_lock:
        if _settle_timer is not None:
            _settle_timer.cancel()
        _settle_timer = threading.Timer(_SETTLE_DEBOUNCE, _broadcast_context)
        _settle_timer.daemon = True
        _settle_timer.start()


def _approve(question):
    """The safety gate, web-shaped: send the page the question (drawn as an
    approve/deny bubble), then block this worker thread until it's answered -
    by a button, /approve, or the user sending a new message, which denies it
    and moves on. Tagged with the chat whose turn hit the gate."""
    # turn_chat() is the chat's FILE, not the chat object - chat_id() turns it
    # into the id everything here keys off. Reading .id off the path was an
    # AttributeError, which meant a safety-flagged call failed the whole turn
    # instead of asking for approval.
    #
    # Two ids, and they differ for a cron job: the QUESTION goes out under the
    # route, because the page answers it by POSTing /input?chat=<what it was
    # told>, and a bare 'ai-brief' names no chat the server can find. The
    # answer is waited for under the bare id, which is what command_processor
    # (and every other per-chat register in here) keys off.
    path = main.turn_chat()
    stem = main.chat_id(path)
    _broadcast({"type": "approval", "chat": main.chat_route(path), "text": question})
    return command_processor.wait_approval(stem, question)


def _run_turn(text, kind="user", target=None):
    """One turn of `target` on a worker thread, streamed to every open page
    tagged with its chat. Typed input arrives here from POST /input carrying
    the chat that browser window is in, spoken input from voice_input, subagent
    reports through main.notify. `kind` labels what the text is - "user" for
    something the user said, "report" for a subagent's note - so the page can
    draw them differently even though both enter the history the same way.

    `target` of None means the terminal's chat (main.current) - the fallback
    for input that arrives with no window behind it to say where it belongs,
    which is the hold-to-talk key and nothing else. Every web request names
    its chat.

    Turns of different chats run at the same time and always could: the lock
    is per Agent, not global, and nothing below reaches for a shared "current"
    - which is what makes prompting several chats in parallel work."""
    c = target or main.current
    # The bare id keys everything INSIDE this process (the stop set, the busy
    # list); the route is the id the page holds and filters events by, and for
    # a cron job the two are different strings. Every broadcast below carries
    # the route - tagged with the bare id, a cron chat's own window threw the
    # whole turn away as belonging to somebody else, so it streamed nothing and
    # never redrew at the end.
    stem = c.id
    route = c.route

    def stream_chunk(chunk):
        _broadcast({"type": "chunk", "text": chunk, "chat": route})

    def worker():
        try:
            main.turn(c, text,
                      on_text=stream_chunk,
                      on_tool_call=lambda shown: _broadcast({"type": "toolcall",
                                                        "text": shown, "chat": route}),
                      on_tool_result=lambda result: _broadcast({"type": "toolresult",
                                                        "text": result, "chat": route}),
                      # `checked` False is the gate being OFF for this chat, not
                      # a verdict - the page draws that row differently.
                      on_safety=lambda safe, reason, checked=True: _broadcast(
                                                       {"type": "safety",
                                                        "safe": safe, "reason": reason,
                                                        "checked": checked,
                                                        "chat": route}),
                      approve=_approve)
        except Exception as e:
            if turnctx.cancelled():
                return  # stopped mid-flight; the finally below stays quiet too
            # The provider's own message is the part worth reading (which
            # model id or parameter it rejected, an auth failure...). Without
            # this the exception died with the worker thread and the page saw
            # a turn that just ended with no reply at all.
            msg = type(e).__name__ + ": " + str(e)
            # Into the HISTORY, not just the live stream: the post-turn redraw
            # and any reload rebuild the transcript from the history, so an
            # error kept anywhere else vanishes seconds after it appears - an
            # error nobody can read can't be fixed. The model sees it on the
            # next turn too, which is simply accurate. append_error() files it
            # as a real turn in the JSON, not raw text concatenated onto the
            # end of it - that used to corrupt the history so the next turn's
            # parse failed and silently discarded everything before it.
            main.append_error(c, msg)
            _broadcast({"type": "error", "chat": route, "text": msg})
        finally:
            # Nothing from a turn that /stop abandoned. That turn was already
            # announced as over the moment it was stopped (see
            # _on_stop_callback), and the chat has very likely started a NEW
            # turn since - the queued message that went out on the back of it.
            # A second "done" arriving here would tell the page that turn had
            # finished too, wiping a live reply off the screen until its own
            # done landed. Whenever this thread finally unwinds, it does so
            # silently.
            if turnctx.cancelled():
                return
            # Always tell the page the turn is over, even if the provider blew
            # up - otherwise the input would look stuck mid-reply forever.
            _broadcast({"type": "done", "chat": route})
            # A turn moves the token count and may have read files into the
            # conversation (read_skill), so the context panel is stale now.
            _broadcast_context(c)
            # And the sidebar: this chat's busy dot goes out, its position in
            # the by-recency order has changed, and if this was its first turn
            # it only just appeared on disk at all.
            _broadcast_chats()

    _broadcast({"type": kind, "text": text, "chat": route})
    # Before the work starts, so the busy dot lights immediately and a chat
    # being talked to for the first time shows up in the list right away.
    _broadcast_chats()
    threading.Thread(target=worker, daemon=True).start()


def _status(c):
    """Who is working right now, from the point of view of chat `c`: its own
    turn and live subagents (worker threads named subagent-<chat>/<name>, the
    same registry subagent.py itself scans), plus which OTHER chats are
    mid-turn - the page shows those as background work.

    Asked per chat rather than about "the loaded chat", because there isn't
    one: two windows in two different chats each ask about their own and get
    different answers, and `background` is what tells each of them the other
    is busy.

    `compacting` says WHICH of those the main agent is doing. A /compact holds
    the chat's lock exactly as a turn does, so it already shows up in `main` -
    but nothing streams back while it runs, and a silent "main agent working"
    bar for a long wait with no reply behind it is precisely what made a
    compaction look like nothing was happening at all."""
    busy = main.busy_chats()
    if c is None:
        # A window whose chat doesn't exist yet: nothing of its own can be
        # running, but it still wants to know what else is, and which model a
        # message typed now would go to (the settings default, since a chat
        # that isn't there can't have pinned one).
        #
        # No "tokens" key here on purpose, rather than an empty one: a chat
        # that doesn't exist has nothing recorded, but its panel may well have
        # counted and drawn what a message sent now WOULD cost (see
        # /injection's blank-chat case). An empty block would wipe that back to
        # "-" every couple of seconds, forever.
        default = settings.load()
        return {"chat": None, "main": False, "subagents": [],
                "compacting": False, "background": busy,
                "provider": default["provider"], "model": default["model"],
                "temperature": default["temperature"],
                "pinned": False, "temperature_pinned": False,
                "approval": None}
    cur = c.id
    tag = "subagent-" + cur + "/"
    subs = [t.name[len(tag):] for t in threading.enumerate()
            if t.name.startswith(tag)]
    # This chat's own model (its settings, or the default it follows), so the
    # corner switcher shows what THIS chat runs on, not a global setting.
    prov, mod, temp = c.models()
    # "chat" is the ROUTE, because the page matches it against the id it holds
    # and against the chat an approval bubble came from; `busy` is bare ids, so
    # the background list is mapped over to match (see main.route_of).
    return {"chat": c.route, "main": cur in busy, "subagents": sorted(subs),
            "compacting": compaction.is_compacting(cur),
            "background": [main.route_of(b) for b in busy if b != cur],
            "provider": prov, "model": mod, "temperature": temp,
            "pinned": bool(c.provider or c.model),
            "temperature_pinned": c.temperature is not None,
            "approval": command_processor.pending_question(cur),
            # Which workspace THIS chat's tools work in. Rides along here for
            # the same reason the model does: the corner dropdown has to show
            # the chat you just opened, and this is the request the page
            # already makes the instant you switch chats.
            "workspace": c.workspace or "",
            # THIS chat's token count, read off its settings .json - no
            # injection resolved, no tokenizer run (see main.stored_usage).
            # It rides along here because this is the request the page already
            # makes the instant you switch chats, so the bar shows the chat
            # you just opened rather than the one you left, without waiting on
            # the panel's much heavier /injection fetch and without a round
            # trip of its own.
            "tokens": main.stored_usage(c, prov, mod)}


def _tools():
    """Every tool and skill the folder knows about, scanned fresh from tools/
    and its subdirectories - one {name, description, type, loaded, path}
    object per working tool module (.py), each skill (.md), AND each .py file
    that exists but failed to import, so the sidebar can show it as present-
    but-not-loaded rather than silently vanishing it. Alphabetical by name.

    `path` is relative to tools/ and is what /context/pin takes to copy that
    tool or skill's file into context/ - a real .py module carries its own
    __file__, a skill's stand-in namespace carries PATH (see
    tool_processor._read_skill and _discovery.others())."""
    mods, broken = _tool_discovery.others()
    tools = []
    for m in mods:
        if hasattr(m, "__file__") and m.__file__:
            path = Path(m.__file__).resolve().relative_to(
                tool_processor.TOOLS_DIR.resolve()).as_posix()
        else:
            path = getattr(m, "PATH", None)
        tools.append({
            "name": m.NAME,
            "description": m.DESCRIPTION,
            "type": "skill" if hasattr(m, "INSTRUCTIONS") and not hasattr(m, "run") else "tool",
            "loaded": True,
            "path": path,
        })
    for b in broken:
        # "found_name.py (ErrorType: message)" - see _discovery.others().
        name, _, reason = b.partition(" (")
        tools.append({
            "name": name.removesuffix(".py"),
            "description": reason[:-1] if reason.endswith(")") else reason,
            "type": "tool",
            "loaded": False,
            "path": name,
        })
    tools.sort(key=lambda t: t["name"])
    return tools


_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _injection(c, record=True):
    """What's actually in chat `c`'s context right now: the static injection
    breakdown (models_custom.json's list for this model, resolved into
    labelled pieces - context files, memories, tool list/syntax) PLUS this
    specific chat's own pinned tools/skills (c.pinned - see Agent.add_pinned,
    scoped to this one chat only, never shared with any other), any read_skill
    reads that entered the conversation dynamically mid-chat, and this chat's
    token usage against the model's configured context window. What the
    right-hand context panel draws - NOT the same thing as /context, which is
    the settings page's context/ *.md file editor.

    The token block always has a number in it, including for a chat that has
    never run a turn - see main.context_usage(), which counts what's injected
    when no provider has reported anything yet."""
    prov, mod, _ = c.models()
    # Built once and handed to both readers - resolving it re-reads every
    # context file, so doing it twice per request would double the cost of
    # the most expensive thing this endpoint does.
    injected = main.injection_breakdown(prov, mod, c.pinned)
    return {
        "provider": prov, "model": mod,
        "injected": injected,
        "dynamic": main.dynamic_reads(c.history),
        "tokens": main.context_usage(c, prov, mod, injected, record),
    }


def _label_from(path):
    """A chat's sidebar label: the first thing the human actually typed in it,
    trimmed to 80 chars, or "" for a chat that has none yet."""
    try:
        turns = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(turns, list):
        return ""
    for t in turns:
        if not isinstance(t, dict) or t.get("role") != "user":
            continue
        content = t.get("content") or ""
        # Three kinds of user turn nobody typed: a subagent's report, a tool
        # result kept as one, and a note about the chat itself (a workspace
        # move). None of them is what this chat is about.
        if content and not content.startswith("Subagent ") \
                and not content.startswith("Tool result: ") \
                and not content.startswith(main.WORKSPACE_NOTE):
            return content[:80]
    return ""


def _chat_row(p, busy, threads, label=None):
    """One chat's row for the chats panel, from its history.json path.

    `name` is the id /load is given ('cron/ai-brief/003' for a cron run);
    `stem` is the flat id, which is how subagent folders and the busy list are
    keyed. The page needs both and must not derive one from the other."""
    cid = main.chat_id(p)
    if label is None:
        try:
            cfg = json.loads((p.parent / main.SETTINGS_FILE).read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        # A chat named in its settings shows that, rather than its first line -
        # a deliberate title wins over a guessed one. Otherwise the first
        # genuine human message: a "user" turn can also be a subagent's report
        # or a retry/stop message main.py writes itself (see web/index.html's
        # own rendering, which draws those two differently from a real user
        # line), so _label_from skips past those the same way it does.
        label = cfg["name"][:80] if cfg.get("name") else _label_from(p)
    folder = main.CHATS / "subagents" / cid
    subs = sorted(f.stem for f in folder.glob("*.md")) if folder.is_dir() else []
    # `detail` is the small second line on a row. None means "show the id",
    # which is what an ordinary chat wants; a cron job says when its current
    # run fired instead, since its id is a folder number nobody can date.
    return {"name": main.chat_route(p), "stem": cid, "label": label,
            "busy": cid in busy or any(t.startswith("subagent-" + cid + "/")
                                       for t in threads),
            "cron": False, "detail": None, "subagents": subs}


def _cron_rows(busy, threads):
    """One row per cron JOB, newest run first within each - what the page draws
    under its "cron" group.

    A job is a series of chats, one per run (see cron.py's new_run), and the
    row IS its newest run: that is the chat holding the last run and everything
    said since, so clicking the job opens the conversation you would expect to
    carry on. The runs before it go in `history`, oldest last, drawn as a
    collapsed list under the job the way subagents already are - still real
    chats, still openable, just not the one you land on.

    A job whose folder has no runs at all is skipped rather than drawn empty:
    the watcher gives every job in cron.json a chat within 30 seconds of it being
    written, so there is nothing to say about one that hasn't got there yet."""
    out = []
    for folder in sorted((main.CHATS / "cron").glob("*/")):
        found = sorted(p.parent for p in folder.glob("*/" + main.HISTORY_FILE))
        if not found:
            continue
        # The job's name labels its current run - the prompt text is the same
        # every run, so repeating it as a label says nothing.
        row = _chat_row(found[-1] / main.HISTORY_FILE, busy, threads,
                        label=folder.name)
        row["cron"] = True
        row["job"] = folder.name
        started = _started(found[-1])
        row["detail"] = ("last run " + _when(started)) if started else "not run yet"
        row["history"] = [
            _chat_row(p / main.HISTORY_FILE, busy, threads,
                      label=_run_label(p, n))
            for n, p in reversed(list(enumerate(found[:-1], 1)))]
        out.append(row)
    return out


def _started(folder):
    """When the run in `folder` fired, as it was written (see cron.new_run), or
    None if it isn't recorded."""
    try:
        return json.loads((folder / main.SETTINGS_FILE).read_text()).get("started")
    except (OSError, json.JSONDecodeError):
        return None


def _when(started):
    """A run's trigger time, short enough for a sidebar row: '31 Jul 08:00'.
    The year is added only when it isn't this one, so the common case stays
    short and an old run is still unambiguous. Stored as '%Y-%m-%d %H:%M', or
    date-only for a run whose recorded time was only ever a date - anything
    that doesn't parse is shown as it was written rather than guessed at."""
    for fmt, day, full in (("%Y-%m-%d %H:%M", "%-d %b %H:%M", "%-d %b %Y, %H:%M"),
                           ("%Y-%m-%d", "%-d %b", "%-d %b %Y")):
        try:
            when = datetime.datetime.strptime(started, fmt)
        except (ValueError, TypeError):
            continue
        return when.strftime(day if when.year == datetime.date.today().year else full)
    return started


def _run_label(folder, n):
    """What one of a job's earlier runs is called in the history list: when it
    fired, which is the only thing that tells two runs of the same job apart.
    Runs from before that was recorded (and any whose settings went missing)
    fall back to their number."""
    started = _started(folder)
    return _when(started) if started else ("run " + str(n))


def _chats():
    """Every chat, newest first, with its subagent children and whether it is
    working right now - what the page's chats panel draws.

    Says nothing about which chat is "current", because that is now a property
    of each browser window rather than of the server - the page marks its own
    row from the chat it is showing. That also makes this response identical
    for every client, which is what lets the page skip a redraw when the
    payload it just fetched matches the one it drew last."""
    busy = set(main.busy_chats())
    threads = [t.name for t in threading.enumerate()]
    # Each chat is a folder holding history.json; a cron RUN is two levels
    # further down, in chats/cron/<job>/<nnn>/, and is grouped by job rather
    # than listed loose - see _cron_rows.
    out = _cron_rows(busy, threads)
    files = sorted(main.CHATS.glob("chat-*/" + main.HISTORY_FILE),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    out += [_chat_row(p, busy, threads) for p in files]
    # A chat that has been minted but never written to isn't here, and that is
    # deliberate: it exists only in the window that minted it (see
    # main.new_chat_id), and that window draws its own row for it. Listing it
    # would mean showing every other window a chat they can't see the point of.
    return out


def _subagent_file(query):
    """The transcript path a /subagent?chat=..&name=.. request asks for, or
    None. Both parts must be plain slugs, so the path can't escape chats/."""
    q = parse_qs(query)
    chat = _stem_of(q.get("chat", [""])[0])
    name = q.get("name", [""])[0]
    if chat is None or not re.fullmatch(r"[\w-]+", name):
        return None
    path = main.CHATS / "subagents" / chat / (name + ".md")
    return path if path.exists() else None


def _image_type(head):
    """The MIME type the first bytes of a file actually say it is, or None.

    Content, never the extension. This is the ONLY thing standing between
    /image and "read any file on this machine", so it has to be the bytes:
    renaming id_rsa to key.png doesn't get it past this, and a .txt holding a
    real PNG is served happily because it genuinely is one.

    SVG is deliberately missing. It's XML that can carry <script>, it has no
    magic number to sniff being text, and this route answers same-origin - so
    a "picture" here could run script as the page itself. Raster only.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
    return None


def _image_file(query):
    """(path, mime) for the image /image?path=.. names, or None if that isn't
    an image - which is also the answer for "doesn't exist" and "can't be
    read", since none of those should tell a caller anything different.

    Any readable path on the machine is fair game (a relative one is taken
    against the Uniagent folder), because the point of this is that the agent
    can show the user a file wherever it happens to be - a render in a project
    dir, a screenshot in /tmp. What keeps it from being a general file-read
    hole is _image_type() above.

    Worth being clear-eyed though: like every other route here this is
    unauthenticated, and HOST is 0.0.0.0 (see the module docstring), so any
    image on this machine is fetchable by anyone on the network who can guess
    its path. That's the same trust boundary that already lets them drive the
    agent and rewrite its prompt, not a new one - but it is one more thing
    behind it.
    """
    raw = parse_qs(query).get("path", [""])[0]
    if not raw or "\x00" in raw:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        # resolve() first: the symlink's target is what actually gets read, so
        # it's the target that has to pass the sniff below.
        path = path.resolve()
        if not path.is_file():
            return None
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    mime = _image_type(head)
    return (path, mime) if mime else None


ICON_DIR = ROOT / "web" / "icons"

# What a bundled icon's filename may look like. Deliberately narrow, because
# these are the only SVGs this server hands out and the name is the ONLY thing
# that reaches the filesystem here - no dots beyond the extension, so no ".."
# and nothing to escape web/icons/ with.
_ICON_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.svg$")


def _bundled_icons():
    """The /icons/... paths this build ships, sorted. Read off disk rather
    than listed in code so dropping a new .svg into web/icons/ is all it takes
    to offer it in the picker."""
    try:
        return sorted("/icons/" + f.name for f in ICON_DIR.iterdir()
                      if _ICON_NAME.match(f.name))
    except OSError:
        return []


def _icon_file(name):
    """The bundled icon file `name` refers to, or None.

    Matched against _ICON_NAME and then required to actually BE in
    web/icons/ after resolving - the belt to the regex's braces, so even a
    name that somehow got past the pattern can't read outside the folder.

    These serve as image/svg+xml, which /image deliberately refuses (see
    _image_type: SVG is XML and can carry <script>, and this answers
    same-origin). The difference is authorship, not trust in SVG: these five
    files ship with Uniagent and nothing here lets a request name a file
    outside them. A picture the USER points a provider at still goes through
    /image and is still raster-only."""
    if not _ICON_NAME.match(name or ""):
        return None
    try:
        path = (ICON_DIR / name).resolve()
        if path.parent != ICON_DIR.resolve() or not path.is_file():
            return None
    except OSError:
        return None
    return (path, "image/svg+xml")


def _icon_url(path):
    """A path from a provider object, turned into something the page can
    actually put in an <img src>. "" for nothing.

    A bundled "/icons/x.svg" is already a URL on this server. Anything else is
    a file on this machine, which an https: page cannot load directly, so it
    goes back through /image - the same route a picture in a reply uses, with
    the same content sniff standing behind it."""
    path = (path or "").strip()
    if not path:
        return ""
    if path.startswith("/icons/") or path.startswith("data:") \
            or path.startswith("http://") or path.startswith("https://"):
        return path
    return "/image?path=" + quote(path)


def _context_path(rel, kind="context"):
    """The editable file `rel` names, or None if it points anywhere else.
    Resolved and re-checked against its own folder, so a name like
    ../../.bashrc can't be read or written through this - and the suffix must
    be one main.py actually loads, so this can't be used to drop a file the
    agent will never see.

    `kind` picks the folder: the always-injected context/, or memories/, whose
    files the settings page edits through the same box even though only their
    one-line descriptions are ever injected."""
    root = main.MEMORIES if kind == "memories" else main.CONTEXT
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    path = (root / rel).resolve()
    if root.resolve() not in path.parents or path.suffix.lower() not in main.CONTEXT_SUFFIXES:
        return None
    return path


def _pin_path(rel):
    """The tool or skill file `rel` names, resolved and checked the same way
    as _context_path: must stay under tools/ or skills/, must exist, must be a
    tool (.py) or skill (.md) - the only two kinds the sidebar's tools & skills
    list ever hands back a path for.

    Two roots since skills moved into skills/ of their own: a path is relative
    to whichever folder its kind lives in, and the sidebar doesn't say which,
    so both are tried and the first real file wins. Each is still checked to
    be a real parent of the resolved path, so "../" can't climb out of either."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    for root in (tool_processor.TOOLS_DIR, tool_processor.SKILLS_DIR):
        path = (root / rel).resolve()
        if root.resolve() in path.parents and path.suffix.lower() in (".py", ".md") \
                and path.is_file():
            return path
    return None


def _context():
    """What the settings page's context tab draws and edits: the context files
    in the exact order they are fed to the model, the memory files alphabetically
    beside them, and the archived presets that can be swapped back in.

    Both halves in one payload because they are edited on one screen and reset
    by one button - see main.preset_parts for why they are still two folders on
    disk. Each carries `kind`, which is what POST /context needs back to know
    which folder a save belongs to."""
    def read(paths, root, kind):
        out = []
        for p in paths:
            try:
                text = p.read_text()
            except OSError:
                continue
            out.append({"path": p.relative_to(root).as_posix(),
                        "kind": kind, "text": text})
        return out

    return {"context": read(main.context_files(), main.CONTEXT, "context"),
            "memories": read(main.memory_files(), main.MEMORIES, "memories"),
            "presets": main.presets(),
            # Greys out a reset that would do nothing but archive a copy of the
            # defaults next to the defaults.
            "is_default": main.is_default_preset()}


def _settings():
    """The settings, plus what the page needs to offer choices: only the
    providers actually usable right now (a working key/credentials), and every
    model known for each - the hardcoded floor, plus anything /model has
    tested and remembered, plus the provider's own live catalogue (cached a
    few minutes; a provider that can't be asked just shows its floor).

    "defaults" is the shipped value of every setting, so a box the user has
    overwritten can still show what it started as - the compaction tab draws
    the default prompt as its empty box's placeholder, which is the only
    honest way to say "clear this and you get that back"."""
    return {"values": settings.load(),
            "defaults": settings.DEFAULTS,
            "providers": provider.available(),
            "models": provider.known_models()}


def _stt(name):
    """The speech-to-text models provider `name` can be asked for, for the
    voice tab's model box.

    Its own route rather than another field on /settings, and fetched only when
    that tab is opened or its provider changed: reading a provider's catalogue
    is a round trip to that provider, and nearly every settings page load never
    goes near voice. What comes back also explains itself - a wire that can't
    transcribe at all and a provider whose catalogue lists no speech models are
    different situations, and the box under the picker says which."""
    name = (name or "").strip()
    p = provider.custom_provider(name)
    if not p:
        return {"provider": name, "wire": "", "supported": False, "models": [],
                "note": "there is no provider called " + name + " - it may have "
                        "been renamed or deleted on the providers tab."}
    supported = p["wire"] in provider.STT_WIRES
    models = provider.stt_models(name) if supported else []
    if not supported:
        note = (name + " speaks " + p["wire"] + ", which has no speech-to-text - "
                "Claude takes no audio at all, and Bedrock's transcriber is a "
                "separate AWS service. Pick a provider on the "
                + ", ".join(sorted(provider.STT_WIRES)) + " wires.")
    elif not models:
        note = ("nothing speech-shaped in " + name + "'s catalogue - if you know "
                "a model id it serves, type it in anyway; the box is free text.")
    else:
        note = ""
    return {"provider": name, "wire": p["wire"], "supported": supported,
            "models": models, "note": note}


def _email_accounts():
    """What the settings page's email tab draws: every configured account with
    its address, hosts and whether a password is saved - and never the password
    itself, exactly like _env() above. Plus the domains whose hosts are known,
    so the page can tell someone typing an address whether it will need the
    advanced host boxes filled in."""
    accounts = []
    for name, cfg in sorted(_email.accounts().items()):
        accounts.append({"name": name, "address": cfg["address"],
                         "imap": cfg["imap"], "smtp": cfg["smtp"],
                         "imap_port": cfg["imap_port"], "smtp_port": cfg["smtp_port"],
                         "ready": cfg["ready"]})
    return {"accounts": accounts,
            "default": _email.default_name(),
            # Whatever is wrong with EMAIL_ACCOUNTS, if it can't be read at all -
            # otherwise the page would just show no accounts and no reason why.
            "error": _email.config_error(),
            "known": sorted(_email.PROVIDERS),
            "oauth_only": sorted(_email.OAUTH_ONLY),
            "bridge_only": _email.BRIDGE_ONLY,
            "no_imap": _email.NO_IMAP,
            "app_password": _email.APP_PASSWORD}


def _workspaces():
    """What the settings page's workspaces tab draws, and what the chat's
    corner dropdown fills itself from: every workspace, plus which one a chat
    with nothing set falls back to.

    No secrets here to withhold, unlike the email and provider tabs - a
    workspace is a path, a hostname, and at most the path to a key file. The
    key itself is the user's own, already on this machine; Uniagent records
    where it is and never reads or copies the thing."""
    all_ws = provider.workspaces()
    default = provider.default_workspace()
    return {"workspaces": all_ws,
            "default": default["id"] if default else "",
            # What is wrong with WORKSPACES if it can't be read at all -
            # otherwise the page shows an empty list and no reason why.
            "error": provider.workspace_error(),
            # Where a chat with no workspace, and no workspaces configured at
            # all, actually works: the install folder, exactly as before.
            "install_root": str(workspace.INSTALL_ROOT)}


def _email_login_test(name):
    """Actually sign in to both halves of an account and say what happened, in
    one short sentence. Worth the wait: a wrong app password is otherwise only
    discovered later, in the middle of a turn that was trying to do something
    else, and reads there as 'the email tool is broken'."""
    try:
        imap, address = _email.connect(name)
    except _email.EmailError as e:
        return {"ok": False, "text": "IMAP: " + str(e)}
    try:
        imap.logout()
    except Exception:
        pass
    try:
        smtp, _address = _email.smtp_connect(name)
    except _email.EmailError as e:
        return {"ok": False, "text": "reading works, but sending failed - " + str(e)}
    try:
        smtp.quit()
    except Exception:
        pass
    return {"ok": True, "text": "signed in to " + address + " - reading and sending both work."}


def _providers():
    """Every provider there is, as the settings page's providers tab draws
    them - one card each, in the order the dropdowns list them.

    There is nothing else. No built-in list sits behind these: every card is a
    provider object out of .env, and every one of them can be renamed,
    repointed, re-keyed or deleted. What a card says is what the request uses.

    The key DOES come back down here, unlike the keys and email tabs. That is a
    deliberate exception, and it is what "full malleability" costs: a card you
    can only write to is a card you can't audit, and this page is already
    behind the login and only ever served over https. Anything reachable
    without the password still never sees a key.

    Two wires ignore what's typed in their key and URL boxes - bedrock signs
    with the AWS credentials on this machine (and reads its URL box as a
    region), claude-subscription drives the Claude Code CLI, which owns its
    login. They say so in `note` rather than being treated as a separate kind
    of thing."""
    usable = set(provider.available())

    cards = []
    for p in provider.custom_providers():
        wire = p["wire"]
        keyless = wire in provider.KEYLESS_WIRES
        cards.append({
            "name": p["name"],
            "wire": wire,
            "base_url": p["base_url"],
            # Where it actually points when it named no URL of its own, so the
            # page can show the real host as a placeholder rather than an empty
            # box that looks unconfigured.
            "effective_url": provider.custom_base_url(p),
            "default_url": provider.WIRE_DEFAULT_URL.get(wire, ""),
            "key": p["key"],
            "has_key": bool(p["key"]),
            # icon is what the object stores ("" = follow my wire), icon_path
            # is what it resolves to, and icon_url is that made loadable by a
            # browser. The page needs all three: the box shows icon, the tile
            # loads icon_url, and icon_path is what the placeholder says it
            # would use if the box stays empty.
            "icon": p["icon"],
            "icon_path": provider.icon_for(p),
            "icon_url": _icon_url(provider.icon_for(p)),
            # This provider's answers to its wire's setup form, and what each
            # field would ACTUALLY resolve to if left blank - so the page can
            # show "AWS_REGION: eu-west-2 (from your environment)" rather than
            # an empty box that looks unconfigured on a machine where it is
            # perfectly configured, just not here.
            "config": p["config"],
            "resolved": provider.resolved_setup(p),
            "models": p["models"],
            "ready": p["name"] in usable,
            "keyless": keyless,
            "note": ("signs with the AWS credentials on this machine - ~/.aws or the "
                     "AWS_* variables. Its key box is unused; its URL box sets the "
                     "region." if wire == "bedrock" else
                     "drives the Claude Code CLI, which owns its own login - sign in "
                     "with claude login in a terminal. Its key and URL boxes are "
                     "unused." if wire == "claude-subscription" else ""),
        })

    return {
        "providers": cards,
        "wires": sorted(provider.WIRES),
        "wire_urls": provider.WIRE_DEFAULT_URL,
        # Which wires ignore the key and URL boxes, so the page can grey them
        # out the moment one is picked rather than after a save.
        "keyless_wires": sorted(provider.KEYLESS_WIRES),
        # {wire: url} for the picture each wire falls back to, so the icon
        # picker can show what "leave this empty" would actually look like,
        # and offer the bundled ones as one-click choices.
        "wire_icons": {w: _icon_url(provider.WIRE_ICONS.get(w, provider.UNKNOWN_ICON))
                       for w in provider.WIRES},
        "bundled_icons": [{"path": p, "url": _icon_url(p)} for p in _bundled_icons()],
        # What each wire's setup form asks for, out of
        # provider_Request_Template.json. A wire missing from here uses the
        # default form - a base URL and an API key - which is most of them.
        "templates": {w: {"label": provider.template_for(w).get("label", ""),
                          "help": provider.template_for(w).get("help", ""),
                          "base_url": provider.wants_base_url(w),
                          "key": provider.wants_key(w),
                          "fields": provider.template_fields(w)}
                      for w in provider.WIRES},
        "template_error": provider.template_error(),
        # Whatever is wrong with LLM_PROVIDERS, if it can't be read at all -
        # otherwise the page shows no providers and no reason why.
        "error": provider.custom_error(),
    }


def _provider_test(name):
    """Actually send one tiny request to provider `name` and say what happened
    in a sentence. The same reasoning as the email tab's sign-in test: a wrong
    key or a mistyped base URL is otherwise only discovered later, mid-turn,
    where it reads as 'the agent is broken'."""
    model = provider.default_model(name)
    if not model:
        return {"ok": False, "text": "no models known for " + name + " yet - its catalogue "
                "could not be read, so add one under 'models' and test again."}
    error = provider.test_model(name, model)
    if error:
        return {"ok": False, "text": "tried " + model + " and it failed - " + error[:300]}
    return {"ok": True, "text": model + " answered - " + name + " works."}


# Two variables are one line of JSON holding a whole list of objects, each
# owned by the tab built for it: EMAIL_ACCOUNTS by the /email routes (which
# work out the servers and test the sign-in) and LLM_PROVIDERS by the
# /providers routes above (which validate the name and wire, and keep a saved
# key when only the URL is being edited). Neither can be set through /env -
# retyping either blob into a one-line box is how you lose every account, or
# every provider and its key, at once.
ENV_HIDDEN = ("EMAIL_ACCOUNTS", provider.CUSTOM_VAR)


def _env():
    """Every variable in .env: the names, and whether each one has a value.
    Never the values themselves - the same one-way contract as the /providers
    and /email routes, so a secret that goes into .env from this page cannot be
    read back out of it.

    No tab draws this now. Providers, mail accounts and the transcriber each
    own their own settings, which is what the environment tab was mostly for,
    and the rest of .env is a file you edit in an editor. The route survives
    because the web password is still changed through it (see _post_env), and
    because a value written here is live on the next call with nothing to
    restart."""
    return {"vars": [v for v in provider.env_names()
                     if v["name"] not in ENV_HIDDEN],
            "hidden": list(ENV_HIDDEN),
            "password": auth.ENV_NAME}


UPDATE_LOG = ROOT / "update.log"


def _update_local():
    """Which commit this install is on, WITHOUT going to the network. The
    settings page draws this on open, and an update check is a click - see
    _post_update_check. Nothing here is worth making the page wait on a remote
    that might be slow or unreachable."""
    info = {"ref": update.target_ref(), "current": update._commit("HEAD"), "log": ""}
    try:
        # The tail only: a long update writes plenty and the page redraws this
        # every second while one is running.
        info["log"] = UPDATE_LOG.read_text(errors="replace")[-20000:]
    except OSError:
        pass
    return info


def _spawn_update():
    """Run scripts/update.py as its own detached process, logging to
    update.log, and come straight back.

    Detached because the update ENDS by restarting this server: a child of ours
    would be killed with us, and a child that is merely backgrounded would
    still be in the unit's cgroup. start_new_session (setsid) is not enough to
    leave the cgroup either, which is why update.restart_services() asks systemd
    with --no-block and writes nothing afterwards - by then the job is queued
    and it no longer matters whether the process lives to see it."""
    py = update._python()
    log = open(UPDATE_LOG, "w")
    kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([py, str(ROOT / "scripts" / "update.py")], **kwargs)


def _restart_cron():
    """Restart the cron watcher, which is a separate process and so cannot
    restart itself from here. Only possible if it is running as the user
    service; started by hand, it has to be restarted by hand."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "uniagent-cron.service"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return "could not restart cron: " + type(e).__name__
    if r.returncode == 0:
        return "cron watcher restarted."
    return "could not restart cron (" + (r.stderr.strip()[:120] or "unknown") + ")"


def _restart_self():
    """Replace this process with a fresh one - the whole point being that Python
    reads every .py once at start and never looks again, so edited code only
    takes effect on a new process. execv keeps the same PID, so systemd sees no
    exit and does not count it as a failure or race its own Restart=always.

    Runs on its own thread after a beat, so the HTTP response that asked for the
    restart is actually written before the process is gone."""
    def go():
        time.sleep(0.4)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=go, daemon=True).start()


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # one line per request just buries the chat in noise

    def _send(self, body, ctype="text/plain; charset=utf-8", code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Nothing here is worth caching - the page is read from disk each time
        # and every other route is live state. Without this the browser is free
        # to hold on to whatever it got last, so an edited index.html does not
        # show up on reload and the page looks like it simply ignored a change.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        """A file straight off disk, as bytes - _send() above takes a str and
        can only do text.

        Cached, unlike everything else here, and that's deliberate: setBody()
        rebuilds a bubble's HTML on EVERY streamed chunk, and render() rebuilds
        every bubble on every refresh, so each redraw builds a fresh <img>. With
        no-store that's a re-read and re-send of the whole file each time, and
        the picture visibly blinks as the reply types itself out. A short
        max-age keeps it out of the network for the length of a turn; the ETag
        (mtime + size) means that once it does revalidate, a file written again
        at the same path still comes back new rather than stale.
        """
        try:
            st = path.stat()
            data = path.read_bytes()
        except OSError:
            self._send("not found", code=404)
            return
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "max-age=30")
        self.end_headers()
        self.wfile.write(data)

    # --- The gate. Everything below it assumes a valid session. ---

    def _session_cookie(self):
        """This request's session token, or "" if it hasn't got one."""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _client_ip(self):
        """Who's asking, for the wrong-password lockout. There is no reverse
        proxy in front of this, so the socket's own address is the real one -
        deliberately NOT X-Forwarded-For, which the client sets and could use to
        get a fresh allowance on every guess."""
        return self.client_address[0]

    def _set_session(self, token):
        """Hand back a session as a cookie. Secure because the app is https
        only; HttpOnly so a bug in the page can't read it back out into a
        script; SameSite=Strict so another site can't make your browser drive
        the agent with your own cookie attached."""
        self.send_header(
            "Set-Cookie",
            COOKIE + "=" + token + "; Path=/; Max-Age="
            + str(auth.SESSION_DAYS * 86400)
            + "; HttpOnly; Secure; SameSite=Strict")

    def _allowed(self):
        """Whether this request may go through to a real route. When it may
        not, this has already answered it - the caller just returns.

        A page navigation gets the login page, so typing the address in a
        browser lands on a password box. Anything else gets a bare 401, which
        is what the app's own fetches see when a session ages out: index.html
        watches for it and sends the tab to /login rather than quietly drawing
        an empty chat."""
        route = urlparse(self.path).path
        if route in OPEN_ROUTES or auth.valid_session(self._session_cookie()):
            return True
        if self.command == "GET" and "text/html" in (self.headers.get("Accept") or ""):
            self._send_login()
        else:
            self._send(json.dumps({"error": "not logged in"}),
                       "application/json", code=401)
        return False

    def _send_login(self):
        self._send(LOGIN_PAGE.read_text(), "text/html; charset=utf-8")

    def _post_login(self):
        """Check a password and issue a session. Both failure paths say exactly
        the same thing - a wrong password and a password submitted during a
        lockout are not distinguishable from out here, beyond the wait."""
        ip = self._client_ip()
        wait = auth.locked_for(ip)
        if wait:
            self._send(json.dumps(
                {"error": "Too many attempts. Try again in "
                          + str(wait) + "s."}), "application/json", code=429)
            return
        body = self._body() or {}
        if not auth.check_password(body.get("password")):
            auth.note_failure(ip)
            self._send(json.dumps({"error": "Wrong password."}),
                       "application/json", code=401)
            return
        auth.note_success(ip)
        data = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._set_session(auth.new_session())
        self.end_headers()
        self.wfile.write(data)

    def _post_logout(self):
        """Drop the cookie. The token stays valid until it expires - nothing
        stores it to revoke (see auth.py) - so this ends the session on this
        device only. Changing the password in .env is what ends all of them."""
        data = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie",
                         COOKIE + "=; Path=/; Max-Age=0; HttpOnly; Secure;"
                         " SameSite=Strict")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return
        if self.path.startswith("/login"):
            # An open route, so being here says nothing about being logged in -
            # it has to ask. Someone with a session asking for the login page
            # has nothing to log into and gets the app; anyone else gets the
            # password box, which is also where _allowed() sends a navigation.
            if auth.valid_session(self._session_cookie()):
                self._send(PAGE.read_text(), "text/html; charset=utf-8")
            else:
                self._send_login()
        elif self.path == "/":
            self._send(PAGE.read_text(), "text/html; charset=utf-8")
        elif self.path == "/history" or self.path.startswith("/history?"):
            # ?chat= names which one. A chat the client holds but has never
            # sent anything to has no file and no history yet - that's an empty
            # transcript, not an error, so create=True and let it answer "".
            c = _chat_of(self, create=True)
            self._send("" if c is None else c.history)
        elif self.path == "/status" or self.path.startswith("/status?"):
            # A window sitting in a chat that doesn't exist yet still needs the
            # background list (another chat may be working) and still has to
            # hear about approvals, so this answers for "no chat" rather than
            # refusing - there is simply nothing running in a chat nobody has
            # said anything in.
            c = _chat_of(self, create=True)
            self._send(json.dumps(_status(c)), "application/json")
        elif self.path == "/chats":
            self._send(json.dumps(_chats()), "application/json")
        elif self.path.startswith("/subagent?"):
            path = _subagent_file(urlparse(self.path).query)
            if path is None:
                self._send("not found", code=404)
            else:
                self._send(path.read_text())
        elif self.path.startswith("/icons/"):
            # The pictures that ship with Uniagent - what a provider shows
            # when it hasn't been pointed at one of its own.
            found = _icon_file(unquote(self.path[len("/icons/"):]))
            if found is None:
                self._send("not found", code=404)
            else:
                self._send_file(*found)
        elif self.path.startswith("/image?"):
            # What ![alt](/some/path.png) in a reply resolves to - a browser
            # won't load a file: sub-resource into an http: page, so a local
            # image has to come back through here.
            found = _image_file(urlparse(self.path).query)
            if found is None:
                self._send("not an image", code=404)
            else:
                self._send_file(*found)
        elif self.path.startswith("/validations?"):
            q = parse_qs(urlparse(self.path).query)
            stem = _stem_of(q.get("chat", [""])[0])
            if stem is None:
                self._send("not found", code=404)
            else:
                # One JSON object per line, in tool-result order -> a JSON list.
                path = main.VALIDATIONS / (stem + ".jsonl")
                lines = path.read_text().splitlines() if path.exists() else []
                self._send("[" + ",".join(lines) + "]", "application/json")
        elif self.path.startswith("/context/preset?"):
            # One preset's files, fetched only when a row is expanded to be
            # read - the tab's own payload lists presets without their bodies,
            # so having many stays cheap.
            q = parse_qs(urlparse(self.path).query)
            try:
                files = main.preset_files(q.get("name", [""])[0])
            except FileNotFoundError as e:
                self._send(str(e), code=404)
            else:
                self._send(json.dumps(files), "application/json")
        elif self.path == "/settings":
            self._send(json.dumps(_settings()), "application/json")
        elif self.path == "/providers":
            self._send(json.dumps(_providers()), "application/json")
        elif self.path == "/env":
            self._send(json.dumps(_env()), "application/json")
        elif self.path.startswith("/voice/models"):
            q = parse_qs(urlparse(self.path).query)
            self._send(json.dumps(_stt(q.get("provider", [""])[0])),
                       "application/json")
        elif self.path == "/email":
            self._send(json.dumps(_email_accounts()), "application/json")
        elif self.path == "/workspaces":
            self._send(json.dumps(_workspaces()), "application/json")
        elif self.path == "/context":
            self._send(json.dumps(_context()), "application/json")
        elif self.path == "/tools":
            self._send(json.dumps(_tools()), "application/json")
        elif self.path == "/inventory":
            self._send(json.dumps(tool_processor.inventory()), "application/json")
        elif self.path == "/market" or self.path.startswith("/market?"):
            # Network, so only when the marketplace is actually opened - never
            # on a page load, and never on a timer. Cached inside market.py;
            # ?refresh=1 is the button that says "ask GitHub again anyway".
            q = parse_qs(urlparse(self.path).query)
            try:
                data = market.catalogue(refresh=q.get("refresh", [""])[0] == "1")
            except Exception as e:
                # Browsing is a round trip to somebody else's server, so it can
                # fail in ways nothing here controls. The tab shows the note.
                data = {"entries": [], "notes": [type(e).__name__ + ": " + str(e)],
                        "fetched": 0}
            self._send(json.dumps(data), "application/json")
        elif self.path == "/injection" or self.path.startswith("/injection?"):
            # Fetched when the page is told something changed, not on a timer -
            # but one signal ("a turn ended") covers several things that may
            # each have moved or not, and the payload carries the full text of
            # every injected file. The client sends back the hash it last drew
            # (?have=), so a signal that turns out to change nothing costs one
            # small JSON object instead of tens of KB down the wire and through
            # JSON.parse. Same data, same freshness, none of the copying.
            c = _chat_of(self, create=True)
            if c is None and not _asks_blank(self):
                self._send("no such chat", code=404)
                return
            # A window in a conversation that hasn't been started yet still has
            # a panel to fill: it answers for the chat that message would open,
            # and records nothing, since there is no chat to record it against.
            data = _injection(c) if c is not None else _injection(_blank_chat(), False)
            digest = hashlib.sha1(json.dumps(data).encode()).hexdigest()[:16]
            have = parse_qs(urlparse(self.path).query).get("have", [""])[0]
            if have == digest:
                self._send(json.dumps({"unchanged": True}), "application/json")
            else:
                data["hash"] = digest
                self._send(json.dumps(data), "application/json")
        elif self.path == "/update":
            self._send(json.dumps(_update_local()), "application/json")
        elif self.path == "/cron":
            # An empty box would be a dead end on a fresh install: whatever is
            # typed into it has to be valid JSON to save, so hand over the
            # skeleton to type into rather than nothing.
            self._send(CRON_FILE.read_text() if CRON_FILE.exists()
                       else '{\n  "jobs": []\n}\n')
        elif self.path == "/stream":
            self._stream()
        else:
            self._send("not found", code=404)

    def _body(self):
        """This request's JSON body, or None if it isn't valid JSON."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return None

    def do_POST(self):
        if self.path == "/login":
            self._post_login()
            return
        if not self._allowed():
            return
        if self.path == "/logout":
            self._post_logout()
            return
        if self.path == "/settings":
            self._post_settings()
            return
        if self.path == "/providers":
            self._post_provider()
            return
        if self.path == "/providers/remove":
            self._post_provider_remove()
            return
        if self.path == "/providers/test":
            self._post_provider_test()
            return
        if self.path == "/env":
            self._post_env()
            return
        if self.path == "/context":
            self._post_context()
            return
        if self.path == "/context/revert":
            self._post_context_revert()
            return
        if self.path == "/context/restore":
            self._post_context_restore()
            return
        if self.path == "/context/preset/rename":
            self._post_preset_rename()
            return
        if self.path == "/cron":
            self._post_cron()
            return
        if self.path == "/tools":
            self._post_tools()
            return
        if self.path == "/inventory/toggle":
            self._post_toggle()
            return
        if self.path == "/market/install":
            self._post_install()
            return
        if self.path == "/context/pin":
            self._post_pin()
            return
        if self.path == "/update/check":
            self._post_update_check()
            return
        if self.path == "/update":
            self._post_update()
            return
        if self.path == "/restart":
            self._post_restart()
            return
        if self.path == "/voice":
            self._post_voice()
            return
        if self.path == "/email":
            self._post_email()
            return
        if self.path == "/email/test":
            self._post_email_test()
            return
        if self.path == "/email/remove":
            self._post_email_remove()
            return
        if self.path == "/email/default":
            self._post_email_default()
            return
        if self.path == "/workspaces":
            self._post_workspace()
            return
        if self.path == "/workspaces/test":
            self._post_workspace_test()
            return
        if self.path == "/workspaces/remove":
            self._post_workspace_remove()
            return
        if self.path == "/workspaces/default":
            self._post_workspace_default()
            return
        if self.path == "/workspace" or self.path.startswith("/workspace?"):
            self._post_chat_workspace()
            return
        if self.path != "/input" and not self.path.startswith("/input?"):
            self._send("not found", code=404)
            return
        length = int(self.headers.get("Content-Length", 0))
        text = self.rfile.read(length).decode().strip()
        if not text:
            self._send(json.dumps({"type": "system", "text": ""}), "application/json")
            return

        # Which chat this is for comes off the request, not off a global. A
        # window that has minted a chat but never written to it sends an id
        # with no folder behind it yet, hence create=True: the folder appears
        # when the turn below writes the first message into it.
        c = _chat_of(self, create=True, mint=True)
        if c is None:
            self._send(json.dumps({"type": "system",
                                   "text": "that chat no longer exists."}),
                       "application/json")
            return

        result = command_processor.process(text, c)
        if result is not None:
            reply, goto = result
            # `goto` is /load, /new or a /delete that removed the chat you were
            # in: the id for THIS window to switch to. It moves nobody else -
            # which is the point, since another window may be sitting in the
            # very chat this one just left.
            answer = {"type": "system", "text": reply}
            if goto is not None:
                answer["goto"] = goto
            self._send(json.dumps(answer), "application/json")
            # Any command may have changed what's in context: /model changes
            # the injection list and the window it's measured against, /pin and
            # /compact change the content itself. One small signal covers the
            # lot, and an unchanged injection costs the page a single
            # {"unchanged": true} (see GET /injection's ?have=).
            _broadcast_context(c)
            # /delete and /name both move the sidebar; the others don't, but
            # this is one tiny event against a list the page only re-fetches
            # when it hears one, so it isn't worth being clever about which.
            _broadcast_chats()
        else:
            # Sending a message while this chat waits on an approval answers it
            # NO - moving on IS the answer - and the message queues as the next
            # turn behind the denied one.
            if command_processor.deny_pending(c.id):
                _broadcast({"type": "system", "chat": c.route,
                            "text": "pending approval denied - your message follows."})
            global _last_input_chat
            with _last_input_lock:
                _last_input_chat = c
            _run_turn(text, target=c)
            # The chat id goes back with the answer so a window that sent its
            # very first message into a freshly minted chat can confirm which
            # one it landed in, without a second round trip to ask. The route,
            # since it is the page that has to be able to name it again.
            self._send(json.dumps({"type": "started", "chat": c.route}),
                       "application/json")

    def _post_settings(self):
        """Save changed settings and hand back the full set as it now stands.
        Nothing needs restarting: main.run and the cron watcher both read these
        per turn, so the next message uses the new model."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        self._send(json.dumps(settings.save(body)), "application/json")
        # The model can have changed, and with it both the injection list this
        # chat follows and the context window it's measured against.
        _broadcast_context()

    def _post_provider(self):
        """Add a provider object, or update the one already under this name.

        `key` is optional and means what it does everywhere else on this page:
        left out (or null), whatever key is saved stays saved - which is what
        lets the tab edit a base URL or a model list without the key ever
        having come back down to the browser to be sent up again. Pass "" to
        clear it deliberately.

        Nothing is special-cased. Every provider is one of these objects, so
        this route renames, repoints, re-keys and re-wires any of them - the
        only refusals are an illegal name, a name another provider already has,
        and a wire this code can't speak.

        Live immediately: provider.custom_providers() re-reads .env on every
        call, so the next turn - in this process and in the cron watcher -
        already has it, with nothing to restart."""
        body = self._body() or {}
        name = body.get("name")
        if not isinstance(name, str):
            self._send("expected a \"name\"", code=400)
            return
        models = body.get("models")
        key = body.get("key")
        icon = body.get("icon")
        config = body.get("config")
        rename_from = body.get("rename_from") or None
        try:
            provider.save_custom_provider(
                name,
                wire=body.get("wire") or "openai",
                base_url=body.get("base_url") or "",
                key=key if isinstance(key, str) else None,
                icon=icon if isinstance(icon, str) else None,
                config=config if isinstance(config, dict) else None,
                models=[str(m) for m in models] if isinstance(models, list) else None,
                rename_from=rename_from,
            )
        except ValueError as e:
            self._send(str(e), code=400)
            return
        # A rename is a rename, not a delete-and-recreate. The provider's own
        # id carried its model lists across on its own (provider.py); this
        # moves the other half - the settings, chats, subagents and cron jobs
        # that name the provider they run on - so nothing silently drops back
        # to the default because its provider got a different label.
        if rename_from and rename_from.strip().lower() != name.strip().lower():
            provider_refs.rename_everywhere(rename_from, name)
        self._send(json.dumps(_providers()), "application/json")
        # A provider appearing or changing changes what every model dropdown
        # on the page has to offer, and what a chat can be pointed at.
        _broadcast_context()

    def _post_provider_remove(self):
        """Delete one provider, whichever it is - there is no protected set.

        That is the provider gone, key and all: anything still pointed at it
        falls back to the default the next time settings are read, since
        settings.py refuses one that isn't available()."""
        body = self._body() or {}
        name = body.get("name")
        if not isinstance(name, str):
            self._send("expected a \"name\"", code=400)
            return
        provider.remove_custom_provider(name)
        self._send(json.dumps(_providers()), "application/json")
        _broadcast_context()

    def _post_provider_test(self):
        """Send one real request to a provider and report what came back. Slow
        by design - it is a live round trip - which is why it is its own button
        rather than something every save does."""
        body = self._body() or {}
        name = body.get("name")
        if not isinstance(name, str) or name not in provider.provider_names():
            self._send("no such provider", code=400)
            return
        self._send(json.dumps(_provider_test(name)), "application/json")

    def _post_env(self):
        """Set - or, given a blank value, remove - one variable in .env.
        Nothing here loads .env into the
        process environment at startup: every reader parses the file when it
        needs it, so a saved value is live from the next call onwards with
        nothing to restart.

        Two names are guarded. EMAIL_ACCOUNTS belongs to the email tab (see
        ENV_HIDDEN). UNIAGENT_PASSWORD may be changed but never cleared: with
        no password in .env, auth.password() invents one and prints it to
        whatever terminal the server started from, which under systemd is a log
        nobody is watching - the door would stay locked with nobody holding a
        key."""
        body = self._body()
        name = (body or {}).get("name")
        value = (body or {}).get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            self._send("expected {\"name\": ..., \"value\": ...}", code=400)
            return
        # Uppercased rather than rejected: set_env only accepts uppercase names
        # and that is the convention here anyway, so typing a lowercase one is
        # a typo to fix, not an error to report.
        name = name.strip().upper()
        if name in ENV_HIDDEN:
            self._send(name + " has a settings tab of its own - it holds a whole "
                       "list of objects, and retyping it as one line would lose "
                       "the lot.", code=400)
            return
        if name == auth.ENV_NAME:
            if not value.strip():
                self._send("the web password cannot be blank - type a new one "
                           "to change it.", code=400)
                return
            # auth.password() strips surrounding quotes off the line it reads,
            # so a password wrapped in them would be saved as one thing and
            # checked as another, and the next login would fail with no way to
            # tell why.
            if value.strip() != value.strip().strip("\"'"):
                self._send("the web password cannot start or end with a quote "
                           "- .env drops those when it is read back, so you "
                           "could not log in with what you typed.", code=400)
                return
        try:
            provider.set_env(name, value)
        except ValueError as e:
            self._send(str(e), code=400)
            return
        if name != auth.ENV_NAME:
            self._send(json.dumps(_env()), "application/json")
            return
        # The password just changed, and every session cookie ever issued was
        # signed with the old one (see auth.py), so as of that write they are
        # all invalid - this browser's included. Handing back a session signed
        # with the NEW password keeps the device that made the change logged in
        # and logs out every other one, which is the point of changing it.
        data = json.dumps(dict(_env(), password_changed=True)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._set_session(auth.new_session())
        self.end_headers()
        self.wfile.write(data)

    def _post_email(self):
        """Save one email account: an address, its password, and - for a domain
        the table in _email.py doesn't know - the IMAP and SMTP hosts. Written
        straight to .env from here.

        The password comes in over this request and goes out to .env; it is
        never put in a chat, never reaches a model, and is never handed back by
        GET /email. That's the entire reason this is an endpoint the settings
        page posts to, rather than a tool the agent could call: a tool's
        arguments are part of the conversation, so setting an account up that
        way would copy the password into the history file on disk and into a
        request to whichever provider is configured.

        Answers with the refreshed account list and the result of actually
        signing in, so a typo in an app password is caught here and now.
        """
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        address = str(body.get("address", "")).strip()
        password = str(body.get("password", ""))
        imap = str(body.get("imap", "")).strip()
        smtp = str(body.get("smtp", "")).strip()
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            self._send("that isn't an email address", code=400)
            return

        domain = _email.domain_of(address)
        if domain in _email.NO_IMAP:
            self._send(_email.NO_IMAP[domain] + " gives no IMAP or SMTP access at "
                       "all, so " + domain + " can't be used here by any means.",
                       code=400)
            return
        if domain in _email.OAUTH_ONLY:
            self._send(_email.OAUTH_ONLY[domain] + " no longer lets apps sign in to "
                       + domain + " with a password - that account needs OAuth2, "
                       "which isn't supported here yet.", code=400)
            return
        # An unknown domain is not a refusal any more: ask the provider itself
        # (autoconfig, Thunderbird's database, then the usual hostnames) and
        # only insist on the boxes being filled in when even that finds nothing.
        if not (imap and smtp) and domain not in _email.PROVIDERS:
            found = _email.discover_hosts(domain) or {}
            imap = imap or found.get("imap", "")
            smtp = smtp or found.get("smtp", "")
            if not (imap and smtp):
                self._send("the mail servers for " + domain + " couldn't be found "
                           "automatically, so the IMAP and SMTP hosts have to be "
                           "filled in too.", code=400)
                return
            body.setdefault("imap_port", found.get("imap_port"))
            body.setdefault("smtp_port", found.get("smtp_port"))
            body.setdefault("imap_security", found.get("imap_security"))
            body.setdefault("smtp_security", found.get("smtp_security"))

        name = str(body.get("name") or body.get("alias") or "").strip().lower()
        if name and not _email.NAME_RE.match(name):
            self._send("an account name can only be lowercase letters, digits, "
                       "dashes and underscores", code=400)
            return

        try:
            name = _email.save_account(
                address, password=password, name=name, imap=imap, smtp=smtp,
                imap_port=body.get("imap_port"), smtp_port=body.get("smtp_port"),
                imap_security=body.get("imap_security"),
                smtp_security=body.get("smtp_security"),
                tls_verify=body.get("tls_verify", True),
                default=bool(body.get("default")))
        except (ValueError, OSError) as e:
            self._send("could not save: " + str(e), code=500)
            return

        result = _email_login_test(name) if password or _email.accounts().get(name, {}).get("ready") else \
            {"ok": False, "text": "saved, but there's no password for this account yet."}
        self._send(json.dumps({"accounts": _email_accounts(), "test": result,
                               "name": name}), "application/json")

    def _post_email_test(self):
        """Sign in to an already-saved account and report back. Same check the
        save does, for when you want to know whether a password has since been
        revoked without retyping it."""
        name = self._email_name()
        if name is None:
            return
        self._send(json.dumps(_email_login_test(name)), "application/json")

    def _email_name(self):
        """The account name out of a request body, or None having already sent
        the error. Accepts "alias" as well, which is what it used to be called."""
        body = self._body() or {}
        name = str(body.get("name") or body.get("alias") or "").strip().lower()
        if not _email.NAME_RE.match(name or ""):
            self._send("which account?", code=400)
            return None
        return name

    def _post_email_remove(self):
        """Forget an account: it comes out of EMAIL_ACCOUNTS in .env, password
        and all. The mailbox itself is untouched - this only drops the
        credentials this machine was keeping."""
        name = self._email_name()
        if name is None:
            return
        try:
            _email.remove_account(name)
        except (ValueError, OSError) as e:
            self._send("could not remove: " + str(e), code=500)
            return
        self._send(json.dumps(_email_accounts()), "application/json")

    def _post_email_default(self):
        """Make one account the one that tool calls get when they don't name
        another."""
        name = self._email_name()
        if name is None:
            return
        try:
            if not _email.set_default(name):
                self._send("there is no account called " + name, code=400)
                return
        except (ValueError, OSError) as e:
            self._send("could not save: " + str(e), code=500)
            return
        self._send(json.dumps(_email_accounts()), "application/json")

    # --- workspaces --------------------------------------------------------
    #
    # Where a chat's file and terminal tools work, and on which machine. These
    # write .env through provider.save_workspace(), the same single place the
    # settings page and anything else has to go through, and they answer with
    # the refreshed list so the page never has to guess what it now looks like.

    def _post_workspace(self):
        """Add a workspace, or update the one with this id."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        try:
            saved = provider.save_workspace(
                name=str(body.get("name", "")),
                path=str(body.get("path", "")),
                ssh=str(body.get("ssh", "")),
                port=body.get("port") or 0,
                key=str(body.get("key", "")),
                wsid=str(body.get("id", "")).strip(),
                default=bool(body.get("default")))
        except ValueError as e:
            self._send(str(e), code=400)
            return
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        out = _workspaces()
        # Whether it can actually be reached, checked once here rather than
        # left for the first tool call to discover mid-turn. A workspace that
        # saves fine and then cannot be used is exactly the thing a settings
        # page should catch while the person is still looking at it.
        ok, message = workspace.get(saved["id"]).check()
        out["tested"] = {"id": saved["id"], "ok": ok, "message": message}
        self._send(json.dumps(out), "application/json")

    def _post_workspace_test(self):
        """Reachability, on demand - the tab's test button.

        Takes either a saved id, or the fields as typed, so a workspace can be
        tested before it is saved."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        wsid = str(body.get("id", "")).strip()
        if wsid:
            ws = workspace.get(wsid)
        else:
            ws = workspace.Workspace({"id": "", "name": str(body.get("name") or "this one"),
                                      "path": str(body.get("path", "")),
                                      "ssh": str(body.get("ssh", "")),
                                      "port": body.get("port") or 0,
                                      "key": str(body.get("key", ""))})
        ok, message = ws.check()
        self._send(json.dumps({"ok": ok, "message": message}), "application/json")

    def _post_workspace_remove(self):
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        try:
            provider.delete_workspace(str(body.get("id", "")))
        except ValueError as e:
            self._send(str(e), code=400)
            return
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        self._send(json.dumps(_workspaces()), "application/json")

    def _post_workspace_default(self):
        """Which workspace a chat gets when it has never been given one."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        try:
            # Through provider, not by rewriting the list here: the built-in is
            # in that list but must never be written to .env, and it holds the
            # default by nothing else claiming it rather than by a flag.
            provider.set_default_workspace(str(body.get("id", "")))
        except ValueError as e:
            self._send(str(e), code=400)
            return
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        self._send(json.dumps(_workspaces()), "application/json")

    def _post_chat_workspace(self):
        """Put THIS chat in a workspace - the dropdown in the corner of the
        chat window.

        Written to the chat's own settings.json, not to any global: two chats
        open side by side can be working on two different machines, which is
        most of the point. An empty id means "follow the default", which is
        how a chat goes back to being unpinned.

        The move is also written into the chat's history as a note the model
        reads on its next pass (main.workspace_note) - the same thing
        /workspace does, because it is the same event. Without it the agent
        carries on believing it is where it was, mid-turn most of all, which is
        exactly when the picker gets used."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        wsid = str(body.get("workspace", "")).strip().lower()
        if wsid and not any(w["id"] == wsid for w in provider.workspaces()):
            self._send("there is no workspace called " + wsid, code=400)
            return
        c = _chat_of(self, create=True, mint=True)
        if c is None:
            self._send("no chat to set a workspace on", code=400)
            return
        # Where it was working, resolved BEFORE the write - a chat that was
        # following the default was already working somewhere, and pinning it
        # to that same workspace moves nothing.
        was = workspace.get(c.workspace)
        try:
            c.set_workspace(wsid)
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        ws = workspace.get(wsid)
        ok, message = ws.check()
        # Only when it actually moved: re-picking the workspace a chat is
        # already in is not an event, and a note for it would be noise in the
        # transcript and a wasted turn in the next request.
        if ws.id != was.id:
            note = main.workspace_note(c, ws, ok, message, following_default=not wsid)
            # So the window that switched - and any other watching this chat -
            # draws the note now, rather than only on its next redraw.
            _broadcast({"type": "note", "chat": c.route, "text": note})
        # The page redraws its dropdown from this rather than from what it
        # sent, so a chat that was minted by this very request comes back with
        # the id it was given.
        self._send(json.dumps({"chat": c.route, "workspace": wsid,
                               "name": ws.name, "where": ws.where,
                               "ok": ok, "message": message}),
                   "application/json")

    def _post_context(self):
        """Write one context or memory file. The next turn picks it up on its
        own - both are re-read whenever their files change."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        kind = body.get("kind", "context")
        path = _context_path(body.get("path", ""), kind)
        text = body.get("text")
        if path is None or not isinstance(text, str):
            self._send("bad path or text", code=400)
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        self._send(json.dumps({"saved": body.get("path"), "kind": kind}),
                   "application/json")
        _broadcast_context()  # that file is injected - the panel draws it

    def _post_context_revert(self):
        """Reset context/ AND memories/ to the shipped defaults. No body and no
        path: this is the whole preset, not a file - see main.revert_preset,
        which archives what was there under archive/ before replacing it.

        Whole-preset on purpose. Reverting one file at a time leaves the agent
        holding a default system prompt next to a memories folder full of
        projects it was told to forget, and no obvious way back to a
        known-good starting state. The archive is what makes it safe rather
        than final."""
        try:
            archived = main.revert_preset()
        except (OSError, FileNotFoundError) as e:
            self._send("could not revert: " + str(e), code=500)
            return
        self._send(json.dumps({"archived": archived.name if archived else None,
                               "state": _context()}), "application/json")
        _broadcast_context()  # every injected file just changed

    def _post_context_restore(self):
        """Swap an archived preset back in, archiving the live one on the way
        past unless it is exactly the shipped defaults. The archive being
        restored is copied, not consumed, so it stays in the list to come back
        to later."""
        body = self._body()
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            self._send("expected {\"name\": ...}", code=400)
            return
        try:
            archived = main.restore_preset(body["name"])
        except FileNotFoundError as e:
            self._send(str(e), code=400)
            return
        except OSError as e:
            self._send("could not restore: " + str(e), code=500)
            return
        self._send(json.dumps({"restored": body["name"],
                               "archived": archived.name if archived else None,
                               "state": _context()}), "application/json")
        _broadcast_context()

    def _post_preset_rename(self):
        """Name a saved preset. Only the label moves - the folder keeps the
        timestamp it was taken, which is what everything else points at."""
        body = self._body()
        if not isinstance(body, dict) or not isinstance(body.get("name"), str) \
                or not isinstance(body.get("label"), str):
            self._send("expected {\"name\": ..., \"label\": ...}", code=400)
            return
        try:
            meta = main.rename_preset(body["name"], body["label"])
        except FileNotFoundError as e:
            self._send(str(e), code=400)
            return
        self._send(json.dumps({"name": body["name"], "label": meta["label"]}),
                   "application/json")

    def _post_cron(self):
        """Write cron.json whole - one text field, no path needed since there's
        only the one file. The watcher re-reads it every tick, so a save here
        is live within ~30 seconds; no restart.

        Checked before it lands: one missing bracket in this box would leave
        the watcher with a file it can't read, and every scheduled job silently
        stops. So a save that isn't valid JSON is refused here, with the line
        and column, and what's on disk keeps running."""
        body = self._body()
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            self._send("expected {\"text\": ...}", code=400)
            return
        try:
            json.loads(body["text"])
        except ValueError as e:
            self._send("not valid JSON - " + str(e) + " (nothing saved)", code=400)
            return
        try:
            CRON_FILE.write_text(body["text"])
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        self._send(json.dumps({"saved": True}), "application/json")

    def _post_tools(self):
        """Write a new tool (.py) or skill (.md) file into tools/ from the
        sidebar's manual-add form, then hand back the refreshed list - so the
        page can show right away whether what got typed actually loaded, or
        came up broken (a syntax error, a missing NAME/DESCRIPTION/run).
        Never overwrites a file that's already there; editing an existing
        one is a job for a real editor, not this form."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        kind = body.get("kind")
        name = body.get("name", "")
        code = body.get("code", "")
        if kind not in ("tool", "skill") or not isinstance(code, str):
            self._send('expected {"kind": "tool"|"skill", "name": ..., "code": ...}', code=400)
            return
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            self._send("name must be lowercase letters, digits, or underscores, "
                       "starting with a letter", code=400)
            return
        # A skill goes to skills/, a tool to tools/ - they no longer share a
        # folder. A new skill gets the folder-with-SKILL.md shape Claude uses
        # and create-skill documents, rather than a loose <name>.md, so what
        # this writes matches what the project already holds.
        if kind == "skill":
            path = tool_processor.SKILLS_DIR / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = tool_processor.TOOLS_DIR / (name + ".py")
        if path.exists():
            self._send("a " + kind + " named " + name + " already exists", code=409)
            return
        try:
            path.write_text(code)
        except OSError as e:
            self._send("could not save: " + str(e), code=500)
            return
        tool_processor.load_tools()  # so the response already reflects loaded or broken
        self._send(json.dumps(_tools()), "application/json")
        # This page already has the new list (it's the reply above); the signal
        # is for every OTHER open page. The tool list is injected too, so the
        # context section moves with it.
        _broadcast_tools()
        _broadcast_context()

    def _post_toggle(self):
        """Switch one tool or skill on or off - which MOVES its file between
        tools/ and disabled/tools/ (or skills/ and disabled/skills/), see
        tool_processor.set_enabled - and hand back the whole inventory as it
        now stands, so the tab redraws from what is actually on disk rather
        than from what it assumed the click did.

        A refusal (something already there under that name, a file that moved
        underneath us) comes back 200 with ok:false and the reason, not as an
        HTTP error: it is an answer about tools, and the tab shows it in the
        same line it shows a success in."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        kind = body.get("kind")
        path = body.get("path", "")
        enable = body.get("enabled")
        if kind not in ("tool", "skill") or not isinstance(path, str) \
                or not isinstance(enable, bool):
            self._send('expected {"kind": "tool"|"skill", "path": ..., '
                       '"enabled": true|false}', code=400)
            return
        ok, note = tool_processor.set_enabled(kind, path, enable)
        self._send(json.dumps({"ok": ok, "note": note,
                               "items": tool_processor.inventory()}),
                   "application/json")
        if ok:
            # The tool list is part of what every chat is told, so this moves
            # the sidebar and the context panel in every other open window too.
            _broadcast_tools()
            _broadcast_context()

    def _post_install(self):
        """Download one marketplace entry into tools/ or skills/, switched on -
        see market.py. The reply carries the refreshed inventory, so the tab
        redraws with the new arrival in the enabled list rather than guessing
        where it went."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        repo = body.get("repo", "")
        path = body.get("path", "")
        if not isinstance(repo, str) or not isinstance(path, str) \
                or not repo or not path:
            self._send('expected {"repo": "owner/repo", "path": ...}', code=400)
            return
        try:
            ok, note = market.install(repo, path)
        except Exception as e:
            ok, note = False, "install failed - " + type(e).__name__ + ": " + str(e)
        self._send(json.dumps({"ok": ok, "note": note,
                               "items": tool_processor.inventory()}),
                   "application/json")
        if ok:
            _broadcast_tools()

    def _post_pin(self):
        """Pin an existing tool or skill's content into ONE chat - the one the
        asking window names in ?chat= - written into that chat's own settings
        .json (see Agent.add_pinned), never into context/ or any shared folder,
        so it's injected in full on every turn of THAT chat from here on and no
        other chat ever sees it. Dragging a sidebar item onto the context panel,
        or the click-the-name popup, both hit this. A .py tool's source is
        fenced as a code block first, since it's about to sit alongside markdown
        prose in the system message; a skill's markdown is copied as-is.
        Refuses a name already pinned to that chat - unpinning is a job for
        the context tab, not this endpoint. Appending is what makes it "the
        latest addition" the moment it lands: the chat's pinned list is written
        straight away and reload_model() re-reads it at the top of the very
        next turn, no restart, no separate cache to keep in step.

        This is one of the routes that writes to a chat which may have nothing
        in it yet - setting a chat up before talking to it - so it accepts an
        id the window has minted but never used, and lets the settings .json be
        what brings the folder into being."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        c = _chat_of(self, create=True)
        if c is None:
            self._send("no such chat", code=404)
            return
        src = _pin_path(body.get("path", ""))
        if src is None:
            self._send("bad or missing tools/ path", code=400)
            return
        try:
            text = src.read_text()
        except OSError as e:
            self._send("could not read: " + str(e), code=500)
            return
        if src.suffix.lower() == ".py":
            text = "```python\n" + text.rstrip("\n") + "\n```"
        # A skill lives at <folder>/SKILL.md - the folder is its real name,
        # same convention tool_processor._read_skill uses.
        name = src.parent.name if src.name.upper() == "SKILL.MD" else src.stem
        if any(p.get("label") == name for p in c.pinned):
            self._send(name + " is already pinned to this chat", code=409)
            return
        c.add_pinned(name, "--- " + name + " ---\n" + text)
        self._send(json.dumps({"pinned": name}), "application/json")
        _broadcast_context(c)

    def _post_voice(self):
        """A clip the page recorded while its mic button (or the hold-to-talk
        key) was held: raw audio bytes as the whole body, the browser's own
        format named by Content-Type. Answers with the transcript and stops
        there - the page then sends that text through POST /input like anything
        typed, so a spoken message takes exactly the path a typed one does and
        nothing here needs to know about chats, queueing or commands."""
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send("no audio", code=400)
            return
        if length > MAX_CLIP:
            self._send("clip too long", code=413)
            return
        clip = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            text = voice_input.transcribe_audio(clip, "speech." + CLIP_EXT.get(ctype, "webm"))
        except voice_input.VoiceError as e:
            # 502, not 500: what failed is Whisper (or the key it needs), not
            # this server - and the page shows the message as-is, so whatever
            # went wrong is readable from the phone that recorded it.
            self._send(str(e), code=502)
            return
        self._send(json.dumps({"text": text}), "application/json")

    def _post_update_check(self):
        """Ask the remote what is new. This is the one route here that waits on
        the network, and it only ever runs because someone pressed the button."""
        py = update._python()
        try:
            r = subprocess.run([py, str(ROOT / "scripts" / "update.py"),
                                "--check", "--json"],
                               capture_output=True, text=True, timeout=90, cwd=str(ROOT))
        except (OSError, subprocess.SubprocessError) as e:
            self._send(json.dumps({"ok": False, "error": "could not run the check: "
                                   + type(e).__name__}), "application/json")
            return
        # --json puts the survey on the last line; anything before it is noise
        # worth keeping out of the JSON parse but worth showing if it failed.
        line = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            self._send(json.dumps(json.loads(line)), "application/json")
        except ValueError:
            self._send(json.dumps({"ok": False, "error":
                                   (r.stderr.strip() or line or "the check said nothing")[:400]}),
                       "application/json")

    def _post_update(self):
        """Start the update and answer at once. Nothing is streamed back from
        here: the update outlives this process by design (it restarts us), so
        the page follows update.log through GET /update instead and waits for
        the server to come back the same way a restart does."""
        try:
            _spawn_update()
        except OSError as e:
            self._send(json.dumps({"ok": False, "error": "could not start the update: "
                                   + type(e).__name__}), "application/json")
            return
        self._send(json.dumps({"ok": True, "text": "updating - watch the log below."}),
                   "application/json")

    def _post_restart(self):
        """Restart the server, the cron watcher, or both. Answer FIRST - once
        the server goes, there is nothing left to reply with."""
        body = self._body() or {}
        what = body.get("what", "server")
        notes = []
        if what in ("cron", "both"):
            notes.append(_restart_cron())
        if what in ("server", "both"):
            notes.append("server restarting - the page will reconnect on its own.")
        self._send(json.dumps({"text": " ".join(notes) or "nothing to restart."}),
                   "application/json")
        if what in ("server", "both"):
            _restart_self()

    def _stream(self):
        """Server-sent events. Holds this connection open and forwards every
        broadcast; a silent stretch gets a keepalive comment, which doubles as
        the way we notice the page has gone."""
        q = queue.Queue()
        with _streams_lock:
            _streams.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    self.wfile.write(("data: " + q.get(timeout=15) + "\n\n").encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            pass  # page closed - normal, not an error
        finally:
            with _streams_lock:
                _streams.remove(q)


def lan_ip():
    """The address this machine is actually reachable on from the rest of the
    network, or None. Found by asking the routing table which interface a
    packet to the internet would leave by - a UDP socket is connect()ed but
    nothing is ever sent, so this needs no network and no name lookup. The
    hostname is deliberately NOT used for this: on Debian and its children
    that resolves to 127.0.1.1, which no phone can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _cert_names():
    """The certificate's subjectAltName list. A browser checks the address you
    TYPED against these, so the LAN IP has to be in here - a cert naming only
    the hostname is rejected at https://192.168.x.x no matter how valid it is."""
    names = ["DNS:localhost", "IP:127.0.0.1"]
    host = socket.gethostname()
    if host and host != "localhost":
        names.append("DNS:" + host)
        names.append("DNS:" + host + ".local")  # what mDNS answers to
    ip = lan_ip()
    if ip:
        names.append("IP:" + ip)
    return names


def _make_cert():
    """Generate the self-signed certificate. True if there's a usable cert
    afterwards.

    Regenerated when the addresses it was made for have changed, not only when
    it's missing: a router handing this machine a new DHCP lease leaves the old
    cert naming an IP nobody can reach any more, and a browser then refuses the
    address you actually typed. The names are kept beside the cert because
    reading them back out of it means parsing a certificate, and this is the
    only thing that writes either file.

    The certificate is made in-process with the `cryptography` package rather
    than by shelling out to openssl, so an install needs nothing but its own
    pip packages - which is what makes the server run on Windows, where no
    openssl binary exists. It also keeps the SAN list on this machine's side of
    the fence, where the same code can compare it without parsing anything.
    """
    names = _cert_names()
    want = ",".join(names)
    if CERT_FILE.exists() and KEY_FILE.exists():
        try:
            if NAMES_FILE.read_text().strip() == want:
                return True
        except OSError:
            pass  # pre-dates this file, or unreadable - remake it
        print("network address changed - making a new certificate")
    CERTS.mkdir(parents=True, exist_ok=True)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("certificate generation needs the 'cryptography' package -"
              " install it with: pip install cryptography")
        return False
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        san = []
        for part in names:
            kind, _, value = part.partition(":")
            if kind == "DNS":
                san.append(x509.DNSName(value))
            elif kind == "IP":
                try:
                    san.append(x509.IPAddress(ipaddress.ip_address(value)))
                except ValueError:
                    continue  # a LAN address that's gone - skip, don't fail
        who = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "uniagent")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(who)
                .issuer_name(who)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                # A day of back-dating on the start so a client whose clock is
                # slightly ahead doesn't reject a brand-new cert.
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
                .add_extension(x509.SubjectAlternativeName(san), critical=False)
                .sign(key, hashes.SHA256()))
        KEY_FILE.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
        CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    except Exception as e:
        print("certificate generation failed - "
              + type(e).__name__ + ": " + str(e))
        return False
    # The key is as good as a password for this origin - keep it to this user.
    try:
        KEY_FILE.chmod(0o600)
    except OSError:
        pass  # Windows has no real chmod; the file sits in the user's profile
    NAMES_FILE.write_text(want + "\n")
    print("made a self-signed certificate in " + str(CERTS))
    print("  covering: " + want)
    return True


class TLSServer(ThreadingHTTPServer):
    """https, with the handshake on the connection's own thread.

    The obvious way to do TLS here is to wrap the LISTENING socket once, and
    that is a trap: the handshake then happens inside accept(), which runs on
    the single serve_forever() thread. One client that opens a connection and
    never finishes the handshake blocks every other connection, from
    everywhere, until TCP itself gives up minutes later. On a LAN that never
    showed - a browser handshakes in milliseconds - but on a forwarded port it
    wedges the server within the hour, because the internet is full of scanners
    that connect, send nothing and leave.

    So the listening socket stays plain. get_request() accepts and hands back
    an unwrapped socket, and finish_request() - which ThreadingMixIn has
    already moved onto a per-connection thread - does the handshake there. A
    stalled client now costs one thread, and times out.

    Also why the noise is turned down: a plain-http request to this port, or a
    browser hanging up at the certificate warning, comes out of the handshake
    as an SSLError. That's someone knocking on the wrong door, not a fault
    here, and a traceback per occurrence would bury the chat - which matters
    far more now that the door faces the internet.
    """

    daemon_threads = True
    # The default is 5: the queue the kernel holds connections in between our
    # accepts. A handful of simultaneous probes fills that, and everything
    # arriving after it is refused or silently times out.
    request_queue_size = 128

    def __init__(self, addr, handler, ctx):
        self.ctx = ctx
        super().__init__(addr, handler)

    def get_request(self):
        """A plain accepted socket. Nothing the client controls may be waited
        for here - this runs on the accept loop, and blocking it stops the
        whole server."""
        sock, addr = self.socket.accept()
        sock.settimeout(HANDSHAKE_TIMEOUT)
        return sock, addr

    def finish_request(self, request, client_address):
        """Handshake, then serve - both on this connection's own thread.

        wrap_socket DETACHES the socket it is given (its fd goes to -1), so the
        plain `request` socketserver later calls shutdown_request() on is an
        empty shell and closes nothing. Closing the wrapped socket is therefore
        this method's job; without the finally below, every connection leaks an
        fd until the process hits its limit.
        """
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # not a TLS client, or it gave up. Nothing to answer.
        try:
            tls.settimeout(REQUEST_TIMEOUT)
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            tls.close()

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ssl.SSLError, socket.timeout)):
            super().handle_error(request, client_address)


class RedirectHandler(BaseHTTPRequestHandler):
    """All PORT (http) answers with: go to https, same host, same path.

    Kept rather than closing the port so that an old bookmark, a typed
    "<ip>:8763" or the desktop shortcut still arrive somewhere useful instead of
    a connection refused. It never reads a body and never touches a route - so
    nothing sensitive can be asked for here, let alone answered.

    The host comes from the request's own Host header, so whichever address you
    reached this machine by is the one you get sent back to - redirecting to
    lan_ip() would bounce a localhost visitor onto the LAN address and break the
    cert match for anyone using the hostname.
    """

    def log_message(self, fmt, *args):
        pass

    def _go(self):
        host = (self.headers.get("Host") or lan_ip() or "localhost")
        host = host.rsplit(":", 1)[0]  # drop :8763, add the https port below
        if ":" in host:               # a bare IPv6 literal, needs its brackets
            host = "[" + host.strip("[]") + "]"
        self.send_response(308)  # 308, not 302: keeps a POST a POST
        self.send_header(
            "Location",
            "https://" + host + ":" + str(HTTPS_PORT) + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _go


def _serve_redirect():
    """The http listener, on its own thread. If the port is taken there's
    nothing to redirect and https carries on without it - a lost redirect is an
    inconvenience, not a reason to have no server."""
    try:
        ThreadingHTTPServer((HOST, PORT), RedirectHandler).serve_forever()
    except OSError as e:
        print("http redirect off - " + type(e).__name__ + ": " + str(e))


def _https_server():
    """The TLS listener, or None if it could not be built.

    Unlike every other failure in this file this one is fatal to the caller, and
    deliberately: https is the only thing serving the app now, so falling back
    to plain http would silently put the password and the session cookie on the
    wire in the clear. Better to stop and say why."""
    if not _make_cert():
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        # The context is handed to the server rather than used to wrap the
        # listening socket here: TLSServer wraps each accepted connection on
        # that connection's thread. See its docstring for why that difference
        # is the whole ballgame.
        return TLSServer((HOST, HTTPS_PORT), Handler, ctx)
    except (OSError, ssl.SSLError) as e:
        print(type(e).__name__ + ": " + str(e))
        return None


def serve():
    # Before anything is listening: generates and prints the password if this
    # is a fresh install, so that line is at the top of the output rather than
    # buried under whatever the first turn logs.
    auth.password()
    httpd = _https_server()
    if httpd is None:
        print("\nCannot start: no working https certificate, and the app is"
              " https-only.\nInstall the 'cryptography' package (pip install"
              " cryptography)\nand check that port " + str(HTTPS_PORT)
              + " is free.")
        sys.exit(1)
    ip = lan_ip() or "localhost"
    print("Uniagent: https://" + ip + ":" + str(HTTPS_PORT))
    print("  (http://" + ip + ":" + str(PORT) + " redirects here)")
    # The terminal's chat, not the app's - every browser window carries its own
    # now, so there is no single chat the server is "in" to print here.
    print("terminal chat: " + str(main.current.path))
    # Subagent reports come back through here, aimed at the chat that spawned
    # the subagent - current or not - and stream to the page the moment they
    # land, marked as a report, not something the user typed.
    main.notify = lambda note, origin: _run_turn(note, kind="report",
                                                 target=main.chat(origin))
    # /stop doesn't ask a turn to end, it ENDS it: by the time this is called
    # the transcript has been closed out and the chat handed on, and only an
    # abandoned thread is still winding down somewhere - and that thread can no
    # longer reach this page (main.turn guards every callback it holds). So this
    # says what is true, in the same breath and in the same shape as a turn
    # finishing normally: seal the bubble, and then everything "done" does.
    #
    # Saying it in full here is the whole reason the page can react instantly.
    # It used to send only "stopped" and then sleep 200ms hoping the worker had
    # let go of the chat, which is why the busy bar lingered and a queued
    # message sat greyed out waiting for a "done" that was still minutes away.
    def _on_stop_callback(stem):
        # main.on_stop is given the bare id (that's what the stop set keys off),
        # so it's mapped to the route the page filters events by.
        route = main.route_of(stem)
        # Seals the streaming bubble; the page draws nothing more into it.
        _broadcast({"type": "stopped", "chat": route})
        # And the turn is over - this is what drops the "main agent working"
        # bar and sends whatever the user had queued behind it.
        _broadcast({"type": "done", "chat": route})
        c = main.open_agent(stem)
        if c is not None:
            _broadcast_context(c)
        # The busy dot, and this chat's new position in the by-recency order.
        _broadcast_chats()

    main.on_stop = _on_stop_callback
    main.on_restart = _restart_self
    # The only thing in the context panel that arrives on its own schedule: a
    # background token count coming back. Everything else the panel shows moves
    # because something here made it move, and is broadcast at that point.
    tokens.on_settled = _on_tokens_settled
    # The hold-to-talk key, which has no browser behind it to say which chat it
    # means - so it aims at the one last spoken to. Everything arriving over
    # HTTP names its own chat and never comes through here.
    voice_input.start(lambda text: _run_turn(text, target=_voice_chat()))
    # The only thing left watching anything on a timer, and it watches with
    # stat() rather than by reading - see _watch_chats.
    threading.Thread(target=_watch_chats, daemon=True).start()
    threading.Thread(target=_serve_redirect, daemon=True).start()
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
