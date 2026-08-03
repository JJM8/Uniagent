"""Does /stop actually stop - immediately, and leaving a coherent chat behind.

Run it directly:  python3 scripts/test_stop.py

This is the regression guard for instant stopping. Stopping used to be a flag
the turn checked in two places, so it was only ever as fast as the slowest
blocking call sitting between those checks - and every blocking call added
afterwards (the safety check, a tool, a new provider) quietly made it slower
again, because nothing failed when it did. That is what this file exists to
catch: not "does stopping work" but "does it still work IMMEDIATELY, from
wherever the turn happens to be".

The turn is stopped at the four places it can realistically be stuck:

  1. waiting on a provider that took the request and then went quiet - the
     common case, and the one the old cooperative check could not see at all,
     since a check between chunks never runs when no chunk ever comes;
  2. part-way through a reply that is streaming normally;
  3. inside the safety check's own model round-trip;
  4. inside a tool that is still running.

Each asserts the same three things: the stop returns almost at once, the chat
is free for the next turn almost at once, and the transcript left behind is
valid JSON that ends the way a stopped turn should - including a tool call
never being left without a result, which abandoning a thread can produce and
the old cooperative stop could not.

A real HTTP server on localhost stands in for the provider, so the streaming
path under test is the actual one - requests, _stream_post, _sse - and not a
stub that politely agrees to be cancelled.
"""

import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import main
import provider
import settings
import tool_processor
import tool_validation
import turnctx

# How long a stop may take before this file calls it broken. Generous on
# purpose: the point is to catch a regression back to "waits out the blocking
# call" - seconds to minutes - not to police milliseconds on a busy machine.
BUDGET = 1.0

# What the stand-in provider does with a request, set per case:
#   "silent"    take it and then send nothing, ever - a model still thinking
#   "trickle"   send a word every few seconds - a reply in mid-flight
#   "toolcall"  answer with a finished tool call and close
mode = "silent"

# Set the moment the stand-in has a request in hand, so a case can stop the turn
# at the point it actually means to - parked on the provider - rather than
# somewhere in the setup a moment earlier.
request_arrived = threading.Event()

TOOL_FRAMES = [
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_test", "type": "function",
         "function": {"name": "pretend", "arguments": ""}}]}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
]


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 with chunked transfer encoding, which is what every real SSE
    # provider uses and is not a detail that can be skipped here: on a plain
    # unchunked body urllib3 reads a fixed block size and holds everything back
    # until it has filled one, so a slow stand-in would look like a slow STOP
    # when it is nothing of the sort. Chunked, each frame is delivered the
    # moment it is written - the behaviour being measured.
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        request_arrived.set()
        try:
            if mode == "trickle":
                for i in range(1000):
                    self._frame({"choices": [{"delta": {"content": "word%d " % i}}]})
                    time.sleep(3)
            elif mode == "toolcall":
                for frame in TOOL_FRAMES:
                    self._frame(frame)
                self._write(b"data: [DONE]\n\n")
                self._write(b"")  # the terminating zero-length chunk
            else:
                time.sleep(600)  # taken, and then nothing
        except Exception:
            pass  # the client hung up, which is precisely what is being tested

    def _frame(self, obj):
        self._write(b"data: " + json.dumps(obj).encode() + b"\n\n")

    def _write(self, payload):
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


def _install_provider(url):
    """Wire the stand-in in the way a real provider is wired, so every layer
    stays under test: _logged wraps the generator, _openai_style makes the
    request through _stream_post, _sse reads it under the turn's watch."""

    def call(model, prompt, temperature=0, usage=None, tools=None, tool_call=None,
             reasoning=None, on_call_delta=None):
        body = {"model": model, "messages": [{"role": "user", "content": "x"}],
                "stream": True}
        return provider._openai_style(url, {}, body, usage=usage, tools=tools,
                                      tool_call=tool_call, reasoning=reasoning,
                                      on_call_delta=on_call_delta)

    provider.providers = lambda: [{"name": "test", "call": call}]
    provider.wire_for = lambda name: "openai"
    provider.context_window = lambda name, model: 128000


class Case:
    def __init__(self, name):
        self.name = name
        self.stop_took = None
        self.free_took = None
        self.problems = []

    @property
    def ok(self):
        return not self.problems


