"""Talks to MCP servers, so the mcp tool doesn't have to.

An MCP server is a separate program - Uniagent starts it as a child process and
speaks JSON-RPC to it down a pipe. Two messages matter: "tools/list" (what have
you got, answered with a name, a description and a JSON Schema per tool - the
same three things every tool in tools/ declares) and "tools/call" (run this
one). Nothing here is visible to the model: it calls the mcp tool, and this
file is what actually reaches the server.

WHY A BACKGROUND THREAD. The MCP SDK is asyncio, and every tool's run() here is
an ordinary blocking function called from the server's per-turn thread. So one
event loop runs forever on its own thread, every server gets a task on it that
opens its session and then just sits there holding it open, and the sync side
talks to those tasks through run_coroutine_threadsafe. The alternative -
starting a server per call - would pay a whole process launch (node or python,
so the better part of a second) on every single tool call.

WHY THE SESSIONS ARE HELD BY A TASK THAT PARKS. The SDK's context managers are
anyio-based, and anyio insists a cancel scope is exited by the same task that
entered it. Opening the session in one coroutine and closing it in another -
which is what an AsyncExitStack stored in a global ends up doing - raises
"Attempted to exit cancel scope in a different task". So _serve() enters both
contexts and then waits on a shutdown event, and teardown happens inside that
same task when the event is set. Calls come from other tasks, which is fine:
the rule is about exiting the context, not about using the session.

Nothing in here ever raises at the caller. A server that won't start is
recorded in _state and skipped, exactly as tool_processor treats a tool file
that won't import - one broken server must never take the agent down.
"""

import asyncio
import json
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import filecache

CONFIG_FILE = Path(__file__).parent.parent / "mcp.json"
LOG_FILE = Path(__file__).parent.parent / "mcp_servers.log"

# How long to wait for the servers to finish connecting when something actually
# needs them. Only ever paid once, on the first call of a session - start() is
# fired at import, so in practice they are up long before a model asks.
READY_TIMEOUT = 30
CALL_TIMEOUT = 60          # default per-call ceiling, overridable per call
CONNECT_TIMEOUT = 20       # per server, so one hanging server can't block the rest

# A server is entitled to page a long list, and the SDK hands back one page.
# Asking once would quietly show the model the first slice of a much longer
# catalogue - worse than an obvious truncation, because nothing says so.
MAX_PAGES = 20
MAX_ITEMS = 500
LOG_RING = 200             # per server; log notifications arrive unasked for

_lock = threading.Lock()
_loop = None
_started = False
_ready = threading.Event()     # set once every server has settled (up or broken)
_shutdown = None               # asyncio.Event, created on the loop

# server name -> {"state": ready|connecting|broken|disabled, "error": str,
#                 "tools": [ {name, description, schema} ],
#                 "caps": {resources, prompts, completions, logging, subscribe},
#                 "resources"|"templates"|"prompts": None until first asked for}
_state = {}
_sessions = {}                 # server name -> live ClientSession
_logs = {}                     # server name -> deque of log lines it pushed at us


def _config():
    """mcp.json as {server name: config}, or {} if there isn't one.

    Deliberately NOT ~/.claude.json. provider.py goes out of its way to stop
    the Claude Code path inheriting the human's own MCP setup (see
    _claude_subscription's mcp_servers={}, strict_mcp_config=True), and
    Uniagent reaching into that file here would undo the same decision from
    the other side. Uniagent's servers are Uniagent's to declare."""
    # Through filecache. This is reached from flattened(), which load_tools()
    # calls on EVERY call - and load_tools() runs three times per pass of the
    # tool loop, so this was three opens of mcp.json per message. A server
    # added to the file still appears within a second, which is what the
    # "added after startup still gets dialled" path below needs.
    try:
        data = json.loads(filecache.text(CONFIG_FILE, default="{}"))
    except json.JSONDecodeError:
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _flat(cfg):
    """Whether this server's tools are handed to the model as tools of their
    own (see flattened()) rather than reached through the generic `mcp` one.

    Having an allowlist IS the request to flatten. The two things want exactly
    the same judgement from whoever writes mcp.json - "which handful of these
    do I actually want the model to carry?" - so asking for it twice, once as
    `tools` and once as some `flatten: true`, would only create the case where
    they disagree."""
    allow = cfg.get("tools") if isinstance(cfg, dict) else None
    return isinstance(allow, list) and bool(allow)


def _allowed(cfg, name):
    """Whether `name` survives this server's "tools" allowlist. No allowlist
    means everything, which is the honest default but rarely the right one -
    see the note in mcp.json.example. The list exists because schemas are not
    free: every tool that gets through here goes into the mcp tool's own
    listing, and a server with forty tools is thousands of tokens the moment
    the model asks what's on it. Under flattening it costs that much on every
    single turn instead, which is why the same list decides both."""
    allow = cfg.get("tools")
    if not isinstance(allow, list) or not allow:
        return True
    return name in allow


