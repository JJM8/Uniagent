"""Keep one tool result from eating a whole chat.

A tool result is not like other text in a conversation: nobody chose its
length. The model asks a question, something on the other side answers with
whatever it happens to hold, and the answer is appended to the history
verbatim - where it is then re-sent on every single turn for the rest of that
chat's life. One `get_gameobject` against a Unity scene, four levels deep with
component properties on, returned 2,037,177 characters here: 96% of the whole
conversation, ~435k tokens, in one turn, on a chat that was otherwise 20k.

So every result passes through clamp() on its way back from the tool, and the
model never sees more than BUDGET characters of one. The full text is not
thrown away - it is written into the chat's own results/ folder and the model
is told the path, so a result that really does need reading in full is still
readable, by a tool call that pays for the part it actually wants rather than
by the whole chat paying forever.

Clipping is done STRUCTURALLY where it can be. Byte-chopping 2MB of JSON hands
the model a fragment that no longer parses and cannot be reasoned about at
all; pruning the same JSON - long arrays to their first few entries, deep
nesting to a marker, giant strings to their heads - leaves valid JSON with its
shape intact, which is nearly always what the model was actually looking at.
The byte squeeze below is the fallback for text that isn't JSON, and it keeps
the head AND the tail, because the head says what the thing is and the tail is
where errors and totals live. It is the middle that is filler.

The trim is deterministic - same result in, same text out - which is what
keeps it invisible to the prompt cache. A result that gets clipped differently
each time it is replayed would invalidate the cache from that turn onwards.
"""

import hashlib
import json
from pathlib import Path

# What the model is allowed to see of one result, in characters. ~5k tokens at
# the usual ~4 chars per token, and the ceiling covers the whole return - the
# note naming the spill file is counted against it, not added on top.
BUDGET = 20000

# Where the untrimmed copy goes, inside the chat's own folder.
RESULTS_DIR = "results"

# How hard the structural pruner tries. Each pair is (nesting depth kept,
# array entries kept per array), generous first; the first rung that fits the
# budget wins, so an ordinary result loses almost nothing and only a genuinely
# enormous one is pruned to the bone.
#
# The two numbers step down together rather than one at a time because they
# trade against each other, and a ladder that only ever cut depth would leave
# most of the budget unspent. On the 2MB Unity dump this was written for:
# depth 6 x 12 entries is 40k and overflows, depth 5 x 10 is 22k and still
# overflows, depth 5 x 8 is 17k and fits - where a four-rung ladder that
# skipped from depth 6 to depth 4 handed the model 6.8k and threw away two
# thirds of what it was allowed to see.
LADDER = ((10, 50), (8, 30), (7, 20), (6, 12), (5, 10), (5, 8),
          (4, 10), (4, 6), (3, 6), (3, 3), (2, 2))

# A single string value longer than this is clipped where it sits. One
# base64 blob or one embedded log can blow a budget on its own while the
# structure around it is small and worth keeping.
MAX_STRING = 2000


def clamp(result, tool, chat_id=None):
    """`result` as the model should see it: itself if it is small enough,
    otherwise a trimmed version ending in a line that says what was cut and
    where the whole thing was put.

    Anything that isn't a string is handed straight back - this is a text
    budget, and a tool returning something else has a caller that knows what
    to do with it better than this does."""
    if not isinstance(result, str) or len(result) <= BUDGET:
        return result

    full = len(result)
    path = _spill(result, tool, chat_id)

    # The note is written first because its length comes out of the budget:
    # the point of the number is that the WHOLE return fits under it.
    note = _note(full, path)
    room = max(0, BUDGET - len(note))

    body = _prune(result, room)
    if body is None:
        body = _squeeze(result, room)
    return body + note


def _note(full, path):
    """The line the model reads in place of the missing 2MB.

    It has to answer three questions - how much is gone, how to get it, and
    what to do instead - or a trimmed result is silently treated as a whole
    one, which is the one failure mode worse than the size itself. A model
    that concludes a field was empty because the field was cut will go on to
    be confidently wrong about it."""
    n = ("\n\n[tool result clipped: " + f"{full:,}" + " characters, far too "
         "large to keep in the conversation. What is above is a trimmed copy - "
         "long lists, deep nesting and long strings were cut, and every cut is "
         "marked in place. ")
    if path is None:
        return n + ("The full result was not saved anywhere. Re-run this call "
                    "with narrower arguments if you need more of it.]")
    return n + ("The FULL result is saved on the machine running Uniagent at "
                + path + " - read_file can page through it with offset and "
                "limit. Usually better than reading it: re-run this call with "
                "narrower arguments (less depth, fewer fields, a filter) so the "
                "answer comes back small enough to use directly.]")


# --- The full copy -----------------------------------------------------------

