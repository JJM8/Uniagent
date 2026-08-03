"""Squash a long conversation down to a short one, in place.

/compact runs compact(), which does three things in order:

  1. copies the chat's whole folder into chats/chat_archive/ - the transcript
     about to be replaced is never simply lost, so a compaction that turns out
     to have dropped something important can be read back (or copied back) by
     hand;
  2. asks the model to condense the conversation;
  3. swaps what comes back in as the chat's history.

The condensing runs on the CHAT'S OWN model, at its own temperature, behind
the chat's own system message - and, on a native tool-calling model, with the
same tools array a normal turn sends. That is deliberate: the request is then
byte-identical to a normal turn right up to the final instruction, so it lands
on the prompt cache the chat has already paid to build instead of starting a
fresh one. The old module had a compaction model of its own (deepseek-v4-flash,
hardcoded here) which meant every compaction was a cold cache on a model the
chat wasn't even using.

The history arrives here as what it now is on disk - a JSON turns list in
OpenAI's own message shape (see main.run's docstring) - and goes over as real
turns, not flattened into a "User: ... / Uniagent: ..." wall of text the way
the previous version had to. Tool calls and their results therefore survive
into the compaction request intact.

What comes BACK is whatever the model decides to write. It is stored as a
single user turn carrying that text (see HEADER), so the history stays a valid
turns list no matter what shape the summary itself takes - the format the model
answers in is still being felt out, and nothing here depends on it being JSON.
"""

import json
import shutil
import threading

import main
import provider
import settings
import tool_processor
import turnctx

# Chats being compacted right now, by id. A compaction holds the chat's own
# turn slot for its whole length, so main.busy_chats() already counts it as busy
# and a message sent meanwhile queues behind it - but "main agent working" is
# the wrong thing to say about it: nothing is streaming back, there is just a wait.
# The page asks here which of the two is happening, so it can say "main agent
# compacting" instead. See server._status().
_running = set()
_running_lock = threading.Lock()


def is_compacting(chat_id):
    """Whether that chat is being compacted right this moment."""
    with _running_lock:
        return chat_id in _running

# Where a chat's folder is copied before its history is replaced. Sits beside
# deleted_chats/ inside chats/, and is skipped by the sidebar and /chats for
# the same reason that one is: the listings glob one level down (chats/<id>/
# history.json), so anything a level deeper isn't a chat as far as they care.
ARCHIVE = main.CHATS / "chat_archive"

# Prefixed to the compacted history so it's obvious in the transcript - and to
# the model - that the conversation above this point was summarised rather than
# actually said.
HEADER = ("[compacted history - the conversation up to here, summarised to "
          "save context. The full transcript is in chats/chat_archive/.]\n\n")

def _prompt():
    """The instruction to send as the last user turn after the whole
    conversation - what the settings page's compaction tab edits.

    There is no {history} placeholder: the history is the messages ahead of
    this one, not text pasted into it, so the setting reaches the model
    verbatim. Read per compaction rather than held in a module constant, so an
    edit on the settings page applies to the very next /compact without a
    restart - the same way every other setting works.

    A blank setting falls back to the shipped default, which is what makes
    emptying the box on that tab a "restore the default" rather than a way to
    send the model a conversation and no instruction at all."""
    return ((settings.get("compaction_prompt") or "").strip()
            or settings.DEFAULTS["compaction_prompt"])


def _turns(history):
    """A chat's stored history as a turns list. An old flat-text transcript
    (or anything else that won't parse) becomes one user turn holding the lot,
    so it still gets compacted rather than being silently sent as nothing."""
    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        return [{"role": "user", "content": history}]
    return turns if isinstance(turns, list) else []