def _why(e):
    """The real reason, dug out of anyio's ExceptionGroup wrappers.

    anyio runs the transports inside task groups, so a plain refused connection
    surfaces as "ExceptionGroup: unhandled errors in a TaskGroup (1
    sub-exception)" - which names nothing and helps nobody. The actual
    ConnectionRefusedError is a leaf one or two levels down. This walks to the
    leaves and reports those instead, because this text is what the model is
    shown when a server is unavailable, and what the description line says."""
    found = []

    def walk(x, depth=0):
        subs = getattr(x, "exceptions", None)
        if subs and depth < 5:
            for sub in subs:
                walk(sub, depth + 1)
            return
        text = str(x).strip()
        found.append(type(x).__name__ + (": " + text if text else ""))

    walk(e)
    out = []
    for f in found:            # dedupe, keeping order - retries repeat themselves
        if f not in out:
            out.append(f)
    return "; ".join(out) or type(e).__name__


def _transports(cfg):
    """Which transport(s) to try for this server, in order.

    A `command` means a child process over stdin/stdout. A `url` means the
    server is already running and listening on a port, and there are two ways
    to talk to one of those: Streamable HTTP, which is the current standard,
    and SSE, which is the older one a lot of servers still only speak.

    Naming `transport` in mcp.json pins it. Leaving it out tries HTTP and then
    SSE, so pointing Uniagent at a port is enough on its own - you do not have
    to know which of the two a given server implements, and the wrong guess
    costs one failed connect at startup rather than a config puzzle."""
    if cfg.get("command"):
        return ["stdio"]
    if not cfg.get("url"):
        return []
    named = (cfg.get("transport") or "").strip().lower()
    if named in ("http", "streamable-http", "streamable_http"):
        return ["http"]
    if named == "sse":
        return ["sse"]
    if named == "stdio":
        return ["stdio"]      # will fail on the missing command, and say so
    return ["http", "sse"]


@asynccontextmanager
async def _open(cfg, kind, errlog):
    """(read, write) for one transport. The three the SDK offers hand back
    slightly different things - streamable HTTP adds a session-id getter
    nothing here needs - so they are evened out to one pair."""
    if kind == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        if not cfg.get("command"):
            raise ValueError("transport \"stdio\" needs a \"command\" in mcp.json")
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args") or [],
            env=cfg.get("env") or None,
            cwd=cfg.get("cwd") or None,
        )
        # errlog, or the server's own stderr lands in Uniagent's. It is not a
        # small amount - a FastMCP-based server prints a boxed ASCII banner, a
        # version notice and a transport line before it says anything useful,
        # and under systemd all of that goes to the journal on every restart.
        # It is also written to a stdout/stderr that may not be alive - a
        # server started from a terminal since closed - which is the same trap
        # tool_validation._note() guards against. So it goes to a real file:
        # kept, because when a server won't start its stderr is the only place
        # that says why, and _state's one-line error is not enough to debug on.
        async with stdio_client(params, errlog=errlog) as (read, write):
            yield read, write
    elif kind == "sse":
        from mcp.client.sse import sse_client
        async with sse_client(cfg["url"], headers=cfg.get("headers") or None) as (read, write):
            yield read, write
    else:
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(
                cfg["url"], headers=cfg.get("headers") or None) as (read, write, _session_id):
            yield read, write


def _caps(session):
    """Which of the four surfaces this server actually offers, off the
    capabilities it declared during initialize.

    Read rather than guessed so that "this server has no prompts" can be said
    plainly instead of being discovered as a JSON-RPC method-not-found from
    the far end - which reads to a model like something broke, and invites it
    to try again."""
    c = session.get_server_capabilities()
    res = getattr(c, "resources", None)
    return {"tools": bool(getattr(c, "tools", None)),
            "resources": bool(res),
            "prompts": bool(getattr(c, "prompts", None)),
            "completions": bool(getattr(c, "completions", None)),
            "logging": bool(getattr(c, "logging", None)),
            "subscribe": bool(getattr(res, "subscribe", False))}


def _log_sink(name):
    """logging_callback for one server: keep the last LOG_RING lines it sent.

    A server pushes these whenever it feels like it, so they cannot be
    returned from a call - there has to be somewhere to put them, and a bounded
    ring is that somewhere. Unbounded, a chatty server would grow this process
    without limit over a long session for output nobody has asked to see."""
    async def sink(params):
        line = getattr(params, "data", "")
        if not isinstance(line, str):
            line = json.dumps(line, default=str)
        logger = getattr(params, "logger", "") or ""
        _logs.setdefault(name, deque(maxlen=LOG_RING)).append(
            time.strftime("%H:%M:%S") + " [" + str(getattr(params, "level", "?")) + "]"
            + (" " + logger if logger else "") + " " + line)
    return sink


async def _relist(name):
    """Re-read one server's tools after it said they changed.

    Its own task rather than inline in the notification handler: the handler
    runs ON the session's receive loop, so awaiting a fresh request from
    inside it would wait for a reply that only that same loop can deliver -
    a deadlock. Handing the work to a separate task lets the receive loop
    carry on and process the answer."""
    session = _sessions.get(name)
    s = _state.get(name)
    if session is None or not s:
        return
    try:
        listed = await asyncio.wait_for(session.list_tools(), CONNECT_TIMEOUT)
    except Exception:
        return          # it will be re-listed on the next reconnect
    cfg = _config().get(name) or {}
    s["tools"] = [{"name": t.name, "description": t.description or "",
                   "schema": t.inputSchema or {"type": "object", "properties": {}}}
                  for t in listed.tools if _allowed(cfg, t.name)]


