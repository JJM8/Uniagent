"""Infinite chat: a chat that splits itself when the subject really changes.

Turn the mode on for a chat (Agent.set_infinite, the toggle in the corner of
the chat window) and after every turn a small, cheap judge model is shown an
abbreviated version of the recent conversation and asked one question: has the
SUBJECT changed? When it says yes, this module forks a new chat, copies the
turns from where the new subject started into it, and the browser follows.

The judge is held to a majority rule, in the prompt rather than in code: more
than half the turns it was shown have to be on the one new subject before it
may answer "new", and anything short of that is an "aside" that does nothing.
That is what keeps a genuinely different question asked once from splitting a
conversation - it has to have actually taken the chat over. It lives in the
prompt because it is a judgement about what the turns are ABOUT, which is the
thing being asked; counting them here would need this module to know that too.

## Why it runs after the turn rather than during it

after_turn() is called from server.py's _run_turn finally block, once "done"
has already been broadcast. Two things fall out of that for free, and both are
requirements rather than conveniences:

  * the user never waits on the judge. The turn is over and the page has been
    told so before a single token of this is sent.
  * the split can only ever land BETWEEN turns. A fork mid-stream would have to
    reach into a turns list a running turn is appending to.

## Why the judge sees a PARSED transcript and never the real history

The history is the thing that goes to the provider on every request - full tool
results, whole files that were read, thousands of lines of terminal output. It
is both enormous and almost entirely noise for this one question. parse() below
builds a separate view instead:

  * it is a list of EXCHANGES, not of turns - one thing the person said, and
    the LAST thing the agent said back. Everything the agent said on the way
    there is dropped: the running commentary between tool calls is the agent
    talking to itself about how to do the work, and what the exchange was
    ABOUT is in the answer it ended on. Tool RESULTS go too, entirely.
  * each tool CALL survives as its name and its first path-ish argument, and
    nothing else - collected onto the exchange's one agent line. This is the
    deliberate exception to "the last message only", and it is worth it: a
    chat whose calls move from /Projects/Uniagent/ to /Projects/drone/ has
    changed project, no amount of prose says it as plainly, and a path costs
    a few words.
  * user turns nobody typed - a subagent's report, a tool result filed as one,
    a workspace note, this module's own fork markers - are skipped, and do not
    open an exchange (see _NOT_SAID).
  * long messages are cut to their first and last ~50 characters.
  * a real pause between two exchanges is written out as a line of its own -
    "[3 hours later]". Nearly free, and one of the strongest signals there is:
    a gap plus a subject change is a new session; the same words with no gap
    are a follow-up.
  * each exchange is numbered with its USER turn's index in the REAL history,
    so the number the judge names as a cutoff is directly usable as a split
    point - and always lands on a user turn, the only boundary a history can
    safely be cut at.

The exact text of every judgement is appended to logs/infini.log and the last
one is kept in memory for the infiniagent settings tab to show. This will be
wrong at first and has to be tunable by eye, which means being readable.

## Failing closed

Anything that will not parse, an unknown verdict, a cutoff that would leave the
new chat empty or carry the whole history, a provider that is down, a cron or
subagent chat - all of them mean DO NOTHING. A judge that cannot be understood
must never fork a chat, and no failure here may ever break or delay a turn:
after_turn() catches everything.

## Copy, do not move

The parent keeps its history. It gets compaction.archive()'d first (the same
undo copy /compact takes), the carried turns are COPIED into the child, and the
parent has one short stub appended saying where things continued. If the judge
was wrong, nothing has been amputated from a chat the user is still working in.
"""

import json
import re
import threading
import time
from pathlib import Path

import compaction
import main
import provider
import settings
import usage

# Where every judgement is written down, in full - the parsed transcript that
# was sent and the answer that came back. Beside the server's own logs rather
# than in the chat's folder: this is a record of the FEATURE being tuned, not
# of anything that happened in the conversation, and nothing here ever reaches
# a provider.
LOG = Path(__file__).parent.parent / "logs" / "infini.log"

# Only an ordinary chat is ever forked - 'chat-' and eight hex digits, exactly
# what main.new_chat_id() mints. This is the guard against forking a cron run
# ('cron/<job>/<run>') or a subagent, neither of which is a conversation
# somebody is sitting in: a cron job that split itself would leave its next run
# looking at a chat the schedule knows nothing about.
_ORDINARY = re.compile(r"^chat-[0-9a-f]{8}$")

