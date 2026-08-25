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

import concurrent.futures as futures
import datetime
import hashlib
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import _term
import auth
import claude_session
import command_processor
import compaction
import cron
import main
import market
import provider
import provider_refs
import service
import settings
import tokens
import tool_processor
import tool_validation
import update
import usage
import wires
import workspace
import turnctx
import voice_input
import wake_word
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

# POST /attach: the biggest file we'll take into a chat's attachments folder.
# Generous, because the point of attaching something is that it is the thing
# you want looked at - but bounded, since the body is written to disk as it
# arrives and a missing bound is an invitation to fill the disk. Nothing here
# is loaded into memory whole; see _post_attach.
MAX_UPLOAD = 200 * 1024 * 1024
UPLOAD_BLOCK = 1024 * 1024

# Everything a file name may not carry on either platform this runs on: the
# separators, and the set Windows refuses. A name off the wire is hostile
# until proven otherwise - only its last segment is kept, so "../../.env"
# becomes a file called ".env" INSIDE the chat rather than one written
# anywhere else.
_BAD_IN_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

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


# ---- the response in flight ---------------------------------------------
# What the CURRENT response of each running turn has done so far: how long it
# has been waiting, what it has thought, what it has written. Keyed by route,
# the same id every broadcast carries.
#
# This exists because everything else about a turn is already recorded and this
# was not. A finished message carries its timing and its thinking on the
# history turn itself (main.run's spoke(), and _partial_turn for the two ways a
# response ends early), so any window that opens afterwards rebuilds it exactly.
# The response still being written had no record anywhere: it existed only as
# broadcasts, and a broadcast is only seen by a window that is looking at that
# chat at that moment. Switch away mid-think and come back and the wait, the
# thinking and the half-written reply were simply gone until the turn ended.
#
# Kept in memory rather than in a file beside the chat, which is where the
# safety verdicts and the timestamps live (main.VALIDATIONS, main.STAMPS). Those
# two describe things that HAPPENED and must outlive everything; this describes
# something happening NOW, and it cannot outlive the process that is doing it -
# a turn dies with the server, so a copy of this on disk could only ever be
# read back as a response that looks live and never moves again. The route it
# is served on and the way the page consumes it are exactly the sidecar
# pattern those two use; only the storage differs, and it differs because the
# lifetime does.
_live = {}
_live_lock = threading.Lock()


def _live_set(route, **fields):
    """Update the in-flight record for `route`, creating it if needed."""
    with _live_lock:
        at = _live.setdefault(route, {"started": time.time()})
        at.update(fields)


def _live_begin(route):
    """Start a fresh record: a turn is beginning, or its last response has
    finished and the next one is coming.

    The rule this keeps is the whole point of the thing: THE RECORD EXISTS FOR
    EXACTLY AS LONG AS THE TURN DOES. It used to be created when the request
    went out and thrown away when the response finished, which left two gaps
    where a turn was plainly running and a window rejoining it found nothing to
    draw - the setup before the first request (taking the chat's turn slot,
    building the prompt, serialising the tool schemas), and the space between
    one response finishing and the next request going out (the safety check,
    the tool actually running). Landing in either of those was the difference
    between "it restores what I left" and "it restores what I left, usually",
    which is the worse of the two by a distance.

    A new record starts in the waiting phase because that is what is true of
    it: the turn is working and no token is on its way yet."""
    _live_set(route, started=time.time(), phase=None, phase_at=None,
              thinking="", partial="", thought=None, latency=None)
    _live_phase(route, "waiting")


def _live_phase(route, phase):
    """Move the record into `phase`, stamping WHEN it got there - but only on
    an actual change.

    The two streaming phases are reported per token, so a plain assignment
    would push the phase's start forward with every fragment and the page would
    rebuild a model that had been thinking for a minute as one that had just
    started. The clock has to belong to the phase, not to the last token of
    it."""
    with _live_lock:
        at = _live.setdefault(route, {"started": time.time()})
        if at.get("phase") != phase:
            now = time.time()
            # Leaving the wait is the arrival of the first token, and so the
            # one moment the latency is known. Stamped here rather than worked
            # out later from the current phase's start: by the time the model
            # is writing, that start is the end of the THINKING, and a reader
            # deriving the wait from it would report the wait plus everything
            # the model thought - which is how a 1.4s wait came to be drawn as
            # 3.8s on a chat rejoined mid-reply.
            # `is None` rather than a membership test: the key is always
            # there, reset to None by the start of each response.
            if at.get("phase") == "waiting" and at.get("latency") is None:
                at["latency"] = max(0.0, (now - at.get("started", now)) * 1000)
            at["phase"] = phase
            at["phase_at"] = now


def _live_add(route, field, text):
    """Append to one of the record's two growing texts (the thinking, the
    reply). Separate from _live_set because these arrive a token at a time and
    concatenating under the lock is the whole operation."""
    with _live_lock:
        at = _live.get(route)
        if at is not None:
            at[field] = at.get(field, "") + text


def _live_clear(route):
    with _live_lock:
        _live.pop(route, None)


def _live_get(route):
    """A copy of the record, or None. Copied under the lock so a reader can
    never serialise a dict another thread is mid-update on."""
    with _live_lock:
        found = _live.get(route)
        return dict(found) if found else None


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


# Messages typed into a chat that is ALREADY working, waiting to be folded into
# the turn already running rather than to start one of their own. Keyed by the
# bare chat id, oldest first; main.run asks _drain_inject() for them at the top
# of every pass and files whatever it gets as a labelled user turn (main.py's
# MID_TURN). That is what "enter, mid-turn" means on the page: the message
# lands the moment the running tool call comes back, instead of waiting out the
# whole turn - the same arrangement cli.py has had at the terminal, where enter
# injects and tab queues.
#
# What is still in here when a turn ends never had a pass to land in (the model
# stopped calling tools and just answered). It is not dropped: _run_turn's
# worker takes it at the end and runs it as the next turn, which is the "or
# when the turn finishes" half of the promise the page makes when it greys the
# bubble.
_inject_lock = threading.Lock()
_injects = {}


def _queue_inject(c, text):
    """Hold `text` for the turn `c` is running now. False when it isn't running
    one, in which case the caller starts an ordinary turn with it instead.

    The check and the append are under one lock, and so is the drain at the end
    of a turn, so the text is either read by the turn that is running, taken by
    that turn's leftover pass, or refused here - never quietly stranded in a
    queue nothing will look at again."""
    with _inject_lock:
        if not c.slot.held():
            return False
        _injects.setdefault(c.id, []).append(text)
        return True


def _drain_inject(stem, route=None):
    """Everything waiting for `stem`, as one string, or None. Joined blank-line
    separated for the same reason flushQueue joins on the page: two thoughts
    typed while the agent worked are one interruption, not two.

    `route` is given when this is a real injection into a running turn, and the
    page is told about it here - at the moment the text actually enters the
    conversation, which is what un-greys the bubble it has been holding. The
    leftover drain passes none: that text is about to be a turn of its own, and
    _run_turn broadcasts its message itself."""
    with _inject_lock:
        texts = _injects.pop(stem, None)
    if not texts:
        return None
    if route:
        for t in texts:
            # One event per message rather than one for the joined text, so a
            # page holding two grey bubbles clears both. The history keeps them
            # as the single turn they were folded in as, and the end-of-turn
            # redraw reconciles the two - which is exactly what it is for.
            _broadcast({"type": "user", "text": t, "chat": route,
                        "at": int(time.time())})
    return "\n\n".join(texts)


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


def _asks_when(handler):
    """When this message wants to be sent, off "?when=" - "tool" for one typed
    with enter into a working chat (fold it into the turn already running),
    anything else for the ordinary "start a turn with it" path. See
    _queue_inject."""
    q = parse_qs(urlparse(handler.path).query)
    return q.get("when", [""])[0]


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

# How many threads look at the chat folder at once - see _chat_files.
_SCAN_THREADS = 8


def _chats_signature():
    """Something cheap that changes whenever the chat list would look
    different: how many chats there are and the newest mtime among them.

    stat() only - no reading, no parsing. Over ~800 chats this is about 150ms,
    against the ~600ms and 55MB of reading it takes to build the list itself,
    which is exactly why the list is fetched on a signal and this is what
    watches for one. See _chat_files, which is the scan both share."""
    try:
        rows = _chat_files()
        cron = [p.stat().st_mtime for p in
                (main.CHATS / "cron").glob("*/*/" + main.HISTORY_FILE)]
    except OSError:
        return None
    mtimes = cron + ([rows[0][1]] if rows else [])  # rows are newest first
    return len(rows) + len(cron), max(mtimes, default=0)


def _watch_chats():
    """Broadcast a chats signal when the list changes underneath us.

    Everything the web front-end does to the list says so directly (see
    _broadcast_chats's callers); this is only here for the cron watcher, which
    is another process entirely and can't. Half a minute of staleness on a
    scheduled job's row is fine - it's a job nobody is sitting and watching -
    and this costs a couple of milliseconds to check.

    The first pass builds the whole list rather than just signing it, which is
    what fills the label cache (see _label_of). Nobody is waiting on this
    thread at startup, so the one expensive read of every chat on disk happens
    here, in the background, seconds before the first page asks - rather than
    on that page's own /chats request while someone watches an empty sidebar."""
    try:
        _chats()
    except Exception:
        pass  # a failed warm-up costs a slow first listing, nothing else
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


# The replies the page has been told to read out, by id. A finished turn puts
# its text here and broadcasts the id; the page fetches GET /speak?id=<n> and
# plays what comes back.
#
# The audio is made on that fetch rather than when the turn ends, so an install
# with nobody watching never pays to synthesise anything - a cron run at 4am,
# or a phone that has been shut since morning, costs nothing. It is made ONCE
# and kept here because several windows can be open on the same chat, and each
# of them asks: without this, the desktop and the phone would each buy their
# own copy of the same sentence.
#
# A summary, when the voice tab asks for one, is written on that same fetch and
# for the same reason - it is another model call, and a turn nobody is
# listening to shouldn't pay for one.
#
# One entry per MESSAGE, not per turn. A turn of tool work is several things
# said, and both the reading and the summarising happen a message at a time -
# so the page gets a list of ids and plays them one after another.
_speak_lock = threading.Lock()
_speak_said = {}          # id -> {"route", "batch", "text", "summarise", "audio", "mime", "error"}
_speak_id = 0
# How many messages stay readable before the oldest are dropped. Generous
# because a reading is never interrupted by anything except stop: the queue on
# the page runs across turns, so the speaker can be a long way behind a chat
# that has kept working - and an id dropped out from under it is the end of
# that reading, mid-sentence, with an error where the rest of the words were.
SPEAK_KEEP = 48
# A reply this short (in characters) is spoken as written, even in the summary
# modes. Roughly a sentence or two - past this a reply is worth boiling down,
# under it there is nothing to boil and the summariser is pure latency.
SPEAK_VERBATIM = 180
# Readings that have already complained about the summarising model, by batch -
# the turn the message came from, since in the per-message modes each message
# is registered on its own as it lands. A dead summariser fails once per
# message, and six lines saying the same thing about the same turn is not six
# times as informative as one.
_speak_moaned = set()
_speak_batch_id = 0


def _speak_batch():
    """A tag for one turn's worth of reading, unique in this process. Its own
    counter rather than the message ids, because a turn's tag has to be settled
    before any of its messages exist."""
    global _speak_batch_id
    with _speak_lock:
        _speak_batch_id += 1
        return "turn-" + str(_speak_batch_id)