def _notified(name):
    """message_handler for one server, for the notifications it sends unasked.

    A changed list is handled by THROWING THE CACHE AWAY rather than by
    fetching a new one: resources and prompts are fetched lazily anyway, so
    clearing is both the cheapest correct answer and one that costs nothing
    when the model never asks again. Tools are the exception - they go into
    the prompt every turn without anybody asking, so they are re-fetched."""
    async def handler(message):
        method = str(getattr(getattr(message, "root", message), "method", "") or "")
        s = _state.get(name)
        if not s:
            return
        if method.endswith("tools/list_changed"):
            asyncio.create_task(_relist(name))
        elif method.endswith("resources/list_changed"):
            s["resources"] = None
            s["templates"] = None
        elif method.endswith("prompts/list_changed"):
            s["prompts"] = None
        elif method.endswith("resources/updated"):
            uri = getattr(getattr(getattr(message, "root", message), "params", None),
                          "uri", "")
            _logs.setdefault(name, deque(maxlen=LOG_RING)).append(
                time.strftime("%H:%M:%S") + " [resource changed] " + str(uri))
    return handler


async def _serve(name, cfg):
    """Hold one server's session open until shutdown. One task per server."""
    from mcp import ClientSession

    kinds = _transports(cfg)
    if not kinds:
        _state[name] = {"state": "broken", "tools": [],
                        "error": "needs either \"command\" (a program to run) or "
                                 "\"url\" (a server already listening) in mcp.json"}
        return

    failures = []
    try:
        for kind in kinds:
            try:
                # errors="replace" because this is somebody else's stderr: a
                # server that prints a byte we cannot decode must not take the
                # whole connection down with it.
                with open(LOG_FILE, "a", encoding="utf-8",
                          errors="replace") as errlog:
                    async with _open(cfg, kind, errlog) as (read, write):
                        async with ClientSession(
                                read, write,
                                logging_callback=_log_sink(name),
                                message_handler=_notified(name)) as session:
                            # A server that never answers initialize would
                            # otherwise leave this task parked forever and
                            # _ready never set.
                            await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                            caps = _caps(session)

                            # Only ask for tools if it said it has them. A
                            # server exposing nothing but resources or prompts
                            # is perfectly legal, and answers tools/list with
                            # "Method not found" - which, asked unconditionally,
                            # failed the whole connection and made such a server
                            # unusable rather than merely tool-less.
                            tools = []
                            if caps["tools"]:
                                listed = await asyncio.wait_for(
                                    session.list_tools(), CONNECT_TIMEOUT)
                                tools = [{"name": t.name,
                                          "description": t.description or "",
                                          "schema": t.inputSchema
                                                    or {"type": "object", "properties": {}}}
                                         for t in listed.tools if _allowed(cfg, t.name)]

                            _sessions[name] = session
                            # Only the tools are listed now. Resources, their
                            # templates and prompts are left as None and
                            # fetched on the first ask - three more round trips
                            # per server on every boot, for catalogues most
                            # sessions never open, would be paid by everyone to
                            # help the few. A server that advertises a surface
                            # and then mishandles the call also cannot take the
                            # connection down with it this way.
                            _state[name] = {"state": "ready", "tools": tools,
                                            "error": "", "transport": kind,
                                            "caps": caps,
                                            "resources": None, "templates": None,
                                            "prompts": None}
                            await _shutdown.wait()
                # Shut down cleanly on a transport that WORKED - not a failure,
                # so don't fall through and try the next one.
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures.append(kind + " (" + _why(e)[:160] + ")")

        _state[name] = {"state": "broken", "tools": [],
                        "error": "could not connect over " + ", ".join(failures)}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _state[name] = {"state": "broken", "tools": [], "error": _why(e)}
    finally:
        _sessions.pop(name, None)


def _ensure_loop():
    """The background event loop, started on first need. Caller holds _lock.

    Lazy rather than created in start(), because start() returns early when
    mcp.json has no servers in it - and a server added after that still needs
    a loop to be dialled on (see _ensure)."""
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, name="mcp", daemon=True).start()
    return _loop


def _settle_now(name, cfg):
    """The state a server can be given without connecting to it - disabled, or
    misconfigured. True if one was set, so the caller knows not to dial.
    Shared by the startup sweep and _ensure, which must agree."""
    if not isinstance(cfg, dict) or not (cfg.get("command") or cfg.get("url")):
        _state[name] = {"state": "broken", "tools": [],
                        "error": "needs either \"command\" (a program to run) or "
                                 "\"url\" (a server already listening) in mcp.json"}
        return True
    if cfg.get("enabled") is False:
        _state[name] = {"state": "disabled", "tools": [], "error": ""}
        return True
    return False


async def _run_one(name, cfg):
    """_serve, with the shutdown event created first if it doesn't exist yet.

    _connect_all makes that event, so a server dialled by _ensure on a run
    where _connect_all had nothing to do would otherwise find it None and fail
    on the park at the end of _serve."""
    global _shutdown
    if _shutdown is None:
        _shutdown = asyncio.Event()
    await _serve(name, cfg)