# How long a message may be before it is cut to its two ends. Roughly the first
# 50 and last 50 characters, as the design asks - a message shorter than the
# two halves plus the ellipsis is left exactly as it is, since cutting it would
# make it longer.
_HEAD = _TAIL = 50

# A pause worth writing out. Under this and a gap line would appear between
# almost every pair of turns and stop meaning anything.
_GAP_SECONDS = 20 * 60

# The line that opens a forked chat, naming where its first turns came from.
# A constant because the page matches on it: index.html draws a turn starting
# with this as the thin "split from" divider rather than as a message.
CARRIED = "[carried from "

# And the line left at the end of the parent, naming where things went on. Also
# matched on by the page, and read by the model on the parent's next turn -
# which is the point of it being a real turn rather than a note on the side.
CONTINUED = "[continued in "

# Which of a chat's own settings a fork carries into the child. Everything that
# describes HOW this conversation runs, and nothing that describes what it has
# already done: a copy of the parent's token counts would have the child
# claiming a context size it has never had, and its own first turn corrects it
# anyway.
#
# "infinite" is in here on purpose - the mode continues in the new chat, which
# is what makes it a mode rather than a one-off - and so is "workspace", without
# which the seam shows up the first time a tool resolves a relative path.
CARRY = ("provider", "model", "temperature", "workspace", "pinned",
         "safety", "safety_threshold", "safety_extra", "safety_prompt",
         "infinite")

# The last judgement, for the infiniagent tab to show: what was sent, what came
# back, and what was done about it. In memory only - it describes the most
# recent run of a thing that is still being tuned, and a copy on disk could only
# ever be read back as a judgement that looks current and never moves again.
# The log file is the durable record.
_last = {}
_last_lock = threading.Lock()


def last():
    """The most recent judgement, as the settings tab draws it, or {} if none
    has happened this run. A copy, so the caller can serialise it without
    racing the turn that is writing the next one."""
    with _last_lock:
        return dict(_last)


def _note(text):
    """Append one block to logs/infini.log. Never raises: losing a log line
    must not cost a fork, let alone a turn."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n\n")
    except OSError:
        pass


def _gap_words(before, after):
    """"[3 hours later]" for the pause between two turn times, or None when
    they are close enough together to be one sitting.

    Deliberately coarse - minutes, hours, days. The judge is being asked
    whether this looks like a new session, and "[3 hours later]" answers that
    exactly as well as a precise duration would while costing fewer tokens."""
    if not before or not after:
        return None
    seconds = after - before
    if seconds < _GAP_SECONDS:
        return None
    if seconds < 3600:
        n, unit = round(seconds / 60), "minute"
    elif seconds < 86400:
        n, unit = round(seconds / 3600), "hour"
    else:
        n, unit = round(seconds / 86400), "day"
    return "[" + str(n) + " " + unit + ("" if n == 1 else "s") + " later]"


def _short(text):
    """`text` cut to its first and last ~50 characters, on one line.

    Both ends, not just the head: the end of a message is where the actual
    request usually is ("...so can you point that at the drone repo instead"),
    and a head-only truncation throws exactly that away."""
    text = " ".join((text or "").split())
    if len(text) <= _HEAD + _TAIL + 3:
        return text
    return text[:_HEAD] + "..." + text[-_TAIL:]


def _path_arg(arguments):
    """The first path-ish argument of a tool call, or "".

    Path-ish means it contains a slash. That is crude on purpose: what this is
    looking for is the project root a call is working in, and every form that
    matters - an absolute path, a relative one, a URL, a path inside a shell
    command - carries one. A call with no slash anywhere in it contributes its
    name and nothing else, which is the honest answer."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (TypeError, ValueError):
        args = None
    values = list(args.values()) if isinstance(args, dict) else []
    for value in values:
        if isinstance(value, str) and "/" in value:
            return _short(value)
    return ""


def _calls(turn):
    """One turn's tool calls as "read_file /Projects/Uniagent/scripts/main.py",
    or [] when it made none. Names and paths only - never the arguments in
    full, which is where a call's size actually is."""
    said = []
    for call in turn.get("tool_calls") or []:
        fn = call.get("function") or {}
        name = fn.get("name") or "tool"
        where = _path_arg(fn.get("arguments"))
        said.append(name + (" " + where if where else ""))
    return said