def _speak_offer(route, texts, summarise=False, batch=None):
    """Register each of `texts` as something to read out for chat `route`, in
    order, and tell every open page about the lot. `summarise` puts each
    message through the summarising model first, when the page comes to ask for
    its audio. `batch` ties them to the turn they came from - only used to keep
    one turn's complaints down to one. Does nothing at all when the voice tab
    names no speaker, which is the default and means the page stays silent."""
    global _speak_id
    if not texts:
        return
    try:
        chosen = settings.load()
    except Exception as e:
        _speak_note(route, "nothing was read out: " + type(e).__name__ + ": " + str(e))
        return
    # No speaker named is not a failure - it is the voice tab's own default,
    # and an install that has never opened it stays silent without being told
    # so after every single turn.
    if not chosen.get("speak_provider"):
        return
    ids = []
    with _speak_lock:
        batch = batch or "msg-" + str(_speak_id + 1)
        for text in texts:
            _speak_id += 1
            ids.append(_speak_id)
            _speak_said[_speak_id] = {"route": route, "batch": batch, "text": text,
                                      "summarise": summarise, "lock": threading.Lock(),
                                      "audio": None, "mime": "", "error": ""}
        # SPEAK_KEEP is counted in messages, so it has to be big enough to hold
        # a whole turn's worth of them and the turn before it - dropping an id
        # the page is still working through would silence the end of a reading
        # that had already started.
        for old in sorted(_speak_said)[:-SPEAK_KEEP]:
            _speak_moaned.discard(_speak_said[old]["batch"])
            del _speak_said[old]
    # The playback speed rides along with the ids because the page has no other
    # way to learn it: settings are only fetched when the settings overlay is
    # opened, and a window that has been sitting on a chat since this morning
    # has never asked. Sent per offer, so changing the slider and saving is in
    # force for the very next thing the agent says.
    _broadcast({"type": "speak", "chat": route, "ids": ids,
                "speed": chosen.get("speak_speed", 1)})


def _moan_once(batch):
    """True the first time this turn's reading has something to complain about,
    False every time after - checked and claimed under the one lock, since the
    messages of a turn are synthesised on whichever threads ask for them."""
    with _speak_lock:
        if batch in _speak_moaned:
            return False
        _speak_moaned.add(batch)
        return True


def _speak_note(route, text):
    """Tell chat `route`'s open windows something went wrong with reading a
    reply out, without ending the reading. Its own event type rather than an
    "error": that one means the TURN blew up, and the page tears down the live
    reply when it sees one."""
    _broadcast({"type": "speaknote", "chat": route, "text": text})


# The voice tab's speak_mode, as two questions this file actually asks (see
# settings.SPEAK_MODES): does THIS message get read, and is it summarised
# first. Everything but "summary" is decided a message at a time, the moment
# the message exists - which is what makes the speaking start while the tool it
# just asked for is still running, instead of after the whole turn is over.
#
# The mode's kinds, against main.run's on_message: "answer" is a message that
# ended the turn, "call" is one that ended in a tool call.
SPEAK_KINDS = {
    "final": ("answer",),
    "summary_final": ("answer",),
    "all": ("answer", "call"),
    "summary_each": ("answer", "call"),
}


def _speak_message(route, text, kind, batch=None):
    """Read one finished message out, if the voice tab's mode wants that one.
    Called from inside the turn, as each message lands.

    "summary" is the one mode not handled here: a single account of the whole
    turn cannot be written until there IS a whole turn, so it waits for
    _speak_turn below. Every other mode speaks from here, as early as the words
    exist."""
    try:
        mode = settings.load().get("speak_mode", "final")
    except Exception as e:
        _speak_note(route, "nothing was read out: " + type(e).__name__ + ": " + str(e))
        return
    if kind not in SPEAK_KINDS.get(mode, ()):
        return
    _speak_offer(route, [text], mode.startswith("summary"), batch)


def _speak_turn(agent, mark):
    """What to read out once the whole turn is over, as (messages, summarise) -
    ([], False) in every mode that has already read it out message by message.
    `mark` is where the turn started in the history, from main.turn_count().

    Only "summary" ends up here, because only it needs the turn entire: every
    message the agent wrote, joined into one thing for the summarising model,
    so what you hear is a single account of the turn rather than a sentence
    about each step of it. The joining is blank-line separated, the way the
    messages already sit apart from each other in the transcript.

    It doesn't summarise here either: it says that it needs doing, and
    _speak_audio does it if and when the audio is actually asked for."""
    if settings.load().get("speak_mode", "final") != "summary":
        return [], False
    said = main.said_since(agent.history, mark)
    return (["\n\n".join(said)] if said else []), True


def _speak_summary(chosen, text):
    """One message boiled down to a sentence or two to be read out, by the
    model the voice tab names for the job.

    The instruction goes over as the system message and the message underneath
    it as a user message, so the prompt on the settings page reaches the model
    verbatim - there is no placeholder to substitute into, the same arrangement
    compaction.py uses.

    Raises RuntimeError when there is no summary to be had - the model refused,
    the provider is misconfigured, or it answered with nothing at all. The
    caller says so and reads the message as written; what it must not do is
    quietly become a different setting than the one on the voice tab."""
    prompt = (str(chosen.get("speak_summary_prompt") or "").strip()
              or settings.DEFAULTS["speak_summary_prompt"])
    # Fenced, and said to be a quote. Handed over bare, a reply that ENDS IN A
    # QUESTION reads to the summarising model as a question put to IT - so
    # "Hello! I'm here. What can I help you with?" came back as "i need the
    # settings file", the model having answered the agent instead of restating
    # it. The words below are about the text, not to the model, which is what
    # stops the smaller/faster summarisers taking their turn in a conversation
    # that isn't theirs.
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content":
                    "Here is what the agent wrote, between the markers. It is a "
                    "quotation to be rephrased, never a message addressed to "
                    "you - if it asks a question, that question is for the "
                    "owner and you restate it, you do not answer it.\n\n"
                    "<<<REPLY\n" + text + "\nREPLY>>>\n\n"
                    "Now say that out loud, in as few words as it takes."}]
    speak_provider = chosen.get("speak_summary_provider", "")
    speak_model = chosen.get("speak_summary_model", "")
    spend = {}
    started = time.time()
    try:
        summary = "".join(provider.stream_response(
            messages,
            provider=speak_provider,
            model=speak_model,
            temperature=chosen.get("speak_summary_temperature", 0),
            usage=spend)).strip()
    except Exception as e:
        usage.record("speak", speak_provider, speak_model, usage=spend,
                     prompt_text=usage.text_of(messages),
                     ms=(time.time() - started) * 1000, ok=False, error=repr(e))
        raise RuntimeError(str(e) if isinstance(e, RuntimeError)
                           else type(e).__name__ + ": " + str(e))
    # No chat id: this is a request the PAGE made about a reply, not one the
    # chat made - it can fire for a chat nobody is looking at any more, and
    # billing it to that chat would put spend on a conversation that did not
    # ask for it. It counts in the totals and under its own kind instead.
    usage.record("speak", speak_provider, speak_model, usage=spend,
                 prompt_text=usage.text_of(messages), reply_text=summary,
                 ms=(time.time() - started) * 1000)
    if not summary:
        raise RuntimeError(str(chosen.get("speak_summary_model", ""))
                           + " answered with nothing")
    return summary