def _ensure(name):
    """Connect a server that is in mcp.json but has never been dialled.

    This is the "added while Uniagent was running" case, and it used to be a
    dead end. _connect_all fills _state once, at startup, but _config() re-reads
    mcp.json on every call - so a server added afterwards passed the "no such
    server" check and then had no state at all. catalogue() read that missing
    entry as "not ready yet" and told the model the server was still starting
    up, on every attempt, forever: nothing was ever going to dial it.

    Editing mcp.json should just work, so a server with no state gets dialled
    here and now, and the caller waits the same amount of time startup would
    have. Blocking is fine because this only ever happens on the FIRST call to
    a newly added server - after it, the state is cached like any other."""
    cfg = _config().get(name)
    if not isinstance(cfg, dict):
        return

    with _lock:
        if name in _state:
            return                       # already dialled, or already settled
        if _settle_now(name, cfg):
            return
        _state[name] = {"state": "connecting", "tools": [], "error": ""}
        loop = _ensure_loop()

    asyncio.run_coroutine_threadsafe(_run_one(name, cfg), loop)

    limit = CONNECT_TIMEOUT + 5
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if (_state.get(name) or {}).get("state") != "connecting":
            return
        time.sleep(0.05)
    _state[name] = {"state": "broken", "tools": [],
                    "error": "did not finish connecting in " + str(limit) + "s"}


async def _connect_all():
    """Start every enabled server, then let the sync side through once they
    have all either come up or failed."""
    global _shutdown
    _shutdown = asyncio.Event()
    servers = _config()

    tasks = []
    for name, cfg in servers.items():
        if _settle_now(name, cfg):
            continue
        _state[name] = {"state": "connecting", "tools": [], "error": ""}
        tasks.append(asyncio.create_task(_serve(name, cfg)))

    # Settled means "ready or broken", NOT "task finished" - a healthy task
    # parks on _shutdown and never finishes, so waiting on the tasks would
    # wait forever. Poll the states the tasks publish instead.
    async def settled():
        while any(s["state"] == "connecting" for s in _state.values()):
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(settled(), CONNECT_TIMEOUT + 5)
    except asyncio.TimeoutError:
        for name, s in _state.items():
            if s["state"] == "connecting":
                _state[name] = {"state": "broken", "tools": [],
                                "error": "did not finish connecting in "
                                         + str(CONNECT_TIMEOUT + 5) + "s"}
    _ready.set()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def start():
    """Bring the servers up in the background. Safe to call any number of
    times - only the first does anything.

    That guard is load-bearing. tools/mcp_tool.py calls this at import, and
    tool_processor.load_tools() re-imports every tool file on EVERY turn, so
    without it each turn would launch another event loop thread and another
    copy of every server. This module lives in scripts/ and is never reloaded
    itself, so its globals are what survive that."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
        if not _config():
            _ready.set()      # nothing configured, so nothing to wait for
            return
        loop = _ensure_loop()
    asyncio.run_coroutine_threadsafe(_connect_all(), loop)


def _wait_ready(timeout=READY_TIMEOUT):
    start()
    return _ready.wait(timeout)


def summary():
    """{server: {"state", "tools" (a count), "error"}} - what to tell the model
    about, without blocking on anything.

    Read straight off the cache because this is called while BUILDING the
    prompt, once per turn: tool_processor.load_tools() re-reads the mcp tool's
    DESCRIPTION every turn, and that description is where the server list comes
    from. Anything that waited here would put that wait on every single turn."""
    start()
    servers = _config()
    out = {}
    for name, cfg in servers.items():
        # Read straight off the config rather than waiting for the loop to
        # publish it. Otherwise a server that is switched OFF reads as
        # "connecting" for the first moment of the process - which is not a
        # state it will ever leave, and it goes into the description the model
        # is shown on that first turn.
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            out[name] = {"state": "disabled", "tools": 0, "error": "",
                         "transport": "", "flat": _flat(cfg), "caps": {}}
            continue
        s = _state.get(name) or {"state": "connecting", "tools": [], "error": ""}
        # The capabilities, not counts of what is in them: a count would mean
        # listing, and this runs while the prompt is being assembled. "has
        # resources" is what the model needs to decide whether to look.
        out[name] = {"state": s["state"], "tools": len(s["tools"]),
                     "error": s["error"], "transport": s.get("transport", ""),
                     "flat": _flat(cfg), "caps": s.get("caps") or {}}
    return out


# `mcp__server__tool`, the same shape Claude Code uses. The prefix is what
# marks a name as belonging to a server rather than to a file in tools/, and
# the doubled underscore is what lets the two halves be told apart again by
# eye - single ones appear inside real server and tool names all the time.
NAME_PREFIX = "mcp__"
MAX_NAME = 64        # the tightest limit the four provider APIs impose
MAX_DESC = 1024      # somebody else's prose, and it goes in every request


def _safe(part):
    """One name component reduced to what a provider will accept in a tool
    name. Server names in mcp.json are free text and routinely have dots and
    spaces in them; a tool name that carries those is rejected outright."""
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in part)


def _wire_name(server, tool):
    """What the model calls this tool.

    Trimming takes from the SERVER half and never the tool half: the tool name
    is the part carrying the meaning the model matches on, and two tools on
    one server have to stay distinguishable from each other. Collisions are
    still possible once two long server names trim to the same stem, so
    flattened() drops duplicates rather than trusting this to be injective -
    two schemas under one name is a hard 400 from every provider."""
    server_s, tool_s = _safe(server), _safe(tool)
    name = NAME_PREFIX + server_s + "__" + tool_s
    if len(name) <= MAX_NAME:
        return name
    room = MAX_NAME - len(NAME_PREFIX) - len("__") - len(tool_s)
    if room > 0:
        return NAME_PREFIX + server_s[:room] + "__" + tool_s
    return (NAME_PREFIX + tool_s)[:MAX_NAME]


def _object_schema(schema):
    """The server's own inputSchema, guaranteed to be the object schema the
    provider APIs insist on at the top level.

    Passed through untouched when it already is one, which is the entire point
    of flattening: the model is constrained by the SERVER's own contract,
    argument names and types and all, rather than by a paraphrase of it that
    something here rendered into prose. Only the top level is checked - what's
    nested inside is the server's business, and tool_processor._gemini_schema
    already handles the one provider that can't take all of it."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        fixed = dict(schema)
        fixed["type"] = "object"
        fixed.setdefault("properties", {})
        return fixed
    return schema


