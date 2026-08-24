"""One long-lived Claude Code session per chat, gated by Uniagent.

Every other provider in this project is a text endpoint: Uniagent assembles a
prompt, the provider answers, and Uniagent's own loop runs whatever tool the
answer asked for. This module is the one deliberate exception. Here Claude Code
keeps the conversation and runs the tool loop itself, and Uniagent's job changes
from "drive the agent" to "decide what it is allowed to do and write down what
happened".

That trade buys the two things the per-turn provider could not have:

  a warm session   the CLI process stays up between turns, so its own prompt
                   cache stays warm and each turn sends ONLY the new user
                   message instead of re-flattening the whole transcript. This
                   is the "claude code's caching" half of the design.
  real tools       Claude Code's own Read/Edit/Bash/Grep, which are the reason
                   to spend a Claude subscription rather than an API key, plus
                   every Uniagent tool alongside them (see _mcp_server).

Uniagent still owns everything a person sees or trusts: the approval bubble,
the safety dial, the transcript on disk, the token bar, /stop. None of that
moved.

THE THREADING RULE, which shapes the whole file
-----------------------------------------------
main.turn_chat() is a threading.local (main.py:1423), and so is the terminal
tool's per-chat shell. So Uniagent code MUST run on the worker thread that owns
the turn, and the SDK's asyncio loop is not that thread.

Nothing Uniagent-ish therefore runs on the session thread. The session thread
only ever PROPOSES - "Claude wants to call this", "here is some text" - by
putting a request on a queue and waiting. The worker thread, sitting in
run_turn() draining that queue, DISPOSES: it runs the safety check, shows the
approval, executes Uniagent tools, appends to history, and posts the answer
back. Two threads, one direction of authority, no Uniagent state touched from
the wrong side.

WHY THE GATE IS A HOOK AND NOT can_use_tool
-------------------------------------------
The SDK offers can_use_tool, which looks like the obvious approval hook and is
not: it only fires when the CLI would have asked a human, so in any permission
mode a read-only tool is auto-allowed and never reaches it. A gate with holes
in it is worse than no gate, because the holes are invisible.

A PreToolUse hook fires for EVERY call whatever the mode, carries the tool name
and its arguments, and can answer allow/deny (and even rewrite the arguments).
So the mode is set to bypassPermissions - meaning "do not run your own dialog",
NOT "allow anything" - and this hook becomes the single place a call can be
approved. Every tool call, built-in or Uniagent's, goes through Uniagent's
safety dial and Uniagent's approval bubble, and nothing runs unasked.
"""

import asyncio
import json
import queue
import threading
import time

import provider
import timing
import tool_processor
import tool_validation
import workspace

# The in-process MCP server Uniagent's own tools are served on, and the prefix
# Claude Code therefore knows them by. A call arrives as "mcp__uniagent__terminal"
# and has to be handed to tool_processor as "terminal", which is what _plain()
# is for.
MCP_SERVER = "uniagent"
PREFIX = "mcp__" + MCP_SERVER + "__"

# How long a hook may sit waiting. The default is 60s and this one blocks on a
# human reading an approval bubble, so it needs to be a human amount of time -
# an hour, after which the CLI gives up rather than hanging for ever.
GATE_TIMEOUT = 3600

# How long to wait for the session thread to say anything at all before
# concluding it has died. Generous because the first turn of a cold session
# pays for process start plus the model's own thinking.
IDLE_TIMEOUT = 600


def _plain(name):
    """A tool name as Uniagent knows it - "mcp__uniagent__terminal" -> "terminal",
    and a built-in like "Bash" unchanged. Everything downstream (the safety
    check, the approval text, the transcript) speaks this name, so the MCP
    prefix is stripped exactly once, here."""
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


# --- The bridge between the two threads --------------------------------------

class _Request:
    """One thing the session thread needs the worker thread to decide or do.

    Deliberately a plain object with an Event rather than an asyncio Future:
    the two ends live on different threads with different loops, and an Event
    is the one primitive both can use without either having to know about the
    other's scheduler."""

    def __init__(self, kind, **data):
        self.kind = kind
        self.data = data
        self.done = threading.Event()
        self.answer = None

    def reply(self, answer):
        self.answer = answer
        self.done.set()


