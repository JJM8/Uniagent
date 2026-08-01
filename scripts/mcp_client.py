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
from contextlib import asynccontextmanager
from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / "mcp.json"
LOG_FILE = Path(__file__).parent.parent / "mcp_servers.log"

# How long to wait for the servers to finish connecting when something actually
# needs them. Only ever paid once, on the first call of a session - start() is
# fired at import, so in practice they are up long before a model asks.
READY_TIMEOUT = 30
CALL_TIMEOUT = 60          # default per-call ceiling, overridable per call
CONNECT_TIMEOUT = 20       # per server, so one hanging server can't block the rest

_lock = threading.Lock()
_loop = None
_started = False
_ready = threading.Event()     # set once every server has settled (up or broken)
_shutdown = None               # asyncio.Event, created on the loop

# server name -> {"state": ready|connecting|broken|disabled, "error": str,
#                 "tools": [ {name, description, schema} ]}
_state = {}
_sessions = {}                 # server name -> live ClientSession


def _config():
    """mcp.json as {server name: config}, or {} if there isn't one.

    Deliberately NOT ~/.claude.json. provider.py goes out of its way to stop
    the Claude Code path inheriting the human's own MCP setup (see
    _claude_subscription's mcp_servers={}, strict_mcp_config=True), and
    Uniagent reaching into that file here would undo the same decision from
    the other side. Uniagent's servers are Uniagent's to declare."""
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _allowed(cfg, name):
    """Whether `name` survives this server's "tools" allowlist. No allowlist
    means everything, which is the honest default but rarely the right one -
    see the note in mcp.json.example. The list exists because schemas are not
    free: every tool that gets through here goes into the mcp tool's own
    listing, and a server with forty tools is thousands of tokens the moment
    the model asks what's on it."""
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
                with open(LOG_FILE, "a") as errlog:
                    async with _open(cfg, kind, errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            # A server that never answers initialize would
                            # otherwise leave this task parked forever and
                            # _ready never set.
                            await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                            listed = await asyncio.wait_for(session.list_tools(),
                                                            CONNECT_TIMEOUT)

                            tools = [{"name": t.name,
                                      "description": t.description or "",
                                      "schema": t.inputSchema
                                                or {"type": "object", "properties": {}}}
                                     for t in listed.tools if _allowed(cfg, t.name)]

                            _sessions[name] = session
                            _state[name] = {"state": "ready", "tools": tools,
                                            "error": "", "transport": kind}
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
            out[name] = {"state": "disabled", "tools": 0, "error": "", "transport": ""}
            continue
        s = _state.get(name) or {"state": "connecting", "tools": [], "error": ""}
        out[name] = {"state": s["state"], "tools": len(s["tools"]),
                     "error": s["error"], "transport": s.get("transport", "")}
    return out


def catalogue(server, tool=None):
    """(tools, error) for one server - the tool dicts as tools/list gave them,
    narrowed to `tool` if named. error is "" when all is well, otherwise the
    text to hand the model instead."""
    if not _wait_ready():
        return [], "the MCP servers have not finished starting up. Try again."
    if server not in _config():
        known = ", ".join(_config()) or "(none configured in mcp.json)"
        return [], "there is no MCP server called \"" + server + "\". You have: " + known

    # Configured but never dialled - added to mcp.json since Uniagent started.
    # Dial it now rather than reporting a startup that is never going to happen.
    if server not in _state:
        _ensure(server)

    s = _state.get(server, {})
    if s.get("state") == "broken":
        return [], "the \"" + server + "\" server did not start: " + s.get("error", "")
    if s.get("state") == "disabled":
        return [], "the \"" + server + "\" server is switched off in mcp.json."
    if s.get("state") != "ready":
        return [], ("the \"" + server + "\" server is still connecting. Wait a "
                    "moment and try once more; if it keeps saying this, the "
                    "server is not answering and its command or url in mcp.json "
                    "is likely wrong.")

    tools = s["tools"]
    if tool:
        tools = [t for t in tools if t["name"] == tool]
        if not tools:
            names = ", ".join(t["name"] for t in s["tools"])
            return [], ("\"" + server + "\" has no tool called \"" + tool
                        + "\". It has: " + names)
    return tools, ""


def _flatten(result):
    """An MCP CallToolResult as the plain string every tool in tools/ returns.

    A result is a list of content blocks, not text - so text blocks are joined
    and anything else is described rather than dumped. An image is the one
    worth spelling out: it arrives as base64 that would swamp the context to no
    purpose, so it is named and its size given, and view_image remains the way
    to actually look at something."""
    parts = []
    for block in (result.content or []):
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

    text = "\n".join(p for p in parts if p)
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


def call(server, tool, args=None, timeout=CALL_TIMEOUT, _retry=True):
    """Run one tool on one server and return its output as text. Never raises -
    every failure comes back as an "ERROR: ..." string, same as every tool in
    tools/ does, so a bad call is a turn the model can recover from rather than
    a crashed one."""
    found, error = catalogue(server, tool)
    if error:
        return "ERROR: " + error

    session = _sessions.get(server)
    if session is None:
        return "ERROR: the \"" + server + "\" server is not connected right now."

    try:
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool, args or {}), _loop)
        # +5 so the outer wait always loses to the inner one, and a timeout is
        # reported as the server being slow rather than as a bare future error.
        result = future.result(timeout + 5)
    except TimeoutError:
        # The server is alive and simply slow (or not answering) - NOT a dead
        # pipe, so the session is left alone. Reconnecting here would throw
        # away a perfectly good server every time one call ran long.
        return ("ERROR: " + server + "." + tool + " did not answer within "
                + str(timeout) + "s. Raise `timeout` if it needs longer.")
    except Exception as e:
        # A tool that merely FAILED comes back as a result with isError set, so
        # an exception out of call_tool means the transport itself broke - the
        # server process died, or its pipes closed. Re-dial once and try again:
        # that turns "ClosedResourceError forever" into one slow call.
        if _retry:
            drop(server)
            if _config().get(server):
                return call(server, tool, args, timeout, _retry=False)
        return ("ERROR calling " + server + "." + tool + ": " + type(e).__name__
                + ": " + str(e) + " (the connection was re-dialled and it "
                "failed again - the server may have stopped)")
    return _flatten(result)


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
    start()
    _wait_ready()
    return summary()