def _make_run(server, tool, timeout):
    """run() for one flattened tool. A closure, so the server and tool names
    are carried by the entry itself and dispatch needs no registry to map a
    wire name back - tool_processor finds this like any other tool's run().

    **args because the arguments are the server's, not ours: whatever the
    schema declared is what arrives, and it goes straight through."""
    def run(**args):
        return call(server, tool, args, timeout)
    return run


def flattened():
    """Every allowlisted tool on a ready server, as tool_processor TOOLS
    entries - so an MCP tool reaches the model as a real tool with the
    server's own JSON Schema on it, instead of as arguments typed by hand into
    the generic `mcp` tool's untyped `args` object.

    NEVER waits and never dials. This is called from load_tools(), which runs
    on every single turn while the prompt is being built, so the cost has to
    be a dict lookup. A server still connecting contributes nothing this turn
    and appears on the next one.

    ONLY servers with a "tools" allowlist, and that is the context budget
    rather than a formality: each tool here spends its full schema on every
    request from now on, used or not, while a server left behind the generic
    tool costs one schema no matter how many tools sit on the far side. Four
    Unity servers flattened whole would be most of a context window before the
    model has read anything."""
    start()
    out = []
    seen = set()
    for server, cfg in _config().items():
        if not isinstance(cfg, dict) or cfg.get("enabled") is False or not _flat(cfg):
            continue
        s = _state.get(server) or {}
        if s.get("state") != "ready":
            continue

        # Per-server rather than an argument on the tool: adding our own
        # `timeout` property would corrupt the server's schema - a server is
        # perfectly entitled to have a `timeout` argument of its own - and the
        # model has no way to know a Unity build needs longer than a file read.
        try:
            timeout = int(cfg.get("timeout") or CALL_TIMEOUT)
        except (TypeError, ValueError):
            timeout = CALL_TIMEOUT

        for t in s["tools"]:
            name = _wire_name(server, t["name"])
            if name in seen:
                continue
            seen.add(name)
            desc = (t["description"] or "").strip()[:MAX_DESC]
            if not desc:
                desc = "The \"" + t["name"] + "\" tool."
            # The server is named in the description because the wire name may
            # have been trimmed, and because it is what `mcp` action
            # "reconnect" takes when this tool starts failing.
            out.append({
                "name": name,
                "description": desc + " (From the \"" + server + "\" MCP server.)",
                "instructions": ("Runs " + t["name"] + " on the \"" + server
                                 + "\" MCP server. Its arguments are on the "
                                   "schema attached to this tool."),
                "run": _make_run(server, t["name"], timeout),
                "schema": _object_schema(t["schema"]),
            })
    return out


def _up(server):
    """"" if `server` is connected and usable, otherwise the text to hand the
    model instead of whatever it asked for.

    Every entry point below runs this first, so a server that is unknown,
    switched off, broken or still connecting is reported the same way whether
    the model wanted a tool, a resource, a prompt or a completion - one
    explanation of one situation, rather than four that could drift apart."""
    if not _wait_ready():
        return "the MCP servers have not finished starting up. Try again."
    if server not in _config():
        known = ", ".join(_config()) or "(none configured in mcp.json)"
        return "there is no MCP server called \"" + server + "\". You have: " + known

    # Configured but never dialled - added to mcp.json since Uniagent started.
    # Dial it now rather than reporting a startup that is never going to happen.
    if server not in _state:
        _ensure(server)

    s = _state.get(server, {})
    if s.get("state") == "broken":
        return "the \"" + server + "\" server did not start: " + s.get("error", "")
    if s.get("state") == "disabled":
        return "the \"" + server + "\" server is switched off in mcp.json."
    if s.get("state") != "ready":
        return ("the \"" + server + "\" server is still connecting. Wait a "
                "moment and try once more; if it keeps saying this, the "
                "server is not answering and its command or url in mcp.json "
                "is likely wrong.")
    return ""


def _offers(server, surface):
    """"" if `server` declared `surface` at initialize, else the text to say so.

    Checked before the request goes out, because a server that simply does not
    do prompts answers a prompts/list with a JSON-RPC error, and an error is
    indistinguishable to a model from something having gone wrong - so it
    tries again, and again."""
    caps = (_state.get(server) or {}).get("caps") or {}
    if caps.get(surface):
        return ""
    return ("the \"" + server + "\" server does not offer " + surface
            + " - it did not advertise that capability when it connected.")


def catalogue(server, tool=None):
    """(tools, error) for one server - the tool dicts as tools/list gave them,
    narrowed to `tool` if named. error is "" when all is well, otherwise the
    text to hand the model instead."""
    error = _up(server)
    if error:
        return [], error

    s = _state.get(server, {})
    tools = s["tools"]
    if tool:
        tools = [t for t in tools if t["name"] == tool]
        if not tools:
            names = ", ".join(t["name"] for t in s["tools"])
            return [], ("\"" + server + "\" has no tool called \"" + tool
                        + "\". It has: " + names)
    return tools, ""


