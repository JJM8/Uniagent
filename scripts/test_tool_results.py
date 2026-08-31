"""Does a tool result stay inside its budget - whatever the tool hands back?

Run it directly:  python3 scripts/test_tool_results.py

This is the regression guard for one specific accident: a single tool result
big enough to swallow an entire conversation. One `get_gameobject` against a
Unity scene returned 2,037,177 characters, which went into a chat's history
verbatim and turned a 20k-token conversation into a 435k-token one - to be
re-sent, in full, on every turn after it. Nothing failed at the time; the
number just quietly went up by twenty times.

So the assertion these tests all make is the same one: whatever comes back,
tool_results.clamp() returns something no larger than BUDGET, and it says so
in a way the model can act on. The interesting cases are the ones where the
clipping has to be clever rather than just short - JSON has to still parse
after it, or the model is left holding a fragment it cannot read at all.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import tool_results


def _sandbox():
    """Point the spill at a temp folder, so a test never writes into a real
    chat's results/ - these run against the live chats directory otherwise."""
    tmp = Path(tempfile.mkdtemp(prefix="tool-results-test-"))
    tool_results._chat_dir = lambda cid: tmp / cid
    return tmp


def _body(out):
    return out.split("\n\n[tool result clipped")[0]


def test_small_is_untouched():
    """The common case by far, and the one that must cost nothing: a result
    under the budget comes back as itself, byte for byte, with no note, no
    file written and no parsing attempted."""
    for text in ("", "ok", '{"a": 1}', "x" * (tool_results.BUDGET - 1),
                 "x" * tool_results.BUDGET):
        assert tool_results.clamp(text, "shell", "chat-x") is text
    print("ok  small results pass through untouched")


def test_non_string_passes_through():
    """A tool that returns something other than text has a caller that knows
    what to do with it. This is a text budget and has no opinion."""
    for value in (None, 42, {"a": 1}, ["b"]):
        assert tool_results.clamp(value, "t", "chat-x") is value
    print("ok  non-string results pass through untouched")


def test_json_stays_json():
    """The one that matters most. A byte-chopped 2MB of JSON is a fragment
    the model cannot parse, cannot query and cannot reason about - so the
    trim has to be structural, and what comes back has to still load()."""
    tmp = _sandbox()
    data = {"items": [{"id": i, "tags": list(range(60)),
                       "meta": {"a": {"b": {"c": {"d": {"e": i}}}}}}
                      for i in range(4000)]}
    out = tool_results.clamp(json.dumps(data, indent=2), "mcp", "chat-x")
    assert len(out) <= tool_results.BUDGET, len(out)
    shape = json.loads(_body(out))          # parses, or this raises
    assert shape["items"], "the array was pruned out of existence"
    assert "omitted" in json.dumps(shape), "nothing said anything was cut"
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  huge JSON is trimmed structurally and still parses")


def test_uses_the_budget_it_has():
    """Clipping small is its own failure. A trim that answers 2k when 19k was
    allowed has thrown away nine tenths of what the model was entitled to
    see, which is how the first version of the ladder behaved."""
    tmp = _sandbox()
    data = {"components": [{"type": "C%d" % i, "properties": {"p%d" % k: k
                                                              for k in range(40)}}
                           for i in range(500)]}
    out = tool_results.clamp(json.dumps(data, indent=2), "mcp", "chat-x")
    assert len(out) <= tool_results.BUDGET, len(out)
    assert len(out) > tool_results.BUDGET * 0.5, \
        "used only %d of %d characters" % (len(out), tool_results.BUDGET)
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  trimming fills the budget rather than undershooting it")


def test_plain_text_keeps_both_ends():
    """Text that isn't JSON gets the byte squeeze - and it keeps the TAIL as
    well as the head, because the end of a long output is where the error,
    the total and the "done" live. A plain [:budget] throws exactly that
    away."""
    tmp = _sandbox()
    text = "HEAD-MARKER\n" + ("filler line\n" * 200000) + "TAIL-MARKER"
    out = tool_results.clamp(text, "shell", "chat-x")
    assert len(out) <= tool_results.BUDGET, len(out)
    assert "HEAD-MARKER" in out and "TAIL-MARKER" in out
    assert "omitted from the middle" in out
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  long plain text keeps its head and its tail")