def _wait_for(predicate, limit=15.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _check_transcript(case, path):
    try:
        turns = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        case.problems.append("transcript unreadable: " + str(e))
        return
    if not turns or turns[-1].get("content") != main.STOPPED:
        case.problems.append('transcript does not end with "' + main.STOPPED + '"')
    if not any(t.get("role") == "user" for t in turns):
        case.problems.append("the user's own message is missing")
    answered = {t.get("tool_call_id") for t in turns if t.get("role") == "tool"}
    for t in turns:
        for tc in t.get("tool_calls") or []:
            if tc.get("id") not in answered:
                case.problems.append("a tool call was left with no result behind it")


def _run_case(name, folder, arrived, setup=None, on_text=None):
    """One stop, timed. `arrived` says how to tell the turn has reached the
    place we mean to stop it at; `setup` runs just before it starts."""
    case = Case(name)
    folder.mkdir(parents=True, exist_ok=True)
    request_arrived.clear()
    c = main.chat(folder / "history.json")
    c.pin("test", "test-model")  # written to its settings, so reload_model keeps it
    if setup:
        setup()

    threading.Thread(target=lambda: main.turn(c, "go",
                                              on_text=on_text or (lambda _p: None)),
                     daemon=True).start()

    if not _wait_for(arrived):
        case.problems.append("the turn never reached the point being tested")
        return case

    start = time.monotonic()
    main.request_stop(c.id)
    case.stop_took = time.monotonic() - start

    # The chat being free is what the page's busy bar and the queued-message
    # flush are actually waiting on, and it is what used to take as long as the
    # blocking call did.
    _wait_for(lambda: not c.slot.held(), limit=BUDGET)
    case.free_took = time.monotonic() - start

    if case.stop_took > BUDGET:
        case.problems.append("the stop itself took %.2fs" % case.stop_took)
    if c.slot.held():
        case.problems.append("the chat was still held %.2fs after the stop"
                             % case.free_took)
    _check_transcript(case, c.path)

    # And the chat really is usable again: a second turn must be able to take
    # it, which is the whole point of handing the slot on rather than waiting.
    taken = threading.Event()
    probe = turnctx.TurnContext(c.id + "-probe")
    threading.Thread(target=lambda: (c.slot.acquire(probe), taken.set(),
                                     c.slot.release(probe)), daemon=True).start()
    if not taken.wait(BUDGET):
        case.problems.append("a following turn could not take the chat")
    return case


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the client hanging up IS the stop working - not worth a traceback


def run():
    global mode
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1]
    _install_provider(url)

    real_validate = tool_validation.validate_tool_use
    real_process = tool_processor.process
    workdir = Path(tempfile.mkdtemp(prefix="uniagent-stop-"))
    cases = []

    # 1. The provider has the request and has gone quiet. No chunk ever arrives,
    #    so a check between chunks never runs - the case that made /stop feel
    #    broken, and now the socket being closed underneath the read.
    mode = "silent"
    cases.append(_run_case("model is thinking, nothing sent yet", workdir / "a",
                           arrived=request_arrived.is_set))

    # 2. A reply arriving slowly. There ARE chunks, just far apart - stopping
    #    must not wait for the next one. Watched through on_text, because
    #    nothing is written to the transcript until the response is complete.
    mode = "trickle"
    replying = threading.Event()
    cases.append(_run_case("mid-reply, between chunks", workdir / "b",
                           arrived=replying.is_set,
                           on_text=lambda _p: replying.set()))

    # 3. Inside the safety check - a whole extra model round-trip which, until
    #    now, had no way to be interrupted at all.
    mode = "toolcall"
    at_validation = threading.Event()

    def slow_validate(call, prompt=None):
        at_validation.set()
        turnctx.check()
        time.sleep(600)
        return True, "unreachable"

    cases.append(_run_case("inside the safety check", workdir / "c",
                           arrived=at_validation.is_set,
                           setup=lambda: setattr(tool_validation, "validate_tool_use",
                                                 slow_validate)))

    # 4. Inside a tool that is still running. The tool is left to finish - a
    #    command cannot be un-run - but nothing waits for it and its result is
    #    thrown away, so the chat comes free just as fast.
    at_tool = threading.Event()

    def slow_tool(call, chat_id=None):
        at_tool.set()
        time.sleep(600)
        return "unreachable"

    def setup_tool():
        tool_validation.validate_tool_use = lambda call, prompt=None: (True, "fine")
        tool_processor.process = slow_tool

    cases.append(_run_case("inside a running tool", workdir / "d",
                           arrived=at_tool.is_set, setup=setup_tool))

    tool_validation.validate_tool_use = real_validate
    tool_processor.process = real_process
    srv.shutdown()
    shutil.rmtree(workdir, ignore_errors=True)
    return cases


if __name__ == "__main__":
    failed = 0
    for case in run():
        mark = "ok  " if case.ok else "FAIL"
        took = ("stop %.3fs, chat free %.3fs" % (case.stop_took, case.free_took)
                if case.stop_took is not None else "never got there")
        print("%s %-38s %s" % (mark, case.name, took))
        for problem in case.problems:
            print("       - " + problem)
        failed += 0 if case.ok else 1
    print(("\nall four stopped instantly" if not failed
           else "\n%d of 4 failed" % failed))
    sys.exit(1 if failed else 0)