def _blocks(blocks):
    """A list of MCP content blocks as plain text.

    Text blocks are joined and anything else is described rather than dumped.
    An image is the one worth spelling out: it arrives as base64 that would
    swamp the context to no purpose, so it is named and its size given, and
    view_image remains the way to actually look at something.

    Shared by tool results and prompt messages, which are made of the same
    blocks - so an image comes back described the same way whichever surface
    it arrived through."""
    parts = []
    for block in (blocks or []):
        if block is None:
            continue
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind == "image":
            parts.append("[image: " + str(getattr(block, "mimeType", "?")) + ", "
                         + str(len(getattr(block, "data", "") or "")) + " base64 chars, not shown]")
        elif kind == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            parts.append(text if text else "[resource: " + str(getattr(res, "uri", "?")) + "]")
        else:
            parts.append("[" + (kind or "unknown") + " content, not shown]")
    return "\n".join(p for p in parts if p)


def _contents(result):
    """A ReadResourceResult as text. Contents are not content blocks - they
    are text-or-blob records carrying a uri - so this is not _blocks."""
    parts = []
    for c in (getattr(result, "contents", None) or []):
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
            continue
        blob = getattr(c, "blob", None)
        if blob is not None:
            parts.append("[binary resource: "
                         + str(getattr(c, "mimeType", "?") or "?") + ", "
                         + str(len(blob)) + " base64 chars, not shown]")
    return "\n".join(p for p in parts if p) or "(the resource is empty)"


def _messages(result):
    """A GetPromptResult as text to follow.

    Roles are kept and labelled, because a prompt is a scripted conversation
    and which side says what is often the whole point of it. Rendered as text
    rather than spliced into the real history: this is knowledge to act on,
    which is what a skill is, and read_skill hands those back as text too."""
    out = []
    desc = (getattr(result, "description", "") or "").strip()
    if desc:
        out.append(desc + "\n")
    for m in (getattr(result, "messages", None) or []):
        body = _blocks([getattr(m, "content", None)])
        out.append("[" + str(getattr(m, "role", "") or "?") + "]\n" + body)
    return "\n".join(out).strip() or "(the prompt is empty)"


def _flatten(result):
    """An MCP CallToolResult as the plain string every tool in tools/ returns."""
    text = _blocks(getattr(result, "content", None))
    # structuredContent is the newer typed half of a result; some servers put
    # everything there and leave content empty, which would otherwise come back
    # as a blank result the model can't act on.
    if not text and getattr(result, "structuredContent", None):
        text = json.dumps(result.structuredContent, indent=2)
    if not text:
        text = "(the tool ran and returned nothing)"

    if getattr(result, "isError", False):
        return "ERROR from the MCP server: " + text
    return text


def drop(server):
    """Forget everything about one server, so the next use re-dials it.

    A dead session is otherwise permanent. _serve parks on _shutdown once it
    has published "ready", so if the server PROCESS then dies - it crashed, or
    something killed it - nothing in that task is awaiting the streams and
    nothing notices. _state keeps saying "ready", _sessions keeps handing out a
    session whose pipes are shut, and every call from then on fails with
    ClosedResourceError, forever, with no way back short of restarting
    Uniagent. Clearing both is what makes _ensure dial again."""
    with _lock:
        _state.pop(server, None)
        _sessions.pop(server, None)


def _invoke(server, work, timeout, what, _retry=True):
    """(result, error) for one request against a server's live session.

    Every surface goes through here, so the awkward parts are solved once:
    getting onto the background loop from a blocking caller, the timeout, and
    the single silent re-dial. `work` is handed the session and returns the
    coroutine to await, which is what lets one function serve calls, reads,
    prompts and completions without knowing anything about them.

    Never raises. Every failure is an error string, same as every tool in
    tools/ returns, so a bad request is a turn the model can recover from
    rather than a crashed one."""
    error = _up(server)
    if error:
        return None, error
    session = _sessions.get(server)
    if session is None:
        return None, "the \"" + server + "\" server is not connected right now."

    try:
        future = asyncio.run_coroutine_threadsafe(work(session), _loop)
        # +5 so the outer wait always loses to the inner one, and a timeout is
        # reported as the server being slow rather than as a bare future error.
        return future.result(timeout + 5), ""
    except TimeoutError:
        # The server is alive and simply slow (or not answering) - NOT a dead
        # pipe, so the session is left alone. Reconnecting here would throw
        # away a perfectly good server every time one request ran long.
        return None, (what + " did not answer within " + str(timeout)
                      + "s. Raise `timeout` if it needs longer.")
    except Exception as e:
        # An McpError is the server ANSWERING, with an error: a method it does
        # not implement, a parameter it rejected. The connection is perfectly
        # healthy, so re-dialling would throw away a working session and every
        # catalogue cached on it to no purpose - and the second attempt would
        # be refused in exactly the same way. Report it and leave the session
        # alone. Only a broken transport earns a re-dial.
        from mcp.shared.exceptions import McpError
        if isinstance(e, McpError):
            return None, (what + " was refused by the server: " + str(e))

        # Anything else means the transport itself broke - the server process
        # died, or its pipes closed. Re-dial once and try again: that turns
        # "ClosedResourceError forever" into one slow request. drop() clears the
        # state, so _up() inside the retry re-dials on the way past.
        if _retry:
            drop(server)
            if _config().get(server):
                return _invoke(server, work, timeout, what, _retry=False)
        return None, (what + " failed: " + type(e).__name__ + ": " + str(e)
                      + " (the connection was re-dialled and it failed again - "
                        "the server may have stopped)")