def archive(agent):
    """Copy this agent's whole folder into chats/chat_archive/ and return where
    it landed. Numbered on a clash rather than overwritten - compacting the same
    chat twice is normal, and the second archive must not erase the first."""
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    # The id, not the folder name: a cron job lives at chats/cron/<name>/, so by
    # folder name alone its archive would collide with an ordinary chat of the
    # same name. Same reasoning as _delete's in command_processor.py.
    base = agent.id.replace("/", "-")
    destination = ARCHIVE / base
    n = 2
    while destination.exists():
        destination = ARCHIVE / (base + "-" + str(n))
        n += 1
    shutil.copytree(agent.path.parent, destination)
    return destination


def _ask(agent, turns, prompt):
    """The condensed conversation, as the model writes it.

    Everything about the request bar the last message is what a normal turn of
    this agent would send - same provider, model and temperature, same system
    message (this chat's pins included), same tools array -
    so the cached prefix is hit instead of rebuilt."""
    provider_name, model, temperature = agent.models()
    messages = [{"role": "system",
                 "content": main.system_text(provider_name, model, agent.pinned)}]
    messages += turns
    messages.append({"role": "user", "content": prompt})
    tools = tool_processor.tools_schema(tool_processor.shape_for(provider_name))
    # tool_call is left None on purpose: with tools declared the model COULD
    # answer with a call instead of prose, and ignoring it means such a reply
    # comes back as empty text - which compact() below refuses to swap in,
    # rather than replacing a whole conversation with nothing.
    return "".join(provider.stream_response(messages, provider=provider_name,
                                            model=model, temperature=temperature,
                                            tools=tools)).strip()


def compact(agent, prompt=None):
    """Compact `agent`'s history in place, returning what to tell the user.

    Blocking: whoever ran /compact is waiting on the answer. `prompt` overrides
    the saved compaction prompt (see _prompt()) for this one call.

    The chat's turn slot is HELD for the whole compaction, the same slot a turn
    takes - so a message sent meanwhile waits for the new history instead of
    running against the old one and writing it straight back over the top. It is
    taken without blocking, though: a chat already mid-turn is told so rather
    than leaving whoever typed /compact waiting out a turn that could run for
    minutes.

    The context it holds the slot with is marked "compaction", which is what
    stops /stop from abandoning it: this is one request to the model with no
    safe point to give up at and no half-written transcript to rescue, so it is
    the one thing in here that genuinely has to be waited out (see
    main.request_stop and command_processor._stop).

    Nothing is written until the model has actually answered - if the request
    fails, or comes back empty, the chat is left exactly as it was. The archive
    copy is taken first regardless, which is the point of it.
    """
    ctx = turnctx.TurnContext(agent.id, kind="compaction")
    if not agent.slot.acquire(ctx, blocking=False):
        # Two different reasons the chat is busy, and only one of them can be
        # answered with "/stop it" - a compaction has no safe point to stop at.
        if is_compacting(agent.id):
            return "that chat is already being compacted - give it a moment."
        return ("that chat is mid-turn - wait for it to finish (or /stop it) "
                "before compacting.")
    with _running_lock:
        _running.add(agent.id)
    try:
        turns = _turns(agent.history)
        if not turns:
            return "nothing to compact - this chat is empty."

        where = archive(agent)
        summary = _ask(agent, turns, prompt or _prompt())
        if not summary:
            return ("nothing to compact with - the model returned an empty reply, "
                    "so the chat is untouched. (Archived a copy at "
                    + str(where) + ".)")

        agent.history = json.dumps([{"role": "user", "content": HEADER + summary}],
                                   indent=2)
        agent.save()
        # The last real token count was taken against the history that just went
        # away, and context_usage() prefers a reported count over its own
        # projection whenever the reported one is bigger - so without clearing it
        # the bar would keep showing the OLD, larger context until the next turn
        # happened to report a new one. Cleared, the panel falls straight back to
        # measuring what is actually there now.
        main._set_usage(agent.id, {}, None)
        return ("compacted " + str(len(turns)) + " turns into one. The full "
                "transcript is archived at " + str(where) + ".")
    finally:
        with _running_lock:
            _running.discard(agent.id)
        agent.slot.release(ctx)