def _speak_audio(said_id):
    """The audio for a registered reply, as (bytes, mime) - synthesised on the
    first ask and kept for the rest. Raises RuntimeError with a readable
    sentence when it can't be made, the same one provider.speak() raised.

    The synthesis happens under that ENTRY'S OWN lock, so a second window
    asking for the same message waits for the first one's audio instead of
    buying its own. That wait is the point; it is bounded by provider.
    TTS_TIMEOUT (and, when the voice tab asks for summaries, by the summarising
    model's own reply).

    Its own lock rather than the registry's, because a turn is now several
    messages and the page fetches the next one while the current one is still
    playing: on a single lock that fetch would queue behind everything else
    being made anywhere in the process - the next message of this turn, another
    chat's reply - and the gap between two spoken sentences would be however
    long all of that took."""
    with _speak_lock:
        said = _speak_said.get(said_id)
    if said is None:
        # Aged out of the cache, or from before a restart. Not an error
        # worth shouting about - the reply it belonged to is long read.
        raise RuntimeError("that reply is no longer waiting to be read out")
    with said["lock"]:
        if said["audio"] is not None:
            return said["audio"], said["mime"]
        if said["error"]:
            # A failure is remembered too: three windows open on a chat whose
            # speech model is wrong should say so once each, not retry it
            # three times over a fresh network round trip apiece.
            raise RuntimeError(said["error"])
        chosen = settings.load()
        # Already short enough to say. Summarising this costs a whole round
        # trip to another model before a single word can be spoken, to shorten
        # something that is not long - and it is exactly where a summariser has
        # nothing to compress and so invents instead. Read it as written: it is
        # faster, cheaper, and it is what the sentence actually says.
        if said["summarise"] and len(said["text"].strip()) <= SPEAK_VERBATIM:
            said["summarise"] = False
        if said["summarise"]:
            # Settled once per entry either way, so a second window - or a
            # retry after a speech failure - reads the summary that was already
            # written rather than paying for another one that says the same
            # thing in different words, and doesn't repeat the complaint below
            # if there wasn't one.
            said["summarise"] = False
            try:
                said["text"] = _speak_summary(chosen, said["text"])
            except Exception as e:
                # Read as written instead - the words are there and silence
                # would be worse - but SAY that this is not the setting the
                # voice tab is on. A summariser that has been quietly bypassed
                # for a week is the thing to avoid here: the reading gets long
                # and nothing anywhere explains why.
                #
                # Once per turn's reading, not once per message of it: the
                # complaint is about a setting, and it is the same complaint
                # every time whichever message hit it.
                if _moan_once(said["batch"]):
                    _speak_note(said["route"],
                                "no summary to read (" + str(e) + ") - reading "
                                "the reply as written instead")
        try:
            audio, mime = provider.speak(chosen.get("speak_provider", ""),
                                         chosen.get("speak_model", ""), said["text"],
                                         voice=chosen.get("speak_voice", ""),
                                         instructions=chosen.get("speak_instructions", ""))
        except Exception as e:
            said["error"] = str(e) if isinstance(e, RuntimeError) else \
                            type(e).__name__ + ": " + str(e)
            raise RuntimeError(said["error"])
        said["audio"], said["mime"] = audio, mime
        return audio, mime


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
        # Recorded first, broadcast second. The record is what a window that
        # is NOT watching will rebuild from; the broadcast only reaches the
        # ones that are.
        _live_add(route, "partial", chunk)
        _live_phase(route, "writing")
        _broadcast({"type": "chunk", "text": chunk, "chat": route})

    # Where this turn starts in the chat's history. The "read everything out"
    # modes need to tell what this turn said from what the conversation already
    # contained, and afterwards nothing in the history marks the boundary.
    #
    # Filled in by main.turn the moment it owns the chat, not here: a message
    # sent while another turn is still running waits inside turn() for the slot,
    # and a mark taken now would be from before that turn had written anything -
    # so its whole reply would be read out a second time on the back of this one.
    mark = [main.turn_count(c.history)]
    # One tag for everything this turn has read out, however many messages that
    # turns out to be - so a summarising model that is down says so once for
    # the turn instead of once per message of it.
    batch = _speak_batch()

    def worker():
        try:
            main.turn(c, text,
                      on_text=stream_chunk,
                      on_begin=lambda history: mark.__setitem__(0, main.turn_count(history)),
                      on_tool_call=lambda shown: _broadcast({"type": "toolcall",
                                                        "text": shown, "chat": route}),
                      # `name` is which call this is the result of, when the
                      # caller knows - the page draws it on the box so several
                      # calls at once stay tellable apart. Optional, so a caller
                      # that doesn't pass one behaves exactly as before.
                      on_tool_result=lambda result, name=None, took=None: _broadcast(
                                                       {"type": "toolresult",
                                                        "text": result, "name": name,
                                                        "timing": took,
                                                        "chat": route}),
                      # The model thinking, live. Its own event and never
                      # folded into "chunk": the reply bubble is the reply,
                      # and thinking appended to it would be indistinguishable
                      # from the model having said it out loud. The page draws
                      # it in a block of its own above the bubble, open while
                      # it is happening and folded to a one-line "thought for
                      # 8.4s" once the answer starts - which is the whole
                      # difference between a local model that looks hung for
                      # forty seconds and one you can watch working.
                      on_reasoning=lambda text: (
                          _live_add(route, "thinking", text),
                          _live_phase(route, "thinking"),
                          _broadcast({"type": "thinking", "text": text,
                                      "chat": route})),
                      # What that message cost in time, sent the moment the
                      # response is complete and BEFORE the tool call or the
                      # answer it turned out to be - so the page seals the
                      # bubble it is already streaming into with the numbers
                      # on it, rather than drawing them and then rewriting
                      # them at the end-of-turn redraw. The same dict is
                      # stored on the turn, so the redraw agrees with it.
                      on_timing=lambda spent: (
                          # This response is complete and is a turn in the
                          # history now, so the record hands over to the next
                          # one rather than disappearing - the turn is still
                          # running, and whatever comes next (a safety check, a
                          # tool, another request) is still something to wait
                          # on. Its text is dropped here, which is what stops
                          # the finished message being drawn twice: once from
                          # the record and once from the history.
                          _live_begin(route),
                          _broadcast({"type": "timing", "timing": spent,
                                      "chat": route})),
                      # Thinking has just ended, and these are its numbers.
                      # Sent separately from "timing" - which does not arrive
                      # until the whole response is over - because this is the
                      # instant the block on screen stops saying "thinking",
                      # and the rate belongs in that label rather than turning
                      # up seconds later to replace it.
                      on_thought=lambda spent: (
                          _live_set(route, thought=spent),
                          _broadcast({"type": "thought", "timing": spent,
                                      "chat": route})),
                      # A model that never sent a content chunk at all - its
                      # whole answer came back on the reasoning channel and
                      # main._stream is handing it over as the reply instead
                      # of on_text, to avoid streaming the same words a
                      # second time as their own new "chunk" broadcast (see
                      # main._stream's on_reclassify and provider.py's
                      # _read_openai tail). The live record follows: the
                      # thinking this turn accumulated is cleared - it is the
                      # reply now, not thinking a redraw should fold into its
                      # own block - and the same text goes into `partial`
                      # exactly as stream_chunk records a normal reply, so a
                      # window that rejoins mid-turn rebuilds a reply bubble
                      # rather than a thinking block.
                      on_reclassify=lambda text: (
                          _live_set(route, thinking=""),
                          _live_add(route, "partial", text),
                          _live_phase(route, "writing"),
                          _broadcast({"type": "reclassify", "text": text,
                                      "chat": route})),
                      # A request going out. The page starts counting the wait
                      # from here - which on a local model with a long
                      # conversation is the longest stretch of a turn and the
                      # only one with nothing on screen. The measured figure
                      # follows on "timing" as `latency` and replaces it.
                      on_request=lambda: (
                          # A new response, so the previous one's text goes -
                          # it is in the history by now, as its own turn.
                          _live_begin(route),
                          _broadcast({"type": "waiting", "chat": route})),
                      # `checked` False is the gate being OFF for this chat, not
                      # a verdict - the page draws that row differently.
                      on_safety=lambda safe, reason, checked=True: _broadcast(
                                                       {"type": "safety",
                                                        "safe": safe, "reason": reason,
                                                        "checked": checked,
                                                        "chat": route}),
                      # Each finished message, read out as it lands rather than
                      # at the end of the turn - so a long turn talks its way
                      # through the work instead of going quiet for a minute
                      # and then saying everything at once.
                      on_message=lambda said, kind: _speak_message(route, said, kind, batch),
                      # Anything typed at this chat while this very turn was
                      # running, asked for between passes and folded in as a
                      # user turn there (see _injects above and main.run's
                      # `inject`). The page draws the bubble the moment this
                      # hands the text over, not when it was typed - which is
                      # the honest moment, since until here it had not reached
                      # the conversation at all.
                      inject=lambda: _drain_inject(stem, route),
                      approve=_approve)
        except Exception as e:
            if turnctx.cancelled():
                return  # stopped mid-flight; the finally below stays quiet too
            # The provider's own message is the part worth reading (which
            # model id or parameter it rejected, an auth failure...). Without
            # this the exception died with the worker thread and the page saw
            # a turn that just ended with no reply at all.
            _live_clear(route)
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
                _live_clear(route)
                # A message typed at this turn that it never got to (it was
                # stopped before another pass came round) is still a message
                # the user sent. It becomes the next turn, exactly as it would
                # have at a normal end - losing it because the turn it was
                # aimed at was cut short is the one outcome nobody wants.
                left = _drain_inject(stem)
                if left:
                    _run_turn(left, target=c)
                return
            # Anything typed mid-turn that no pass ever came round to collect:
            # the model stopped calling tools and just answered. Taken here, and
            # run as the next turn at the bottom of this block - which is the
            # other half of what the page promises when it holds a bubble grey:
            # at the next tool call, or when the turn is over.
            left = _drain_inject(stem)
            # Always tell the page the turn is over, even if the provider blew
            # up - otherwise the input would look stuck mid-reply forever.
            _live_clear(route)
            # `next` says another turn is starting on the back of this one, so
            # the page keeps its bar up and holds anything TAB queued (which is
            # queued for when the chat is actually idle) instead of racing this
            # for the slot.
            _broadcast({"type": "done", "chat": route, "next": bool(left)})
            # And, for the one mode that summarises the turn as a whole, read
            # it out now that there is a whole turn to summarise. Every other
            # mode has been reading each message out since it landed (see
            # on_message above) and _speak_turn hands back nothing here.
            # A voice tab that can't be read - a mode that isn't one, a
            # settings file that won't parse - is said out loud rather than
            # swallowed into "the speaker has gone quiet again", which is the
            # one failure here nobody would ever think to look for.
            try:
                said, summarise = _speak_turn(c, mark[0])
            except Exception as e:
                said, summarise = [], False
                _speak_note(route, "nothing was read out: " + type(e).__name__
                            + ": " + str(e))
            _speak_offer(route, said, summarise, batch)
            # A turn moves the token count and may have read files into the
            # conversation (read_skill), so the context panel is stale now.
            _broadcast_context(c)
            # And the sidebar: this chat's busy dot goes out, its position in
            # the by-recency order has changed, and if this was its first turn
            # it only just appeared on disk at all.
            _broadcast_chats()
            # Last, so the turn that just ended has finished being wound up -
            # read out, counted, filed - before the next one starts writing to
            # the same chat. The slot is free by now, so this doesn't wait.
            if left:
                _run_turn(left, target=c)

    # `at` so the bubble carries its time the moment it appears, rather than
    # being undated until the end-of-turn redraw pulls the real stamp back off
    # disk (see main.stamp_history). The SERVER's clock, not the browser's:
    # the two disagree, and the time shown while the reply is still coming has
    # to be the one that stays there afterwards.
    # A continue has no message to draw - it picks a turn up where it was
    # dropped (see main.continue_from) - so there is no bubble to put on the
    # page and nothing to echo to the other windows. The busy dot below still
    # lights, which is what says the chat is working again.
    if text is not None:
        _broadcast({"type": kind, "text": text, "chat": route, "at": int(time.time())})
    # Before the work starts, so the busy dot lights immediately and a chat
    # being talked to for the first time shows up in the list right away.
    _broadcast_chats()
    # And the in-flight record opens here, with the turn - not when the first
    # request goes out. Everything between the two is setup that can easily
    # take a second (waiting for the chat's turn slot behind another turn,
    # building the prompt, serialising the tool schemas), and a window that
    # rejoined the chat during it used to find no record and draw nothing.
    _live_begin(route)
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
                "approval": None,
                "safety_threshold": tool_validation.threshold_for(chosen=default),
                "safety_own": False}
    cur = c.id
    tag = "subagent-" + cur + "/"
    subs = [t.name[len(tag):] for t in threading.enumerate()
            if t.name.startswith(tag)]
    # This chat's own model (its settings, or the default it follows), so the
    # corner switcher shows what THIS chat runs on, not a global setting.
    prov, mod, temp = c.models()
    # Which safety number THIS chat runs at, for the same reason the model and
    # the workspace ride along here: the corner control has to show the chat
    # you just opened, and this is the request the page already makes the
    # instant you switch chats. Just the id and whether it is the chat's own -
    # the rest of the dropdown's contents are behind GET /safety, which is
    # fetched when it is actually opened rather than every two seconds.
    safety_threshold, safety_own = c.safety_state()
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
            "safety_threshold": safety_threshold, "safety_own": safety_own,
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


def _skills():
    """Just the skills (.md), in the same row shape _tools() gives them -
    alphabetical by name, one at a time.

    The sidebar's list is skills and nothing else (every tool's schema goes to
    the model automatically, so there is nothing there to choose), and this is
    what it asks for. _tools() would answer the same question, but only after
    re-importing every .py in tools/ to describe tools the panel then throws
    away - about 200ms of module reloading per open, against 40ms of reading
    the skill files themselves.

    The skill FILES are still read every time rather than taken from
    tool_processor.TOOLS, because the panel's refresh button exists precisely
    to notice a skill dropped into skills/ from outside this process, and
    TOOLS is only as new as the last turn.

    Tool names still matter for one thing: a skill whose name a real tool has
    already taken is unreachable - load_tools() gives the name to the tool -
    so listing it would offer something that cannot be read. TOOLS is the
    loaded set as of the last scan and costs nothing to consult; its own skill
    entries are excluded, since a skill does not shadow itself."""
    taken = {t["name"] for t in tool_processor.TOOLS if not t.get("skill")}
    found = sorted((s for s in tool_processor.find_skills()
                    if s["name"] not in taken), key=lambda s: s["name"])
    for skill in found:
        yield {
            "name": skill["name"],
            "description": skill["description"],
            "type": "skill",
            "loaded": True,
            "path": skill["path"],
        }


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
        turns = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(turns, list):
        return ""
    for t in turns:
        if not isinstance(t, dict) or t.get("role") != "user":
            continue
        content = t.get("content") or ""
        # A message sent mid-turn was stored with the label main.run puts on it
        # for the model's benefit; what the user actually typed is the rest of
        # it, and that is what a chat labelled by it should say.
        if content.startswith(main.MID_TURN):
            content = content[len(main.MID_TURN):]
        # Three kinds of user turn nobody typed: a subagent's report, a tool
        # result kept as one, and a note about the chat itself (a workspace
        # move). None of them is what this chat is about.
        if content and not content.startswith("Subagent ") \
                and not content.startswith("Tool result: ") \
                and not content.startswith(main.WORKSPACE_NOTE):
            return content[:80]
    return ""