def call(server, tool, args=None, timeout=CALL_TIMEOUT):
    """Run one tool on one server and return its output as text."""
    found, error = catalogue(server, tool)
    if error:
        return "ERROR: " + error
    result, error = _invoke(server, lambda s: s.call_tool(tool, args or {}),
                            timeout, server + "." + tool)
    if error:
        return "ERROR: " + error
    return _flatten(result)


# ---------------------------------------------------------------------------
# Resources, prompts, completions and logging - the surfaces beyond tools.
#
# Tools are the one surface worth flattening into real tools of the model's
# own, because a tool is a thing to CALL and has a schema saying how. The rest
# are not shaped like that: a resource is addressed by URI and read, a prompt
# is knowledge to follow rather than a result to report, a completion answers
# a question about an argument. They stay behind the mcp tool, where an action
# and a couple of arguments cover all of them.
# ---------------------------------------------------------------------------

# kind -> (capability it needs, how to ask for a page, field holding the items)
_LISTS = {
    "resources": ("resources", lambda s, c: s.list_resources(c), "resources"),
    "templates": ("resources", lambda s, c: s.list_resource_templates(c),
                  "resourceTemplates"),
    "prompts": ("prompts", lambda s, c: s.list_prompts(c), "prompts"),
}

LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical",
              "alert", "emergency")


async def _pages(fetch, attr):
    """Every page of a cursor-paginated list, to a bound. See MAX_PAGES."""
    out = []
    cursor = None
    for _ in range(MAX_PAGES):
        page = await fetch(cursor)
        out.extend(getattr(page, attr, None) or [])
        cursor = getattr(page, "nextCursor", None)
        if not cursor or len(out) >= MAX_ITEMS:
            break
    return out[:MAX_ITEMS]


def _plain(kind, x):
    """One listed item as a plain dict.

    Nothing pydantic leaves this module, exactly as the tool catalogue is
    stored as dicts rather than as SDK objects: the tool layer above should
    not have to know the SDK exists, and a field the SDK renames between
    versions then breaks one line here instead of several up there."""
    if kind == "prompts":
        return {"name": x.name,
                "description": (x.description or "").strip(),
                "arguments": [{"name": a.name,
                               "description": (a.description or "").strip(),
                               "required": bool(a.required)}
                              for a in (x.arguments or [])]}
    if kind == "templates":
        return {"uri": x.uriTemplate, "name": x.name or "",
                "description": (x.description or "").strip(),
                "mime": x.mimeType or ""}
    return {"uri": str(x.uri), "name": x.name or "",
            "description": (x.description or "").strip(),
            "mime": x.mimeType or ""}


def _fetch_list(server, kind, timeout=CALL_TIMEOUT):
    """(items, error) for one lazy catalogue, fetched once and then cached.

    The cache is cleared by the server's own list_changed notification (see
    _notified) and by drop(), so it cannot go stale in the ways that matter."""
    error = _up(server)
    if error:
        return [], error
    s = _state.get(server) or {}
    if s.get(kind) is not None:
        return s[kind], ""

    surface, fetch, attr = _LISTS[kind]
    error = _offers(server, surface)
    if error:
        return [], error

    raw, error = _invoke(server,
                         lambda sess: _pages(lambda c: fetch(sess, c), attr),
                         timeout, server + "'s " + kind)
    if error:
        return [], error

    items = [_plain(kind, x) for x in raw]
    s = _state.get(server)
    if s is not None:                       # may have been dropped while we waited
        s[kind] = items
    return items, ""


def resources(server):
    """(resources, uri templates, error) for one server.

    Both halves of one surface, fetched together because a listing that showed
    only one of them would read as complete and be wrong - plenty of servers
    expose everything through templates and list no fixed resources at all.
    Only a failure of BOTH is an error, so one unimplemented half never costs
    the model the other."""
    items, e1 = _fetch_list(server, "resources")
    tmpl, e2 = _fetch_list(server, "templates")
    if e1 and e2:
        return [], [], e1
    return items, tmpl, ""


def read(server, uri, timeout=CALL_TIMEOUT):
    """One resource's contents, as text."""
    error = _up(server) or _offers(server, "resources")
    if error:
        return "ERROR: " + error
    try:
        from pydantic import AnyUrl
        target = AnyUrl(str(uri))
    except Exception as e:
        return ("ERROR: \"" + str(uri) + "\" is not a usable resource URI ("
                + type(e).__name__ + "). Use one exactly as the resource "
                "listing gave it, scheme and all.")
    result, error = _invoke(server, lambda s: s.read_resource(target), timeout,
                            "reading " + str(uri))
    if error:
        return "ERROR: " + error
    return _contents(result)