# The four kinds of "user" turn nobody typed. A subagent's report, a retry or
# stop message main.py files as one, a note about the chat itself (a workspace
# move), and this module's own fork markers. The first three are server.py's
# own list (see _chat_label's, which picks a chat's title the same way) - what
# counts as a thing the person actually SAID has to mean the same everywhere.
#
# They are skipped rather than shown, and they do not open an exchange: a tool
# result standing in as a user turn would otherwise split one exchange into two
# and put the reply under a message nobody sent.
_NOT_SAID = ("Subagent ", "Tool result: ", main.WORKSPACE_NOTE,
             CARRIED, CONTINUED)


def _said(turn):
    """Whether this user-role turn is something the person actually typed or
    spoke, as opposed to one of the four above."""
    content = turn.get("content")
    return isinstance(content, str) and not content.startswith(_NOT_SAID)


def _typed(content):
    """What the person actually wrote, with the labels main.py puts in front
    of it for the model's benefit taken off - the mid-turn preamble and the
    spoken-message marker. Neither is part of what was said, and both would
    otherwise be the first fifty characters of every line they appear on,
    which is exactly the half _short() keeps."""
    if content.startswith(main.MID_TURN):
        content = content[len(main.MID_TURN):]
    if content.startswith(main.VOICE_INPUT):
        content = content[len(main.VOICE_INPUT):]
    return content


def exchanges(turns):
    """`turns` grouped into exchanges - one thing the person said and what the
    agent finally answered - newest last.

    Each is (index, said, replied, calls):

      index     where that user turn sits in `turns`, so it can be named as a
                cutoff and used as a split point without being mapped back
      said      what the person wrote
      replied   the agent's LAST message of that exchange, and only that one.
                Everything it said on the way - the running commentary between
                tool calls - is dropped: it is the agent talking to itself
                about how to do the work, and what the exchange was ABOUT is in
                the answer it ended on.
      calls     every tool call the exchange made, in order, de-duplicated.
                NOT dropped with the messages that carried them, and this is
                the one deliberate exception to "the last message only": the
                paths are the strongest signal in the whole transcript that the
                project changed, they cost a few words each, and they are not
                really messages - they are what the agent DID, which is the
                thing being judged.

    An exchange with no reply yet (the turn in flight, or one that died) has
    replied "". An assistant turn arriving before anything was said - which a
    forked chat's carried tail can start with - opens an exchange of its own
    with said "", so the transcript never silently loses the top of itself."""
    found = []
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role == "user":
            if _said(turn):
                found.append([i, _typed(turn["content"] or ""), "", []])
            continue
        if role != "assistant":
            continue           # a tool result: dropped entirely, as ever
        if not found:
            found.append([i, "", "", []])
        # Last one wins, so what stands at the end of the loop is the last
        # thing said - but only a message with words in it. A pure tool-call
        # turn must not blank the answer that came before it.
        content = turn.get("content") or ""
        if content:
            found[-1][2] = content
        found[-1][3] += _calls(turn)
    return [(i, said, replied, list(dict.fromkeys(calls)))
            for i, said, replied, calls in found]


def parse(turns, stamps, limit):
    """The abbreviated transcript the judge is shown - see the module docstring
    for what is kept and why.

    `turns` is the chat's real history (main.run's turns list, OpenAI shape),
    `stamps` its times from main.read_stamps(), and `limit` how many recent
    EXCHANGES to end on. Exchanges, not raw turns: the setting is "how many
    recent turns it checks" in the sense a person means it - what I said and
    what came back - so 2 is two of those, four lines, not the last four rows
    of a JSON array that might all be one tool loop.

    What comes out alternates, one exchange per pair of lines:

        7. user: can you look at the wake word threshold
           agent: it is 0.5 [calls: read_file /Projects/Uniagent/scripts/...]
        9. user: different thing now - the drone
           agent: opening it [calls: read_file /Projects/drone/main.c]

    The number is the user turn's index in `turns` itself, NOT its position in
    this view - that is what makes the judge's "cutoff" a real split point, and
    a number it names always lands on a user turn, which is the only boundary
    a history can safely be cut at (see snap)."""
    found = exchanges(turns)[-max(limit, 1):]
    lines = []
    previous = None
    for i, said, replied, calls in found:
        at = stamps[i].get("at") if i < len(stamps) and isinstance(stamps[i], dict) else None
        gap = _gap_words(previous, at)
        if gap:
            lines.append(gap)
        previous = at or previous
        lines.append(str(i) + ". user: " + (_short(said) or "(nothing)"))
        reply = _short(replied)
        if calls:
            reply = (reply + " " if reply else "") + "[calls: " + "; ".join(calls) + "]"
        lines.append("   agent: " + (reply or "(no reply)"))
    return "\n".join(lines)