def _spill(text, tool, chat_id):
    """Write the untrimmed result into the chat's results/ folder and return
    its path as a string, or None if there is nowhere to put it.

    Named by a hash of its own content, so the same result arriving twice
    (a retried call, a re-run of the same query) writes one file rather than
    two, and so the name cannot collide with another call's."""
    if not chat_id:
        return None
    try:
        folder = _chat_dir(chat_id) / RESULTS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        stem = _slug(tool) + "-" + hashlib.sha1(
            text.encode("utf-8", "replace")).hexdigest()[:10]
        path = folder / (stem + (".json" if _parse(text) is not None else ".txt"))
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        return str(path)
    except OSError:
        # A result that can't be written to disk is still a result worth
        # trimming and handing back. The note says so rather than pretending
        # to a path that isn't there.
        return None


def _chat_dir(chat_id):
    """The chat's folder, asked of main so the one definition of where a chat
    lives stays in one place. Imported here rather than at the top because
    main imports this module's caller (tool_processor) - by the time any tool
    has run, main is long since loaded and this is a dict lookup."""
    import main
    return main.chat_dir(chat_id)


def _slug(tool):
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (tool or "tool")]
    return "".join(keep)[:40].strip("-") or "tool"


# --- Structural trimming -----------------------------------------------------

def _parse(text):
    """The result as JSON, or None if it isn't JSON. Objects and arrays only:
    a bare string or number that happens to parse is not a structure there is
    anything to prune."""
    head = text.lstrip()[:1]
    if head not in ("{", "["):
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _prune(text, room):
    """JSON trimmed to fit `room` characters with its shape intact, or None if
    the text isn't JSON (in which case the caller squeezes bytes instead).

    Each rung of LADDER is tried in turn and the first one that fits is the
    answer, so nothing is cut that didn't have to be. If even the tightest
    rung overflows - a single enormous flat object, say - the pruned JSON is
    handed back anyway for the byte squeeze to finish, because a pruned blob
    is still a better thing to chop than the raw one."""
    data = _parse(text)
    if data is None:
        return None
    out = None
    for depth, items in LADDER:
        out = json.dumps(_walk(data, depth, items), indent=2)
        if len(out) <= room:
            return out
    return _squeeze(out, room)


def _walk(node, depth, items):
    """`node` with its arrays cut to `items` entries, its nesting cut to
    `depth` levels and its long strings cut to MAX_STRING - every cut leaving
    a marker in place of what went, so the model can see that something was
    there and ask for it rather than concluding the field was empty."""
    if isinstance(node, str):
        if len(node) <= MAX_STRING:
            return node
        return node[:MAX_STRING] + "... [+" + f"{len(node) - MAX_STRING:,}" + " chars]"
    if isinstance(node, list):
        if depth <= 0:
            return ["... " + f"{len(node):,}" + " entries omitted, nested too deep to show"] if node else []
        kept = [_walk(v, depth - 1, items) for v in node[:items]]
        if len(node) > items:
            kept.append("... " + f"{len(node) - items:,}" + " more entries omitted")
        return kept
    if isinstance(node, dict):
        if depth <= 0:
            return "... object omitted (" + str(len(node)) + " keys, see saved file)" \
                if node else {}
        return {k: _walk(v, depth - 1, items) for k, v in node.items()}
    return node


# --- The byte fallback -------------------------------------------------------

def _squeeze(text, room):
    """`text` cut to `room` characters, keeping the front and the back with a
    labelled gap between them.

    Not a plain [:room]: the end of a long output is where the error, the
    total, the summary line and the "done" live, and a chop that keeps only
    the head throws away the half most likely to answer the question. Two
    thirds front, one third back, because the front is usually the denser.

    Both cuts are pulled back to a line boundary where one is close enough.
    Most of what reaches this path is lines - a file read, a log, a command's
    output - and half a line at each edge is worse than useless: read_file
    numbers its lines so edit_file has something exact to quote back, and a
    truncated one is a quotation that will not match anything."""
    if len(text) <= room:
        return text
    gap = "\n\n... [" + f"{len(text) - room:,}" + " characters omitted from the middle] ...\n\n"
    body = room - len(gap)
    if body <= 0:
        # No room for the marker as well as the text. The text wins: a note
        # about what is missing is worthless with nothing left beside it.
        return text[:room]
    head, tail = _line_cut(text, (body * 2) // 3, body - (body * 2) // 3)
    return head + gap + tail


# How far back a cut will look for a line ending before giving up and cutting
# mid-line. Far enough for ordinary source and log lines, short enough that a
# file of one enormous line loses nothing to the search.
SNAP = 400


def _line_cut(text, head_len, tail_len):
    """The first `head_len` and last `tail_len` characters of `text`, each
    moved to the nearest line boundary within SNAP, and never overlapping."""
    head = text[:head_len]
    cut = head.rfind("\n")
    if cut >= 0 and head_len - cut <= SNAP:
        head = head[:cut]
    tail = text[len(text) - tail_len:] if tail_len else ""
    cut = tail.find("\n")
    if 0 <= cut <= SNAP:
        tail = tail[cut + 1:]
    return head, tail