def watch(server, uri, on=True, timeout=CALL_TIMEOUT):
    """Ask to be told when one resource changes, or stop asking.

    Without this the server sends nothing: resources/updated only arrives for
    a uri that was subscribed to. What arrives lands in the same ring logs()
    reads, because a turn-based agent has nowhere else to put a message that
    turns up between turns - there is no callback for it to interrupt. So the
    pattern is subscribe, do other work, then check logs()."""
    error = _up(server) or _offers(server, "subscribe")
    if error:
        return ("ERROR: " + error.replace(
            "does not offer subscribe",
            "does not support watching resources for changes"))
    try:
        from pydantic import AnyUrl
        target = AnyUrl(str(uri))
    except Exception as e:
        return ("ERROR: \"" + str(uri) + "\" is not a usable resource URI ("
                + type(e).__name__ + ").")

    _, error = _invoke(
        server,
        (lambda s: s.subscribe_resource(target)) if on
        else (lambda s: s.unsubscribe_resource(target)),
        timeout, ("watching " if on else "unwatching ") + str(uri))
    if error:
        return "ERROR: " + error
    if not on:
        return "No longer watching " + str(uri) + " on \"" + server + "\"."
    return ("Watching " + str(uri) + " on \"" + server + "\". Changes are "
            "reported through action \"logs\" on this server - they arrive "
            "between turns, so check there rather than waiting.")


def prompts(server):
    """(prompts, error) - each with its arguments and which are required."""
    return _fetch_list(server, "prompts")


def prompt(server, name, args=None, timeout=CALL_TIMEOUT):
    """One prompt, rendered as text to follow."""
    error = _up(server) or _offers(server, "prompts")
    if error:
        return "ERROR: " + error
    # Every value forced to str: the protocol types prompt arguments as
    # strings, so a model passing 3 rather than "3" would otherwise be
    # rejected by pydantic before the request was even sent - a validation
    # error about types, for what is really a correct answer.
    clean = {str(k): str(v) for k, v in (args or {}).items()}
    result, error = _invoke(server, lambda s: s.get_prompt(name, clean or None),
                            timeout, "prompt \"" + str(name) + "\"")
    if error:
        return "ERROR: " + error
    return _messages(result)


def complete(server, kind, target, argument, value="", timeout=CALL_TIMEOUT):
    """Suggested values for one argument of a prompt or a URI template."""
    error = _up(server) or _offers(server, "completions")
    if error:
        return "ERROR: " + error

    from mcp.types import PromptReference, ResourceTemplateReference
    if kind == "prompt":
        ref = PromptReference(type="ref/prompt", name=str(target))
    elif kind == "resource":
        ref = ResourceTemplateReference(type="ref/resource", uri=str(target))
    else:
        return ("ERROR: `kind` must be \"prompt\" (completing a prompt's "
                "argument) or \"resource\" (completing a URI template's), not \""
                + str(kind) + "\".")

    result, error = _invoke(
        server,
        lambda s: s.complete(ref, {"name": str(argument), "value": str(value)}),
        timeout, "completions for \"" + str(argument) + "\"")
    if error:
        return "ERROR: " + error

    comp = getattr(result, "completion", None)
    values = list(getattr(comp, "values", None) or [])
    if not values:
        return ("The server has no suggestions for \"" + str(argument) + "\""
                + (" starting \"" + str(value) + "\"." if value else "."))
    text = ("Values for \"" + str(argument) + "\": "
            + ", ".join(str(v) for v in values))
    if getattr(comp, "hasMore", False):
        text += " (there are more - pass a longer `value` to narrow them)"
    return text


def logs(server, level=None, timeout=CALL_TIMEOUT):
    """What the server has pushed at us, and optionally how much it should send.

    A read of the ring _log_sink fills rather than a request, because that is
    what logging is: the server sends when it wants to, and `level` only
    changes how much. Asking for a level the server does not have is not worth
    a round trip to discover, hence the check against LOG_LEVELS first."""
    error = _up(server)
    if error:
        return "ERROR: " + error

    if level:
        level = str(level).strip().lower()
        if level not in LOG_LEVELS:
            return ("ERROR: `level` must be one of " + ", ".join(LOG_LEVELS)
                    + " - not \"" + level + "\".")
        error = _offers(server, "logging")
        if error:
            return "ERROR: " + error
        _, error = _invoke(server, lambda s: s.set_logging_level(level), timeout,
                           "setting the log level")
        if error:
            return "ERROR: " + error

    lines = list(_logs.get(server) or [])
    if not lines:
        return ("\"" + server + "\" has logged nothing"
                + (" since the level was set to " + level + "." if level else
                   ". Set `level` to \"debug\" or \"info\" to ask it for more."))
    return ("The last " + str(len(lines)) + " lines from \"" + server + "\""
            + (" (level now " + level + ")" if level else "") + ":\n"
            + "\n".join(lines))


def refresh():
    """Re-read mcp.json and reconnect everything. For when a server's tools
    have changed, or one was added while Uniagent was running - the catalogue
    is fetched once at startup and cached, precisely because load_tools() runs
    every turn and could never afford to re-ask."""
    global _started, _loop, _shutdown
    with _lock:
        if _loop is not None and _shutdown is not None:
            _loop.call_soon_threadsafe(_shutdown.set)
            _loop.call_soon_threadsafe(_loop.stop)
        _started = False
        _loop = None
        _shutdown = None
        _ready.clear()
        _state.clear()
        _sessions.clear()
        _logs.clear()      # a new connection's log ring starts empty
    start()
    _wait_ready()
    return summary()