def _chat_label(path):
    """What a chat is called in the sidebar, from its history.json path.

    A chat named in its settings shows that, rather than its first line - a
    deliberate title wins over a guessed one. Otherwise the first genuine human
    message: a "user" turn can also be a subagent's report or a retry/stop
    message main.py writes itself (see web/index.html's own rendering, which
    draws those two differently from a real user line), so _label_from skips
    past those the same way it does."""
    try:
        cfg = json.loads((path.parent / main.SETTINGS_FILE)
                         .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    return cfg["name"][:80] if cfg.get("name") else _label_from(path)


# history.json path -> (stat key, label). Reading a label means parsing a whole
# history.json, and across ~800 chats that is 55MB and most of a second - by
# some way the most expensive thing this server does, and the reason opening
# the app used to sit on an empty sidebar. It is also almost entirely wasted
# work: a chat's label comes from the FIRST thing typed in it, which is written
# once and then never changes again however long the conversation runs.
#
# So it is read once and kept, under a key made of exactly what would have to
# change for the answer to change - the size and mtime of the two files it is
# derived from, which _chat_files has already stat()ed to sort the list. A chat
# that grows re-reads (one file); every other chat costs nothing at all.
#
# No lock. Two listings at once can only race to compute the SAME label from
# the same two files and store it twice, which is a wasted read and not a wrong
# answer - and dict get/set are each a single bytecode, so neither can see a
# half-written entry.
_labels = {}


def _label_of(path, key):
    """`path`'s label, read at most once per change to the files behind it."""
    hit = _labels.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    label = _chat_label(path)
    _labels[path] = (key, label)
    return label


def _chat_files():
    """Every ordinary chat's history.json, newest first, as (path, mtime, key)
    - where `key` is what _label_of caches that chat's label under.

    scandir plus one stat per file, rather than Path.glob followed by another
    stat to sort: glob has to stat every candidate to know it is there, and
    then the sort stats each one again, which is twice the syscalls for the
    same answer.

    Threaded, because this is pure waiting: a stat is a disk round trip and
    the GIL is dropped for each one, so ~1600 of them overlap instead of
    queueing. Measured on ~800 chats: 350ms as a Path.glob, 130ms as one
    serial scandir pass, 50ms across eight threads. More than eight buys
    nothing measurable.

    Nothing here reads a chat, only asks after it - the labels stream in
    behind, one file at a time, as _chat_rows yields. This is what stands
    between opening the app and the first chat row appearing, which is why it
    is worth the threads."""
    def look(entry):
        """(path, mtime, cache key) for one chat folder, or None if there is
        no chat in it."""
        try:
            h = os.stat(os.path.join(entry.path, main.HISTORY_FILE))
        except OSError:
            # A chat folder with no history yet isn't a chat anyone can see -
            # see _chat_rows on why an unwritten chat is deliberately absent.
            return None
        try:
            s = os.stat(os.path.join(entry.path, main.SETTINGS_FILE))
            settings_key = (s.st_mtime, s.st_size)
        except OSError:
            settings_key = None  # no settings file: nothing to name it
        return (Path(entry.path) / main.HISTORY_FILE, h.st_mtime,
                (h.st_mtime, h.st_size, settings_key))

    try:
        folders = [e for e in os.scandir(main.CHATS)
                   if e.name.startswith("chat-") and e.is_dir()]
    except OSError:
        return []
    with futures.ThreadPoolExecutor(_SCAN_THREADS) as pool:
        rows = [r for r in pool.map(look, folders, chunksize=32) if r]
    rows.sort(key=lambda r: r[1], reverse=True)
    # Chats that are gone stop being worth remembering the moment we have
    # looked and not found them. Rebuilt rather than deleted from, so the swap
    # is one assignment: another thread listing at this moment carries on
    # reading the dict it started with instead of a half-emptied one.
    global _labels
    live = {r[0] for r in rows}
    _labels = {p: v for p, v in _labels.items() if p in live}
    return rows


def _subagent_index():
    """{chat id: [subagent name, ...]} for every chat that has any.

    One walk of chats/subagents/ for the whole listing. The rows used to look
    this up per chat, which over ~800 chats is ~800 directory probes to find
    the dozen folders that actually exist."""
    index = {}
    try:
        with os.scandir(main.CHATS / "subagents") as it:
            for e in it:
                if not e.is_dir():
                    continue
                with os.scandir(e.path) as files:
                    index[e.name] = sorted(f.name[:-len(".md")] for f in files
                                           if f.name.endswith(".md"))
    except OSError:
        pass  # no subagents folder yet - every chat simply has none
    return index


def _chat_row(p, busy, threads, subs, label):
    """One chat's row for the chats panel, from its history.json path.

    `name` is the id /load is given ('cron/ai-brief/003' for a cron run);
    `stem` is the flat id, which is how subagent folders and the busy list are
    keyed. The page needs both and must not derive one from the other.

    `label` and `subs` are handed in rather than looked up here, because both
    are far cheaper found for the whole listing at once than per row - see
    _label_of and _subagent_index."""
    cid = main.chat_id(p)
    # `detail` is the small second line on a row. None means "show the id",
    # which is what an ordinary chat wants; a cron job says when its current
    # run fired instead, since its id is a folder number nobody can date.
    return {"name": main.chat_route(p), "stem": cid, "label": label,
            "busy": cid in busy or any(t.startswith("subagent-" + cid + "/")
                                       for t in threads),
            "cron": False, "detail": None, "subagents": subs.get(cid, [])}


def _cron_rows(busy, threads, subs):
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
        row = _chat_row(found[-1] / main.HISTORY_FILE, busy, threads, subs,
                        folder.name)
        row["cron"] = True
        row["job"] = folder.name
        started = _started(found[-1])
        row["detail"] = ("last run " + _when(started)) if started else "not run yet"
        row["history"] = [
            _chat_row(p / main.HISTORY_FILE, busy, threads, subs,
                      _run_label(p, n))
            for n, p in reversed(list(enumerate(found[:-1], 1)))]
        out.append(row)
    return out


def _started(folder):
    """When the run in `folder` fired, as it was written (see cron.new_run), or
    None if it isn't recorded."""
    try:
        return json.loads((folder / main.SETTINGS_FILE)
                          .read_text(encoding="utf-8")).get("started")
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


def _chat_rows():
    """Every chat, cron jobs first and then ordinary chats newest first, with
    its subagent children and whether it is working right now - what the page's
    chats panel draws.

    A generator, and that is the point: rows are yielded one at a time so
    /chats can put each on the wire as it is built. The list runs to hundreds
    of rows and the first one can only be named once the whole folder has been
    scanned and sorted, so as a single JSON document the page waited for the
    LAST chat before it could draw the first. Streamed, it draws each as it
    lands - and the ordering work all happens before the first yield, so what
    arrives is still in its final order and nothing has to be moved later.

    Says nothing about which chat is "current", because that is a property of
    each browser window rather than of the server - the page marks its own row
    from the chat it is showing."""
    busy = set(main.busy_chats())
    threads = [t.name for t in threading.enumerate()]
    subs = _subagent_index()
    # Each chat is a folder holding history.json; a cron RUN is two levels
    # further down, in chats/cron/<job>/<nnn>/, and is grouped by job rather
    # than listed loose - see _cron_rows. Cron first because the page draws it
    # as a small collapsed group above the chats.
    for row in _cron_rows(busy, threads, subs):
        yield row
    # A chat that has been minted but never written to isn't here, and that is
    # deliberate: it exists only in the window that minted it (see
    # main.new_chat_id), and that window draws its own row for it. Listing it
    # would mean showing every other window a chat they can't see the point of.
    for path, _, key in _chat_files():
        yield _chat_row(path, busy, threads, subs, _label_of(path, key))


def _chats():
    """_chat_rows() as a plain list, for the callers that want the whole thing
    in hand rather than a row at a time."""
    return list(_chat_rows())


# ---- searching the chats -----------------------------------------------------
# The sidebar's search box (index.html's #chat-search) asks /chats?q=..., and
# this is what answers it. The skills panel above it searches the list it
# already holds, because a skill is a name and one line of description; a chat
# is its whole transcript, which is 58MB across ~900 chats here and is never
# going to the browser. So the same search happens on this side instead, and
# only the rows that matched are sent - ranked the same way the skills box
# ranks: whatever the query is DENSEST in comes first, and a hit in the chat's
# title outranks any hit in its text.

# How many matching chats one search answers with. Ranking every match means
# reading it, so an unhelpfully common word ("the") would otherwise mean
# parsing every chat on disk to order a list nobody scrolls to the end of.
_SEARCH_LIMIT = 60

# How much of the matching line comes back as the row's second line.
_SNIPPET = 140


def _search_terms(query):
    """The words a chat has to contain to match, lowercased.

    Split on whitespace and ALL of them must appear (not necessarily together,
    and not necessarily in that order) - two words typed into a search box are
    two things half-remembered about the same conversation, not a phrase to be
    found verbatim."""
    return [w for w in query.lower().split() if w]


def _term_forms(term):
    """Every way `term` could be written inside a history.json, for the raw
    scan below - which looks at the file as it is stored rather than at the
    text it decodes to.

    A transcript is JSON, and JSON has two spellings for the same word. Most of
    these files come from json.dumps at its default settings, where a quote is
    \\", a backslash is \\\\ and anything non-ASCII is a \\uXXXX escape; some
    (older ones, and any written with ensure_ascii off) carry the characters
    themselves. A word is spelled one way or the other in a given file, never
    half of each, so counting both forms costs one extra pass and is the
    difference between finding a price in pounds and not finding it at all.

    The parse in _chat_text sees through all of this and is the authority on
    whether a chat really matched. This only has to avoid rejecting one."""
    return {term.encode("utf-8", "replace"),
            json.dumps(term)[1:-1].lower().encode("utf-8", "replace")}


def _raw_hits(path, terms):
    """(occurrences, size) for a whole history.json read as bytes and never
    parsed - the cheap first pass, which is here to say NO.

    A search reads every chat on disk, and reading them as text is ~200ms
    across this folder against ~1s to parse them. Nearly all of them are about
    to be thrown away, so the parse is worth doing only for the few that get
    past this - see _search_rows.

    0 the moment a term is missing: a chat has to contain all of them, so
    there is nothing to add up once one is not there. Counting bytes rather
    than characters over-states the size of a transcript full of escapes, and
    that is fine - it is the same measure for every chat being compared."""
    try:
        blob = path.read_bytes().lower()
    except OSError:
        return 0, 0     # deleted between the listing and here
    total = 0
    for term in terms:
        found = sum(blob.count(form) for form in _term_forms(term))
        if not found:
            return 0, 0
        total += found
    return total, len(blob)


def _chat_text(path):
    """Everything a chat's transcript actually SHOWS, as one string: what was
    said, what the model thought, and the tool calls as they were written.

    The keys, ids and timings around them are not searched - a chat is not
    "about" the word "content" because every turn in it has that key, and the
    raw scan above already lets a few of those through for this pass to reject.
    Tool RESULTS are in here (they are a turn's content like any other): a
    command's output is part of the conversation and is often the only place
    the thing being looked for was ever written down."""
    try:
        turns = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    parts = []
    for turn in turns if isinstance(turns, list) else []:
        if not isinstance(turn, dict):
            continue
        for key in ("content", "reasoning_content", "raw_call"):
            value = turn.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
    return "\n".join(parts)


def _density(text, terms):
    """How dense `terms` are in `text` - the count of every term, per character
    of the text, or 0 if any of them is missing.

    The same measure index.html's occurrenceScore applies to skills, and for
    the same reason: a short chat that is about the query beats a long one that
    mentioned it once."""
    if not text:
        return 0.0
    total = 0
    for term in terms:
        found = text.count(term)
        if not found:
            return 0.0
        total += found
    return total / len(text)


def _snippet(text, lower, terms, after=0):
    """The line to show under a matching chat's title: the text around the
    first term that appears in it, whitespace collapsed so a snippet cut out
    of a code block or a command's output is still one line.

    `after` is where to start looking, and is how a chat whose TITLE matched
    avoids a row that says the same thing twice - a title is the first 80
    characters of the first thing typed (see _chat_label), so the first hit in
    the text is nearly always the one already printed above it. Looking past it
    finds where the conversation came back to the subject instead, and finding
    nothing there leaves the row to show its id, as an unsearched one does."""
    at = min((i for i in (lower.find(t, after) for t in terms) if i >= 0), default=-1)
    if at < 0:
        return ""
    start = max(0, at - _SNIPPET // 3)
    end = min(len(text), start + _SNIPPET)
    cut = " ".join(text[start:end].split())
    return ("..." if start else "") + cut + ("..." if end < len(text) else "")


def _cron_run_files():
    """(path, label) for every cron RUN, newest job first - the chats the
    ordinary listing keeps folded away inside their job (see _cron_rows).

    A search goes across all of them: a run is a real chat with a real
    transcript, and it is exactly the kind of thing worth searching for,
    because nobody remembers which of a job's forty runs said the thing. Its
    label names both halves - the job alone would give forty identical rows."""
    out = []
    try:
        folders = sorted((main.CHATS / "cron").glob("*/"))
    except OSError:
        return out
    for folder in folders:
        for run in sorted(p.parent for p in folder.glob("*/" + main.HISTORY_FILE)):
            out.append((run / main.HISTORY_FILE,
                        folder.name + " - " + _run_label(run, run.name)))
    return out


def _search_rows(query):
    """Every chat matching `query`, best first - what /chats?q= streams.

    Three passes, cheapest first, because the first one runs over every chat on
    disk and the last one over a handful:

      the title    already in hand for every chat (_label_of caches it), so a
                   title match costs nothing to find and is always shown above
                   the text matches, however dense those are - the same two
                   groups the skills box sorts into.
      the bytes    every other chat, read but not parsed, to reject the ones
                   that cannot match at all.
      the text     the survivors, parsed, scored on what the transcript really
                   shows and given the line of it that matched. This is the
                   pass that costs, so only _SEARCH_LIMIT chats reach it -
                   picked by their raw density, which is a good enough stand-in
                   for the real score to choose WHICH to look at properly.

    The last row is a note rather than a chat when there were more matches than
    came back, so the panel can say so instead of quietly ending."""
    terms = _search_terms(query)
    if not terms:
        return
    candidates = [(path, _label_of(path, key)) for path, _, key in _chat_files()]
    candidates += _cron_run_files()

    # Title matches: kept whole, and ranked among themselves by how much of the
    # title the query accounts for.
    titled, rest = [], []
    for path, label in candidates:
        score = _density(label.lower(), terms)
        (titled if score else rest).append((path, label, score))

    with futures.ThreadPoolExecutor(_SCAN_THREADS) as pool:
        raw = list(pool.map(lambda c: _raw_hits(c[0], terms), rest, chunksize=32))
    hits = [(c, n / size) for c, (n, size) in zip(rest, raw) if n]
    # Newest first inside equal scores: sorted() is stable and the candidates
    # arrived in that order.
    hits.sort(key=lambda h: h[1], reverse=True)
    room = max(0, _SEARCH_LIMIT - len(titled))
    chosen = titled[:_SEARCH_LIMIT] + [c for c, _ in hits[:room]]
    more = len(titled) + len(hits) - len(chosen)

    def look(entry):
        """One chosen chat, read properly: its real score and the line that
        matched. A title match with nothing in its text still stands (it
        matched the title) and simply has no line to show."""
        path, label, title_score = entry
        text = _chat_text(path)
        lower = text.lower()
        return (path, label, title_score, _density(lower, terms),
                _snippet(text, lower, terms, len(label) if title_score else 0))
    with futures.ThreadPoolExecutor(_SCAN_THREADS) as pool:
        looked = list(pool.map(look, chosen, chunksize=8))

    # A chat the raw scan liked and the parse did not is one whose only hits
    # were in the JSON around the conversation - drop it rather than show a row
    # with nothing to point at.
    looked = [row for row in looked if row[2] or row[4]]
    looked.sort(key=lambda r: (bool(r[2]), r[2] or r[3]), reverse=True)

    busy = set(main.busy_chats())
    threads = [t.name for t in threading.enumerate()]
    subs = _subagent_index()
    for path, label, _, _, snippet in looked:
        row = _chat_row(path, busy, threads, subs, label)
        # The id would normally be the second line. What the chat says about
        # the thing being searched for is worth more than the folder it is in.
        row["detail"] = snippet or None
        yield row
    if more > 0:
        yield {"note": "+ " + str(more) + " more match" + ("es" if more > 1 else "")
                       + " - narrow the search to see them"}


def _usage_summary(query):
    """The usage tab's payload: usage.summary() for the window asked for, with
    the chat rows given readable names.

    The names come from _chats() rather than from the ledger, because the
    ledger stores an id and only an id - it is written while a turn is running
    and must not depend on a chat file it might outlive. Looking the label up
    here also means a chat renamed since costs nothing to keep current, and a
    chat DELETED since keeps its spend in the totals under its bare id, which
    is the honest answer: the money was spent whether or not the conversation
    still exists."""
    q = parse_qs(query)
    window = q.get("range", ["30d"])[0]
    if window not in usage.RANGES:
        window = "30d"
    chat = q.get("chat", [""])[0] or None
    data = usage.summary(window, chat=chat)
    labels = {}
    try:
        for row in _chats():
            labels[row["stem"]] = row["label"] or row["stem"]
            for child in row.get("history") or []:
                # A cron job's earlier runs are chats in their own right and
                # each has its own spend, so they need naming too - as
                # "<job> - <when>", since "08:00" alone names nothing.
                labels[child["stem"]] = (row.get("job") or row["label"]) + \
                    " - " + (child["label"] or child["stem"])
    except Exception:
        pass  # a listing that fails costs readable names, not the numbers
    for row in data["by_chat"]:
        row["label"] = labels.get(row["chat"]) or row["chat"]
        row["gone"] = row["chat"] not in labels
    data["ranges"] = list(usage.RANGES)
    return data


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


def _attach_name(raw):
    """A file name safe to create inside a chat folder, taken from what a
    browser said the file was called - or None if nothing usable is left.

    The name is kept as close to the original as it can be, because it is what
    the person who attached it will look for and what the model is about to be
    told the file is called. Only what would make it something other than a
    plain name in one folder is taken out."""
    raw = (raw or "").replace("\\", "/")
    name = _BAD_IN_NAME.sub("_", raw.rsplit("/", 1)[-1]).strip()
    if not name or not name.strip("."):
        return None  # empty, "." or ".." - nothing to call a file
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        # A long name is cut, not refused, and the extension survives the cut:
        # it is what says what the file IS. A "." near the end is an extension,
        # one in the middle of a 200-character name is just a dot.
        name = stem[:120 - len(ext) - 1] + dot + ext if dot and len(ext) <= 12 \
            else name[:120]
    return name


def _reserve_upload(folder, name):
    """An empty file called `name` in `folder`, or `name-2`, `name-3` ... if
    that is taken, created here and now.

    Two files with the same name are two files: attaching last week's
    report.pdf and this week's must not leave one of them overwritten. The
    empty file is made with O_EXCL rather than after an exists() check, so two
    uploads landing at the same moment cannot both decide the same name is
    free. It is a placeholder - _post_attach renames the finished upload over
    it - and the caller deletes it if the transfer never finishes."""
    stem, dot, ext = name.rpartition(".")
    if not stem:  # ".env" and the like: all name, no extension
        stem, dot, ext = name, "", ""
    n = 1
    while True:
        target = folder / (name if n == 1 else stem + "-" + str(n) + dot + ext)
        try:
            target.touch(exist_ok=False)
            return target
        except FileExistsError:
            n += 1


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
                text = p.read_text(encoding="utf-8")
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


def _safety(c):
    """Everything the safety dropdown in the corner of the chat window draws,
    for chat `c`.

    Its own route rather than more fields on /status, because /status is
    polled every two seconds by every open page and this is only looked at
    when the dropdown is opened. The one thing that DOES ride on /status is
    which level is current, since the corner button has to relabel itself the
    instant you switch chats - see _status.

    "prompt_is_rating" is the awkward truth this has to tell. A prompt written
    before the slider existed asks the checking model for a "{true}" marker
    rather than a 0-10 rating, and a marker cannot be compared against a
    number - so on one of those, every setting from 1 to 9 behaves the same.
    Rather than let that be discovered by the slider appearing to do nothing,
    the page says it, and offers the rating prompt as a one-click
    replacement."""
    chosen = settings.load()
    shared = chosen["safety_prompt"]
    if c is None:
        # A window whose chat doesn't exist yet: it can still open the dropdown
        # and read what a message sent now would run under, which is whatever
        # the settings page says. Nothing of its own to report.
        level, own, extra, custom = tool_validation.threshold_for(chosen=chosen), False, "", ""
    else:
        level, own = c.safety_state()
        extra, custom = c.safety_extra or "", c.safety_prompt or ""
    return {"chat": None if c is None else c.route,
            "threshold": level,
            # Whether that number is this chat's own or the settings default it
            # happens to be following - the page says which, and only offers
            # "follow the default again" when there is something to clear.
            "own": own,
            "default_threshold": tool_validation.threshold_for(chosen=chosen),
            "extra": extra,
            "prompt": custom,
            "shared_prompt": shared,
            "prompt_is_rating": tool_validation.is_rating_prompt(custom or shared),
            "rating_prompt": settings.DEFAULTS["safety_prompt"],
            # So the panel can say what still applies at every level without
            # the user having to go and look at the safety tab.
            "whitelist": chosen["safety_whitelist"],
            "blacklist": chosen["safety_blacklist"]}


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


def _tts(name):
    """The text-to-speech models provider `name` can be asked for, for the
    voice tab's other model box. The same shape and the same reasoning as _stt
    above, asked of the endpoint that runs the other way."""
    name = (name or "").strip()
    p = provider.custom_provider(name)
    if not p:
        return {"provider": name, "wire": "", "supported": False, "models": [],
                "note": "there is no provider called " + name + " - it may have "
                        "been renamed or deleted on the providers tab."}
    supported = p["wire"] in provider.TTS_WIRES
    models = provider.tts_models(name) if supported else []
    if not supported:
        note = (name + " speaks " + p["wire"] + ", which has no text-to-speech - "
                "Claude produces no audio, and Polly is a separate AWS service. "
                "Pick a provider on the "
                + ", ".join(sorted(provider.TTS_WIRES)) + " wires.")
    elif not models:
        note = ("nothing speech-shaped in " + name + "'s catalogue - if you know "
                "a model id it serves, type it in anyway; the box is free text.")
    else:
        note = ""
    return {"provider": name, "wire": p["wire"], "supported": supported,
            "models": models, "note": note}


def _voices(name, model):
    """Which voices provider `name` offers on `model`, for the voice tab's
    dropdown, and whether that model can be told how to read.

    Its own route rather than another field on _tts above, because the answer
    depends on the MODEL as well as the provider - OpenAI's tts-1 pair takes
    nine of the thirteen - and the model box is free text being typed into. It
    costs nothing to ask again on each keystroke: unlike the model lists, this
    is a table in provider.py and never a round trip to anyone.

    An empty list is a normal answer, not a failure. A local endpoint serving
    kokoro has voice names nothing here knows, so the note says to leave the
    picker alone and let the endpoint use its own."""
    name = (name or "").strip()
    p = provider.custom_provider(name)
    if not p:
        return {"provider": name, "supported": False, "voices": [],
                "instructions": False,
                "note": "there is no provider called " + name + " - it may have "
                        "been renamed or deleted on the providers tab."}
    supported = p["wire"] in provider.TTS_WIRES
    voices = provider.tts_voices(name, model)
    if voices or not supported:
        # A wire that cannot speak says so through the model box above, which
        # is where that choice is made - repeating it here would put the same
        # sentence on screen twice.
        note = ""
    else:
        note = ("no published voice list for " + name + " - it reads in whatever "
                "voice its endpoint defaults to.")
    return {"provider": name, "supported": supported, "voices": voices,
            "default": provider.TTS_VOICE_DEFAULT.get(p["wire"], ""),
            "instructions": provider.tts_instructable(name, model), "note": note}


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
        keyless = not provider.wants_key(wire)
        cards.append({
            "name": p["name"],
            "wire": wire,
            "base_url": p["base_url"],
            # Where it actually points when it named no URL of its own, so the
            # page can show the real host as a placeholder rather than an empty
            # box that looks unconfigured.
            "effective_url": provider.custom_base_url(p),
            "default_url": provider.wire_default_url(wire),
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
                     "unused." if wire == "claude-subscription" else
                     "runs the local piper text-to-speech engine - no key or URL. "
                     "Set its PIPER_PATH and PIPER_VOICE_DIR boxes on the providers "
                     "tab." if wire == "piper" else ""),
        })

    return {
        "providers": cards,
        "wires": provider.wire_names(),
        "wire_urls": {w: provider.wire_default_url(w)
                      for w in provider.wire_names()},
        # Which wires ignore the key and URL boxes, so the page can grey them
        # out the moment one is picked rather than after a save.
        "keyless_wires": provider.keyless_wires(),
        # Which wires Uniagent ships, so a row can tell "yours, over a
        # shipped one" (revert restores the shipped one) apart from
        # "entirely yours" (revert deletes it).
        "shipped_wires": wires.shipped(),
        # {wire: url} for the picture each wire falls back to, so the icon
        # picker can show what "leave this empty" would actually look like,
        # and offer the bundled ones as one-click choices.
        "wire_icons": {w: _icon_url(provider.wire_icon(w) or provider.UNKNOWN_ICON)
                       for w in provider.wire_names()},
        "bundled_icons": [{"path": p, "url": _icon_url(p)} for p in _bundled_icons()],
        # Each wire's whole spec, as the wires tab needs it: what its setup
        # form asks for, and - for the wires that are a template rather than a
        # Python function - the request itself, so it can be shown, edited and
        # cloned. See scripts/wires.py.
        "templates": {w: {"label": provider.wire_label(w),
                          "help": provider.template_for(w).get("help", ""),
                          "base_url": provider.wants_base_url(w),
                          "key": provider.wants_key(w),
                          "fields": provider.template_fields(w),
                          "native": wires.is_native(w),
                          "native_reason": wires.NATIVE_REASON.get(w, ""),
                          "custom": wires.is_custom(w),
                          "spec": provider.template_for(w)}
                      for w in provider.wire_names()},
        "template_error": provider.template_error(),
        # Whatever is wrong with LLM_PROVIDERS, if it can't be read at all -
        # otherwise the page shows no providers and no reason why.
        "error": provider.custom_error(),
    }


def _wire_preview(wire, spec=None):
    """The exact request `wire` would send, without sending it.

    This is the whole answer to "why doesn't my wire work". A wrong auth header
    or a body key an endpoint doesn't recognise is otherwise a 401 or a 400
    with somebody else's error message on it, and no way to see what went out;
    here it is the URL, the headers and the body, rendered against a real
    turn's values exactly as a live call would render them.

    `spec` is the version being edited, so the preview follows the textarea
    rather than what was last saved - you see the effect of a change before
    committing to it. Falls back to the saved wire when nothing is passed.

    The key is rendered as a visible placeholder rather than the real one. The
    point of this is to show WHERE the key goes and in what format, which the
    placeholder does exactly as well, and it means the panel can be left open
    or screenshotted without leaking a credential."""
    spec = spec if isinstance(spec, dict) else wires.spec_for(wire)
    problems = wires.problems(spec)
    if spec.get("native"):
        return {"native": True,
                "reason": wires.NATIVE_REASON.get(wire, ""), "problems": []}

    dialect = spec.get("dialect") or "openai"
    sample = [{"role": "system", "content": "You are Uniagent."},
              {"role": "user", "content": "Hello."}]
    try:
        system, messages = provider._dialect_turns(dialect, sample, None, spec)
        url, headers, body = wires.build(spec, {
            "model": "MODEL-ID",
            "messages": messages,
            "system": system,
            "temperature": 0,
            "tools": None,
            "stop": provider.STOP,
            "max_tokens": spec.get("max_tokens"),
            "want_usage": True,
            "key": "YOUR-API-KEY",
            "base_url": "",
            "setting": {f["env"]: f["default"] or ("YOUR-" + f["env"])
                        for f in wires.fields(spec)},
        })
    except Exception as e:
        return {"problems": problems + ["Could not render: " + str(e)]}

    return {
        "url": url,
        "headers": headers,
        # Serialized here rather than in the page, because the ORDER is the
        # thing being previewed and only the server can promise the page sees
        # the same bytes the endpoint will.
        "body": json.dumps(body, indent=2),
        "dialect": dialect,
        "problems": problems,
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

# The last answer the remote gave, so the page can say "up to date" or "3 new
# commits" the moment it opens instead of only after someone presses a button.
# An update nobody knows about is an update nobody installs.
UPDATE_CHECK = ROOT / "update_check.json"

# How old that answer may be before it is quietly fetched again. Long, because
# nothing here is urgent and the refresh costs a git fetch; short enough that a
# page opened tomorrow is not repeating yesterday's news.
UPDATE_CHECK_MAX_AGE = 6 * 3600

# One refresh at a time. Set while the background thread is out at the remote,
# so a second page load joins the first rather than starting its own fetch.
_update_checking = False
_update_check_lock = threading.Lock()

# Why the last background check came back empty-handed, if it did. Kept beside
# the cached answer rather than replacing it - see _refresh_check.
_update_check_error = None


def _trim_survey(s):
    """The survey, minus the parts a page has no use for. The commit list on a
    checkout that has been left alone for months can be hundreds of lines, and
    the file list longer still - both are read as "how big is this update", and
    the first few answer that as well as all of them."""
    if not s.get("ok"):
        return {"ok": False, "error": s.get("error", "the check failed")}
    return {
        "ok": True,
        "ref": s.get("ref"),
        "behind": s.get("behind", 0),
        "ahead": s.get("ahead", 0),
        "up_to_date": s.get("up_to_date", True),
        "diverged": s.get("diverged", False),
        "deps_changed": s.get("deps_changed", False),
        "blocked": s.get("blocked", []),
        "latest": s.get("latest"),
        "commits": s.get("commits", [])[:40],
        "file_count": len(s.get("files", [])),
        "files": s.get("files", [])[:60],
    }


def _write_check(survey):
    """Remember what the remote said, and against WHICH commit it said it.

    The head matters: after an update this file still reads "3 commits behind",
    which was true of the install that no longer exists. Stamping the commit it
    was measured from is what lets _read_check throw it away rather than
    cheerfully offer an update that has already been installed."""
    head = update._commit("HEAD") or {}
    try:
        UPDATE_CHECK.write_text(json.dumps({
            "at": time.time(),
            "head": head.get("sha", ""),
            "survey": _trim_survey(survey),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass


def _read_check():
    """The remembered check, or None if there isn't one worth having: never
    checked, unreadable, or measured against a commit this install has since
    moved off."""
    try:
        c = json.loads(UPDATE_CHECK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    head = update._commit("HEAD") or {}
    if c.get("head") and head.get("sha") and c["head"] != head["sha"]:
        return None
    return c


def _refresh_check():
    """Ask the remote, in a thread, and write down what it says. Nothing waits
    on this - the page has already been answered with whatever was known at the
    time, and picks the new answer up on its next look.

    A check that fails is remembered but not written: an unreachable remote for
    ten seconds should not replace "3 new commits" with "could not check", which
    is both less useful and less true. The last good answer stands and the page
    is told the last attempt failed alongside it."""
    global _update_checking, _update_check_error
    try:
        s = update.survey(update.target_ref())
        if s.get("ok"):
            _write_check(s)
            _update_check_error = None
        else:
            _update_check_error = s.get("error") or "the check failed"
    except Exception as e:
        _update_check_error = type(e).__name__
    finally:
        with _update_check_lock:
            _update_checking = False


def _update_running(info):
    """Is an update going on right now? A log that was written to seconds ago
    and has not yet said it finished. Worth knowing because a background fetch
    across a merge in the same checkout is a race for git's ref locks, and the
    answer it would bring back is about to be wrong anyway."""
    if not info.get("log") or not info.get("log_at"):
        return False
    return (time.time() - info["log_at"] < 120
            and update.DONE not in info["log"])


def _maybe_refresh_check(cached):
    """Start a refresh if what we have is missing or stale. Returns whether one
    is now running, which the page shows as "checking..." rather than as a
    silence it has to guess about."""
    global _update_checking
    fresh = cached and (time.time() - cached.get("at", 0)) < UPDATE_CHECK_MAX_AGE
    with _update_check_lock:
        if _update_checking:
            return True
        if fresh:
            return False
        _update_checking = True
    threading.Thread(target=_refresh_check, daemon=True).start()
    return True


def _update_local():
    """Everything the settings page needs to draw the updater, WITHOUT making
    it wait on the network: which commit this install is on, whatever the last
    update wrote, and the last answer the remote gave. If that answer is stale
    a fresh one is fetched in the background, so the page is never blocked on a
    remote that might be slow or unreachable - see _maybe_refresh_check."""
    info = {"ref": update.target_ref(), "current": update._commit("HEAD"), "log": ""}
    try:
        # The tail only: a long update writes plenty and the page redraws this
        # every second while one is running.
        info["log"] = UPDATE_LOG.read_text(errors="replace", encoding="utf-8")[-20000:]
        # When it was written, so the page can tell the update happening right
        # now from the one that happened in March. A log with no age to it has
        # to be shown always or never, and neither is right.
        info["log_at"] = UPDATE_LOG.stat().st_mtime
    except OSError:
        pass
    if info["current"]:
        cached = _read_check()
        # An update in flight is polling this route every second. Staying off
        # the remote while it runs keeps two gits out of one checkout.
        info["checking"] = (False if _update_running(info)
                            else _maybe_refresh_check(cached))
        if cached:
            info["check"] = cached.get("survey")
            info["checked_at"] = cached.get("at")
        if _update_check_error:
            info["check_error"] = _update_check_error
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
    log = open(UPDATE_LOG, "w", encoding="utf-8")
    kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([py, str(ROOT / "scripts" / "update.py")], **kwargs)


def _restart_cron():
    """Restart the cron watcher, which is a separate process and so cannot
    restart itself from here.

    Two ways to reach it, because the two platforms keep it alive differently.
    systemd knows the unit and restarts it by name. Windows has run-server.ps1,
    which restarts anything it started that exits - so there, stopping the
    process IS restarting it, and the pidfile is how we find which process to
    stop. Started by hand with neither behind it, it has to be restarted by
    hand, and that is what we say."""
    if os.name != "nt" and shutil.which("systemctl"):
        try:
            r = subprocess.run(
                ["systemctl", "--user", "restart", "uniagent-cron.service"],
                capture_output=True, timeout=15,
                text=True, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as e:
            return "could not restart cron: " + type(e).__name__
        if r.returncode == 0:
            return "cron watcher restarted."
        return "could not restart cron (" + (r.stderr.strip()[:120] or "unknown") + ")"

    pid = service.read_pid("cron")
    if not service.alive(pid):
        return ("the cron watcher does not appear to be running - "
                "nothing to restart.")
    if not service.stop("cron"):
        return "could not stop the cron watcher (pid " + str(pid) + ")."
    if service.supervised():
        return "cron watcher restarted."
    # Nothing is waiting to bring it back, so we do it ourselves.
    if service.spawn("cron.py"):
        return "cron watcher restarted."
    return "the cron watcher was stopped but could not be started again."


def _restart_self():
    """Come back on the code that is on disk now.

    The how is service.restart_self's business - it differs per platform and
    per what is supervising this process, and getting it wrong on Windows
    means two servers on one port. See that function."""
    service.restart_self("server.py")


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

    def _send_bytes(self, data, ctype):
        """Bytes that came from somewhere other than a file - _send() takes a
        str and _send_file() reads from disk, and synthesised audio is neither.
        Uncached for the same reason everything else here is: an id is used
        once and the page never asks for it twice."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_ndjson(self, rows):
        """A list sent a row at a time: one JSON object per line, each written
        the moment it exists rather than once the last one does.

        This is for the two lists long enough or slow enough to build that
        waiting for the end of them is what "the app is slow to open" meant -
        the chats panel and the skills panel. The page reads the lines as they
        arrive and draws each straight away (see index.html's ndjson()), so a
        sidebar of hundreds of chats fills in front of you instead of sitting
        empty and then appearing all at once.

        No Content-Length, because the length is not known until the last row
        is built and the whole point is not to wait for that. The body ends
        when the connection does, which on HTTP/1.0 - what BaseHTTPRequestHandler
        speaks, and what every other response here already relies on - is how a
        response ends anyway.

        A row that fails to serialise would truncate the list silently, so it
        is left to raise: an incomplete sidebar with a traceback in the log is
        recoverable, one without is not.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        for row in rows:
            line = (json.dumps(row) + "\n").encode()
            try:
                self.wfile.write(line)
            except OSError:
                # The page navigated away, or asked again and dropped this
                # one (see index.html - a second load aborts the first).
                # Normal, and there is nobody left to tell.
                return

    def _get_speak(self):
        """The audio for a reply the page was told to read out, by the id it
        was told. Made on this request the first time it is asked for - see
        _speak_audio - so this can take as long as the speech model does."""
        q = parse_qs(urlparse(self.path).query)
        try:
            said_id = int(q.get("id", [""])[0])
        except ValueError:
            self._send("expected ?id=<number>", code=400)
            return
        try:
            audio, mime = _speak_audio(said_id)
        except RuntimeError as e:
            # 502, like POST /voice: what failed is the speech provider, not
            # this server, and the page shows the message as it stands.
            self._send(str(e), code=502)
            return
        self._send_bytes(audio, mime)

    def _send_file(self, path, ctype, cache="max-age=30"):
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

        That last part is the backstop, not the mechanism: a browser is free to
        hold an image it already has in memory and never ask again, which is
        what made a re-rendered chart keep showing the old picture. What
        actually fixes that is the &v= the page puts on an /image URL - see the
        /image route above.

        `cache` is what to put in Cache-Control. The page itself passes
        "no-cache" - revalidate every single time, never serve from the cache
        unasked - because a stale app is a worse bug than a slow one, and the
        ETag below is what makes revalidating cheap. See _send_page.
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
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
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
        self._send(LOGIN_PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")

    def _send_page(self):
        """The app itself. Sent with a validator instead of no-store, because
        it is 450KB of one file and no-store means every open - every reload,
        every device, every time - pulls all of it down again before a single
        request for anything in it can go out.

        no-cache is not "don't cache": it is "ask before reusing", so the
        browser still comes here on every load and an edited index.html still
        shows up on the next one, which is what no-store was there to
        guarantee. What changes is the answer when nothing has been edited -
        304 and no body, rather than the whole file again."""
        self._send_file(PAGE, "text/html; charset=utf-8", "no-cache")

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
                self._send_page()
            else:
                self._send_login()
        elif self.path == "/":
            self._send_page()
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
        elif self.path == "/chats" or self.path.startswith("/chats?"):
            # Streamed a row at a time, not sent as one document - see
            # _send_ndjson and _chat_rows.
            #
            # ?q= is the sidebar's search box: the same rows, but only the
            # chats the words are in and best match first, which needs the
            # transcripts themselves and so happens here rather than in the
            # page (see _search_rows).
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send_ndjson(_search_rows(q) if q.strip() else _chat_rows())
        elif self.path.startswith("/subagent?"):
            path = _subagent_file(urlparse(self.path).query)
            if path is None:
                self._send("not found", code=404)
            else:
                self._send(path.read_text(encoding="utf-8"))
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
            #
            # The page also puts a &v=<when the message was written> on these
            # (index.html's imageHtml). It is ignored here - parse_qs only
            # reads path - and exists purely so that the same file rewritten
            # and shown again is asked for under a URL the browser has not
            # cached. See _send_file below for why these are cached at all.
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
                lines = (path.read_text(encoding="utf-8").splitlines()
                         if path.exists() else [])
                self._send("[" + ",".join(lines) + "]", "application/json")
        elif self.path.startswith("/live?"):
            # The response this chat is writing RIGHT NOW, or null when it is
            # not writing one - what a window that was looking somewhere else
            # rebuilds the wait, the thinking and the half-written reply from.
            # The same sidecar shape /validations and /stamps use; see _live
            # for why this one is not a file.
            #
            # `now` is the server's own clock, sent alongside so the page can
            # work out how long each phase has been running without trusting
            # its own: a phone three time zones away, or simply a few seconds
            # out, would otherwise rebuild a model that has been thinking for
            # ten seconds as one that started last week.
            q = parse_qs(urlparse(self.path).query)
            route = q.get("chat", [""])[0]
            found = _live_get(route) if route else None
            if found is not None:
                found["now"] = time.time()
            self._send(json.dumps(found), "application/json")
        elif self.path.startswith("/stamps?"):
            # When each turn was written - one entry per history turn, in the
            # same order (see main.stamp_history). Kept out of the transcript
            # for the same reason the validation log above it is: the history
            # is what the provider gets, and this is for the person reading
            # the page. A chat with no stamps answers [], and the page simply
            # draws no times rather than inventing any.
            q = parse_qs(urlparse(self.path).query)
            stem = _stem_of(q.get("chat", [""])[0])
            if stem is None:
                self._send("not found", code=404)
            else:
                self._send(json.dumps(main.read_stamps(stem)), "application/json")
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
        elif self.path == "/safety" or self.path.startswith("/safety?"):
            # create=True, not mint: opening the dropdown in a window that has
            # not sent anything yet is reading, and reading must not bring a
            # chat folder into existence. _safety() answers for None.
            self._send(json.dumps(_safety(_chat_of(self, create=True))),
                       "application/json")
        elif self.path == "/settings":
            self._send(json.dumps(_settings()), "application/json")
        elif self.path == "/providers":
            self._send(json.dumps(_providers()), "application/json")
        elif self.path == "/env":
            self._send(json.dumps(_env()), "application/json")
        elif self.path == "/wake/models":
            self._send(json.dumps([{"file": n, "label": wake_word.label(n)}
                                   for n in wake_word.available()]),
                       "application/json")
        elif self.path.startswith("/voice/models"):
            q = parse_qs(urlparse(self.path).query)
            self._send(json.dumps(_stt(q.get("provider", [""])[0])),
                       "application/json")
        elif self.path.startswith("/speak/models"):
            q = parse_qs(urlparse(self.path).query)
            self._send(json.dumps(_tts(q.get("provider", [""])[0])),
                       "application/json")
        elif self.path.startswith("/speak/voices"):
            q = parse_qs(urlparse(self.path).query)
            self._send(json.dumps(_voices(q.get("provider", [""])[0],
                                          q.get("model", [""])[0])),
                       "application/json")
        elif self.path.startswith("/speak"):
            self._get_speak()
        elif self.path == "/email":
            self._send(json.dumps(_email_accounts()), "application/json")
        elif self.path == "/workspaces":
            self._send(json.dumps(_workspaces()), "application/json")
        elif self.path == "/context":
            self._send(json.dumps(_context()), "application/json")
        elif self.path == "/tools" or self.path.startswith("/tools?"):
            # ?type=skill is the sidebar's list, which is skills and nothing
            # else - answered without importing anything, and streamed like
            # the chats above it. Bare /tools is still the whole inventory,
            # tools included, which is what a POST's reply has to be.
            if parse_qs(urlparse(self.path).query).get("type") == ["skill"]:
                self._send_ndjson(_skills())
            else:
                self._send(json.dumps(_tools()), "application/json")
        elif self.path == "/usage" or self.path.startswith("/usage?"):
            # Read when the tab is opened and when its range changes, not on a
            # timer: the ledger is a file on disk that only grows at the end,
            # and nothing about last month's totals needs a two-second poll.
            self._send(json.dumps(_usage_summary(urlparse(self.path).query)),
                       "application/json")
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
            self._send(CRON_FILE.read_text(encoding="utf-8") if CRON_FILE.exists()
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
        if self.path == "/speak/test":
            self._post_speak_test()
            return
        if self.path == "/wires":
            self._post_wire()
            return
        if self.path == "/wires/remove":
            self._post_wire_remove()
            return
        if self.path == "/wires/preview":
            self._post_wire_preview()
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
        if self.path == "/cron/enabled":
            self._post_cron_enabled()
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
        if self.path == "/wake" or self.path.startswith("/wake?"):
            self._post_wake()
            return
        if self.path == "/voice/say" or self.path.startswith("/voice/say?"):
            self._post_voice_say()
            return
        if self.path == "/attach" or self.path.startswith("/attach?"):
            self._post_attach()
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
        if self.path == "/safety" or self.path.startswith("/safety?"):
            self._post_chat_safety()
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

        # /continue is taken before the command table on purpose: it is not a
        # command that answers, it is a turn that starts with no message. The
        # button on the page posts this exact string, so the button and the
        # typed line are one code path - the same arrangement /stop has.
        if text.strip().lower() == main.CONTINUE:
            why = main.continue_from(c)
            if why is not None:
                self._send(json.dumps({"type": "system", "text": why}),
                           "application/json")
                return
            _run_turn(None, target=c)
            # No "user" broadcast goes with this one and none is wanted: the
            # window that asked takes the marker line off its own transcript
            # when it sees "started", and every other window redraws from the
            # wound-back history when the turn ends.
            self._send(json.dumps({"type": "started", "chat": c.route}),
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
            # turn behind the denied one. Before the injection below on purpose:
            # a turn stopped at an approval is a turn that will never reach
            # another pass, so a message folded into it would sit there unread
            # for as long as the question went unanswered.
            if command_processor.deny_pending(c.id):
                _broadcast({"type": "system", "chat": c.route,
                            "text": "pending approval denied - your message follows."})
            global _last_input_chat
            with _last_input_lock:
                _last_input_chat = c
            # "?when=tool" is the page saying this was sent with enter while the
            # chat was already working: fold it into the turn already running,
            # at its next pass, rather than starting a competing one. Anything
            # else (tab-queued text, voice, a chat that turns out to be idle
            # after all) takes the ordinary path below and is a turn of its own.
            if _asks_when(self) == "tool" and _queue_inject(c, text):
                self._send(json.dumps({"type": "queued", "chat": c.route}),
                           "application/json")
                return
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

    def _post_speak_test(self):
        """Read the test phrase out in one particular voice and hand back the
        audio. The voice tab's "hear it" button, and the same idea as
        /providers/test above: one real round trip, on a press, rather than
        anything a save does on its own.

        Everything is taken from the BODY, falling back to what is saved - so
        the button auditions what is on the form right now, unsaved. Picking a
        voice, hearing it, and only then deciding whether to keep it is the
        whole point; a button that could only play the last SAVED choice would
        make you commit to a voice to find out what it sounds like.

        The phrase itself is settings' speak_test_phrase unless the body sends
        one, so what you hear is a sentence shaped like the things this install
        actually says."""
        body = self._body() or {}
        chosen = settings.load()

        def pick(key):
            """The body's value when it sent one, the saved one otherwise. A
            sent-but-empty string is a real answer ("no instructions", "the
            default voice") and must not fall through to what is saved."""
            value = body.get(key)
            return value if isinstance(value, str) else chosen.get(key, "")

        text = (pick("speak_test_phrase").strip()
                or str(chosen.get("speak_test_phrase") or "").strip()
                or settings.DEFAULTS["speak_test_phrase"])
        try:
            audio, mime = provider.speak(pick("speak_provider"),
                                         pick("speak_model"), text,
                                         voice=pick("speak_voice"),
                                         instructions=pick("speak_instructions"))
        except RuntimeError as e:
            # 502 with a readable sentence, exactly like _get_speak: what failed
            # is the speech provider, and the tab puts the message straight
            # under the button.
            self._send(str(e), code=502)
            return
        except Exception as e:
            self._send(type(e).__name__ + ": " + str(e), code=502)
            return
        self._send_bytes(audio, mime)

    def _post_wire(self):
        """Save one wire into wires.json - a new one, or an override of a
        shipped one.

        Refused rather than saved if it could not work: wires.save() returns
        the problems and nothing is written. A wire that 400s every request is
        found here, at the moment somebody wrote it, rather than three days
        later inside a cron job at 7am."""
        body = self._body() or {}
        name, spec = body.get("name"), body.get("spec")
        if not isinstance(name, str) or not isinstance(spec, dict):
            self._send('expected a "name" and a "spec"', code=400)
            return
        problems = wires.save(name, spec)
        if problems:
            self._send(json.dumps({"problems": problems}), "application/json",
                       code=400)
            return
        self._send(json.dumps(_providers()), "application/json")
        # A wire appearing or changing changes the wire dropdown on every
        # provider card, and can change what a provider sends on its next turn.
        _broadcast_context()

    def _post_wire_remove(self):
        """Take a wire out of wires.json.

        Two different-looking things, one operation: a wire of your own is
        deleted, and a shipped wire you had overridden goes back to exactly
        what Uniagent ships. Both are "stop saying anything about this wire in
        the overlay", which is why there is one button and not two.

        A provider still pointed at a wire that this removes entirely keeps its
        card and stops working, saying so - the same as a provider whose key is
        wrong. Deleting the providers too would be a surprise, and they are one
        click each to repoint."""
        body = self._body() or {}
        name = body.get("name")
        if not isinstance(name, str):
            self._send('expected a "name"', code=400)
            return
        wires.delete(name)
        self._send(json.dumps(_providers()), "application/json")
        _broadcast_context()

    def _post_wire_preview(self):
        """Render the request a wire would send, without sending it."""
        body = self._body() or {}
        name = body.get("name")
        if not isinstance(name, str):
            self._send('expected a "name"', code=400)
            return
        spec = body.get("spec")
        self._send(json.dumps(_wire_preview(name, spec if isinstance(spec, dict) else None)),
                   "application/json")

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

    def _post_chat_safety(self):
        """Set THIS chat's safety gate - the slider in the corner of the chat
        window. Written to the chat's own settings.json, never to any global:
        two chats open side by side can be running at two different numbers,
        which is the whole point of the control being there rather than on the
        settings page.

        The body says only what it is changing. "threshold" moves the number;
        "extra" and "prompt" are the chat's own words for the check, and are
        saved together because the panel edits them together. A key that isn't
        in the body is left alone, so the level buttons and the text boxes can
        save independently without either wiping the other."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        c = _chat_of(self, create=True, mint=True)
        if c is None:
            self._send("no chat to set a safety number on", code=400)
            return

        if "threshold" in body:
            raw = body.get("threshold")
            # None is a real value and means "follow the settings default
            # again", which is how a chat is un-pinned. Anything else must be a
            # number in range - a typo silently becoming "follow the default"
            # would read as the control not working.
            # Range-checked, not clamped. clamp() exists to make a stored
            # value usable, and silently turning a mistyped 42 into 10 would
            # be the one clamp that matters: 10 is "check nothing". A request
            # that asks for a number off the scale is wrong, and says so.
            in_range = (isinstance(raw, int) and not isinstance(raw, bool)
                        and settings.SAFETY_MIN <= raw <= settings.SAFETY_MAX)
            if raw is not None and not in_range:
                self._send("a safety number has to be a whole number from "
                           + str(settings.SAFETY_MIN) + " to "
                           + str(settings.SAFETY_MAX), code=400)
                return
            try:
                c.set_safety_threshold(raw)
            except OSError as e:
                self._send("could not save: " + str(e), code=500)
                return

        if "extra" in body or "prompt" in body:
            extra = body.get("extra", c.safety_extra)
            prompt = body.get("prompt", c.safety_prompt)
            if not isinstance(extra, (str, type(None))) \
                    or not isinstance(prompt, (str, type(None))):
                self._send("extra and prompt must be text", code=400)
                return
            # A whole-prompt override with no {call} in it would ask the
            # checking model about nothing. tool_validation falls back to the
            # shared prompt in that case rather than running it, so this can't
            # be unsafe - but a box that silently does nothing is worse than
            # one that says why, and this is the moment there is somebody
            # there to read it.
            if (prompt or "").strip() and "{call}" not in prompt:
                self._send("a custom prompt has to contain {call} - that is "
                           "where the tool call gets substituted in.", code=400)
                return
            try:
                c.set_safety_text(extra=extra, prompt=prompt)
            except OSError as e:
                self._send("could not save: " + str(e), code=500)
                return

        # The whole state back, not just what changed: the page redraws the
        # panel from this rather than from what it sent, so a chat minted by
        # this very request comes back with the id it was given, and a number
        # that resolved differently from what was asked (cleared back to the
        # default, say) shows what it actually is.
        self._send(json.dumps(_safety(c)), "application/json")

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
            path.write_text(text, encoding="utf-8")
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

    def _post_cron_enabled(self):
        """Turn one cron job on or off, by name.

        Its own endpoint rather than a field the cron tab folds into its
        whole-file save, because the switch is not an edit being composed: it
        takes effect the moment it is flipped, and it must not carry a
        half-typed row sitting further up the page onto disk with it. The
        server re-reads cron.json, changes that one key and writes it back
        (cron.set_job_enabled), so a job the agent added through the cron tool
        since this page loaded is not clobbered by a stale copy in the browser.

        A refusal comes back 200 with ok:false and the reason, the same shape
        the tools tab's toggle uses - it is an answer about a job, not an HTTP
        error."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        name = body.get("name")
        enabled = body.get("enabled")
        if not isinstance(name, str) or not name.strip() or not isinstance(enabled, bool):
            self._send("expected {\"name\": ..., \"enabled\": true|false}", code=400)
            return
        error = cron.set_job_enabled(name.strip(), enabled)
        self._send(json.dumps({"ok": error is None, "error": error or "",
                               "name": name.strip(), "enabled": enabled}),
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
            CRON_FILE.write_text(body["text"], encoding="utf-8")
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
            path.write_text(code, encoding="utf-8")
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
            text = src.read_text(encoding="utf-8")
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

    def _post_wake(self):
        """A chunk of what the room sounds like, on its way past the wake
        model: raw signed 16-bit mono at 16kHz as the whole body, the browser
        session it belongs to in ?session=.

        This is the one route in the whole app that gets audio nobody chose to
        send. That is the point of it, and it is why the audio stops here: it
        is fed to a model that answers one question about it - was that the
        phrase - and is then dropped. Nothing is written down, nothing is
        transcribed, and nothing leaves the machine. Only the answer goes back,
        and only a "yes" makes the page start recording anything.

        ?stop=1 with no body is the page saying it has stopped listening, so
        the model behind that session can be let go.

        WakeError is 503 rather than 500: what is missing is a package or a
        model file on this machine, the message says which, and the page shows
        it as-is under the ear button."""
        q = parse_qs(urlparse(self.path).query)
        session = (q.get("session", [""])[0] or "")[:64]
        if not session:
            self._send("no session", code=400)
            return

        if q.get("stop", [""])[0]:
            wake_word.forget(session)
            self._send(json.dumps({"wake": False}), "application/json")
            return

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_CLIP:
            self._send("no audio", code=400)
            return
        pcm = self.rfile.read(length)

        chosen = settings.load()
        name = chosen.get("wake_model") or ""
        if not name:
            self._send("no wake model chosen - pick one on the settings page's "
                       "voice tab", code=503)
            return
        try:
            answer = wake_word.listen(session, pcm, name,
                                      float(chosen.get("wake_threshold", 0.5)))
        except wake_word.WakeError as e:
            self._send(str(e), code=503)
            return
        except Exception as e:
            self._send("the wake model failed - " + type(e).__name__ + ": "
                       + str(e)[:200], code=503)
            return
        self._send(json.dumps(answer), "application/json")

    def _post_voice_say(self):
        """A message the wake-word listener heard, as the pieces it was said
        in: {"parts": [...], "fresh": n, "interrupt": bool}.

        Not the same thing as POST /voice, which only turns audio into words
        and knows nothing about chats. This is where those words become a turn,
        and it exists as its own route rather than going through /input because
        of one case /input has no way to express: the user carried on talking
        after the message had already gone.

        `fresh` is how many of `parts` have not been sent yet - all of them for
        an ordinary message, fewer when this is somebody finishing a sentence
        late. In that second case the turn already running is stopped and the
        whole message re-sent as one, with the late words marked as the
        continuation they are (see main.voice_message). What that buys is a
        model answering the sentence the user actually said instead of the
        first half of it; what it costs is the tokens the stopped turn had
        produced, which is why "interrupt": false is allowed to say no and fold
        the late words into the running turn the way enter does instead.

        A turn that had already called a tool is never unwound - main.voice_
        rewind refuses, and the late words go on top of the work as their own
        message."""
        body = self._body()
        if not isinstance(body, dict):
            self._send("expected a JSON object", code=400)
            return
        parts = [p.strip() for p in (body.get("parts") or [])
                 if isinstance(p, str) and p.strip()]
        if not parts:
            self._send("nothing was said", code=400)
            return
        # A `fresh` that doesn't describe this list is treated as "all of it",
        # which is the safe misreading: the worst it does is send the whole
        # message as a new one, where the alternative is sending half a
        # sentence with no idea what came before it.
        fresh = body.get("fresh")
        if (not isinstance(fresh, int) or isinstance(fresh, bool)
                or not 1 <= fresh <= len(parts)):
            fresh = len(parts)

        c = _chat_of(self, create=True, mint=True)
        if c is None:
            self._send(json.dumps({"type": "system",
                                   "text": "that chat no longer exists."}),
                       "application/json")
            return

        global _last_input_chat
        with _last_input_lock:
            _last_input_chat = c

        if fresh == len(parts):
            # Nothing of this has been said to the model yet - an ordinary
            # turn, and the whole of the wake word's normal path.
            _run_turn(main.voice_message(parts), target=c)
            self._send(json.dumps({"type": "started", "chat": c.route}),
                       "application/json")
            return

        late = main.voice_message(parts[-fresh:], first=False)

        if not body.get("interrupt", True):
            # The setting says don't interrupt. Same arrangement as a message
            # typed with enter into a working chat: it waits for the next tool
            # result, and starts a turn of its own if there is nothing running.
            if _queue_inject(c, late):
                self._send(json.dumps({"type": "queued", "chat": c.route}),
                           "application/json")
                return
            _run_turn(late, target=c)
            self._send(json.dumps({"type": "started", "chat": c.route}),
                       "application/json")
            return

        # request_stop does the whole job on this thread - cancels the turn,
        # closes the transcript out and hands the chat on - so by the line
        # below there is no turn running and the history on disk is settled,
        # which is exactly what voice_rewind needs to be able to edit it. False
        # means there was nothing running to stop: the turn finished in the
        # time it took to say the rest of the sentence, and its answer stands.
        stopped = main.request_stop(c.id)
        merged = main.voice_rewind(c) if stopped else False
        _run_turn(main.voice_message(parts) if merged else late, target=c)
        self._send(json.dumps({"type": "started", "chat": c.route,
                               "stopped": stopped, "merged": merged}),
                   "application/json")

    def _post_attach(self):
        """One file on its way into a chat's attachments folder: the raw bytes
        as the whole body, the name it had on the other machine in ?name=,
        exactly the shape POST /voice takes a clip in. No multipart parsing
        anywhere.

        One file per request, deliberately: the page can then say which of
        several failed, and a big one that dies half way does not take the
        others with it.

        Nothing is said to the model here - an upload is not a turn. The reply
        is the path the file now has on this machine, and the page writes those
        paths into the message it sends next (see index.html's sendWith), so an
        attachment reaches the model as an ordinary message naming an ordinary
        file it can read.

        The bytes go to disk as they arrive under a .part name and are renamed
        into place once the last one is there, so a transfer cut off half way
        never leaves something that looks like a finished file - which is the
        whole reason the page waits for this reply before sending anything."""
        # create=True, not mint: attaching to a chat the window has minted but
        # not written to is normal - it is how the first message of a new
        # conversation carries a file. This is what brings the folder into
        # being, the same way that message would have.
        c = _chat_of(self, create=True)
        if c is None:
            self._send("that chat no longer exists.", code=404)
            return
        q = parse_qs(urlparse(self.path).query)
        name = _attach_name(q.get("name", [""])[0])
        if name is None:
            self._send("a file needs a name", code=400)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send("no file", code=400)
            return
        if length > MAX_UPLOAD:
            self._send("that file is bigger than the %d MB limit"
                       % (MAX_UPLOAD // (1024 * 1024)), code=413)
            return

        folder = main.attachments_dir(c)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = _reserve_upload(folder, name)
        except OSError as e:
            self._send("could not make room for " + name + ": " + str(e), code=500)
            return
        part = folder / ("." + target.name + ".part")
        try:
            with part.open("wb") as f:
                left = length
                while left > 0:
                    block = self.rfile.read(min(UPLOAD_BLOCK, left))
                    if not block:
                        raise OSError("the connection ended part way through")
                    f.write(block)
                    left -= len(block)
            part.replace(target)
        except OSError as e:
            # Both of them: the .part is a half a file, and the placeholder is
            # a name reserved for something that never arrived.
            part.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            self._send("could not save " + name + ": " + str(e), code=500)
            return
        self._send(json.dumps({"name": target.name, "path": str(target)}),
                   "application/json")

    def _post_update_check(self):
        """Ask the remote what is new, now, because someone pressed the button
        and is watching. The answer is written to the cache on the way out, so
        the "last checked" line and the update marker on the settings button
        agree with what the button just said - see _write_check."""
        global _update_check_error
        py = update._python()
        try:
            r = subprocess.run([py, str(ROOT / "scripts" / "update.py"),
                                "--check", "--json"],
                               capture_output=True, timeout=90, cwd=str(ROOT),
                               text=True, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as e:
            self._send(json.dumps({"ok": False, "error": "could not run the check: "
                                   + type(e).__name__}), "application/json")
            return
        # --json puts the survey on the last line; anything before it is noise
        # worth keeping out of the JSON parse but worth showing if it failed.
        line = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            survey = json.loads(line)
            if survey.get("ok"):
                _write_check(survey)
                _update_check_error = None
            else:
                _update_check_error = survey.get("error") or "the check failed"
            self._send(json.dumps(survey), "application/json")
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
            if NAMES_FILE.read_text(encoding="utf-8").strip() == want:
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
    NAMES_FILE.write_text(want + "\n", encoding="utf-8")
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
    # UTF-8 on the way out before anything writes a word. The server's stdout
    # is a log file under both service managers, and on Windows that file would
    # otherwise be written in the system codepage - so the first reply
    # containing an em dash would raise instead of being logged.
    _term.setup_console()
    # So the cron watcher and an update can find this process later. Written
    # before the port is taken, since a server that fails to bind still wants
    # to be stoppable.
    service.write_pidfile("server")
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
        # There is no response in flight any more, so there is nothing left to
        # restore one from. Cleared HERE and not left to the abandoned worker's
        # finally, which only runs whenever that thread happens to unwind - a
        # long tool can hold it for minutes. Until then the record still read
        # "waiting", counting from the moment the tool call arrived, and the
        # redraw that "done" sets off below would fetch it and draw that stale
        # wait under the stop marker - time the model spent nowhere near this
        # turn, labelled as its latency.
        _live_clear(route)
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
    try:
        httpd.serve_forever()
    finally:
        # Every live Claude Code session is a child process holding a
        # subscription login open. They are daemon-threaded, so an exit would
        # abandon rather than close them; this asks each to shut down properly
        # instead of leaving CLI processes behind on every restart.
        claude_session.close_all()


if __name__ == "__main__":
    serve()