def _prompt():
    """What the judge is asked, after the transcript. Read per judgement rather
    than held in a constant, so an edit on the infiniagent tab applies to the
    very next turn without a restart - the same way every other prompt in the
    app works.

    A blank setting falls back to the shipped default, which is what makes
    emptying that box a "restore the default" rather than a way to send the
    model a transcript and no question."""
    return ((settings.get("infini_prompt") or "").strip()
            or settings.DEFAULTS["infini_prompt"])


def _ask(chat_id, transcript):
    """Put the transcript to the judge and hand back what it said, verbatim.

    Recorded in the usage ledger like any other request - it fires once per
    turn on every chat in this mode, so what share of the bill is the judge
    rather than the chat is exactly the kind of thing the ledger exists to
    answer. No tools, no system message, no memories, no pins: it is answering
    one question about a transcript and anything else in the prompt is a way
    for it to be wrong."""
    chosen = settings.load()
    name, model = chosen["infini_provider"], chosen["infini_model"]
    asked = transcript + "\n\n" + _prompt()
    spend = {}
    started = time.time()
    try:
        reply = provider.get_response(asked, provider=name, model=model,
                                      temperature=chosen["infini_temperature"],
                                      usage=spend)
    except Exception as e:
        usage.record("infini", name, model, chat=chat_id, usage=spend,
                     prompt_text=asked, ms=(time.time() - started) * 1000,
                     ok=False, error=repr(e))
        raise
    usage.record("infini", name, model, chat=chat_id, usage=spend,
                 prompt_text=asked, reply_text=reply,
                 ms=(time.time() - started) * 1000)
    return reply