def test_squeeze_cuts_on_line_boundaries():
    """The commonest thing to reach the byte squeeze is a file read, and
    read_file numbers its lines precisely so edit_file has something exact to
    quote back. A cut through the middle of a line hands the model a
    quotation that cannot match anything, so both edges snap to a newline."""
    tmp = _sandbox()
    src = "".join("%5d| line number %d of the file\n" % (i, i)
                  for i in range(1, 3000))
    out = tool_results.clamp(src, "read_file", "chat-x")
    assert len(out) <= tool_results.BUDGET
    body = _body(out)
    head, tail = body.split("... [")[0], body.split("] ...\n\n")[1]
    for line in (head.strip("\n").split("\n")[-1], tail.split("\n")[0]):
        assert line.endswith("of the file"), "cut mid-line: " + repr(line)
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  the byte squeeze cuts on line boundaries, not mid-line")


def test_one_giant_string_inside_json():
    """A small structure carrying one enormous value - a base64 blob, an
    embedded log. The structure around it is worth keeping and would be lost
    if the only lever were depth."""
    tmp = _sandbox()
    out = tool_results.clamp(
        json.dumps({"file": "a.png", "ok": True, "data": "A" * 900000}),
        "read", "chat-x")
    assert len(out) <= tool_results.BUDGET, len(out)
    shape = json.loads(_body(out))
    assert shape["file"] == "a.png" and shape["ok"] is True
    assert "chars]" in shape["data"], "the giant string wasn't marked as cut"
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  one giant string is clipped without losing the structure")


def test_full_copy_is_saved_and_named():
    """The clipped result is only half the deal - the other half is that the
    whole thing is still readable, and that the model is told exactly where.
    A note without a usable path just tells it something is missing."""
    tmp = _sandbox()
    text = json.dumps({"rows": [{"i": i} for i in range(200000)]})
    out = tool_results.clamp(text, "mcp", "chat-7")
    saved = list((tmp / "chat-7" / tool_results.RESULTS_DIR).iterdir())
    assert len(saved) == 1, saved
    assert saved[0].read_text() == text, "the saved copy is not the full result"
    assert str(saved[0]) in out, "the model was not told the path"
    # Same result again: one file, not two - it is named by its own content.
    tool_results.clamp(text, "mcp", "chat-7")
    assert len(list((tmp / "chat-7" / tool_results.RESULTS_DIR).iterdir())) == 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  the full result is saved, named by content, and its path given")


def test_no_chat_still_clamps():
    """A call with no chat behind it - a cron route being set up, a tool run
    from a test - has nowhere to save. It still must not return 2MB, and it
    must not claim a path that doesn't exist."""
    _sandbox()
    out = tool_results.clamp("z" * 900000, "t", None)
    assert len(out) <= tool_results.BUDGET, len(out)
    assert "was not saved" in out
    print("ok  a result with no chat is still clipped, and says nothing was saved")


def test_deterministic():
    """Same result in, same text out. A trim that wobbled - a timestamp, a
    dict order, a random name - would invalidate the provider's prompt cache
    from that turn onwards every time the history was replayed."""
    tmp = _sandbox()
    text = json.dumps({"a": [{"b": list(range(50))} for _ in range(9000)]})
    first = tool_results.clamp(text, "mcp", "chat-x")
    second = tool_results.clamp(text, "mcp", "chat-x")
    assert first == second
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  clipping the same result twice gives the same text")


def test_the_real_one():
    """The 2MB Unity dump this whole module was written for, if that chat is
    still on disk. Skipped rather than failed when it isn't - it is one real
    payload, not a fixture anyone is obliged to keep."""
    real = Path(__file__).parent.parent / "chats" / "chat-0084c45f" / "history.json"
    if not real.exists():
        print("--  skipped: the 2MB Unity result is no longer on disk")
        return
    tmp = _sandbox()
    turns = json.loads(real.read_text())
    big = max((str(t.get("content", "")) for t in turns), key=len)
    out = tool_results.clamp(big, "mcp", "chat-x")
    assert len(big) > 2000000 and len(out) <= tool_results.BUDGET
    assert json.loads(_body(out))["gameObject"]["components"], "lost the components"
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok  the real 2,037,177-character result comes back under budget")


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
    print("\nall good")