class Session:
    """A live Claude Code process, its asyncio loop, and the thread running it.

    Created per chat and kept in _sessions until something invalidates it (see
    get()). The client is only ever touched from its own loop, through
    _call() - every public method here is safe to call from a worker thread."""

    def __init__(self, chat_id, model, system, key, cwd, remote):
        self.chat_id = chat_id
        self.model = model
        self.system = system
        self.key = key
        self.cwd = cwd
        self.remote = remote
        self.session_id = None
        self.started = time.time()
        self._in_call = None         # name of the tool_use block being written
        self._args = ""              # its arguments, collected until complete
        self._client = None
        self._events = None          # the live turn's queue, or None between turns
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._spin, daemon=True,
                                        name="claude-session-" + str(chat_id))
        self._thread.start()
        self._call(self._connect())

    # -- loop plumbing --------------------------------------------------------

    def _spin(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout=None):
        """Run `coro` on the session loop from a worker thread, and wait."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _emit(self, kind, **data):
        """Put an event on the live turn's queue. Dropped when no turn is
        running, which is the right thing rather than an error: the CLI can
        emit a stray message after a turn has been abandoned, and nobody is
        listening for it."""
        q = self._events
        if q is not None:
            q.put(_Request(kind, **data))

    def _ask(self, kind, **data):
        """Emit, then BLOCK the session loop's caller until the worker thread
        answers. Used by the gate and by Uniagent tool handlers - the two
        places the session genuinely cannot continue without a decision made
        on the other side."""
        q = self._events
        if q is None:
            return None
        req = _Request(kind, **data)
        q.put(req)
        return req

    # -- building the session -------------------------------------------------

    async def _gate(self, input_data, tool_use_id, context):
        """PreToolUse: every tool call, before it runs, whatever the mode.

        Hands the call to the worker thread and waits for a yes or a no. The
        wait is done on a thread rather than inline so the session loop stays
        free to keep streaming - a blocked loop here would also block the text
        the model is writing around the call."""
        name = input_data.get("tool_name") or ""
        args = input_data.get("tool_input") or {}
        req = self._ask("gate", name=_plain(name), args=args,
                        tool_use_id=input_data.get("tool_use_id") or tool_use_id)
        if req is None:
            return {}
        await asyncio.to_thread(req.done.wait)
        allowed, reason, updated = req.answer or (False, "Uniagent denied this call.", None)
        out = {"hookEventName": "PreToolUse",
               "permissionDecision": "allow" if allowed else "deny",
               "permissionDecisionReason": reason or ""}
        # An approval that edited the arguments rewrites the call rather than
        # rejecting it - the same "allow, but not quite like that" the Claude
        # Code dialog offers, routed through Uniagent's UI instead.
        if allowed and updated is not None:
            out["updatedInput"] = updated
        return {"hookSpecificOutput": out}

    def _mcp_server(self):
        """Uniagent's own tools, served in-process to Claude Code.

        The schemas are the SAME ones every other provider is sent - built by
        tool_processor from the tools folder - so a tool written mid-conversation
        is offered here on the next session exactly as it would be anywhere else,
        with no second registry to keep in step.

        The handlers do NOT run the tool. They hand it to the worker thread and
        wait, for the threading reason at the top of this file: a Uniagent tool
        expects main.turn_chat() to be its chat and the terminal expects its own
        shell, and neither is true over here."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        def make(entry):
            name = entry["name"]

            async def handler(args):
                req = self._ask("run", name=name, args=args)
                if req is None:
                    return {"content": [{"type": "text",
                                         "text": "ERROR: this turn is no longer running."}]}
                await asyncio.to_thread(req.done.wait)
                return {"content": [{"type": "text", "text": str(req.answer)}]}

            return tool(name, entry.get("description", ""),
                        entry.get("input_schema", {"type": "object"}))(handler)

        return create_sdk_mcp_server(
            MCP_SERVER, tools=[make(e) for e in tool_processor.tools_schema("anthropic")])

    async def _connect(self):
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

        options = ClaudeAgentOptions(
            # Uniagent's assembled context - identity, memories, workspace,
            # pinned skills - goes in ONCE, as the session's system prompt.
            # That is the whole point of a persistent session: it is the part
            # that does not change between turns, so it is also the part worth
            # caching, and each turn afterwards sends only what the user typed.
            system_prompt=self.system,
            # Claude Code's own tools AND Uniagent's, which is what "both" means.
            # On a REMOTE workspace the built-ins are withheld: they would read
            # and write on the machine running Uniagent while every Uniagent
            # file tool is working over ssh on another one, and a model given
            # two toolsets pointed at two different computers will mix them and
            # be entirely convincing about it.
            tools=None if not self.remote else [],
            mcp_servers={MCP_SERVER: self._mcp_server()},
            strict_mcp_config=True,
            # Nothing from ~/.claude. Whatever the human using this machine has
            # configured for their own Claude Code - hooks, permissions, agents,
            # MCP servers - is not Uniagent's and must not silently become part
            # of what a chat here can do.
            setting_sources=[],
            # NOT "allow anything": it means "run no dialog of your own". There
            # is no dialog anyone could answer - the CLI has no terminal here -
            # and _gate below refuses everything that Uniagent has not approved.
            # See the header.
            permission_mode="bypassPermissions",
            hooks={"PreToolUse": [HookMatcher(hooks=[self._gate],
                                              timeout=GATE_TIMEOUT)]},
            model=self.model,
            cwd=self.cwd,
            include_partial_messages=True,
            # Starting a session is slower here than the SDK's 60s default
            # assumes: the CLI has to come up AND hand back the 16-odd tools of
            # an in-process MCP server before it will answer the initialize
            # handshake, and on a loaded machine that has been seen to run out.
            # A timeout there fails the user's turn outright, so it is given
            # room - this is a startup ceiling, not a per-turn wait.
            load_timeout_ms=180_000,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    # -- driving it -----------------------------------------------------------

    async def _set_model(self, model):
        await self._client.set_model(model)

    def retarget(self, model):
        """Point the live session at another model, keeping its conversation.

        This is what makes /model mean something here. Every other provider
        re-reads the model per turn and the next request simply goes elsewhere;
        a session has already been built around one, so the change has to be
        pushed INTO it - and set_model does that without dropping the context
        the session exists to hold."""
        if model and model != self.model:
            self._call(self._set_model(model), timeout=30)
            self.model = model

    async def _pump(self, text, q):
        """One turn: send `text`, then translate everything the CLI says back
        into events on `q` until the turn's result lands."""
        try:
            await self._client.query(text)
            async for message in self._client.receive_response():
                if self._events is not q:
                    break                      # turn abandoned - stop translating
                self._translate(message)
        except Exception as e:
            q.put(_Request("error", error=_readable(e)))
        else:
            q.put(_Request("end"))

    def _translate(self, message):
        """One SDK message -> the events run_turn() understands."""
        from claude_agent_sdk import (AssistantMessage, RateLimitEvent, ResultMessage,
                                      StreamEvent, ToolResultBlock,
                                      ToolUseBlock, UserMessage)

        if isinstance(message, StreamEvent):
            # The reply as it is typed. Same event shapes provider.py's
            # _read_anthropic parses, because underneath it is the same API.
            event = message.event
            etype = event.get("type")
            if etype == "content_block_start":
                # A tool call starting. Its arguments arrive as JSON fragments
                # below and are COLLECTED rather than forwarded - see the stop
                # branch for why the call is only shown once it is whole.
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    self._in_call = _plain(block.get("name") or "")
                    self._args = ""
            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                text = delta.get("text")
                if text:
                    self._emit("text", text=text)
                elif delta.get("thinking"):
                    # Extended thinking. Its own event, never folded into
                    # "text": that is the reply, and a page appending thinking
                    # to the reply bubble is exactly the confusion this whole
                    # channel exists to avoid.
                    self._emit("thinking", text=delta["thinking"])
                elif self._in_call is not None and delta.get("partial_json"):
                    self._args += delta["partial_json"]
            elif etype == "content_block_stop":
                # The call, complete, in one piece. It is deliberately NOT
                # streamed in fragments: the page re-renders the whole bubble as
                # markdown on every chunk (setBody in index.html), so a call
                # arriving in pieces is repeatedly parsed HALF-WRITTEN - and
                # half a JSON object is not the same markdown as a whole one.
                # That is what made the closing bracket jump to a line of its
                # own and then settle back once the next piece landed. Sent
                # whole, there is no intermediate state to mis-render.
                if self._in_call is not None:
                    self._emit("calltext", text=self._in_call + "(" + self._args + ")")
                    self._in_call, self._args = None, ""
            elif etype == "message_start":
                u = event.get("message", {}).get("usage", {})
                self._emit("usage", usage=provider._anthropic_usage(u))
            elif etype == "message_delta":
                out = event.get("usage", {}).get("output_tokens")
                if out is not None:
                    self._emit("usage", usage={"output_tokens": out})

        elif isinstance(message, AssistantMessage):
            if message.error:
                self._emit("error", error=provider.CLAUDE_ERRORS.get(
                    message.error, "the Claude Code CLI failed: " + str(message.error)))
                return
            # The authoritative form of anything the model just did. ONLY
            # calls are taken from here: include_partial_messages is always on,
            # so every word of the prose already arrived as a delta above, and
            # reading the TextBlock too would write the whole reply twice.
            # These calls are recorded, not gated - the gate ran before this.
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self._emit("call", tool_use_id=block.id, name=_plain(block.name),
                               args=block.input or {})

        elif isinstance(message, UserMessage):
            # A tool's own output coming back into the conversation. For an
            # Uniagent tool this is text the worker thread produced a moment
            # ago; for a built-in it is the only place its result appears at
            # all, which is why the transcript is written from here for both.
            content = message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        self._emit("result", tool_use_id=block.tool_use_id,
                                   text=_as_text(block.content),
                                   is_error=bool(block.is_error))

        elif isinstance(message, RateLimitEvent):
            # Emitted on every CHANGE of rate-limit state, including the change
            # into "allowed" - so reporting each one as trouble puts "your
            # usage limit is spent" on top of a turn that then works perfectly.
            # Only the two states that mean something to a person are passed on.
            info = message.rate_limit_info
            status = getattr(info, "status", "") or ""
            if status == "rejected":
                self._emit("note", text=provider.CLAUDE_ERRORS["rate_limit"])
            elif status == "allowed_warning":
                self._emit("note", text="the Claude subscription's usage window is "
                                        "nearly spent" + _resets(info))

        elif isinstance(message, ResultMessage):
            if message.session_id:
                self.session_id = message.session_id
            if message.is_error:
                self._emit("error", error="the Claude Code CLI failed: " + "; ".join(
                    message.errors or [str(message.subtype)]))

    def start(self, text):
        """Begin a turn and return the queue its events will arrive on."""
        q = queue.Queue()
        self._events = q
        asyncio.run_coroutine_threadsafe(self._pump(text, q), self._loop)
        return q

    def finish(self):
        self._events = None

    def interrupt(self):
        try:
            self._call(self._client.interrupt(), timeout=15)
        except Exception:
            pass          # a session already dying is not a second failure

    def close(self):
        self._events = None
        try:
            self._call(self._client.disconnect(), timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def _as_text(content):
    """A tool result's content as plain text, whatever shape it arrived in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or "")
            else:
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts)
    return "" if content is None else str(content)


def _resets(info):
    """" - refills at HH:MM" for a rate-limit warning that knows when, else "".
    The time is the only part of the warning anyone can act on."""
    when = getattr(info, "resets_at", None)
    if not when:
        return ""
    try:
        return " - refills at " + time.strftime("%H:%M", time.localtime(float(when)))
    except (TypeError, ValueError, OSError):
        return ""


def _readable(e):
    """An SDK failure as something actionable - provider.py already knows how
    to say these, so this only unwraps to it."""
    err = provider._claude_error(e)
    return str(err)


def _gist(description):
    """A tool's description cut to its first sentence, capped.

    Length is only half of why. The other half is that some descriptions are
    LIVE: the mcp tool's lists every server it can currently reach and what
    state each is in, so it genuinely reads differently from one minute to the
    next as servers connect. Pasted whole into a session's system prompt, that
    made the prompt change on its own, which made get() decide the session was
    stale, which quietly rebuilt a warm session between two ordinary turns -
    the exact opposite of what this provider exists to do. One sentence is
    almost always the stable half."""
    text = " ".join((description or "").split())
    stop = text.find(". ")
    if stop != -1:
        text = text[:stop + 1]
    return text[:200].rstrip()


def tools_note():
    """Uniagent's own tools, named and described for the system prompt.

    Needed because of two things that are each reasonable alone and unhelpful
    together. tool_processor._tools_text() leaves every schema-carrying tool OUT
    of the prompt on purpose - on a normal provider they travel in the request's
    own `tools` array, so describing them as prose as well would send each one
    twice. And Claude Code DEFERS mcp tools: they are not in the model's tool
    list at the start of a turn, they are behind a ToolSearch it has to think to
    run.

    Put together, Uniagent's tools were invisible. Nothing in the prompt
    mentioned them and nothing in the tool list showed them, so the model
    reached for Bash every time and, asked directly for an Uniagent tool, had to
    go looking for a name it had never been told. This section is the fix: the
    names exist in the prompt, so they can be asked for and found.

    Descriptions are cut to one sentence - enough to choose by, without pasting
    every tool's full instructions into a prompt that already carries Uniagent's
    context. See _gist(), which also keeps this section from CHANGING under a
    live session."""
    lines = []
    for entry in tool_processor.tools_schema("anthropic"):
        lines.append("  " + PREFIX + entry["name"] + " - " + _gist(entry.get("description")))
    if not lines:
        return ""
    return ("\n\nUNIAGENT'S OWN TOOLS\n"
            "You have your own built-in tools (Bash, Read, Edit, Grep and the rest) "
            "AND Uniagent's tools. Uniagent's are listed here by their real names; "
            "they are available to you now, though they may not appear in your tool "
            "list until you look them up.\n\n" + "\n".join(lines) + "\n\n"
            "Which to use:\n"
            "- If the user NAMES a tool, use that exact tool. Do not substitute a "
            "built-in that does something similar.\n"
            "- Anything only Uniagent can do - email, cron, GUI input, its memories, "
            "its subagents, its mcp servers - has no built-in equivalent, so use "
            "Uniagent's tool.\n"
            "- For ordinary reading, editing and searching of files in the working "
            "directory, your own built-ins are usually the better choice.\n"
            "- Every call, yours or Uniagent's, is put to the user for approval "
            "before it runs.")


# --- The registry -------------------------------------------------------------

_sessions = {}
_lock = threading.Lock()


def get(chat_id, model, system, key, cwd, remote):
    """This chat's live session, building one if it hasn't got a usable one.

    A session is REUSED whenever it can be, because reuse is the feature. It is
    rebuilt only when something baked into it at connect time has changed:

      the context         Uniagent's own system text - identity, memories,
                          pinned skills - plus WHICH tools exist. It went in
                          once and there is no way to revise it in place, so a
                          chat whose memory was just written to has to start a
                          session that knows it.

                          Compared as `key`, which is that text plus the tool
                          NAMES and deliberately not their descriptions: a
                          description can rewrite itself while nothing has
                          actually changed (see _gist), and a session dropped
                          for that is a warm session thrown away for nothing.
      the directory       a workspace move is a different machine or a
                          different root; carrying on in the old one is the
                          "everything says it worked" failure this project
                          calls out by name.

    A model change is NOT in that list: retarget() pushes it into the live
    session instead, which is the whole point of doing it that way.

    Returns (session, fresh). `fresh` matters: a session that was just built
    knows nothing about a conversation that may already be several turns old,
    and run_turn has to hand it that history before the new message or the
    model will answer a chat it cannot see - see _replay()."""
    with _lock:
        live = _sessions.get(chat_id)
        if live is not None and (live.key != key or live.cwd != cwd
                                 or live.remote != remote):
            live.close()
            live = None
            _sessions.pop(chat_id, None)
        if live is None:
            live = Session(chat_id, model, system, key, cwd, remote)
            _sessions[chat_id] = live
            return live, True
        live.retarget(model)
        return live, False


def close(chat_id):
    """Drop this chat's session, if it has one. /clear and a deleted chat both
    want this - the transcript went away, so the context behind it has to."""
    with _lock:
        live = _sessions.pop(chat_id, None)
    if live is not None:
        live.close()
        return True
    return False


def close_all():
    with _lock:
        live = list(_sessions.values())
        _sessions.clear()
    for s in live:
        s.close()


def active():
    """Which chats are holding a session open, for the settings page."""
    with _lock:
        return {c: {"model": s.model, "since": s.started, "session_id": s.session_id}
                for c, s in _sessions.items()}


def _replay(turns):
    """A conversation Claude Code has never seen, as text to open a session with.

    Needed because the context lives on the CLI's side: a session dropped for
    any reason - the server restarted, the memory file changed, /compact
    rewrote the transcript - comes back empty while Uniagent's window still
    shows the whole conversation. Sending only the new message then produces an
    answer to a chat the model cannot see, which reads as the model having
    forgotten rather than as anything being wrong. Replaying costs a cold cache
    on that one turn and is the only honest option.

    The shape mirrors provider._flatten - the same "User:"/"Uniagent:" naming
    the transcript itself uses - with tool calls and their results kept, since
    on this provider they are most of what happened."""
    lines = []
    for t in turns:
        role, content = t.get("role"), (t.get("content") or "").strip()
        if role == "user":
            lines.append("User: " + content)
        elif role == "tool":
            lines.append("Tool result: " + content)
        elif role == "assistant":
            if content:
                lines.append("Uniagent: " + content)
            for call in t.get("tool_calls") or []:
                fn = call.get("function", {})
                lines.append("Uniagent called: " + str(fn.get("name"))
                             + "(" + str(fn.get("arguments")) + ")")
    if not lines:
        return ""
    return ("[This conversation is already in progress. You are resuming it - "
            "everything below has already happened and the user can see all of "
            "it. Do not greet them again or redo any of it; simply carry on.]\n\n"
            + "\n".join(lines) + "\n\n[End of the conversation so far. The "
            "user's next message follows.]\n\n")


# --- The worker side ----------------------------------------------------------

def run_turn(turns, sync, text, chat_id, provider_name, model, system,
             workspace_id, chosen, approve, on_text=None, on_tool_call=None,
             on_tool_result=None, on_safety=None, on_message=None,
             should_stop=None, usage=None, safety=None, safety_prompt=None,
             safety_threshold=None, safety_extra=None,
             on_reasoning=None, on_timing=None, on_request=None, on_thought=None):
    """One turn against this chat's Claude Code session, mirrored into `turns`.

    Called by main.run() INSTEAD of its own tool loop, and it deliberately
    reproduces that loop's every visible step in the same order - the assistant
    turn carrying its tool_calls, on_tool_call, on_message, the safety check,
    the approval, the tool turn carrying the result, sync() after each - so the
    web UI, the transcript on disk and a chat's exported history are the same
    shape they are on every other provider. What differs is only who decided to
    make the call.

    `turns` is mutated in place and `sync` is called as it grows, exactly as in
    run(); nothing is returned."""
    import main

    ws = workspace.get(workspace_id)
    remote = bool(getattr(ws, "is_remote", False))
    # A remote workspace runs its tools over ssh, so the CLI's own cwd is only
    # ever the local install folder - it must not be pointed at a path that
    # means something on the other machine.
    cwd = None if remote else str(getattr(ws, "root", "") or "") or None

    # What the session is TOLD, and what decides whether it is still the right
    # session, are two different strings - see get().
    names = [e["name"] for e in tool_processor.tools_schema("anthropic")]
    session, fresh = get(chat_id, model, system + tools_note(),
                         system + "\n" + "\n".join(sorted(names)), cwd, remote)
    # turns already carries this turn's own user message (run() appends it
    # before calling here), so the history to replay is everything before it.
    opening = _replay(turns[:-1]) if fresh else ""
    # The request going out - the start of the wait a UI counts while it is
    # happening. One per TURN here rather than one per message: Claude Code
    # streams a whole turn down a single connection, so there is exactly one
    # moment where nothing has come back yet.
    if on_request:
        on_request()
    events = session.start(opening + text)

    pending = ""          # prose written since the last call, main.run's `before`
    thought = ""          # this message's thinking, kept the way run() keeps it
    # The clock on the message being written right now. Claude Code streams a
    # whole turn - prose, a call, more prose - down one connection, so the
    # phases here are per MESSAGE rather than per request: a new one starts the
    # moment the last was sealed, which is what makes these numbers mean the
    # same thing they mean on every other provider.
    clock = timing.Phases()
    counted = []          # this message's thinking, counted when it ended
    calls = {}            # tool_use_id -> the tool's name, for labelling
    # Each call's safety verdict, held until its own result comes back rather
    # than announced when the gate decided it. The page holds a verdict aside
    # and hangs it on the next result box it draws, so the two have to arrive
    # together to stay paired - and they do NOT arrive together on their own:
    # a model can make three calls at once, and they finish in whatever order
    # they finish in (measured: two, one, three). Announced at gate time, the
    # first box would wear the first call's verdict whatever call it was
    # actually showing. Held here, every box gets its own.
    verdicts = {}         # tool_use_id -> (safe, reason, checked)
    started = {}          # tool_use_id -> when the call went out, for its duration
    stopped = False
    shown_last = ""       # the last thing put on screen, for spacing
    after_call = False    # ...and whether that thing was a tool call

    def show(piece, is_call=False):
        """Write to the live view, keeping a blank line around tool calls.

        The whole reason this exists: a call and the prose around it go into the
        SAME bubble as plain text (the page has no separate drawing for a call -
        see the calltext branch below), so without a gap three calls in a row
        run together into one unreadable line. Kept in one place so anything
        else that needs to appear between calls later gets the spacing free."""
        nonlocal shown_last, after_call
        if (is_call or after_call) and shown_last and not shown_last.endswith("\n"):
            piece = "\n\n" + piece
        if on_text:
            on_text(piece)
        shown_last, after_call = piece, is_call

    def flush_into(turn):
        """Seal the message being written into `turn`, with what it cost.

        The two extra keys are the same ones main.run() puts on its own turns
        and are read the same way everywhere downstream - the point of this
        function mirroring run()'s loop step for step is that a transcript
        gives no sign of which of the two wrote it."""
        nonlocal pending, thought, clock
        turn["content"] = pending
        spent = clock.as_dict(*main._split_output(provider_name, model,
                                                  thought, pending, usage or {},
                                                  counted[0] if counted else None))
        if spent:
            turn["timing"] = spent
            if on_timing:
                on_timing(spent)
        if thought:
            turn["reasoning_content"] = thought
        pending, thought, clock = "", "", timing.Phases()
        del counted[:]
        return turn

    try:
        while True:
            if should_stop and should_stop() and not stopped:
                # Stop the model where it stands rather than reading out the
                # rest of a reply nobody wants. The transcript is closed off
                # below, once the CLI has actually come to a halt.
                stopped = True
                session.interrupt()
            try:
                req = events.get(timeout=IDLE_TIMEOUT)
            except queue.Empty:
                raise RuntimeError("the Claude Code session sent nothing for "
                                   + str(IDLE_TIMEOUT) + "s - giving up")

            kind, data = req.kind, req.data

            if kind == "text":
                # The first word of the reply ends the thinking, and the
                # thinking's own numbers are wanted right here - that is the
                # moment the label stops saying "thinking". Counted once and
                # reused by flush_into below, so nothing is revised later.
                if thought and clock.think_from is not None and not counted:
                    counted.append(main.tokens.estimate(provider_name, model, thought))
                    if on_thought:
                        on_thought({"think": timing.ms(clock.think_from, clock.think_to),
                                    "think_tok": counted[0]})
                clock.writing()
                pending += data["text"]
                show(data["text"])

            elif kind == "thinking":
                clock.thinking()
                thought += data["text"]
                if on_reasoning:
                    on_reasoning(data["text"])

            elif kind == "calltext":
                # The call, shown the way main._stream's show_call shows one on
                # every other provider - because the page has no drawing for a
                # call at all: its "toolcall" event only SEALS the bubble
                # (index.html), and the call is only ever seen because it was
                # written in as text.
                #
                # Deliberately NOT added to `pending`: that is the model's
                # prose, and the call belongs in tool_calls, not in the turn's
                # content - the same split run() keeps.
                clock.writing()
                show(data["text"], is_call=True)

            elif kind == "usage":
                if usage is not None:
                    usage.update(data["usage"])

            elif kind == "note":
                if on_text:
                    on_text("\n[" + data["text"] + "]\n")

            elif kind == "gate":
                # Every tool call, built-in or Uniagent's, decided here - and
                # decided on THIS thread, where the chat's own context is.
                name, args = data["name"], data["args"]
                call = {"tool": name, "args": args}
                shown = name + "(" + json.dumps(args) + ")"
                threshold = tool_validation.threshold_for(safety_threshold, safety, chosen)
                outcome, reason = tool_validation.check(
                    call, threshold, prompt=safety_prompt, extra=safety_extra)
                if outcome == tool_validation.SKIP:
                    verdicts[data["tool_use_id"]] = (
                        True, main._log_validation(shown, True, None, checked=False), False)
                    req.reply((True, "", None))
                    continue
                safe = outcome == tool_validation.RUN
                main._log_validation(shown, safe, reason)
                verdicts[data["tool_use_id"]] = (safe, reason, True)
                if safe:
                    req.reply((True, "", None))
                    continue
                if approve("[safety] " + reason.rstrip(" .") + " - run it anyway?"):
                    req.reply((True, "approved in Uniagent", None))
                else:
                    # The same words run() denies with, for the same reason:
                    # a denial has to read as "stop", or the model treats it as
                    # an obstacle and goes looking for another way round.
                    req.reply((False,
                               "DENIED - the user did not approve this call. Stop "
                               "working on this task: reply with a brief "
                               "acknowledgement of the denial, then wait for the "
                               "user's next instruction. Do not retry the call, "
                               "work around it another way, or carry on with the "
                               "task unasked.", None))

            elif kind == "run":
                # An Uniagent tool, executed here rather than on the session
                # thread so it gets the chat and the workspace it expects.
                result = tool_processor.process(
                    {"tool": data["name"], "args": data["args"]},
                    chat_id, workspace_id=main.live_workspace(chat_id, workspace_id))
                req.reply(result)

            elif kind == "call":
                name, args = data["name"], data["args"]
                shown = name + "(" + json.dumps(args) + ")"
                calls[data["tool_use_id"]] = name
                started[data["tool_use_id"]] = timing.now()
                said = pending.strip()
                turns.append(flush_into({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": data["tool_use_id"],
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }],
                    "raw_call": shown,
                }))
                sync()
                if on_tool_call:
                    on_tool_call(said + shown if said else shown)
                if on_message and said:
                    on_message(said, "call")

            elif kind == "result":
                began = started.pop(data["tool_use_id"], None)
                took = {"ms": timing.ms(began)} if began is not None else None
                turn = {"role": "tool",
                        "tool_call_id": data["tool_use_id"],
                        "content": data["text"]}
                if took:
                    turn["timing"] = took
                turns.append(turn)
                sync()
                verdict = verdicts.pop(data["tool_use_id"], None)
                if verdict and on_safety:
                    safe, reason, checked = verdict
                    on_safety(safe, reason, checked=checked)
                if on_tool_result:
                    on_tool_result(data["text"], calls.get(data["tool_use_id"]), took)

            elif kind == "error":
                raise RuntimeError(data["error"])

            elif kind == "end":
                break
    finally:
        session.finish()

    if stopped:
        pending = (pending + "\n" + main.STOPPED).strip()
        turns.append(flush_into({"role": "assistant"}))
    elif pending.strip() or thought:
        turns.append(flush_into({"role": "assistant"}))
    sync()