# The JSON object inside whatever the model actually wrote. Models wrap an
# answer in a ```json fence, or a sentence, far more often than they are asked
# to - and a fork refused because of a code fence would read as the feature
# simply not working.
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def verdict(reply):
    """The judge's answer as a dict, or None if it cannot be read.

    None is the answer to EVERYTHING that is not plainly a verdict: no JSON,
    JSON that will not parse, a verdict that is not one of the three words, a
    cutoff that is not a whole number. The caller does nothing at all with a
    None, which is the fail-closed rule this whole feature turns on."""
    found = _OBJECT.search(reply or "")
    if not found:
        return None
    try:
        answer = json.loads(found.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(answer, dict):
        return None
    if answer.get("verdict") not in ("continue", "aside", "new"):
        return None
    cutoff = answer.get("cutoff")
    if not isinstance(cutoff, int) or isinstance(cutoff, bool):
        # Only "new" actually uses it, so a missing or odd cutoff on a
        # "continue" is not a broken answer - it is a field nobody needed.
        if answer["verdict"] == "new":
            return None
        answer["cutoff"] = 0
    return answer


def snap(turns, cutoff):
    """`cutoff` moved back to the nearest USER turn at or before it, or None
    when there is no clean boundary there.

    This is not tidiness. The history is a turns list in OpenAI shape, and an
    assistant turn carrying tool_calls must stay adjacent to the `tool` turns
    that answer it or the provider rejects the whole request. Snapping back to
    a user turn is the one cut that cannot land inside such a pair, whatever
    the judge named - and it is also the right cut for a person to read: a
    conversation starts with somebody saying something."""
    # Out of range is refused rather than clamped. A judge that names a turn
    # which does not exist has not read the numbers it was given, and quietly
    # clamping that onto the nearest real boundary would turn a plainly broken
    # answer into a plausible-looking fork - which is the one thing this whole
    # feature must not do.
    if not isinstance(cutoff, int) or not (0 <= cutoff < len(turns)):
        return None
    for i in range(cutoff, -1, -1):
        turn = turns[i]
        if isinstance(turn, dict) and turn.get("role") == "user":
            return i
    return None


def fork(agent, cutoff, title):
    """Split `agent` at `cutoff`, and hand back the new chat's id.

    In order: archive the parent (compaction's own copy, so a wrong fork can be
    read back or copied back by hand), mint a chat, copy the turns from the
    cutoff onward into it verbatim under a line saying where they came from,
    carry the settings that describe how this conversation runs (CARRY), and
    append one stub turn to the parent saying where things continued.

    The parent's history is otherwise untouched. Copying rather than moving is
    what makes a wrong verdict survivable: the worst a bad fork does is leave a
    chat nobody wanted beside a chat that still has everything in it."""
    turns = json.loads(agent.history)
    carried = turns[cutoff:]
    # The parent's route always, and its name only when it has one - a chat
    # that has never been named would otherwise have the divider say its id
    # twice.
    where = agent.route + (": " + agent.name if agent.name else "")

    compaction.archive(agent)

    cid = main.new_chat_id()
    child = main.chat(main.chat_md(cid))
    child.history = json.dumps(
        [{"role": "user", "content": CARRIED + where + "]"}]
        + carried, indent=2)
    child.save()
    for key in CARRY:
        value = getattr(agent, key, None)
        # A copy of the list, not the parent's own: pinned is mutable and
        # add_pinned appends to it, so a shared reference would have a pin made
        # in one chat appear in the other.
        setattr(child, key, list(value) if isinstance(value, list) else value)
    if title:
        child.name = title
    child._write_settings()

    turns.append({"role": "user",
                  "content": CONTINUED + cid
                             + (": " + title if title else "") + "]"})
    agent.history = json.dumps(turns, indent=2)
    agent.save()
    return cid


def after_turn(agent):
    """Judge the turn that has just ended in `agent`, and fork if it says to.
    Returns the new chat's id, or None - which is every outcome but a fork.

    Called from server.py's _run_turn finally block, after "done" has been
    broadcast. Everything is caught: a judge that fails is a judge that did
    nothing, and it must be invisible to the turn it followed.

    Nothing at all happens - not one request, not one settings read that costs
    anything - unless this chat has the mode turned on. Off is free."""
    try:
        if not getattr(agent, "infinite", None):
            return None
        # Cron runs and subagents, explicitly. Neither is a conversation
        # anybody is sitting in, and a chat that a schedule or a parent turn
        # owns must not move out from under it.
        if not _ORDINARY.match(agent.route or ""):
            return None

        try:
            turns = json.loads(agent.history) if agent.history else []
        except json.JSONDecodeError:
            return None      # a pre-JSON flat-text chat: nothing to split
        if not isinstance(turns, list) or len(turns) < 2:
            return None

        transcript = parse(turns, main.read_stamps(agent.id),
                           settings.get("infini_turns"))
        if not transcript.strip():
            return None

        reply = _ask(agent.id, transcript)
        answer = verdict(reply)
        said = "unreadable" if answer is None else answer["verdict"]
        cut = None if answer is None or said != "new" else snap(turns, answer["cutoff"])
        # A cut at 0 would carry the whole history (nothing was split), and one
        # at or past the end would leave the child empty. Both mean the judge
        # named a boundary that isn't one, so both do nothing.
        if cut is not None and not (0 < cut < len(turns)):
            said, cut = said + " (cutoff " + str(cut) + " splits nothing)", None

        done = None
        if cut is not None:
            done = fork(agent, cut, (answer.get("title") or "").strip()[:60])

        record = {"chat": agent.route, "at": int(time.time()),
                  "transcript": transcript, "reply": reply.strip(),
                  "verdict": said, "why": (answer or {}).get("why", ""),
                  "cutoff": cut, "forked": done}
        with _last_lock:
            _last.clear()
            _last.update(record)
        _note(agent.route + " -> " + said
              + (" -> forked " + done if done else "")
              + "\n--- sent ---\n" + transcript
              + "\n--- said ---\n" + reply.strip())
        return done
    except Exception as e:
        # Never let this reach the turn it followed. The turn is over and the
        # page has been told; a judge that blew up is a line in the log.
        _note("failed on " + str(getattr(agent, "route", "?")) + " - "
              + type(e).__name__ + ": " + str(e))
        return None
