#!/usr/bin/env python3
"""What a turn does when the model, or the wire, misbehaves.

Four failures that all used to end the same way - silently, with something
wrong on screen and nothing anywhere saying so:

  * thinking written inline in the reply, shown to the user as prose
  * a reply the connection cut off mid-word, shown as though it were finished
  * a tool call the provider mangled, shown as tag wreckage and never run
  * a model repeating one paragraph until its context ran out

Run it directly: python3 scripts/test_recovery.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main
import provider

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print("ok   " + name)
    else:
        FAIL.append(name)
        print("FAIL " + name)
        print("       got:  " + repr(got))
        print("       want: " + repr(want))


def split(pieces):
    """Feed `pieces` through the inline-thinking splitter."""
    thought = []
    s = provider._ThinkSplit(on_think=thought.append)
    said = "".join(s.feed(p) for p in pieces) + s.close()
    return said, "".join(thought)


print("--- thinking written inline in the reply ---")
check("plain reply is untouched",
      split(["Nothing to see here."]), ("Nothing to see here.", ""))
check("<think> block is taken out of the reply",
      split(["<think>weighing it up</think>The answer is 42."]),
      ("The answer is 42.", "weighing it up"))
check("a tag split across pieces still matches",
      split(["Hi <th", "ink>quiet", "ly</thi", "nk> done"]),
      ("Hi  done", "quietly"))
check("DeepSeek's DSML spelling",
      split(["<｜DSML｜think｜>hmm<｜/DSML｜think｜>out"]),
      ("out", "hmm"))
check("thinking that never closes is all thinking",
      split(["<think>and on and on"]), ("", "and on and on"))
check("a stray closing tag does not swallow the reply",
      split(["real answer </think> still the answer"]),
      ("real answer  still the answer", ""))
check("angle brackets in prose are left alone",
      split(["if a < b and b > c then"]), ("if a < b and b > c then", ""))
check("a reply ending inside a half-written tag keeps its text",
      split(["the end <thi"]), ("the end <thi", ""))

print()
print("--- a tool call the provider mangled ---")
# What OpenRouter's DeepSeek-v4 builds actually emit when their DSML parser
# eats the opening of a call and keeps the closing: no structured call at all,
# and a reply made of nothing but wreckage.
WRECK = ('","command":""}</｜DSML｜parameter>\n'
         '</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
check("wreckage is recognised", provider.looks_like_stray_markup(WRECK), True)
check("a reply quoting a tag is NOT wreckage",
      provider.looks_like_stray_markup(
          "You close it with </invoke> once the arguments are written, and "
          "the parser takes it from there."), False)
check("ordinary prose is NOT wreckage",
      provider.looks_like_stray_markup("All done - the board builds cleanly."), False)

call, before, shown, retry = main._parse_call(WRECK, {})
check("wreckage is never shown as the answer", before, "")
check("wreckage asks for the call again", bool(retry and "did not come through" in retry), True)
check("wreckage produces no call", call, None)

# A call that DID come through, with a leaked closing tag beside it.
call, before, shown, retry = main._parse_call(
    WRECK, {"name": "terminal", "arguments": '{"command":"ls"}'})
check("a good call still parses", call, {"tool": "terminal", "args": {"command": "ls"}})
check("its leaked wreckage is not shown", before, "")

# Prose that has nothing to do with tools rides through untouched.
call, before, shown, retry = main._parse_call(
    "Here is what I found.", {"name": "terminal", "arguments": '{"command":"ls"}'})
check("real prose beside a real call is kept", before, "Here is what I found.")

print()
print("--- a model repeating itself inside one response ---")
LOOP = ("Let me use the firefox tool to open the local PNG file. It saves the "
        "page as HTML. Let me try that and read the HTML - maybe the image is "
        "embedded.\n\n")
check("a paragraph repeated three times is a loop",
      bool(main._looping(LOOP * 3)), True)
check("said once, it is not", main._looping(LOOP), None)
check("said twice, it is not yet", main._looping(LOOP * 2), None)
check("ordinary prose is not a loop",
      main._looping("The board has four layers, two of them ground planes, and "
                    "the stackup is symmetric about the middle. Impedance is "
                    "controlled on the outer pair only."), None)
check("a short repeated line is not a loop",
      main._looping("| 1 | GND |\n" * 40), None)
check("an empty reply is not a loop", main._looping(""), None)

print()
print("--- a reply the wire cut off ---")


class DeadSocket:
    """An SSE response that dies part-way through, the way a real one does."""

    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def iter_lines(self, delimiter=None):
        import requests
        yield (b'data: {"choices":[{"delta":{"content":"the first half of the "}}]}')
        yield b""
        raise requests.exceptions.ChunkedEncodingError("connection broken")

    def close(self):
        pass


usage = {}
text = "".join(provider._read_openai(DeadSocket(), usage=usage))
check("the text that did arrive is kept", text, "the first half of the ")
check("and the turn is told it was cut", bool(usage.get("truncated")), True)


class LengthCapped:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def iter_lines(self, delimiter=None):
        yield b'data: {"choices":[{"delta":{"content":"as much as it could fit"}}]}'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'
        yield b"data: [DONE]"

    def close(self):
        pass


usage = {}
text = "".join(provider._read_openai(LengthCapped(), usage=usage))
check("a reply stopped at the token limit is kept", text, "as much as it could fit")
check("and is also reported as cut", usage.get("truncated"), "length")


class InlineThinker:
    """A local server that hands the raw generation through, tags and all."""

    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def iter_lines(self, delimiter=None):
        yield b'data: {"choices":[{"delta":{"content":"<think>I should check"}}]}'
        yield b'data: {"choices":[{"delta":{"content":" the file first.</think>Reading it now."}}]}'
        yield b"data: [DONE]"

    def close(self):
        pass


reasoning = {}
text = "".join(provider._read_openai(InlineThinker(), reasoning=reasoning))
check("inline thinking never reaches the reply", text, "Reading it now.")
check("it goes where the rest of the thinking goes",
      reasoning.get("content"), "I should check the file first.")

print()
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
