# Main conversation engine for Uniagent: manages chat history, runs the tool loop,
# handles streaming responses, and coordinates turns across multiple concurrent chats.
# This is the core that both the terminal interface and web server run through.

import inspect
import json
import re
import shutil
import sys
import _term as termios  # no-op stand-in on Windows; the real thing elsewhere
import threading
import time
from datetime import date, datetime
import uuid
from pathlib import Path

import claude_session
import provider
import settings
import timing
import tokens
import tool_processor
import tool_validation
import turnctx
import usage as usage_log  # `usage` is a local in run()'s loop - see there
import workspace
import voice_input

name = "Uniagent"

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

CONTEXT = Path(__file__).parent.parent / "context"

# What counts as a context file. Anything else in context/ (an image, a stray
# .json) is left alone rather than pasted into the prompt as mojibake.
CONTEXT_SUFFIXES = (".md", ".txt")

# The shipped context files, kept OUTSIDE context/ so they are never injected
# alongside the live ones - two copies of the system prompt in every prompt is
# exactly the bug this folder would cause if it lived one level up. These are
# what a fresh install starts from (seed_context) and what "revert to default"
# in the settings page puts back.
DEFAULTS = Path(__file__).parent.parent / "defaults"
DEFAULT_CONTEXT = DEFAULTS / "context"
DEFAULT_MEMORIES = DEFAULTS / "memories"

# Where a preset goes when it is replaced, one dated folder per swap, so a
# reset is undoable by hand and never silently destroys months of facts.
ARCHIVE = Path(__file__).parent.parent / "archive"


def preset_parts():
    """The two halves of a "preset" - the agent's whole learned state - as
    (name, live folder, shipped default) each. Archived, reverted and restored
    together, never one without the other: resetting the prompt while leaving a
    memories/ folder full of the last install's projects behind is not a reset
    of anything.

    They stay SEPARATE FOLDERS on disk, and memories/ is deliberately NOT moved
    inside context/. Everything in context/ is injected in full on every single
    turn; memories/ is the half that must not be, which is the entire reason it
    sits outside (see MEMORIES above). Folding it in would put every memory
    file, in full, into every prompt - the exact cost the split exists to
    avoid. One unit to the user, two folders to the model.

    Read from the module globals at call time rather than frozen into a
    constant, so pointing these at a temp folder is enough to exercise the
    whole thing without touching a real install."""
    return (("context", CONTEXT, DEFAULT_CONTEXT),
            ("memories", MEMORIES, DEFAULT_MEMORIES))

# Individual memory files - one topic per file, kept OUTSIDE context/ on
# purpose so they are NOT swept up and fully injected by context_text() the
# way everything under context/ is. Only their names + a one-line
# description go in the prompt every turn (see memories_text()); the model
# reads a specific one in full with read_file/ask_file when it looks
# relevant, and writes to it with edit_file/write_file - same as any other
# file, no dedicated memory tool needed.
MEMORIES = Path(__file__).parent.parent / "memories"

CHATS = Path(__file__).parent.parent / "chats"

# The two files every chat folder holds. Fixed names, not named after the chat:
# the FOLDER carries the id (see chat_id), so the files just say what they are.
HISTORY_FILE = "history.json"
SETTINGS_FILE = "settings.json"

# Files the user attached to a message live in a folder of their own beside
# those two, so an attachment can never be mistaken for a chat's own files.
# Nothing reads them here: the message that carries them names each one by its
# full path, and the model opens one with read_file the way it opens anything
# else on the disk.
ATTACHMENTS_DIR = "attachments"


def _under_chats(path):
    """The folder parts of a chat file's path, relative to chats/ - ('cron',
    'ai-news', '003') for chats/cron/ai-news/003/history.json. None when the
    path isn't under chats/ at all."""
    folder = Path(path).parent
    # The overwhelmingly common case: a path built from CHATS in the first
    # place, which is already relative to it. Answered without touching the
    # filesystem - and that matters, because the chats panel asks this twice
    # for every chat on disk (chat_id and chat_route), and resolve() is a walk
    # of the whole path in syscalls. Building the sidebar once did ~1600 of
    # them for nothing, which was half the time it took.
    try:
        return folder.relative_to(CHATS).parts
    except ValueError:
        pass
    # Anything else - a path given from outside, a symlinked chats/, one side
    # absolute and the other not - is worth resolving both ends for.
    for base in (CHATS, CHATS.resolve()):
        for candidate in (folder, folder.resolve()):
            try:
                return candidate.relative_to(base).parts
            except ValueError:
                continue
    return None


def chat_id(path):
    """The FLAT id for the chat owning `path` - one word, unique across every
    chat on disk. 'chats/chat-1bb28a87/history.json' -> 'chat-1bb28a87'.

    A cron job's runs each get their own chat folder, nested a level deeper
    (chats/cron/ai-news/003/), and the id folds the two names together:
    'ai-news-003'. It has to, because this id is what names things OUTSIDE the
    chat's own folder - its validation log, its subagent folder, its entry in
    the busy list and the stop set. By folder name alone every job's third run
    would be '003' and they would all share one another's.

    Not the same thing as chat_route() below, which is how a client names a
    chat. This is the internal key; that is the address."""
    parts = _under_chats(path)
    if parts and len(parts) == 3 and parts[0] == "cron":
        return parts[1] + "-" + parts[2]
    return parts[-1] if parts else Path(path).parent.name


def chat_route(path):
    """The id a CLIENT names this chat by: its folder's path under chats/.
    'chats/chat-1bb28a87/history.json' -> 'chat-1bb28a87', one run of a cron
    job -> 'cron/ai-news/003'. It is what /chats hands the page, what /load
    takes, and what chat_md() turns back into a path.

    Deliberately not chat_id(). The web UI knows a chat by this, while
    everything inside this process keys off the flat id - and for a cron run
    the two are different strings. Tag a broadcast with the flat id and the
    page compares it against the id IT holds, decides the event belongs to
    some other chat, and drops it: that is exactly why a cron chat streamed
    nothing and never redrew at the end of a turn. Anything the server SAYS TO
    a client about which chat it means uses this."""
    parts = _under_chats(path)
    return "/".join(parts) if parts else Path(path).parent.name


def route_of(cid):
    """chat_route() for a flat chat id, for the callers that only ever have one
    (busy_chats, the stop set). Only a cron run's id needs any work: it folds a
    job name and a run number together, and the job's own folder is what tells
    the two apart - a job may itself be called 'a-b-003'."""
    folder = CHATS / "cron"
    if folder.is_dir():
        job, _, run = cid.rpartition("-")
        if run and (folder / job / run).is_dir():
            return "cron/" + job + "/" + run
    return cid


# When each turn was written, kept BESIDE the chats rather than in them - one
# .json per chat, holding one entry per history turn, in the same order.
#
# A sidecar for exactly the reason validations/ is one (see VALIDATIONS): the
# transcript is what goes to the provider on every single request, and the
# time under a message is for the person reading the page, not for the model.
# Nothing in this folder is ever sent anywhere - provider.py builds its
# messages from the history and has no idea this file exists.
#
# Keyed by the FLAT chat id (see chat_id), so a cron run's stamps are
# stamps/ai-brief-003.json - the same keying the validation logs use, and
# server.py's /stamps route maps a browser's route onto it the same way.
STAMPS = CHATS / "stamps"


def _stamp_path(cid):
    return STAMPS / (cid + ".json")


def read_stamps(cid):
    """This chat's stamps, or [] when it has none - which is every chat written
    before this existed, and most of them.

    Never guessed at from the file's mtime. That is when the chat was LAST
    touched, so falling back to it would put one confident, identical, wrong
    date under every message of every old conversation. No date at all is the
    honest answer, and the page draws nothing."""
    try:
        found = json.loads(_stamp_path(cid).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return found if isinstance(found, list) else []


def clear_stamps(cid):
    """Forget this chat's stamps - what /delete calls, since the file lives
    outside the folder it moves and would otherwise be inherited by the next
    chat to hold the id (see command_processor._delete)."""
    try:
        _stamp_path(cid).unlink()
    except OSError:
        pass


def stamp_history(cid, history):
    """Bring `cid`'s stamps into line with the history being written.

    Reconciled and rewritten WHOLE, not appended to. Appending is right for
    the validation log - tool calls are only ever added - but a history is not
    only ever added to: /compact replaces the entire transcript with a single
    summary turn (compaction.compact), and a stopped turn rewrites its tail
    (_stopped_history). Either one slides every remaining stamp onto the wrong
    message, and a wrong date is worse than no date, because it reads as
    authoritative.

    So an entry survives only where the turn at its index still matches what
    was stamped - same role, same content length. Anything else is stamped
    now. That is a deliberately generic anchor rather than a note-to-self in
    each of the callers that rewrite a history: it defends itself against the
    next one, which won't remember this file exists.

    A turn's time is therefore when it first reached disk - for a message the
    moment it was sent (run() appends it and saves immediately), for a reply
    the moment it finished.

    Epoch SECONDS, not the ISO-local string the token block uses
    (record_usage). A chat gets read on whatever device is to hand, and only an
    absolute instant lets each of them draw the time in its OWN zone; an ISO
    string with no offset would be shown as the server's wall clock on a phone
    three time zones away, and be wrong without ever looking wrong."""
    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        return  # a pre-JSON flat-text chat: no turns to index against
    if not isinstance(turns, list):
        return
    old = read_stamps(cid)
    now = int(datetime.now().timestamp())
    stamps = []
    for i, turn in enumerate(turns):
        turn = turn if isinstance(turn, dict) else {}
        role = turn.get("role")
        size = len(turn.get("content") or "")
        was = old[i] if i < len(old) and isinstance(old[i], dict) else None
        if was and was.get("role") == role and was.get("n") == size \
                and isinstance(was.get("at"), (int, float)):
            stamps.append(was)
        else:
            stamps.append({"at": now, "role": role, "n": size})
    # save() runs after every step of a turn, so an unchanged file must not
    # mean a disk write every step - same reason record_context() checks.
    if stamps == old:
        return
    try:
        STAMPS.mkdir(parents=True, exist_ok=True)
        _stamp_path(cid).write_text(json.dumps(stamps), encoding="utf-8")
    except OSError:
        pass  # losing a stamp must never cost a turn, same as _log_validation


MAX_TOOL_CALLS = 1000  # cap it so it can't call tools forever

# Closes out a turn that /stop cut short. A constant because it's also matched
# on: a stopped subagent still reports what it managed to write, and finding
# that means stepping back past this line.
STOPPED = "[stopped by the user]"
MAX_BAD_JSON = 5       # how many times to ask the model to fix a broken tool call

# What a failed turn is filed under (see append_error). A constant for the same
# reason STOPPED is: final_answer() has to recognise one of these so a turn that
# blew up isn't mistaken for a reply.
TURN_ERROR = "Error: the turn failed - "

# The tool result a stop leaves on a call it cut short, in two halves. Split
# because continue_from() swaps the tail: "wait for the user" is the truth
# while a stop is the last thing that happened, and exactly the wrong thing to
# leave in front of a model the moment the user has asked it to carry on.
STOPPED_CALL = ("STOPPED - the user stopped the turn before this call "
                "finished. Whether it ran at all is unknown, so check "
                "rather than assume, ")
STOPPED_CALL_WAIT = "and wait for the user's next instruction before carrying on."
STOPPED_CALL_GO = ("and then carry on with what you were doing - the user has "
                   "asked you to continue.")

# What a person types (or a button posts) to pick a stopped or failed turn back
# up. Not in command_processor's table with the others: every command there
# ANSWERS, and this one starts a turn instead, so the two front ends take it
# before the table - see continue_from() and server.py's /input.
CONTINUE = "/continue"

# The one thing a continue adds to a history, and only in the case that needs
# it: a turn cut off mid-sentence leaves an assistant turn last, and a request
# ending there is a prefill on some providers - it would carry on the stopped
# sentence rather than start again. Every other resumable end (a tool result, a
# user message) needs nothing added at all. See continue_from().
CONTINUE_NUDGE = "Carry on from where you left off."

# What labels a message the user sent mid-turn (run()'s `inject`). It has to
# say WHEN it was sent: it arrives after a tool result the model is still
# reasoning about, and read as a plain user turn it looks like an answer to a
# question the model never asked.
MID_TURN = "The user sent this while you were working, mid-task: "

# ---- what a spoken message looks like in a transcript ------------------------
# A message dictated to the wake-word listener arrives as one or more of these
# lines. They are markers in the same sense STOPPED and MID_TURN are - part of
# the record rather than anything the user typed - and they exist because
# speech has a problem writing does not: a person pauses in the middle of a
# sentence, and software has no way to tell that pause from the end of one.
#
# So the listener guesses, sends, and corrects itself when it turns out to have
# guessed early. The first thing said is voice_input, and anything that turns
# out to belong to the same thought is voice_continued underneath it - the
# whole message re-sent, not a second request. Without the marking, "turn the
# heating on" followed by "in the front room only" reads as somebody who
# changed their mind, which is exactly the wrong thing for the model to think.
VOICE_INPUT = "voice_input: "
VOICE_CONTINUED = "voice_continued: "

# Said once at the bottom of any message that has a continuation in it. The
# markers are close to self-explanatory, but "close to" is not a standard worth
# holding a model to, and this costs a line.
VOICE_NOTE = ("(voice: the user was still talking. These lines are one thing "
              "said with pauses in it, not separate requests - read them as a "
              "single message.)")

# What labels a note about something that HAPPENED to the conversation rather
# than something anybody said in it - today, the chat being moved to another
# workspace. It goes into the history as a user turn (see note_turn), so the
# prefix is what lets the front-ends draw it as a note instead of as a line the
# user typed, and what keeps it out of a chat's sidebar label.
WORKSPACE_NOTE = "Workspace changed: "

# Function/arrow keys reach the terminal as escape sequences (F2 is \x1bOQ or
# \x1b[12~). pynput watches keys globally but doesn't swallow them, so holding
# F2 to talk also drops its escape sequence into whatever you were typing.
# Strip those, and any stray control chars, before treating the line as input.
_ESCAPE = re.compile(r"\x1b[\[O][0-9;]*[~A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text):
    return _ESCAPE.sub("", text).strip()


def _approve(question):
    """Ask a y/n question. Flush stale input first so a pasted line can't answer
    it by accident."""
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass
    return input(question + " (y/n) > ").strip().lower() == "y"


class TurnSlot:
    """Which turn owns an agent right now - one at a time, next in line waits.

    This replaces the plain threading.Lock each Agent used to hold for the
    length of its turn. A lock can only be released by the thread that took it,
    and that is precisely what made /stop slow: the chat stayed owned until the
    stopped worker got around to unwinding, so the page could not go idle and a
    queued message could not go out, however long the worker was still stuck in
    a socket read or a tool.

    Ownership here is a value, not a thread's claim on a mutex, so /stop can
    take the chat off the abandoned turn and hand it to the next one
    immediately (see main.request_stop). The abandoned thread's own release()
    then finds it no longer holds the slot and does nothing, which is the whole
    point: whether it unwinds in a millisecond or a minute changes nothing for
    anybody waiting."""

    def __init__(self):
        self._cond = threading.Condition()
        self._ctx = None

    def acquire(self, ctx, blocking=True):
        """Take the slot for `ctx`. Blocking (the default) waits for whoever
        has it; non-blocking returns False rather than waiting, which is what
        /compact wants - a chat already mid-turn is told so, not queued."""
        with self._cond:
            if not blocking:
                if self._ctx is not None:
                    return False
                self._ctx = ctx
                return True
            while self._ctx is not None:
                self._cond.wait()
            self._ctx = ctx
            return True

    def release(self, ctx):
        """Give the slot up, but ONLY if `ctx` still holds it. False means
        somebody else does now - a stopped turn whose chat has already been
        handed on - and the caller must not touch the chat any further."""
        with self._cond:
            if self._ctx is not ctx:
                return False
            self._ctx = None
            self._cond.notify_all()
            return True

    def held(self):
        """Whether anything owns this agent right now - what "is it busy" means
        everywhere, and what goes false the instant /stop lands rather than
        whenever the abandoned worker finally notices."""
        with self._cond:
            return self._ctx is not None

    def context(self):
        with self._cond:
            return self._ctx


class Agent:
    """One agent's conversation and the model it runs on.

    Each agent gets a FOLDER of its own, named by its id, holding two files:
        chats/<id>/history.json   - the transcript, clean, nothing but the chat
        chats/<id>/settings.json  - which model it runs on, its own temperature,
                                    token accounting, and room for more later
    Keeping the settings in their own file means the transcript stays purely the
    conversation, and the model can be changed (by /model, or by hand) without
    touching the history.

    The FOLDER name is the id - not either file's stem, which is the same two
    words for every chat on disk. Read it off `self.id` (or chat_id() for a
    bare path); never off `path.stem`.

    Chat, CronJob and Subagent are all Agents - the same thing, a running
    conversation with a model behind it. The turn slot is PER AGENT, so turns
    of different agents run in parallel.

    provider/model/temperature are None when nothing has been pinned, and the
    agent then follows whatever the settings page has selected (see models())."""

    kind = "agent"

    # Settings we know about today. Kept as a list so the .json is written in a
    # stable order and a stray key can't sneak in - room to grow (see save()).
    # The token block isn't really a "setting" - it's this agent's context
    # accounting - but it lives here so a chat opened fresh (new session, or
    # one that hasn't answered yet this run) still has a number to show
    # instead of nothing:
    #   input_tokens/output_tokens  the last REAL usage a provider reported
    #   tokens_model                "<wire>/<model>" those counts came from
    #                               (see model_key()), so a chat since moved
    #                               to another model doesn't show a count
    #                               taken against a different tokenizer and a
    #                               different window. The WIRE, so that
    #                               renaming a provider - which changes
    #                               nothing about the model - doesn't read as
    #                               "this count belongs to something else"
    #   tokens_at                   when they were reported (ISO, local)
    #   context_input               the last projected input size, counted
    #                               from what's actually injected - what a
    #                               never-answered chat shows, and what gets
    #                               drawn the instant this chat is opened
    #                               (see record_context() and context_usage())
    #   context_max                 the window those were measured against
    #   context_model               "<wire>/<model>" the projection was
    #                               measured on - its own key, NOT shared with
    #                               tokens_model: a projection is recomputed
    #                               constantly and a real count is not, so one
    #                               must never validate the other
    #   context_exact               whether that projection came from the
    #                               model's own tokenizer, so a count drawn
    #                               from this file is labelled "~" exactly as
    #                               it was when it was measured (see
    #                               stored_usage())
    # pinned is this chat's OWN tools/skills manually added to context (see
    # add_pinned()) - scoped to this one agent, never shared context/ or
    # another chat's settings.
    # The safety block - this agent's own answer to the tool-call safety gate.
    # All four are None = follow the settings page:
    #   safety_threshold  0-10, set by the slider in the corner of the chat
    #                  window: the highest danger rating a tool call can be
    #                  given and still run without asking. 0 asks about
    #                  everything, 10 checks nothing. The normal way a chat
    #                  says how carefully to run.
    #   safety_extra   extra rules for THIS chat, added to the shared prompt -
    #                  "anything touching Google Drive is fine". The common
    #                  case, and much better than rewriting the whole prompt:
    #                  the chat keeps following the shared one as it improves
    #                  and only says the bit that is different about itself.
    #   safety_prompt  this agent's own vetting prompt INSTEAD of the global
    #                  one - must contain "{call}" (see tool_validation.check).
    #                  The escape hatch when the shared prompt is wrong for
    #                  this chat rather than merely incomplete.
    #   safety         the True/False flag that predates the number, present in
    #                  every chat folder made before it. Nothing writes it any
    #                  more, and it is read only when safety_threshold is unset
    #                  - see tool_validation.threshold_for.
    # A cron job's are mirrored here from cron.json by cron.py's _ensure_chats -
    # its number into safety_threshold, and the job's task and rules into
    # safety_extra - so opening the job's chat shows what it actually runs under
    # and a question typed into it is judged the way the run was. cron.json
    # stays the source of truth for those.
    # started is when a cron RUN began, written once by cron.new_run(). Only a
    # cron run's chat has one - it is what the chats panel labels the job's
    # older runs by, under its "history" toggle, since a run folder is numbered
    # and a number is not a thing anyone can pick a run out by.
    SETTINGS_KEYS = ("provider", "model", "temperature", "name", "started", "safety",
                     "safety_threshold", "safety_extra",
                     "safety_prompt", "input_tokens",
                     "output_tokens", "tokens_model", "tokens_at",
                     "context_input", "context_max", "context_model",
                     "context_exact", "pinned", "workspace", "last_prompt_client")

    def __init__(self, path, provider=None, model=None, temperature=None):
        self.path = Path(path)  # history.json; settings.json sits beside it
        self.id = chat_id(self.path)
        # The id a browser knows this chat by - the same string for an ordinary
        # chat, 'cron/<name>' for a cron job. See chat_route().
        self.route = chat_route(self.path)
        # Who is running a turn in this agent right now. Not a lock - see
        # TurnSlot's docstring on why ownership has to be transferable.
        self.slot = TurnSlot()
        self.history = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        cfg = self._read_settings()
        # A model passed in (a cron job knows its from cron.json before its files
        # exist) wins; otherwise the .json; otherwise NOTHING - None means "not
        # pinned", and models() reads the live settings default for it every
        # turn. There is deliberately no floor here: a hardcoded pair would
        # make every freshly-loaded chat report itself pinned to that pair
        # (which is what /model then printed), and _write_settings persists
        # every non-None key - so the phantom pin got written into the chat's
        # settings.json the first time anything else about it was saved, and
        # became a real one. Matches reload_model(), which has always taken
        # these straight from the .json with no fallback.
        self.provider = provider or cfg.get("provider")
        self.model = model or cfg.get("model")
        # Not "or" - 0 is a real, common temperature and must not be treated as
        # falsy and fall through to the .json/default the way an empty
        # provider/model string would.
        self.temperature = temperature if temperature is not None else cfg.get("temperature")
        self.name = cfg.get("name")
        self.started = cfg.get("started")
        # Not "or" either - False is a real value here ("never check this
        # agent's calls") and must not fall through to the global default the
        # way None (nothing set) does.
        self.safety = cfg.get("safety")
        self.safety_threshold = cfg.get("safety_threshold")
        self.safety_extra = cfg.get("safety_extra")
        self.safety_prompt = cfg.get("safety_prompt")
        self.input_tokens = cfg.get("input_tokens")
        self.output_tokens = cfg.get("output_tokens")
        self.tokens_model = cfg.get("tokens_model")
        self.tokens_at = cfg.get("tokens_at")
        self.context_input = cfg.get("context_input")
        self.context_max = cfg.get("context_max")
        self.context_model = cfg.get("context_model")
        self.context_exact = cfg.get("context_exact")
        self.pinned = cfg.get("pinned") or []
        # Which workspace this chat's tools work in - a workspace id out of
        # WORKSPACES in .env, or None for the default one. Just the id: the
        # root and the ssh destination behind it are config that can change
        # under a chat that was filed here months ago, and a copy of them in
        # every chat folder would be a hundred stale copies to fix.
        self.workspace = cfg.get("workspace")
        # The browser tab that sent the most recent prompt into this chat - an
        # id it mints itself and holds in localStorage, not a user identity.
        # Read out again when a reply is ready to be spoken, so the audio plays
        # on the device that's actually being looked at instead of every open
        # window on the chat - see server.py's _speak_offer.
        self.last_prompt_client = cfg.get("last_prompt_client")

    def _settings_path(self):
        return self.path.parent / SETTINGS_FILE

    def _read_settings(self):
        try:
            return json.loads(self._settings_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_settings(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(self, k) for k in self.SETTINGS_KEYS
                if getattr(self, k) is not None}
        self._settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def reload_model(self):
        """Re-read the settings .json, so the model is taken fresh at the start
        of every turn: a /model on the loaded chat, or a hand-edit to the .json,
        is picked up on the next request with no restart. Also re-reads pinned,
        so a pin added (via POST /context/pin) while this chat sits open reaches
        the very next turn the same way."""
        cfg = self._read_settings()
        self.provider = cfg.get("provider")
        self.model = cfg.get("model")
        self.temperature = cfg.get("temperature")
        self.name = cfg.get("name")
        self.started = cfg.get("started")
        self.safety = cfg.get("safety")
        self.safety_threshold = cfg.get("safety_threshold")
        self.safety_extra = cfg.get("safety_extra")
        self.safety_prompt = cfg.get("safety_prompt")
        self.pinned = cfg.get("pinned") or []
        # Re-read for the same reason as the model: the workspace dropdown
        # writes the chat's .json, and the very next turn has to run in the
        # new place without the server being restarted.
        self.workspace = cfg.get("workspace")

    def models(self):
        """The (provider, model, temperature) this turn actually runs on: the
        agent's own if pinned, otherwise the live settings default - read
        fresh, so changing the default on the settings page reaches an
        unpinned agent next turn."""
        chosen = settings.load()
        temperature = self.temperature if self.temperature is not None else chosen["temperature"]
        return (self.provider or chosen["provider"], self.model or chosen["model"], temperature)

    def pin(self, provider, model):
        """Fix this agent to a model and write it into its settings .json."""
        self.provider = provider
        self.model = model
        self._write_settings()

    def unpin(self):
        """Drop back to following the settings default."""
        self.provider = None
        self.model = None
        self._write_settings()

    def set_last_prompt_client(self, client_id):
        """Record which browser tab's prompt this chat is now answering, so
        the reply is spoken only there - see the last_prompt_client docstring
        in __init__. Called on every POST /input that names a client, even a
        command, so a device that's merely /model-ing a chat still becomes
        the one it's speaking to. A blank/missing id (no localStorage yet,
        e.g. an old tab open from before this existed, or the terminal's own
        input) leaves the chat's last known client alone rather than clearing
        it - the alternative would silence every window the moment one without
        an id spoke."""
        if not client_id:
            return
        self.last_prompt_client = client_id
        self._write_settings()

    def set_temperature(self, temperature):
        """Fix this agent's temperature and write it into its settings .json -
        the /temperature equivalent of pin()."""
        self.temperature = temperature
        self._write_settings()

    def unpin_temperature(self):
        """Drop back to following the settings default temperature."""
        self.temperature = None
        self._write_settings()

    def set_safety(self, safety, prompt=None):
        """This agent's own answer to the safety gate, written into its settings
        .json. `safety` is True/False to force the check on/off for this agent
        alone, or None to follow the settings page. `prompt` is its own vetting
        prompt, or None to use the global one - passed separately because the
        two are independent: a job can keep the check on and only soften the
        prompt, or turn the check off entirely and keep the prompt on file for
        when it goes back on."""
        self.safety = safety
        self.safety_prompt = prompt
        self._write_settings()

    def set_safety_threshold(self, threshold):
        """Put this chat on a safety number - 0 to 10, or None to follow the
        settings page's default again. The slider in the corner of the chat
        window, and nothing else, writes this.

        Setting it also clears the old True/False `safety` flag on this chat.
        The two answer the same question and the number is the better answer,
        so leaving a stale flag behind would mean a chat whose file says both -
        harmless while the number is set (threshold_for reads it first) and
        quietly wrong the moment it is cleared, which is exactly when nobody
        is thinking about it."""
        self.safety_threshold = tool_validation.clamp(threshold)
        self.safety = None
        self._write_settings()

    def set_safety_text(self, extra=None, prompt=None):
        """This chat's own words for the safety check: `extra` added to the
        shared prompt, `prompt` replacing it outright. Either may be None or
        blank, which clears that one and goes back to the shared wording.

        Both are written together because the dropdown edits them on one panel
        and a save there is one save. They are independent settings, though -
        a chat can add a rule to the shared prompt, or replace the prompt, or
        do both (extra is appended to a custom prompt exactly as it is to the
        shared one, see tool_validation._compose)."""
        self.safety_extra = (extra or "").strip() or None
        self.safety_prompt = (prompt or "").strip() or None
        self._write_settings()

    def safety_state(self):
        """What this chat's safety gate is set to, resolved - (threshold, own),
        where `own` says whether that number is this chat's own choice or the
        settings default it is following. The page draws both: the number, and
        whether clearing it would change anything."""
        return (tool_validation.threshold_for(self.safety_threshold, self.safety),
                self.safety_threshold is not None or self.safety is not None)

    def set_workspace(self, wsid):
        """Move this agent to a workspace - the id of one out of WORKSPACES in
        .env, or None to follow whichever is the default. Written into its own
        settings .json like every other per-chat setting, so it sticks to the
        chat and survives a restart, and read back by reload_model() at the top
        of every turn.

        Only the id is stored - never the root or the ssh destination behind it.
        Those are config that can change under a chat filed here months ago (see
        the SETTINGS_KEYS note on `workspace`)."""
        self.workspace = wsid or None
        self._write_settings()

    def set_started(self, when):
        """Record when this chat's cron run fired, in its settings .json. Only
        cron.new_run() calls it, once, on a chat it has just created."""
        self.started = when
        self._write_settings()

    def rename(self, name):
        """Give this chat an explicit title, written into its settings .json -
        the /name equivalent of pin(). Wins over the auto-derived label
        (the chat's first message) in the chats panel - see server.py's
        _chats()."""
        self.name = name
        self._write_settings()

    def clear_name(self):
        """Drop back to the auto-derived label."""
        self.name = None
        self._write_settings()

    def add_pinned(self, label, text):
        """Pin one tool/skill's content into just THIS chat - written straight
        into this agent's own settings .json (see SETTINGS_KEYS), never into
        context/ or any other chat's file, so it only ever reaches this one
        conversation's system prompt (see injection_breakdown()'s `pinned`
        param). Appended, so pin order is display order - whatever was just
        pinned is always last, the "latest addition" a global pinned/ folder
        would have given by file mtime, but scoped correctly this time."""
        self.pinned = (self.pinned or []) + [{"label": label, "text": text}]
        self._write_settings()

    def record_usage(self, usage, model_key=None):
        """Remember this agent's most recent real token usage and persist it
        to the settings .json - a wholesale replace, not a merge, same as
        _set_usage's in-memory copy (see that function's docstring on why a
        tool-call turn can leave output_tokens missing and that's read
        honestly rather than carried over from an older turn). This is what
        last_usage() falls back to for a chat that hasn't answered yet this
        session, so the count shows the moment the chat is opened.

        `model_key` is the "<wire>/<model>" that produced these counts - built
        by model_key(), which is where the shape and the reason for it are.
        Kept with them because a count is only meaningful against the model it
        was taken on: move the chat to another model and context_usage() has
        to ignore it rather than draw a Sonnet count against a 4k local
        window."""
        self.input_tokens = usage.get("input_tokens")
        self.output_tokens = usage.get("output_tokens")
        self.tokens_model = model_key
        self.tokens_at = datetime.now().isoformat(timespec="seconds")
        self._write_settings()

    def record_context(self, model_key, count, window, exact=True):
        """Remember the projected input size for this chat, so opening it
        paints the bar from its own .json rather than from nothing. Written
        only when the number actually changes - the panel polls every couple
        of seconds and an unchanged count must not mean a disk write every
        couple of seconds.

        `exact` is whether the model's own tokenizer produced this count
        (tokens.measure's "exact"); recorded with it so a bar drawn from this
        file later says "~" in exactly the cases the live one would, rather
        than quietly presenting an approximation as a real count."""
        if (self.context_input == count and self.context_max == window
                and self.context_model == model_key
                and self.context_exact == exact):
            return
        self.context_input = count
        self.context_max = window
        self.context_model = model_key
        self.context_exact = exact
        self._write_settings()

    def reload_tokens(self):
        """Re-read just the token block from the settings .json.

        The counts in memory are only this process's. A cron job's chat is run
        by cron.py, a SEPARATE process, which writes its counts to the same
        file - so without this the web server would keep answering with
        whatever it happened to load when it first opened that chat. Same
        reason reload_model() exists for the model.

        A settings file is written whole (not appended), so a read that lands
        mid-write parses as nothing at all. That is a passing failure of the
        read, not the chat losing its counts, so an empty result is ignored
        rather than blanked in."""
        cfg = self._read_settings()
        if not cfg:
            return
        for key in ("input_tokens", "output_tokens", "tokens_model", "tokens_at",
                    "context_input", "context_max", "context_model", "context_exact"):
            setattr(self, key, cfg.get(key))

    def save(self):
        # The transcript only - settings live in their own file, written when
        # they change (pin/unpin), not on every save of the history.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.history, encoding="utf-8")
        # When each turn landed, into its own file outside the chat folder -
        # never into the transcript, which is the thing that goes to the
        # provider (see STAMPS). Done HERE because this line is the one place
        # every writer of a history passes through: the turn loop's sync,
        # append_error, note_turn, a stopped turn's rewrite, /compact, and
        # cron's first empty save all end up on it.
        stamp_history(self.id, self.history)


class Chat(Agent):
    """A conversation the user types into. Unpinned by default, so a fresh chat
    follows the settings-page model until /model pins it to its own."""

    kind = "chat"


# Every agent opened this run, one object per file, keyed by flat id. Two
# callers asking for the same one must get the same object - separate copies
# would each hold their own history and turn slot, and two turns could scramble
# the file.
_open = {}
_open_lock = threading.Lock()


def open_agent(cid):
    """The already-open Agent with this flat id, or None. Deliberately does not
    open one: the callers that want this (request_stop) are asking about
    something RUNNING, and nothing can be running in an agent no thread has
    opened yet."""
    with _open_lock:
        return _open.get(cid)


def live_workspace(chat_id, fallback=None):
    """The workspace this chat is in RIGHT NOW, not when its turn started.

    A turn is not an instant: /workspace, or the picker in the corner of the
    chat window, can move the chat half way through one, and every tool call
    after that has to land in the new place. Reading
    the id once at the top of run() meant the move was written, reported, and
    then ignored until the next message - so the model would say "moved to the
    Pi", run the next command on the machine it had just left, and be telling
    the truth about the part it could see. That is the worst shape a bug can
    have: everything says it worked.

    Off the open Agent rather than off its .json, because that object IS the
    chat while a turn is running and every mover writes through it. `_open`
    is keyed by the same flat id tools are handed, so no path juggling here -
    and a chat that somehow isn't open falls back to what the turn began with.
    """
    a = _open.get(chat_id)
    return a.workspace if a is not None else fallback


def agent(path, cls=Chat, **kw):
    """The one Agent object for `path`, opened on first use. `cls` picks which
    kind to build the first time (Chat, CronJob, Subagent); a later call for the
    same path gets whatever was built then, so pass it consistently."""
    path = Path(path)
    with _open_lock:
        cid = chat_id(path)
        a = _open.get(cid)
        if a is None:
            a = _open[cid] = cls(path, **kw)
        return a


def chat(path):
    """The Chat for `path` - the common case, kept as its own name so the many
    existing callers read plainly."""
    return agent(path, Chat)


def attachments_dir(agent):
    """Where `agent`'s attached files go. Not created here - the uploader makes
    it when a file actually arrives, so a chat nobody attached anything to
    never grows an empty folder."""
    return agent.path.parent / ATTACHMENTS_DIR


def chat_md(cid):
    """The transcript path for a chat ROUTE (see chat_route). 'chat-1bb28a87'
    lives at chats/chat-1bb28a87/history.json; one run of a cron job carries
    its folders, 'cron/ai-news/003' -> chats/cron/ai-news/003/history.json."""
    return CHATS.joinpath(*cid.split("/"), HISTORY_FILE)


# "[User Tue 2026-07-21 13:55] hi" or the older bare "User: hi"; the agent side
# is "Uniagent [Tue 2026-07-21 13:55]: hi" or "Uniagent: hi". Anything else is a
# continuation line of the turn above it.
_FLAT_USER = re.compile(r"^(?:\[User [^\]]*\]|User:) ?(.*)")
_FLAT_AGENT = re.compile(r"^Uniagent(?: \[[^\]]*\])?: ?(.*)")


def _turns_from_flat(text):
    """An old flat-text transcript as the turns list the JSON format holds.

    Only user and assistant turns survive: the flat format never recorded tool
    calls or their results as anything a machine can pull back apart, so a
    transcript that had them keeps the conversation and loses the mechanics.
    That is still strictly more than before - run_turn() used to hand a
    flat-text chat an EMPTY turns list (it isn't valid JSON), so the whole
    history silently vanished the moment you sent another message in it."""
    turns = []
    for line in text.splitlines():
        m = _FLAT_USER.match(line)
        role = "user"
        if not m:
            m = _FLAT_AGENT.match(line)
            role = "assistant"
        if m:
            turns.append({"role": role, "content": m.group(1)})
        elif turns:
            turns[-1]["content"] += "\n" + line  # continuation of the turn above
    for t in turns:
        t["content"] = t["content"].strip()
    return [t for t in turns if t["content"]]


def _migrate_json_names():
    """One-off: <id>.md + <id>.json -> history.json + settings.json inside each
    chat folder, converting a still-flat transcript to the JSON turns format on
    the way. Runs at import after _migrate_layout, and skips any folder already
    holding a history.json, so it's safe to run every start.

    Order matters: the settings file moves FIRST. Both old names differ only by
    suffix, so writing the transcript to <id>.json before moving the settings
    aside would land it straight on top of them."""
    folders = [p for p in CHATS.glob("chat-*") if p.is_dir()] \
        + [p for p in (CHATS / "cron").glob("*") if p.is_dir()]
    for folder in folders:
        if (folder / HISTORY_FILE).exists():
            continue  # already migrated
        old_json = folder / (folder.name + ".json")
        old_md = folder / (folder.name + ".md")
        if not old_md.is_file():
            # No transcript: a chat that was opened and never spoken to, so all
            # it has is settings. Still rename those, so no old-shaped name is
            # left anywhere on disk to be read by mistake later.
            if old_json.is_file():
                old_json.rename(folder / SETTINGS_FILE)
            continue
        if old_json.is_file():
            old_json.rename(folder / SETTINGS_FILE)
        text = old_md.read_text(encoding="utf-8")
        try:
            turns = json.loads(text)
            if not isinstance(turns, list):
                raise ValueError("not a turns list")
        except (json.JSONDecodeError, ValueError):
            turns = _turns_from_flat(text)  # a legacy flat-text transcript
        (folder / HISTORY_FILE).write_text(json.dumps(turns, indent=2), encoding="utf-8")
        old_md.unlink()


def _migrate_layout():
    """One-off: move the old flat chats/<id>.md files into a folder each, with
    their model split out into a settings .json beside the transcript. Runs at
    import; skips anything already migrated, so it's safe to run every start.

    Per the changeover, every migrated chat is set to deepseek-v4-flash - the
    old per-chat model header (a brief experiment) is not carried over; only a
    chat's name, if it had one, is kept."""
    old = list(CHATS.glob("chat-*.md")) + list((CHATS / "cron").glob("*.md"))
    for flat in old:
        if not flat.is_file():
            continue
        cid = flat.stem
        folder = flat.parent / cid
        if folder.exists():
            # Already migrated. A leftover flat file here is a stale empty one
            # (an old still-running process recreated it against the old path
            # before it was restarted) - the folder is the real chat, so clear
            # the empty straggler. Never touch a non-empty one.
            try:
                if flat.stat().st_size == 0:
                    flat.unlink()
            except OSError:
                pass
            continue  # already migrated
        text = flat.read_text(encoding="utf-8")
        name = None
        if text.startswith("<!-- uniagent"):  # strip the step-2 header if present
            end = text.find("-->")
            if end != -1:
                for line in text[:end].splitlines():
                    if line.strip().lower().startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                text = text[end + 3:].lstrip("\n")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / HISTORY_FILE).write_text(
            json.dumps(_turns_from_flat(text), indent=2), encoding="utf-8")
        cfg = {"provider": "deepseek", "model": "deepseek-v4-flash"}
        if name:
            cfg["name"] = name
        (folder / SETTINGS_FILE).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        flat.unlink()


def seed_context():
    """Put any shipped file that isn't in context/ or memories/ there. Runs at
    import, so a fresh clone - where both are gitignored and therefore absent
    entirely - comes up with a working system prompt and memory file instead of
    an empty prompt and no rule telling the agent to remember anything.

    Only ever ADDS. A file already there is the user's, edited or not, and is
    left exactly as it is: this runs on every single start, so anything else
    would overwrite their prompt every time the server restarted. Putting a
    default back on purpose is revert_preset()'s job, and that one is a button
    someone has to press."""
    for _, live, default in preset_parts():
        live.mkdir(parents=True, exist_ok=True)
        if not default.is_dir():
            continue
        for src in sorted(default.rglob("*")):
            if not src.is_file() or src.name.startswith("."):
                continue
            dst = live / src.relative_to(default)
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


# A preset's own record of itself, kept inside its folder. Dotted, so every
# reader here skips it for free: it must not count as content when presets are
# compared (_tree), and must not be copied into a live folder (_install_preset).
#
# Renaming writes here rather than renaming the folder. The folder name is the
# timestamp it was taken, and that is the one thing about a preset that is
# never allowed to drift - it is the id every other record points at, and a
# name the user can retype is not something to hang identity on.
PRESET_META = ".preset.json"


def _meta(folder):
    """A preset's metadata, filled in from the folder itself for anything
    missing - an archive made before this file existed, or one the user copied
    in by hand, still lists with a name and a date."""
    stamp = folder.name
    out = {"label": stamp, "created": "", "unloaded": ""}
    try:
        saved = json.loads((folder / PRESET_META).read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            out.update({k: v for k, v in saved.items() if k in out and isinstance(v, str)})
    except (OSError, ValueError):
        pass
    if not out["created"]:
        try:
            out["created"] = datetime.fromtimestamp(
                folder.stat().st_mtime).isoformat(" ", "seconds")
        except OSError:
            pass
    # A preset that has never been explicitly unloaded was archived the moment
    # it stopped being live, so that is when it came out of context.
    if not out["unloaded"]:
        out["unloaded"] = out["created"]
    return out


def _write_meta(folder, **fields):
    meta = _meta(folder)
    meta.update(fields)
    try:
        (folder / PRESET_META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        pass
    return meta


def _tree(folder):
    """{relative path: bytes} for every file in `folder`, or {} if it isn't
    there. Dotfiles skipped, so a .gitkeep holding an otherwise-empty default
    folder open doesn't count as content and make a pristine install look
    edited."""
    out = {}
    if not folder.is_dir():
        return out
    for p in folder.rglob("*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            out[p.relative_to(folder).as_posix()] = p.read_bytes()
        except OSError:
            continue
    return out


def is_default_preset():
    """True when the live folders are byte-for-byte the shipped defaults, and
    so hold nothing anyone could want back. What stops every reset and every
    preset switch from leaving another identical copy of the defaults in the
    archive, which is noise the list has to be read past forever."""
    return all(_tree(live) == _tree(default) for _, live, default in preset_parts())


def _matching_preset(tree=None):
    """The archived preset holding exactly this content, or None. `tree`
    defaults to what is live now."""
    want = tree if tree is not None else {part: _tree(live)
                                          for part, live, _ in preset_parts()}
    for folder in (ARCHIVE.iterdir() if ARCHIVE.is_dir() else []):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if all(_tree(folder / part) == want.get(part, {})
               for part, _, _ in preset_parts()):
            return folder
    return None


def archive_preset():
    """Take the live folders out of use and keep them as a preset, leaving
    fresh empty ones behind. Returns the preset folder, or None if there was
    nothing worth keeping (see is_default_preset).

    Nothing is ever deleted, which is what makes a one-button reset of the
    agent's entire memory a reasonable thing to offer at all: every fact it had
    is sitting in a dated folder, readable, and can be put back.

    Content already held by a preset does NOT get a second folder - that preset
    is stamped as unloaded just now and reused. Otherwise switching back and
    forth between two presets would breed a near-identical copy on every swap,
    and "last unloaded" would be a fiction: each copy would look like it had
    come out of context exactly once, at the moment it was made."""
    if is_default_preset():
        return None
    now = datetime.now().isoformat(" ", "seconds")
    same = _matching_preset()
    if same is not None:
        _write_meta(same, unloaded=now)
        for _, live, _ in preset_parts():
            if live.exists():
                shutil.rmtree(live)
            live.mkdir(parents=True, exist_ok=True)
        return same
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    dest = ARCHIVE / stamp
    # Same second, second press: never land on top of an existing archive.
    n = 2
    while dest.exists():
        dest = ARCHIVE / (stamp + "-" + str(n))
        n += 1
    dest.mkdir(parents=True)
    for part, live, _ in preset_parts():
        if live.exists():
            shutil.move(str(live), str(dest / part))
        live.mkdir(parents=True, exist_ok=True)
    _write_meta(dest, label=stamp, created=now, unloaded=now)
    return dest


def _install_preset(source):
    """Replace the live folders with `source`'s copy of them. A part missing
    from source (an archive taken before memories/ was part of a preset, say)
    leaves that folder empty rather than untouched - a preset is the whole
    state, so a half-applied one would be neither what was there nor what was
    asked for."""
    for part, live, _ in preset_parts():
        if live.exists():
            shutil.rmtree(live)
        src = source / part
        if src.is_dir():
            # Dotfiles left behind, the same as everywhere else here reads
            # these folders: the .gitkeep that holds an empty defaults/memories
            # open in the repo is scaffolding, and copying it into a live
            # install would put a stray file in a folder the user opens.
            shutil.copytree(src, live, ignore=shutil.ignore_patterns(".*"))
        else:
            live.mkdir(parents=True, exist_ok=True)


def revert_preset():
    """Reset context/ AND memories/ to the shipped defaults, archiving what was
    there first. A hard replacement: a file the user added themselves - a
    10tools.md, say - is archived with the rest and NOT restored, because it is
    not part of what "the default" means.

    Returns the archive folder, or None when the live state was already the
    defaults and there was nothing to keep."""
    if not DEFAULT_CONTEXT.is_dir():
        raise FileNotFoundError("no defaults to revert to: " + str(DEFAULT_CONTEXT))
    archived = archive_preset()
    _install_preset(DEFAULTS)
    return archived


def preset_path(name):
    """The archived preset `name` points at, or None if it names anything else.
    Resolved and re-checked against archive/, the same guard _context_path uses,
    so a name like ../../.ssh can't be read or copied over the live folders."""
    if not name or name.startswith("/") or "\x00" in name:
        return None
    path = (ARCHIVE / name).resolve()
    if ARCHIVE.resolve() not in path.parents or not path.is_dir():
        return None
    return path


def presets():
    """Every saved preset, most recently out of use first: its id, the name it
    was given, when it was taken, when it last came out of context, and how
    many files each half holds - enough for the settings page to list them
    without reading every file in every preset.

    `live` marks the one whose content is loaded right now, if any, so the list
    can say "in use" instead of showing it as something to go back to."""
    if not ARCHIVE.is_dir():
        return []
    here = {part: _tree(live) for part, live, _ in preset_parts()}
    out = []
    for p in sorted(ARCHIVE.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        meta = _meta(p)
        out.append({"name": p.name,
                    "label": meta["label"],
                    "created": meta["created"],
                    "unloaded": meta["unloaded"],
                    "live": all(_tree(p / part) == here[part]
                                for part, _, _ in preset_parts()),
                    "counts": {part: len(_tree(p / part))
                               for part, _, _ in preset_parts()}})
    out.sort(key=lambda d: d["unloaded"], reverse=True)
    return out


def rename_preset(name, label):
    """Give a preset a name of its own. The folder keeps its timestamp id -
    only the label moves, so nothing that points at this preset breaks and the
    date it was taken stays honest."""
    folder = preset_path(name)
    if folder is None:
        raise FileNotFoundError("no such preset: " + str(name))
    label = " ".join(str(label).split())[:80] or folder.name
    return _write_meta(folder, label=label)


def preset_files(name):
    """Every file in a preset, with its text - what the settings page shows
    when a saved preset is expanded to be read through before loading it."""
    folder = preset_path(name)
    if folder is None:
        raise FileNotFoundError("no such preset: " + str(name))
    out = {}
    for part, _, _ in preset_parts():
        files = []
        # Same numeric order the model is fed them in, not plain alphabetical,
        # so reading a preset here matches how it would actually be loaded.
        items = sorted(_tree(folder / part).items(),
                       key=lambda kv: [_context_order(s) for s in kv[0].split("/")])
        for rel, body in items:
            try:
                files.append({"path": rel, "text": body.decode("utf-8")})
            except UnicodeDecodeError:
                files.append({"path": rel, "text": "(not text)"})
        out[part] = files
    return out


def restore_preset(name):
    """Swap an archived preset back in, archiving the live one on the way past
    - unless it is exactly the shipped defaults, in which case there is nothing
    in it worth another folder.

    The archive being restored is COPIED, not moved, so it stays in the list
    and can be swapped back to again. Returns (archive folder or None), and
    raises if `name` isn't a real preset."""
    source = preset_path(name)
    if source is None:
        raise FileNotFoundError("no such preset: " + str(name))
    # Restoring what is already live: a no-op that would otherwise archive a
    # copy of the preset next to the preset it came from.
    if all(_tree(source / part) == _tree(live) for part, live, _ in preset_parts()):
        return None
    archived = archive_preset()
    _install_preset(source)
    return archived


seed_context()         # a missing context or memory file <- its shipped default
_migrate_layout()      # flat chats/<id>.md  -> a folder per chat
_migrate_json_names()  # <id>.md/<id>.json   -> history.json/settings.json


def busy_chats():
    """Ids of every open chat that is mid-turn right now.

    A chat /stop has abandoned is NOT in here, even if the thread that was
    running it is still winding down: request_stop() takes the slot back as it
    stops the turn, so this goes false at once and the page's "main agent
    working" bar goes with it."""
    with _open_lock:
        return sorted(cid for cid, c in _open.items() if c.slot.held())


# The chat the TERMINAL is looking at - where a line typed into cli.py, and a
# voice clip with no browser behind it, goes. It is NOT "the chat the app is
# showing": every browser window tracks its own, sends it with each request,
# and the server reads it off the wire (see server.py's _chat_named). Two
# devices can therefore sit in different chats, and neither moves the other.
current = None

# Whatever `current` was just before the switch to it - one level of "back",
# so deleting the chat you're in can return you to wherever you were.
previous = None


def _has_conversation(c):
    """Whether anything was ever actually said in `c`. Pins and a pinned model
    deliberately don't count: those are set-up FOR a conversation, and set-up
    nobody went on to use is exactly what discard_if_untouched() is about."""
    try:
        turns = json.loads(c.history) if c.history else []
    except json.JSONDecodeError:
        return True  # unparseable but not empty (an old flat-text chat) - never throw that away
    return bool(turns)


def discard_if_untouched(keep=None):
    """Delete the current chat, for good, if nothing was ever said in it - the
    other half of new_chat() writing one to disk up front. A chat that gets set
    up and then left (pins added, model picked, nothing said) is scratch: it
    goes when you move on, rather than filling the sidebar with empty rows, or
    deleted_chats/ with things that never held a conversation. Returns the id
    thrown away, or None if the chat was kept.

    Never touches: a chat with turns in it, one mid-turn (a turn in flight has
    already written the user's line - see run()'s first sync() - but this
    guards the moment before that lands anyway), anything that isn't a
    chats/chat-*/ folder - a cron run (chats/cron/<job>/<nnn>/) can be the loaded
    chat too, and it owns its own lifecycle - or `keep`, the chat being
    switched TO, which obviously has to survive being switched to.

    This is a real delete, not a move into deleted_chats/: there is nothing in
    an untouched chat to want back, and filing them there would only mean two
    piles of empty chats instead of one."""
    global previous
    c = current
    if c is None or c is keep or c.kind != "chat":
        return None
    if c.slot.held() or _has_conversation(c):
        return None
    folder = c.path.parent
    if folder.exists():
        # Guard the delete hard - it's the only irreversible thing in here.
        # Anything that isn't one of our own chats/chat-*/ folders is left
        # exactly as it is, registry included: dropping it from _open without
        # deleting it would let the next chat() call build a SECOND Agent for
        # the same files, with its own turn slot, which is the one thing _open
        # exists to prevent.
        if not (folder.name.startswith("chat-")
                and folder.parent.resolve() == CHATS.resolve()):
            return None
        try:
            shutil.rmtree(folder)
        except OSError:
            return None  # still on disk, so it is still a chat - keep it
    # No folder at all is the normal case for the chat main.py opens at import
    # (see new_chat's `persist`) - nothing to delete, but still nothing to keep.
    with _open_lock:
        _open.pop(c.id, None)
    if previous is not None and previous == c.path:
        previous = None
    return c.id


def load(path):
    """Make the chat at `path` the current one and return its history."""
    global current, previous
    new_current = chat(path)
    discarded = discard_if_untouched(keep=new_current)
    # Don't offer a chat that was just thrown away as somewhere to go "back" to.
    if current is not None and current is not new_current and not discarded:
        previous = current.path
    current = new_current
    return current.history


def new_chat_id():
    """A fresh chat id, and nothing else - no Agent, no folder, no disk.

    What the web front-end asks for when it needs to name a chat that does not
    exist yet. The folder appears on the first real write into it (save() and
    _write_settings() both mkdir), so a chat that gets minted and then never
    used costs nothing and leaves nothing behind - which is the whole point:
    opening the app used to call /new and persist an empty chat on EVERY page
    load, and each one that was never typed into was clutter in the list.

    The shape is fixed, and server.py checks it before letting a client create
    anything under this name (see _chat_named): 'chat-' plus eight hex digits
    can only ever land on chats/chat-xxxxxxxx/, never in cron/, subagents/ or
    any of the other folders that live alongside the chats."""
    return "chat-" + uuid.uuid4().hex[:8]


def new_chat(persist=True):
    """Start a fresh chat and make it the TERMINAL's current one - cli.py's
    /new, not the web's (that mints an id with new_chat_id() and keeps it in
    the browser).

    `persist` writes its folder and an empty history.json NOW, rather than
    holding both back until the first message. That's what makes a new chat a
    real one you can set up before talking to it: the sidebar lists what's on
    disk, so an unwritten chat is invisible there (the previous chat stays
    highlighted as if nothing happened) and a pin made into it has nowhere to
    be saved. Left with nothing said in it, it's deleted again on the way out -
    see discard_if_untouched().

    persist=False is for the one at the bottom of this module, created when
    main.py is IMPORTED: every process that imports main - the cron watcher,
    the CLI, a tool that pulls it in - would otherwise leave an empty chat
    behind on disk just for starting up."""
    global current, previous
    discarded = discard_if_untouched()
    if current is not None and not discarded:
        previous = current.path
    current = chat(chat_md(new_chat_id()))
    if persist:
        current.history = "[]"  # an empty turns list, the shape every reader expects
        current.save()


def landing_chat_id(prefer=None):
    """Where to go after deleting the chat you were in, WITHOUT moving anyone
    there: `prefer` if it's still on disk, otherwise the most recently touched
    chat, otherwise None for "nothing left, start fresh".

    Split out from load_previous_or_recent() because the answer and the act of
    going there now belong to different places. The terminal has one current
    chat and load_previous_or_recent() still moves it; a browser window has its
    own, so it is told the id and switches itself."""
    if prefer is not None and Path(prefer).exists():
        return chat_id(prefer)
    candidates = list(CHATS.glob("chat-*/" + HISTORY_FILE))
    if not candidates:
        return None
    return chat_id(max(candidates, key=lambda p: p.stat().st_mtime))


def load_previous_or_recent():
    """Land on the chat open just before this one, if it still exists;
    otherwise the most recently touched chat still on disk; a fresh one if
    none are left. Returns the id landed on. Used after deleting the chat you
    were in, which must never leave the UI pointed at a chat that's gone."""
    global previous
    landing = previous
    previous = None
    if landing is not None and landing.exists():
        load(landing)
        return current.id
    candidates = list(CHATS.glob("chat-*/" + HISTORY_FILE))
    if candidates:
        load(max(candidates, key=lambda p: p.stat().st_mtime))
        return current.id
    new_chat()
    return current.id


# In memory only - see new_chat()'s docstring on why importing main.py must
# not put a chat on disk. This is the TERMINAL's starting chat; the web front-
# end never touches it, since each browser window carries its own chat id and
# sends it with every request.
new_chat(persist=False)

# How a background worker's note (a subagent's report) re-enters conversation,
# called as notify(note, chat_file). The note goes to the chat that spawned
# the work - NOT whichever chat is loaded when it lands. Whichever front-end
# is running points this at its turn runner: main() below runs the turn
# directly, server.py streams it to the page. Cron registers nothing: it has
# no live chat, so reports stay in their transcript files.
notify = None

# How a front-end is told a chat's turn is OVER because /stop ended it, called
# as on_stop(stem) from inside request_stop() - on the stopping thread, before
# it returns, and after the transcript has already been closed out and the chat
# handed on. By the time this runs the turn is finished as far as everything
# except one doomed thread is concerned, so a front-end should treat it exactly
# as it treats a turn ending normally: seal the bubble, drop the busy bar, send
# whatever was queued behind it. None when nothing is watching.
on_stop = None

# How a front-end restarts its own process, called as on_restart() with no
# args. server.py points this at execv-ing a fresh python process (so edited
# code takes effect); cli.py registers nothing, so /restart there just says
# it isn't available. None when nothing is watching.
on_restart = None

# Which chat the turn running on THIS thread belongs to. Thread-local because
# turns in different chats run concurrently, so a global would lie to one of
# them. Tools that need to know which conversation called them (subagent)
# read it through turn_chat().
_turn_chat = threading.local()


def turn_chat():
    """The chat file of the turn running on this thread, else the current
    chat's - for tools asking which conversation they belong to."""
    c = getattr(_turn_chat, "chat", None)
    return (c or current).path


# Everything stopped since it last started, by key - a chat's stem for its own
# turn, a subagent's thread tag for a subagent's. This is the coarse, sticky
# record ("was this stopped?"), NOT the mechanism: the real work is done by the
# turn's context in turnctx, which breaks open the blocking call the turn is
# actually sitting in. This set survives the context being dropped, which is
# what lets a caller ask after the fact (subagent.py's report, run()'s loop).
_stops = set()
_stops_lock = threading.Lock()


def _no_result_yet(turns):
    """Tool call ids in the last assistant turn that nothing has answered.

    Only ever non-empty when a turn was cut short between making a call and
    recording its result - which the old cooperative stop could not produce
    (it only ever gave up at the top of a pass) and abandoning the thread very
    much can. Left unanswered, the next request carries an assistant turn
    holding a tool_call with no matching tool result, which the strict
    providers reject outright and the lenient ones simply misread."""
    if not turns or not turns[-1].get("tool_calls"):
        return []
    return [tc.get("id") for tc in turns[-1]["tool_calls"] if tc.get("id")]


def _partial_turn(content, thinking, phases, provider_name="", model="", usage=None):
    """The assistant turn a response that did not finish leaves behind: what it
    managed to say, what it was thinking, and how long it had been at it.

    Used by the two ways a response ends early - the user stopped it, or it
    blew up - because they need exactly the same thing and used to do neither.
    An interrupted response really happened: it waited on the provider, it
    thought, it wrote some of an answer, and every one of those is as
    measurable and as worth keeping as the words it got out. Before this both
    paths wrote the turn as bare content, so the redraw a second later showed
    no thinking and no timing, and an interrupted turn looked like one the
    model had never thought about at all.

    The token counts are local estimates whenever the provider never reached
    its final usage event, which an interrupted request essentially never does
    - that is exactly the third case _split_output is written for. A caller
    with no provider or model name to give (the stop path runs on the stopping
    thread, which knows the chat but not what it was talking to) falls back to
    the same general-purpose encoding tokens.estimate already uses for every
    local model; naming them would not make this more exact than it honestly
    is."""
    turn = {"role": "assistant", "content": content}
    if phases is not None:
        spent = phases.as_dict(*_split_output(provider_name, model, thinking,
                                              content, usage or {}))
        if spent:
            turn["timing"] = spent
    if thinking:
        turn["reasoning_content"] = thinking
    return turn


def _stopped_history(ctx, fallback):
    """The transcript a stopped turn leaves behind, built by whoever STOPPED it
    rather than by the turn itself.

    This is what makes the stop instant instead of merely requested: the chat
    is closed out here and now, on the stopping thread, so nothing waits on the
    abandoned worker reaching a safe point to write the same thing.

    It reproduces exactly what the cooperative path used to write, plus the two
    cases only abandonment can reach: text streamed but not yet folded into the
    turns list, and a tool call left hanging without its result. `fallback` is
    the chat's history as last saved, used when the turn had not published its
    working list yet (it was still setting up)."""
    turns = list(ctx.turns) if ctx.turns is not None else None
    if turns is None:
        # Stopped in the moment between taking the chat and building the turns
        # list. The history on disk is everything up to this turn, so the
        # message being answered has to be put back by hand - without it the
        # transcript would show a stop with nothing before it to explain what
        # was stopped.
        try:
            turns = json.loads(fallback) if fallback else []
        except json.JSONDecodeError:
            turns = []
        if ctx.text:
            turns.append({"role": "user", "content": ctx.text})

    # Whatever the current response had streamed before the stop - the words,
    # the thinking behind them, and the clock that was running on both.
    # Compared against the last turn rather than trusted blindly: run() may
    # have folded this very text in already (the two happen on different
    # threads and either order is possible), and appending it twice would show
    # the reply twice.
    #
    # Kept even when NOTHING was said, as long as something was thought. A
    # model that streams its whole answer on the reasoning channel - which is
    # what several local builds do - has produced nothing else to keep at the
    # moment it is stopped, and dropping the turn there loses the entire
    # response along with every measurement of it.
    partial = ctx.partial
    thinking = getattr(ctx, "thinking", "") or ""
    if (partial and partial.strip()) or thinking:
        if not turns or turns[-1].get("content") != partial:
            turns.append(_partial_turn(partial, thinking,
                                       getattr(ctx, "phases", None)))

    for call_id in _no_result_yet(turns):
        turns.append({"role": "tool", "tool_call_id": call_id,
                      "content": STOPPED_CALL + STOPPED_CALL_WAIT})

    turns.append({"role": "assistant", "content": STOPPED})
    return json.dumps(turns, indent=2)


def _resumable_end(turns):
    """Whether `turns` ends somewhere a turn can simply be run again from -
    i.e. not on an assistant turn, which is the one end a provider reads as
    something to continue writing rather than something to answer.

    A tool result counts, and that is the case this whole feature exists for:
    provider.py's _compat() maps a tool turn to a plain user turn ("Tool
    result: ...") before anything is sent, so a history ending on one already
    ends on a user turn as far as every provider is concerned."""
    return bool(turns) and turns[-1].get("role") in ("user", "tool")


def continue_from(c):
    """Wind `c`'s history back to where a stopped or failed turn dropped it, so
    the next turn can run with NO new message and pick the work up mid-stride.
    Returns None when the chat is ready to run, or the reason it isn't.

    What comes off is only ever bookkeeping. A stopped turn ends with an
    assistant STOPPED turn and a failed one with an assistant TURN_ERROR turn
    (see _stopped_history and append_error); both are filed as assistant turns
    so they render and so the model reads them, but neither is anything the
    agent said - final_answer() draws exactly the same line. Taking them off
    leaves the history ending where the work actually stopped:

      mid tool loop      a tool result       -> run again, nothing added
      the request failed the user's message  -> run again, a clean retry
      mid sentence       an assistant turn   -> CONTINUE_NUDGE, then run
      a call left hanging an assistant call  -> its STOPPED result, then run

    The last two are the only ones that add anything, and the nudge is there
    for the provider's sake rather than the model's: see CONTINUE_NUDGE.

    A call the stop cut short keeps its "unknown whether it ran, check rather
    than assume" result - that is still true and still worth reading - but the
    tail telling the model to wait for the user is swapped for the one telling
    it to go on, since the user asking to continue is that instruction arriving.

    Refuses a chat that is mid-turn: the running turn holds the history in
    memory and rewrites the file after every step, so anything written here
    would be overwritten a moment later and the continue would silently do
    nothing."""
    if c.slot.held():
        return "this chat is already working - stop it first if you want it to start again."
    try:
        turns = json.loads(c.history) if c.history else []
    except json.JSONDecodeError:
        # A pre-JSON flat-text chat. There is no structure to wind back, and
        # guessing at where a turn ended in flat text is how transcripts get
        # mangled - say so rather than half-do it.
        return "this chat is in the old flat-text format, which can't be continued."
    if not isinstance(turns, list) or not turns:
        return "there is nothing in this chat to continue."

    cut = 0
    while turns and _is_marker(turns[-1]):
        turns.pop()
        cut += 1
    if not cut:
        return "this chat isn't part way through anything - its last turn finished."
    if not turns:
        return "there is nothing left to continue - the whole chat was a turn that never ran."

    # The swap described above, over every hanging-call result the stop left -
    # there is one per call that was in flight, which is usually one and can be
    # several.
    for turn in reversed(turns):
        if turn.get("role") != "tool":
            break
        content = turn.get("content")
        if isinstance(content, str) and content == STOPPED_CALL + STOPPED_CALL_WAIT:
            turn["content"] = STOPPED_CALL + STOPPED_CALL_GO

    if not _resumable_end(turns):
        # An assistant turn last. A call with no result behind it is answered
        # the way a stop answers one, so the model knows the call's fate is
        # unknown; anything else is prose cut off mid-flow, and only needs
        # something in the user's voice after it for the request to be a
        # request rather than a prefill.
        hanging = _no_result_yet(turns)
        for call_id in hanging:
            turns.append({"role": "tool", "tool_call_id": call_id,
                          "content": STOPPED_CALL + STOPPED_CALL_GO})
        if not hanging:
            turns.append({"role": "user", "content": CONTINUE_NUDGE})

    c.history = json.dumps(turns, indent=2)
    c.save()
    return None


def _is_marker(turn):
    """Whether `turn` is one of the two "this turn did not finish" markers -
    the only thing continue_from() takes off a history. Deliberately narrow: an
    assistant turn that made a tool call is never a marker however its text
    reads, and neither is a reply that merely happens to quote one of these."""
    if not isinstance(turn, dict) or turn.get("role") != "assistant":
        return False
    if turn.get("tool_calls"):
        return False
    text = turn.get("content")
    if not isinstance(text, str):
        return False
    return text.strip() == STOPPED or text.startswith(TURN_ERROR)


def voice_message(parts, first=True):
    """`parts` - the pieces of one spoken message, in the order they were said
    - as the single message that goes into the history.

    `first` is whether the opening piece is in `parts`. It is not when a
    continuation could not be merged (see voice_rewind): the earlier words are
    already in the transcript above, so what goes now is the late half alone,
    and every line of it is a continuation of something already said."""
    lines = [(VOICE_INPUT if (first and i == 0) else VOICE_CONTINUED) + p
             for i, p in enumerate(parts)]
    if len(lines) > 1 or not first:
        lines += ["", VOICE_NOTE]
    return "\n".join(lines)


def voice_rewind(c):
    """Take a stopped voice turn off `c`'s history, so the words that
    interrupted it replace the message rather than following it. True when the
    turn came off cleanly and the whole spoken message should be sent again,
    False when it did not and only the new words should go.

    Called after request_stop has already ended the turn, and only then: this
    edits the file, and a running turn holds the history in memory and would
    write over anything done here a moment later - the same reason
    continue_from refuses a busy chat.

    What can come off is narrow on purpose:

      the stop's own marker         always, it is bookkeeping (see _is_marker)
      whatever the model had said   prose it never finished, and which the
                                    re-sent message is about to make wrong
      the voice_input message       the thing being replaced

    and it stops dead at the first tool call. A turn that ran something changed
    the machine outside this conversation, and a history rewound past that
    would have the model do it a second time - which is a far worse outcome
    than a transcript in which the user visibly interrupted themselves. So a
    turn that got as far as calling anything keeps everything it did, the stop
    marker included, and the caller sends the late words as their own message
    on top of it."""
    if c.slot.held():
        return False
    try:
        turns = json.loads(c.history) if c.history else []
    except json.JSONDecodeError:
        # A pre-JSON flat-text chat, same as continue_from: there is no
        # structure to wind back and guessing at one mangles transcripts.
        return False
    if not isinstance(turns, list) or not turns:
        return False

    # Built up on a copy and only written if every step of it works out - a
    # half-rewound history is worse than one that was left alone.
    cut = list(turns)
    while cut and _is_marker(cut[-1]):
        cut.pop()
    while (cut and cut[-1].get("role") == "assistant"
            and not cut[-1].get("tool_calls")):
        cut.pop()
    if not cut or cut[-1].get("role") != "user":
        return False
    said = cut[-1].get("content")
    if not isinstance(said, str) or not said.startswith(VOICE_INPUT):
        # Not a spoken message under there - a subagent's report, a note, or a
        # turn this listener did not start. Leave it exactly where it is.
        return False
    cut.pop()

    c.history = json.dumps(cut, indent=2)
    c.save()
    return True


def request_stop(key):
    """Stop whatever is running under `key`, NOW - a chat's stem, or a
    subagent's thread tag.

    For a chat's own turn this does the whole job rather than asking for it:

      1. cancel the turn's context, which closes the provider connection or
         subprocess it is blocked on, so the worker thread is woken this
         instant instead of at its next voluntary check;
      2. write the stopped transcript itself (see _stopped_history), because
         the thread that would otherwise write it is being abandoned;
      3. hand the chat's slot on, so the page goes idle and anything queued
         behind this turn starts immediately;
      4. tell the front-end (on_stop) that the turn is over.

    The worker is not waited for and never joins the story again: every
    callback it holds was wrapped by turnctx.guard when the turn started, so
    once its context is cancelled it can neither stream, save, nor report. It
    exits whenever its blocked call gives up.

    A subagent has no chat to hand on, so it gets step 1 only and unwinds
    through its own report as it always has - now near-instantly, since the
    call it was stuck in is broken open too.

    Returns True if something was actually stopped."""
    with _stops_lock:
        _stops.add(key)

    ctx = turnctx.get(key)
    if ctx is None:
        return False

    # cancel() is the one-shot: a second /stop, or a stop racing the turn's own
    # ending, gets False here and must not close the transcript out twice.
    if not ctx.cancel():
        return False

    if ctx.kind != "turn":
        return True  # a subagent - no chat of its own to release

    c = open_agent(key)
    if c is None:
        return True
    try:
        c.history = _stopped_history(ctx, c.history)
        c.save()
    finally:
        # The handover happens whatever went wrong writing the transcript out.
        # A chat left owned by a turn that has already been cancelled would be
        # busy forever - no thread will ever come back to release it - and that
        # is a far worse outcome than a transcript missing its last line.
        turnctx.unpublish(ctx)
        c.slot.release(ctx)
    if on_stop:
        on_stop(key)
    return True


def stop_requested(key):
    with _stops_lock:
        return key in _stops


def clear_stop(key):
    with _stops_lock:
        _stops.discard(key)


# This chat's most recent known token usage, by stem - the real counts
# whichever provider most recently answered actually reported (never
# tokenized/estimated here - see provider.stream_response's `usage` param).
# Overwritten each pass of run()'s tool loop, so it always reflects the size
# of the LAST request actually sent - which is what "how close to the max
# context window am I" means turn to turn, not a running total across the
# whole chat's history. This in-memory copy is only ever populated by a turn
# actually run in THIS process - see last_usage()'s fallback to the agent's
# own persisted copy (Agent.record_usage) for a chat that hasn't run one yet
# this session.
_usage = {}
_usage_lock = threading.Lock()


def last_usage(stem):
    """This chat's most recent {"input_tokens", "output_tokens"} - either key
    may be missing if the provider didn't report it (see _stream()'s
    docstring on why a tool-call turn often can't get output_tokens). Falls
    back to the agent's own settings .json (Agent.record_usage) when this
    process hasn't run a turn for it yet - a chat just opened, or reopened
    after a restart, still shows its last real count rather than nothing.
    The whole dict is {} only when neither source has one."""
    with _usage_lock:
        cached = dict(_usage.get(stem, {}))
    if cached:
        return cached
    a = _open.get(stem)
    if a is None:
        return {}
    persisted = {"input_tokens": a.input_tokens, "output_tokens": a.output_tokens}
    return {k: v for k, v in persisted.items() if v is not None}


def _set_usage(stem, usage, model_key=None):
    with _usage_lock:
        _usage[stem] = usage
    a = _open.get(stem)
    if a is not None:
        a.record_usage(usage, model_key)


def context_segments(agent, provider_name, model, breakdown=None):
    """Every distinct piece of text this agent's next request would carry, as
    separate strings: the system injection (this model's list, plus this
    chat's own pins) and then each stored turn.

    Separate on purpose - tokens.measure() counts and caches one at a time,
    and the injection pieces are byte-identical across every chat on this
    model, so they are counted once for the whole app rather than once per
    chat. dynamic_reads() is deliberately NOT added: those reads are tool
    results already sitting in the history below, and counting both would
    bill them twice.

    `breakdown` is injection_breakdown()'s output when the caller already has
    it - the context panel does, and building it again would re-read every
    context file on a poll that happens every two seconds."""
    if breakdown is None:
        breakdown = injection_breakdown(provider_name, model, agent.pinned)
    parts = [p["text"] for p in breakdown if p["text"]]
    try:
        turns = json.loads(agent.history) if agent.history else []
    except json.JSONDecodeError:
        turns = []
    for t in turns:
        if isinstance(t, dict) and t.get("content"):
            parts.append(str(t["content"]))
    return parts


def model_key(provider_name, model):
    """How a recorded token count names the model it was taken on:
    "<wire>/<model>" - "deepseek/deepseek-v4-flash".

    The WIRE, not the provider's name. A count is only worth keeping while it
    still describes the model the chat is on, so this string gets compared
    against the chat's current model before the number is trusted (see
    is_model_key) - which means anything unstable in it silently throws counts
    away. The name was exactly that: renaming a provider in the settings page
    changed the string while the model, its tokenizer and its window stayed
    identical, so every count in every chat became "taken on another model"
    over a label. The wire is what the provider actually is, so it holds
    still through a rename, and it stays readable in a file a person opens -
    which the id, the other stable option, would not."""
    return provider.wire_of(provider_name) + "/" + model


def is_model_key(recorded, provider_name, model):
    """Whether a stored tokens_model/context_model names the model this chat
    runs on now. False for a chat that has nothing recorded.

    Both spellings are accepted: the wire-keyed one written today, and the
    older name-keyed one. Nothing on disk had to be migrated for that - a
    count written as "test/deepseek-v4-flash" was taken on this same model
    with this same tokenizer, and the file spelling it the old way doesn't
    make it any less true. Those settle into the wire form on their own, the
    next time anything records a count for that chat."""
    if not recorded:
        return False
    return recorded in (model_key(provider_name, model),
                        (provider_name or "").strip().lower() + "/" + model)


def context_usage(agent, provider_name, model, breakdown=None, record=True):
    """What the context panel's token bar draws, for any agent, at any time -
    there is always a number, including in a chat that has never run a turn.

    Two sources, in order:

      reported   the real input count the provider gave for the last request,
                 when it was taken on the model this chat is on now.
      projected  what's actually injected right now, counted with this
                 model's own tokenizer (see tokens.py).

    The projection wins when it's LARGER than the reported count, because the
    bar answers "how full will the next request be", not "how full was the
    last one" - pin a 40k-token skill into a chat and the bar has to move
    then, not after the next reply. Reported wins otherwise, being the one
    number the provider itself stands behind.

    `exact` is whether this model's real tokenizer did the counting (see
    tokens.tokenizer - bedrock and claude-subscription have no reachable one,
    so they're honestly marked approximate); `settled` is False while a
    network tokenizer is still working, meaning the number will firm up on a
    later poll. Both are passed through to the UI rather than smoothed over.

    Whatever this works out is written into the chat's settings .json on the
    way past, which is what stored_usage() below reads back - so `record` is
    only False for a chat that has no files yet (a window sitting in a new
    conversation nobody has sent anything to), where writing would create the
    very folder the app goes out of its way not to create in advance."""
    stem = agent.id
    key = model_key(provider_name, model)
    window = provider.context_window(provider_name, model)
    reported = last_usage(stem).get("input_tokens")
    if agent.tokens_model is not None \
            and not is_model_key(agent.tokens_model, provider_name, model):
        # Taken on another model - a different tokenizer AND a different
        # window, so it says nothing about this one. A count with no model
        # recorded at all is from before that was tracked: trust it, since
        # the projection below is its floor either way.
        reported = None

    segments = context_segments(agent, provider_name, model, breakdown)
    m = tokens.measure(provider_name, model, segments)
    if record:
        agent.record_context(key, m["tokens"], window, m["exact"])

    output = last_usage(stem).get("output_tokens")
    if reported is not None and reported >= m["tokens"]:
        return {"input": reported, "output": output, "max": window,
                "source": "reported", "exact": True, "settled": True,
                "at": agent.tokens_at}
    return {"input": m["tokens"], "output": output, "max": window,
            "source": "projected", "exact": m["exact"], "settled": m["settled"],
            "at": agent.tokens_at}


def stored_usage(agent, provider_name, model):
    """The number this chat last WROTE DOWN, in the same shape context_usage()
    returns - read straight off its settings .json, with nothing recounted.

    This is what lets switching chats show the right total immediately.
    context_usage() re-reads every context file and runs a tokenizer over the
    lot, which is far too much to do on a two-second status poll and is
    pointless when the same work was already done, and its answer recorded,
    the last time this chat's panel drew. So the panel's bar is painted from
    here the moment a chat is opened, and a real count only moves it if it has
    actually changed.

    The reported-vs-projected choice is deliberately the same one
    context_usage() makes (see there), so the two never disagree about which
    number this chat is showing - only about how fresh it is.

    "input" is None when there is no usable number on file: a chat that has
    never been counted, or one whose every recorded count was taken on a
    different model. That is the caller's cue to show a placeholder, NOT to
    keep drawing whatever the last chat's total was."""
    # A cron job's counts are written by cron.py, another process entirely -
    # so what this process loaded when it opened that chat can be arbitrarily
    # old. Cheap: one small file.
    agent.reload_tokens()
    window = provider.context_window(provider_name, model)
    # last_usage() first (a turn this process ran is fresher than any file),
    # but it only knows agents registered in _open - so for anything opened
    # any other way it answers {} and the real reported count, sitting right
    # there in the object reload_tokens() just refreshed, would be dropped in
    # favour of a projection.
    usage = last_usage(agent.id) or {"input_tokens": agent.input_tokens,
                                     "output_tokens": agent.output_tokens}
    reported = usage.get("input_tokens")
    if agent.tokens_model is not None \
            and not is_model_key(agent.tokens_model, provider_name, model):
        reported = None  # taken on another model - see context_usage()
    # Unlike the reported count, a projection is only ever kept for the model
    # it was measured on: it is recomputed constantly, so there is never a
    # reason to show a stale one from a model this chat has since left.
    projected = (agent.context_input
                 if is_model_key(agent.context_model, provider_name, model) else None)
    output = usage.get("output_tokens")
    if reported is not None and (projected is None or reported >= projected):
        return {"input": reported, "output": output, "max": window,
                "source": "reported", "exact": True, "settled": True,
                "at": agent.tokens_at}
    return {"input": projected, "output": output, "max": window,
            "source": "projected",
            # Recorded alongside the count (record_context), so an
            # approximation stays labelled as one across a restart. Older
            # settings files have no such key and are read as exact, which is
            # what they were assumed to be when they were written.
            "exact": agent.context_exact is not False, "settled": True,
            "at": agent.tokens_at}


# Every safety verdict, kept beside the chats so the page can show WHY each
# tool call was waved through or flagged. One .jsonl per chat, append-only, in
# the same order as the chat's tool results - it never goes in the history,
# because the watchdog's reasoning is for the user, not the model being watched.
#
# Keyed by the BARE chat id (see chat_id), so a cron job's log is
# validations/ai-brief.jsonl - server.py's /validations route is what maps the
# 'cron/ai-brief' a browser asks for onto it.
VALIDATIONS = CHATS / "validations"

# What a line says when the gate was off and the call ran unchecked. Two
# wordings, because the setting comes from two different places and "where do I
# go to change this?" is most of the value of being told: a cron job's is its
# own "safety" field in cron.json (cron.py mirrors it into the chat's settings
# .json, and cron.json stays the source of truth), everything else follows the
# safety tab.
_UNCHECKED_CRON = ("no safety check - this cron job runs at safety "
                   + str(settings.SAFETY_MAX) + ", so its tool calls are "
                   "auto-approved and run unvetted. Give it a lower \"safety\" "
                   "in cron.json for them to be checked.")
_UNCHECKED = ("no safety check - the safety check is off, so this call was "
              "auto-approved and ran unvetted. Turn it back on under settings "
              "> safety.")


def _log_validation(call, safe, reason, checked=True):
    """Record one tool call's verdict, and return the reason actually logged.

    `checked` False means the gate was off and this call was never vetted -
    logged all the same, with a reason saying so, so the row is there to see
    and the log stays one line per call (the page pairs rows to tool results by
    position, so a missing line would shift every row after it onto the wrong
    call)."""
    c = getattr(_turn_chat, "chat", None)
    if not checked:
        reason = _UNCHECKED_CRON if c is not None and c.route.startswith("cron/") \
            else _UNCHECKED
    if c is None:
        return reason  # a subagent turn - no chat window to show it in
    try:
        VALIDATIONS.mkdir(parents=True, exist_ok=True)
        with open(VALIDATIONS / (c.id + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"call": call, "safe": safe, "reason": reason,
                                "checked": checked}) + "\n")
    except OSError:
        pass  # losing one log line must never stop the turn
    return reason


# The assembled context, and the file list + mtimes it was built from. Rebuilt
# only when something under context/ actually changes, so the tool loop can ask
# for it on every iteration without re-reading the whole folder each time.
_context_cache = {"key": None, "value": []}
_context_lock = threading.Lock()


def _context_order(name):
    """Sort key for one path part: the number it starts with, then the rest of
    the name.

    The number is read digit by digit until the digits stop, so it is however
    many of them you feel like typing - 1, 01, 001 and 1 are the same file
    position, and the separator after it (a dash, an underscore, nothing at
    all) is not part of the deal. That is the whole point: 2memory.md sorts
    before 10tools.md, which plain alphabetical order gets backwards the moment
    a tenth file appears.

    A name with no leading digit sorts after every numbered one, alphabetically
    among its own kind - unnumbered files are the ones with no opinion about
    where they go, so they go last rather than silently landing in the middle."""
    digits = ""
    for ch in name:
        if not ch.isdigit():
            break
        digits += ch
    return (0, int(digits), name[len(digits):].lower()) if digits else (1, 0, name.lower())


def context_files():
    """Every context file, in the order they go into the prompt: by the number
    each name starts with, then alphabetically. Numbering a file is how you say
    where it goes - 1system.md, 2memory.md, 10tools.md - and anything
    unnumbered follows them."""
    if not CONTEXT.is_dir():
        return []
    found = [p for p in CONTEXT.rglob("*")
             if p.is_file() and p.suffix.lower() in CONTEXT_SUFFIXES
             and not p.name.startswith(".")]
    return sorted(found, key=lambda p: [_context_order(s)
                                        for s in p.relative_to(CONTEXT).parts])


def context_text():
    """One {"label", "text"} dict per context/ file, in context_files() order -
    label is the file's own name (e.g. "1system.md"), so injection_breakdown()
    (and the panel it feeds) can show real file names instead of folding
    everything into one blob named after this function. Each file's text still
    carries its own "--- filename ---" header, same as before the breakdown
    was split up, so the model can still tell files apart once system_text()
    joins every part back into one message. Re-read whenever a file changes,
    so editing a prompt takes effect on the next turn - no restart."""
    files = context_files()
    try:
        key = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    except OSError:
        key = None  # a file vanished mid-scan - rebuild and let the read below skip it

    with _context_lock:
        if key is not None and key == _context_cache["key"]:
            return _context_cache["value"]

        parts = []
        for p in files:
            try:
                body = p.read_text(encoding="utf-8").strip()
            except OSError:
                continue  # unreadable or just deleted - the rest of the context still stands
            if body:
                name = p.relative_to(CONTEXT).as_posix()
                parts.append({"label": name, "text": "--- " + name + " ---\n" + body})

        _context_cache["key"] = key
        _context_cache["value"] = parts
        return parts


# Same caching trick as context_text(), for the memory-file INDEX (names +
# one-line descriptions only - never the bodies, that's the whole point).
_memories_cache = {"key": None, "text": ""}
_memories_lock = threading.Lock()


def memory_files():
    """Every individual memory file, alphabetical by name."""
    if not MEMORIES.is_dir():
        return []
    return sorted(p for p in MEMORIES.glob("*.md")
                  if p.is_file() and not p.name.startswith("."))


def memories_text():
    """The memory INDEX for this turn's prompt: each memory file's name and a
    one-line description (its first line), never its full body - same idea as
    the tool list, which names every tool without paying for its instructions
    until read_skill actually gets called. Keeps individual memories cheap to
    have many of, while still letting the model spot one is relevant and go
    read it in full with read_file/ask_file."""
    files = memory_files()
    try:
        key = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    except OSError:
        key = None

    with _memories_lock:
        if key is not None and key == _memories_cache["key"]:
            return _memories_cache["text"]

        lines = []
        for p in files:
            try:
                stripped = p.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            first_line = stripped.splitlines()[0] if stripped else ""
            desc = first_line.lstrip("#").strip() or "(no description)"
            lines.append(p.stem + ": " + desc)

        # The instructions go out even with nothing to list. An empty folder is
        # exactly when the model most needs telling that memories/ is where a
        # project fact goes - said only once there is already a file here, it
        # can never write the first one, and every project fact it learns
        # either lands in the always-injected memory file or is lost.
        # The path is qualified by the WORKSPACE it is in, not left as a bare
        # absolute path. A bare one reads as "on the computer you are working
        # on", which is only true while the chat happens to be in the Uniagent
        # folder: a chat moved to a phone or a Pi would look for memories over
        # THERE, find nothing, and quietly conclude it had none. The memories
        # live with Uniagent itself, and saying which workspace that is also
        # says how to get to them from anywhere else.
        text = (
            "Memories: individual topic files, one per file, kept with Uniagent "
            "itself - in the '" + provider.BUILTIN_WORKSPACE_ID + "' workspace, at "
            + str(MEMORIES) + " on the machine running Uniagent. That is NOT "
            "necessarily the workspace this chat is in: if it is working "
            "somewhere else - another folder, or another device - that path does "
            "not exist there, so move this chat to '"
            + provider.BUILTIN_WORKSPACE_ID + "' first with the uniagent_command "
            "tool (/workspace " + provider.BUILTIN_WORKSPACE_ID + "), read or "
            "write the memory, then move back to where you were working. "
            "Memories are NOT loaded automatically, unlike context/. "
            "If what's being discussed matches one below, or reading it would "
            "help, read that file in full FIRST (read_file or ask_file) before "
            "answering - don't wait to be asked. If something worth keeping "
            "comes up that belongs in one of these, append to it (check it "
            "isn't already there first, don't duplicate). If it's a new fact "
            "specific to a project, person, or topic none of these cover - not "
            "a general fact about the user, their computer or this environment, "
            "which belongs in the memory file in context/ - create a new "
            "file with write_file, in that same workspace: " + str(MEMORIES)
            + "/<topic>.md, first line a short one-line description, so it's "
            "listed here next turn."
        )
        text += ("\n" + "\n".join(lines)) if lines \
            else "\nThere are no memory files yet - write the first one when a " \
                 "fact worth keeping turns up."
        _memories_cache["key"] = key
        _memories_cache["text"] = text
        return text


# --- Model-specific system injection: what goes into a turn's system
# message, configurable per model in models_custom.json rather than
# hardcoded here. Each item in a model's "injection" list is one of:
#   "file: <path>"  - a whole file, read relative to the project root (the
#                      same convention read_file/write_file already use)
#   "text: <\"...\">" - a literal string, JSON-quoted so it can carry real
#                      newlines and quotes; not valid JSON quoting is taken
#                      as plain text instead of failing the whole prompt
#   "call: <name>"  - one of INJECTION_CALLS below, NEVER an arbitrary
#                      string - a config file must not be able to run
#                      arbitrary code, so only names on that explicit list
#                      are ever callable this way
# A model with no "injection" of its own falls back to models_custom.json's
# top-level "default" entry - see provider.default_injection(). ---

ROOT = Path(__file__).parent.parent

# The only functions a "call:" entry may name. Each is invoked with whichever
# of provider_name/model it actually declares as a parameter - inspected,
# not guessed - the same pattern tool_processor.process() already uses for
# chat_id, so a function that needs none of them just doesn't ask for any.
INJECTION_CALLS = {
    "main.context_text": context_text,
    "main.memories_text": memories_text,
    "tool_processor.prompt_text": tool_processor.prompt_text,
}


def _injection_call(name, provider_name, model):
    fn = INJECTION_CALLS.get(name)
    if fn is None:
        return ""  # unknown name - skip it, don't crash the turn over a typo
    params = inspect.signature(fn).parameters
    kwargs = {}
    if "provider_name" in params:
        kwargs["provider_name"] = provider_name
    if "model" in params:
        kwargs["model"] = model
    return fn(**kwargs)


def injection_breakdown(provider_name, model, pinned=None, workspace_id=None):
    """This turn's injection list (the model's own from models_custom.json, or
    the shared default), resolved but kept as separate pieces - one
    {"label", "kind", "text"} dict per item, in order, kind/skipped-entries
    handling exactly as the module comment above describes. system_text()
    joins these into the one string a model actually sees; the right-hand
    context panel shows them as separate labelled sections instead - same
    data, two views of it.

    A "call:" entry may resolve to a plain string (one section, labelled with
    the call's own name - tool_processor.prompt_text, main.memories_text) OR a
    list of {"label", "text"} dicts (one section per REAL file behind it,
    labelled with that file's own name - context_text() works this way, so
    "1system.md" shows up as itself instead of folding into
    "main.context_text"). Either way each part still ends up its own entry
    here; system_text() joins them all the same regardless of which call
    contributed how many.

    `pinned`, if given, is THIS chat's own Agent.pinned list (see
    Agent.add_pinned in this module) - tools/skills pinned into just this one
    conversation from the sidebar. Deliberately NOT part of the model's
    injection list above: those entries are shared config that every chat on
    this model gets, whereas a pin is scoped to the one chat it was made in,
    so it's appended here instead, straight from that chat's own settings
    .json, never written to context/ or anywhere shared."""
    items = provider.model_config(provider_name, model).get("injection") \
        or provider.default_injection()
    breakdown = []
    for item in items:
        kind, sep, rest = item.partition(":")
        if not sep:
            continue  # malformed entry - skip it, don't break the whole prompt
        kind, rest = kind.strip(), rest.strip()
        if kind == "file":
            try:
                text = (ROOT / rest).read_text(encoding="utf-8").strip()
            except OSError:
                continue  # missing file - skip it, the rest of the prompt still stands
            label = rest
        elif kind == "text":
            try:
                text = json.loads(rest)
            except json.JSONDecodeError:
                text = rest.strip('"')  # not valid JSON quoting - take as-is
            label = text[:40] + ("..." if len(text) > 40 else "")
        elif kind == "call":
            result = _injection_call(rest, provider_name, model)
            if isinstance(result, list):
                for part in result:
                    if part.get("text"):
                        breakdown.append({"label": part["label"], "kind": kind,
                                          "text": part["text"], "call": rest})
                continue
            if not result:
                continue
            text, label = result, rest
        else:
            continue  # unknown prefix - skip it rather than guess what it meant
        breakdown.append({"label": label, "kind": kind, "text": text})
    # The tool schemas are just as much a part of what the model is sent as
    # anything above - they're simply carried by the API's own `tools` array
    # instead of the system message (see provider.py). They were invisible
    # here because of that, which made the panel say one thing and the request
    # do another, and left the token bar under-counting by everything the
    # schemas cost. One entry per tool, so the panel answers
    # "which tools does this model actually have, and what does each cost".
    #
    # The text is the exact JSON that goes on the wire, not a prettified
    # version of it: this is the one place you come to find out what was
    # really sent, and re-indenting it would inflate every count taken off it
    # (context_segments feeds these straight to the tokenizer).
    for name, text in tool_processor.schema_entries(
            tool_processor.shape_for(provider_name)):
        breakdown.append({"label": "tool schema: " + name, "kind": "schema",
                          "text": text})
    for p in (pinned or []):
        if p.get("text"):
            breakdown.append({"label": p["label"], "kind": "pinned", "text": p["text"]})
    # Where this chat's tools actually work. Last, and always present: a model
    # that does not know it is operating on another machine will confidently
    # hand back paths from the wrong computer, and a model that does not know
    # its root will keep guessing at relative paths. One line, and the context
    # panel shows it alongside everything else the model was told.
    breakdown.append({"label": "workspace", "kind": "workspace",
                      "text": workspace.describe(workspace_id)})
    return breakdown


def system_text(provider_name, model, pinned=None, workspace_id=None):
    """The system message for this turn - injection_breakdown()'s pieces
    joined into one string. This is what every model actually sees in place
    of the old hardcoded context_text() + memories_text() +
    tool_processor.prompt_text() - which is still exactly what the DEFAULT
    injection list reproduces, so nothing changes for a model that hasn't
    been given one of its own. `pinned` (this chat's own Agent.pinned, if any)
    is appended on top - see injection_breakdown().

    "schema" entries are the one kind left OUT. They are in the breakdown
    because the panel and the token count both have to know about them - they
    really are sent - but they travel as the request's own `tools` array, so
    pasting them in here as well would send every schema twice, once in a
    shape no provider parses."""
    parts = injection_breakdown(provider_name, model, pinned, workspace_id)
    return "\n\n".join(p["text"] for p in parts
                       if p["text"] and p["kind"] != "schema")


def dynamic_reads(history):
    """Every read_skill call in this chat's history and what it actually
    returned - a tool's or skill's full instructions, which entered the
    conversation mid-chat by the model's own choice rather than being part of
    the system message from the start. This is context the right-hand panel
    needs to show alongside the static injection breakdown, since it's just
    as real a part of what the model has been told - it just didn't arrive
    the same way."""
    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        return []
    reads = []
    for i, t in enumerate(turns):
        if not isinstance(t, dict) or t.get("role") != "assistant":
            continue
        for call in t.get("tool_calls", []):
            fn = call.get("function", {})
            if fn.get("name") != "read_skill":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            name = args.get("name", "?")
            call_id = call.get("id")
            result = next((u.get("content") for u in turns[i + 1:]
                           if isinstance(u, dict) and u.get("role") == "tool"
                           and u.get("tool_call_id") == call_id), None)
            if result:
                reads.append({"label": "read_skill: " + name, "kind": "read_skill", "text": result})
    return reads


def final_answer(history):
    """The reply a turn ended on: the last turn in `history`, but only when it
    is the model answering in words rather than calling something. None for
    every other way a turn can end.

    "The last assistant turn" and "the turn the chat ends on" are not the same
    question, and it is the second one that matters here. run() appends an
    assistant turn at several points that are not an answer - a reply that
    wouldn't parse as a tool call and is being sent back to be fixed, one half
    of the loop-breaker - and each of those is followed by a user turn telling
    the model so. Reading the list from the end rather than searching backwards
    for a role is what tells those apart: an answer is the last word in the
    chat, and the others never are.

    Ruled out on purpose: a turn /stop cut short (STOPPED), and one that blew
    up (TURN_ERROR). Both are filed as assistant turns so they render and so
    the model reads them next time, but neither is something the agent said."""
    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        return None        # an old flat-text chat - no structure to read
    if not turns:
        return None
    last = turns[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return None
    if last.get("tool_calls"):
        return None
    text = last.get("content")
    if not isinstance(text, str) or not text.strip():
        return None
    if text.strip() == STOPPED or text.startswith(TURN_ERROR):
        return None
    return text.strip()


def turn_count(history):
    """How many turns are in `history` right now - the mark to hand back to
    said_since() after a turn, so it can tell this turn's messages from the
    conversation they were added to. 0 for an unparseable or empty history,
    which makes said_since() read the lot rather than nothing."""
    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        return 0
    return len(turns) if isinstance(turns, list) else 0


def said_since(history, mark):
    """Everything the model wrote from turn `mark` onward, one string per
    message, in order - the messages alongside tool calls included, which is
    the whole difference from final_answer() above.

    A turn of tool work is several assistant messages, most of them a sentence
    of prose plus a call ("Right, let me look at the file" + read_file). Read
    out loud they are the running commentary; final_answer() only ever sees the
    last one. The calls themselves aren't here - a tool name and its arguments
    are not something anyone wants read to them - so an assistant message that
    was nothing but a call contributes nothing.

    Kept as a list rather than joined, because a message is the unit everything
    downstream works in: server.py reads them out one after another, and a
    summary is written per message, not one for the turn.

    The same two exclusions as final_answer(): a turn /stop cut short and one
    that blew up are both filed as assistant turns, and neither is the agent
    speaking. [] when there is nothing to say."""
    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        return []
    if not isinstance(turns, list):
        return []
    said = []
    for turn in turns[max(mark, 0):]:
        if not isinstance(turn, dict) or turn.get("role") != "assistant":
            continue
        text = turn.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()
        if text == STOPPED or text.startswith(TURN_ERROR):
            continue
        said.append(text)
    return said


def _split_output(provider_name, model, thought, reply, usage, counted=None):
    """A response's output tokens, split between what it thought and what it
    wrote. Returns (think_tokens, write_tokens), either of which may be None.

    `counted` is the thinking already counted at the moment thinking ENDED
    (see _stream's thinking_done). When it is there it is used as-is, and the
    reply simply takes the rest of the provider's total. That is what keeps
    the thinking rate from being revised a few seconds after it is shown: the
    number on screen and the number stored on the turn are the same number,
    taken once. A provider that reports its own reasoning count still wins
    over it - that is the model's own tokenizer counting its own output, and
    no local estimate beats it.

    This is the number the two speeds are computed from, and getting it from
    one figure is what made a 30 tok/s local model report 1000: providers
    report ONE output count for the whole response, thinking included, and
    dividing all of it by the seconds spent writing counts every reasoning
    token as though it had been written in the reply's few seconds.

    Three sources, best first:

    1. THE PROVIDER SPLIT IT. OpenAI's reasoning models and Gemini both report
       a reasoning count, and provider.py normalises both to the same
       convention: reasoning_tokens is a subset of output_tokens. Nothing is
       measured here in that case - the model's own tokenizer counted its own
       output, which is as good as this gets.

    2. NOBODY SPLIT IT, BUT THE TOTAL IS KNOWN. Both halves are measured with a
       local tokenizer (tokens.estimate - an average ruler, not this model's
       own) and then scaled so they add up to the total the provider reported.
       The proportion is the part being estimated; the total stays the
       provider's own, so the two speeds and the usage ledger cannot drift
       apart.

    3. NOTHING IS KNOWN. The local measurements stand on their own. This is the
       common case on a turn that called a tool, where the provider stops
       generating at the call and often never sends its final usage event.

    Nothing is ever invented: a stream that produced nothing gets None rather
    than 0, and a rate is simply not drawn for it (timing.rate)."""
    thought, reply = thought or "", reply or ""
    total = usage.get("output_tokens") if isinstance(usage, dict) else None
    thinking = usage.get("reasoning_tokens") if isinstance(usage, dict) else None

    if isinstance(thinking, int) and isinstance(total, int):
        return thinking, max(0, total - thinking)

    if isinstance(counted, int) and counted > 0:
        if isinstance(total, int):
            return counted, max(0, total - counted)
        return counted, (tokens.estimate(provider_name, model, reply) or None) if reply else None

    # tokens.estimate is local, cached and immediate - never a network
    # tokenizer, which is the one thing that must not happen on the turn
    # thread. It is the same measurement usage.py falls back to when a provider
    # declines to report, so the two agree by construction.
    think_est = tokens.estimate(provider_name, model, thought) if thought else 0
    write_est = tokens.estimate(provider_name, model, reply) if reply else 0
    measured = think_est + write_est

    if isinstance(total, int) and measured > 0:
        # Scaled, not replaced. The split is a guess; the total is not, and a
        # pair of halves that don't add up to the reported whole would be a
        # third number nobody asked for.
        think_tok = int(round(total * think_est / measured))
        return think_tok, total - think_tok

    return (think_est or None), (write_est or None)


def _stream(messages, provider_name, model, temperature, on_text, should_stop=None, usage=None,
            native_call=None, reasoning=None, phases=None, on_request=None,
            on_thought=None, on_reclassify=None):
    """One model response, read as it's written. Returns everything received.

    Shows each piece the moment it arrives - printed here, or handed to on_text
    if the caller wants it somewhere other than the terminal. Stops reading the
    moment a complete tool call has come through: a capable model keeps writing
    after its call, inventing tool results and firing more calls, and none of
    that tail should be shown, remembered, or paid for.

    `should_stop`, if given, is checked between chunks - a backstop now rather
    than the mechanism. A /stop breaks the provider connection open through
    this thread's turn context (provider._stream_post), so the loop below
    normally exits by raising Stopped out of the generator on the spot; this
    check only catches the case where a chunk happened to be in hand already.
    Whatever arrived before either is still returned so it can be kept.

    Text is published onto the turn's context as it arrives, so a /stop landing
    mid-answer can write that partial reply into the transcript itself instead
    of losing it with the abandoned thread (see _stopped_history).

    `usage`, if given, is a dict provider.stream_response() fills in with real
    token counts as the provider reports them. Breaking out at the first tool
    call (below) means a turn that makes a call often never reaches the
    provider's final usage event - output_tokens can end up missing for those
    turns. That's read honestly as "not reported", never guessed at, rather
    than waiting out the tail just to complete a token count.

    `native_call`, if given, is a dict provider.stream_response() fills in
    with a real structured tool call ({"id","name","arguments"}) - which is
    how EVERY tool call arrives now: the schemas go over as the provider's own
    `tools` array (tool_processor.tools_schema()) and the call comes back on
    its own structured channel. Passing None is what a request with no tools
    at all looks like (a safety check, compaction), not a second call syntax.
    There is no text-embedded call to scan for: the provider itself stops
    generating once it decides to call a tool, so the loop below just streams
    whatever preamble text arrives and lets the generator run to its own
    natural end. See run()'s _parse_call() for how the filled-in dict becomes
    a {"tool","args"} call afterward.

    `reasoning`, if given, is a dict provider.stream_response() fills in with
    a thinking model's own reasoning_content, under "content". Never part of
    the reply - it is not yielded, and none of it reaches `response`.

    Two things read it. DeepSeek's thinking models demand a turn's own
    reasoning back on the next request (see provider.py's _REASONING_KEY),
    which is why run() stores it on the turn. And it is now SHOWN: put an
    "on_delta" callable in that dict and it is called with each fragment as it
    arrives, which is what lets a page draw the model thinking rather than
    going silent for forty seconds. This function wraps whatever the caller
    put there so the clock below sees the fragments too.

    `phases`, if given, is a timing.Phases - filled in as the stream arrives
    with how long the model waited, thought and wrote. Same in-place
    convention as the three dicts above, and same reason: this is the one
    place every provider's stream actually passes through, so it is the only
    place that can time all of them.

    `on_thought`, if given, is called once, at the moment thinking ENDS - the
    first token of the reply after any reasoning - with that stream's finished
    numbers: {"think": ms, "think_tok": n}. It exists because those numbers
    are wanted the instant the label changes from "thinking" to "thought", and
    the provider's own token count does not arrive until the whole response is
    over, several seconds later. So the thinking is counted here instead, once,
    and the same figure is handed to _split_output afterwards - which is what
    stops the rate being shown and then quietly revised.

    `on_request`, if given, is called with no arguments at the moment the
    request goes out - the same instant the clock starts. It is called from
    HERE rather than by run(), which is where it is handed in from, for
    exactly that reason: everything between the two is prompt assembly, and a
    UI counting from before it would report a wait a tenth of a second longer
    than the one this function measures.

    `on_reclassify`, if given, is called once with the full reply text in
    place of on_text, when provider.py's _read_openai tail decides a model
    that never sent a content chunk answered entirely on the reasoning
    channel. That text has already been shown live as thinking, one
    on_reasoning fragment at a time; on_text is skipped for it so a caller
    that only wired on_text is not shown it a second time as a brand new
    reply. The text still rides along on_reclassify itself (rather than
    leaving the caller to have kept it from on_reasoning) so a caller that
    missed a fragment - a page reconnecting mid-turn - still has the whole
    thing.
    """
    response = ""
    in_call = False  # has the call started being written yet?

    # This response's text as it grows, readable by whoever stops the turn.
    # Reset here rather than at the end of the pass: from the moment a new
    # response starts streaming, the previous one is already accounted for in
    # the turns list, and leaving it published would have a stop append it a
    # second time.
    ctx = turnctx.current()
    if ctx is not None:
        ctx.partial = ""
        # Published for the same reason and at the same moment as partial: a
        # /stop is written by the STOPPING thread, which can only keep what it
        # can see from here. Reset per response, so a stop lands on this
        # response's numbers rather than the previous one's.
        ctx.thinking = ""
        ctx.phases = phases

    #temp guard
    if temperature is None:
        temperature = chosen["temperature"]

    tools = None
    if native_call is not None:
        tools = tool_processor.tools_schema(tool_processor.shape_for(provider_name))

    # The clock rides in on the reasoning channel rather than as another
    # argument to provider.stream_response, because that channel already
    # reaches every reader in provider.py and thinking is the one phase this
    # loop cannot see for itself - reasoning fragments are never yielded here.
    # Whatever the caller wanted to watch thinking with is kept and called
    # after the clock, so this is transparent to it.
    if reasoning is not None:
        watching = reasoning.get("on_delta")

        def thought(part):
            if phases is not None:
                phases.thinking()
            if ctx is not None:
                ctx.thinking = reasoning.get("content", "")
            if watching:
                watching(part)

        reasoning["on_delta"] = thought

    told = [False]

    def thinking_done():
        """Thinking has just ended - the first token of the reply is in hand.

        Counted here rather than at the end of the response because that is
        where the answer is wanted: the block on screen stops saying
        "thinking…" at this exact moment, and a rate that turned up seconds
        afterwards would either arrive too late to be part of that label or
        replace it with a different number while being read.

        The count goes back onto the reasoning dict as well as out to the
        caller, so run() hands the very same figure to _split_output and the
        turn is stored with the number that was displayed.

        This runs inside the streaming loop, which is worth being deliberate
        about: tokens.estimate is local and cached, measured at 350ms the
        first time in a process (loading tiktoken's tables off disk) and
        nothing at all afterwards. It never reaches the network - a tokenizer
        that did could not go here - and a tiktoken that will not load at all
        falls back to a coarser count rather than raising."""
        if told[0] or phases is None or phases.think_from is None:
            return
        told[0] = True
        thinking = (reasoning or {}).get("content") or ""
        if not thinking:
            return
        counted = tokens.estimate(provider_name, model, thinking)
        if reasoning is not None:
            reasoning["tokens"] = counted
        if on_thought:
            on_thought({"think": timing.ms(phases.think_from, phases.think_to),
                        "think_tok": counted})

    def show_call(piece):
        """A native tool call, shown as it's written rather than after.

        A native call is never yielded as reply text - it arrives on its own
        structured channel - so without this a turn that calls a tool shows
        NOTHING for its whole length and then produces a finished call out of
        nowhere.

        The pieces spell out the same text the turn is stored and redrawn
        with (_parse_call's shown_call), so what was watched appearing is what
        stays on screen afterwards - no rewrite when the bubble is sealed. It
        is deliberately NOT added to `response`: that is the model's prose,
        and the call belongs in tool_calls, not in the turn's content."""
        nonlocal in_call
        thinking_done()       # a call being typed ends the thinking too
        if phases is not None:
            phases.writing()  # a call being typed is the model writing
        if not in_call:
            in_call = True
            if response and not response.endswith("\n"):
                piece = "\n\n" + piece  # same gap the redraw puts there
        if on_text:
            on_text(piece)
        else:
            print(GREEN + piece + RESET, end="", flush=True)

    # The clock starts HERE, not where the Phases was made: everything above -
    # assembling and serialising the tool schemas, most of all - is Uniagent
    # getting ready to ask, and counting it as time the provider took to answer
    # blames the wrong thing. See timing.Phases.restart().
    if phases is not None:
        phases.restart()
    # And the wait begins, for anyone watching. On a local model with a long
    # conversation this is the longest stretch of a turn and the one with
    # nothing on screen, so it is also the only stretch where a page can look
    # hung when it is simply waiting.
    if on_request:
        on_request()

    for chunk in provider.stream_response(messages, provider=provider_name, model=model,
                                          temperature=temperature, usage=usage,
                                          tools=tools, tool_call=native_call,
                                          reasoning=reasoning,
                                          on_call_delta=show_call):
        if should_stop and should_stop():
            break
        # provider.py sets this immediately before yielding this exact chunk,
        # in the one branch where the whole answer came back as reasoning and
        # is being handed over as the reply - see _read_openai's tail. That
        # text has already been shown, live, as thinking; a caller that gave
        # on_reclassify is told with that instead of on_text, so it is not
        # streamed a second time as a brand new reply. A caller that did NOT
        # give on_reclassify is untouched - old behaviour, text on_text same
        # as any other chunk - so this only changes anything for a caller
        # that opted in.
        reclassified = bool(chunk) and on_reclassify is not None \
            and reasoning is not None and reasoning.get("reclassified")
        if chunk and not reclassified:
            thinking_done()
        if chunk and phases is not None:
            phases.writing()
        response += chunk
        if ctx is not None:
            ctx.partial = response
        # Reply text only. The call itself never comes through here - it
        # arrives on the structured channel and is shown by show_call above -
        # so there is nothing to scan, trim or break out of mid-stream: the
        # provider stops on its own once it has decided to call a tool.
        if chunk:
            if reclassified:
                on_reclassify(chunk)
            elif on_text:
                on_text(chunk)
            else:
                print(chunk, end="", flush=True)

    if not on_text:
        print(RESET if in_call else "")

    if phases is not None:
        # A provider that streamed its whole answer on the reasoning channel
        # has said so by now; what was timed as thinking was the reply being
        # written. See provider._read_openai's tail.
        if reasoning is not None and reasoning.pop("reclassified", False):
            phases.reclassify()
        phases.end()
    return response


def _parse_call(response, native_call):
    """(call, before, shown_call, retry_msg) for this pass's reply - the one
    place run() figures out whether a tool call is in there.

    There is no text to scan. The call already arrived as `native_call`
    ({"id","name","arguments"}), filled in by provider.py straight from the
    provider's own structured response - see _stream()'s docstring.
    `shown_call` is a synthesized "name(args)" text used only for display (the
    UI's on_tool_call, the safety log) and as this turn's raw_call for history
    replay; there is no syntax the model "wrote" to preserve verbatim, because
    a native call never had a text form at all.

    retry_msg is None when nothing needs resending; otherwise it's the
    "Tool result: ..." text to send back and try again - arguments that
    arrived as something other than valid JSON, or a reply that WROTE a call
    out as prose instead of making one."""
    if native_call.get("name"):
        try:
            args = json.loads(native_call.get("arguments") or "{}")
        except json.JSONDecodeError:
            return None, response, "", ("Tool result: that tool call's arguments were "
                                         "not valid JSON, so nothing ran. Try the call again.")
        call = {"tool": native_call["name"], "args": args}
        # Built from the arguments string exactly as it streamed, not from
        # json.dumps(args) - re-dumping reformats the whitespace, so the
        # sealed text would differ from the text _stream's show_call just
        # showed being written, and the bubble would visibly rewrite itself.
        shown_call = call["tool"] + "(" + (native_call.get("arguments") or "{}") + ")"
        return call, response, shown_call, None

    # No structured call - but the reply may still have TRIED to call
    # something by typing it out, which a model does often enough to be worth
    # catching: it looks like a finished answer while nothing actually ran.
    # looks_like_call() ignores anything inside a ``` fence, so a tool's
    # source quoted back or a pinned skill's content (see Agent.add_pinned)
    # doesn't trip it - that was never an attempt, so there is nothing to ask
    # the model to fix.
    if tool_processor.looks_like_call(response):
        return None, response, "", ("Tool result: that looked like a tool call written out as "
                                    "text, so nothing ran. Your tools are attached to this "
                                    "request as real schemas - call one for real instead of "
                                    "writing the call in your reply.")
    return None, response, "", None


def _say(text):
    """Format one line of the model's own turn for history: 'name [when]: text'.

    Stamped like the user's turns are, so the transcript reads as a dated
    back-and-forth rather than only the user's side carrying a time. The stamp
    is added HERE, when the reply is stored - the model is still prompted with a
    plain 'name: ', so it never has to write the bracket itself, and the value
    is the moment the turn landed."""
    when = datetime.now().strftime("%a %Y-%m-%d %H:%M")
    return name + ": " + text + "\n"


def append_error(c, msg):
    """Record a failed turn's error INTO `c`'s history as a proper turn, not
    concatenated raw text onto the end of the JSON - history is a serialized
    turns list (see run()'s docstring), and string-appending onto that broke
    it: the next turn's json.loads() choked on the trailing text and fell
    back to "start fresh", silently discarding everything before the error.
    Filed as an assistant turn (not "system") so it both renders like any
    other reply AND survives into the next request - provider.py's _compat()
    strips system turns before anything reaches a provider, which would have
    hidden the error from the model right when it matters most.

    Falls back to the old plain-text append only if `c.history` is already
    unparseable - a pre-JSON flat-text chat, where there's no structure to
    preserve anyway."""
    text = TURN_ERROR + msg
    try:
        turns = json.loads(c.history) if c.history else []
    except json.JSONDecodeError:
        c.history += text + "\n"
        c.save()
        return
    turns.append({"role": "assistant", "content": text})
    c.history = json.dumps(turns, indent=2)
    c.save()


def note_turn(c, text):
    """Record something that happened TO chat `c` into its history, as a user
    turn the model reads on its next pass.

    A workspace change is the case this exists for. It is not a tool result and
    nobody typed it, but the model has to be told the same way it is told a
    tool's result: in the conversation, in a turn that goes back to the provider
    on every request after it, and kept in the chat file so it is still there
    tomorrow. A message flashed on the page instead would leave the model
    working in a place it has no idea it moved to.

    Filed as a "user" turn rather than "system" for the same reason
    append_error() is an assistant one - provider.py's _compat() strips system
    turns before anything reaches a provider, so a system turn would be visible
    on the page and invisible to the model, which is exactly backwards here.

    MID-TURN IS THE NORMAL CASE, not the exception: the workspace is usually
    changed while the agent is working, which is the whole reason it is worth
    telling it about. A running turn owns the history - it holds its turns list
    in memory and rewrites the file after every step (run()'s sync) - so the
    note is appended to THAT list. Writing the file instead would be overwritten
    by the turn's next save, and the model would never see it."""
    ctx = c.slot.context()
    live = getattr(ctx, "turns", None) if ctx is not None else None
    if live is not None:
        # The same list object run() is appending to, published for exactly
        # this kind of reach-in (see turnctx.TurnContext). The turn writes it
        # out at its next step and hands it to the model on its next pass.
        live.append({"role": "user", "content": text})
        return
    # Nothing running (or a turn in the instant before it has built its list -
    # a /compact, which owns the chat but has no turns list of its own, lands
    # here too and then replaces the history with its summary, so a note left
    # in that gap is lost. It is a gap of milliseconds against a change made by
    # hand, and the alternative is holding the chat's slot to write one line).
    try:
        turns = json.loads(c.history) if c.history else []
    except json.JSONDecodeError:
        # A pre-JSON flat-text chat: no structure to preserve, so append as
        # text, the same fallback append_error() takes.
        c.history += text + "\n"
        c.save()
        return
    turns.append({"role": "user", "content": text})
    c.history = json.dumps(turns, indent=2)
    c.save()


def workspace_note(c, ws, ok=True, message="", following_default=False):
    """Tell chat `c` - and so the model - that the user has moved it to `ws`.
    Returns the line written, so the caller can show the same words on screen.

    Said as plainly as possible, because the model has to act on it: every file
    path and every terminal command from here on lands somewhere else, and if
    that somewhere else is another machine then what it knows about this one no
    longer applies. `ok`/`message` are workspace.check()'s answer - an
    unreachable workspace is worth saying outright rather than leaving the next
    tool call to discover it."""
    text = (WORKSPACE_NOTE + "the user moved this chat to " + ws.name
            + (" (the default workspace)" if following_default else "")
            + " - " + ws.where + ". Every file tool and the terminal work there "
            "now, and relative paths are resolved from that root.")
    if not ok:
        text += (" It is not reachable at the moment: " + message
                 + " Say so rather than retrying blindly.")
    note_turn(c, text)
    return text


def run(text, history, provider_name=None, model=None, temperature=0, approve=_approve,
        on_save=None, on_text=None, should_stop=None, chat_id=None,
        on_tool_call=None, on_tool_result=None, on_safety=None, pinned=None,
        safety=None, safety_prompt=None, inject=None, workspace_id=None,
        safety_threshold=None, safety_extra=None, on_message=None,
        on_reasoning=None, on_timing=None, on_request=None, on_thought=None,
        on_reclassify=None):
    """Run one turn over `history` and return the updated history: reply to text,
    and work through any tool calls it makes.

    `history` is a JSON-encoded list of turns in DeepSeek/OpenAI's own message
    shape - {"role": "user"/"assistant"/"tool", "content": ..., "tool_calls": [...]
    / "tool_call_id": ...} - rather than the old flat "User: .../Uniagent: ..."
    text. Step 1 of a larger change: nothing outside this function needs to know
    the difference yet, since it's still handed in and returned as a plain string
    (just JSON text instead of flat text) - see plans/quiet-splashing-glacier.md.
    An old flat-text chat, or a brand new empty one, both simply start a fresh
    turns list - there is no migration of old history in this step.

    This owns no global state - the caller passes the conversation in and gets it
    back - so the chat keeps one history and each cron job keeps its own, all
    running through this exact tool-processing loop. provider_name/model/temperature
    pick what the request runs on, and default to whatever the settings page has
    selected - read HERE, per turn, so changing them takes effect on the next
    message rather than the next restart. `approve` decides a safety-flagged call (the chat hands it a
    human y/n, a cron job hands it an auto-deny). `on_save`, if given, is handed
    the history every time it grows, so the chat file keeps up with a long tool
    loop instead of only landing at the end of the turn. `on_text`, if given, is
    handed each piece of the reply as the model writes it and takes over from
    printing - so a UI can show the answer arriving instead of waiting out the
    whole turn. `should_stop`, if given, is checked between tool-loop passes -
    the backstop for a caller that stops a turn without a context to cancel
    (a subagent's parent, cli.py's Ctrl-C). The turn's own cancellation is
    sharper than that: /stop cancels this thread's turn context, which breaks
    open whatever blocking call it is actually in and unwinds this function
    with turnctx.Stopped. Either way the history returned is whatever had been
    written when it gave up.

    `on_tool_call`, `on_tool_result` and `on_safety`, if given, fire the moment
    a call is parsed, its result comes back, and the safety verdict is known - so
    a UI can draw the tool's own block (the call, safety row and result dropdown)
    as it happens, instead of waiting out the whole turn for a redraw.
    on_tool_result(result, name=None, timing=None): `name` is which call this
    answered, and `timing` is how long that call took to run ({"ms": n}). Both
    are optional and both default to None, so a caller written before they
    existed - and there are several - still works untouched.

    `on_message`, if given, is handed each finished assistant message the
    moment this function knows what it was - on_message(text, kind), where kind
    is "call" for a message that ended in a tool call (the text is the prose
    before it) and "answer" for one that ends the turn. It fires from inside
    the loop rather than after it, which is the whole point: server.py reads a
    message out loud from here, so the speaking starts while the tool it just
    asked for is still running instead of after everything is over.
    Deliberately NOT fired for the messages that aren't the agent speaking - a
    reply being sent back to be reparsed, the loop-breaker, a stopped turn -
    which is the same line final_answer() draws.

    `on_reasoning`, if given, is handed each fragment of a thinking model's
    working as it arrives - on_reasoning(text) - so a UI can show the model
    thinking instead of showing nothing at all for however long that takes. It
    fires only while the thinking is happening; the finished text is kept on
    the turn as `reasoning_content` (below), which is where a reload reads it
    from. Never mixed into on_text: thinking is not the reply, and a caller
    that wants only the reply should not have to filter it back out.

    `on_thought`, if given, is handed the thinking stream's finished numbers
    ({"think": ms, "think_tok": n}) the moment thinking ends, which is the
    moment a UI stops saying "thinking" and starts saying "thought" - so the
    rate lands with the label rather than seconds later when the response
    finishes. See _stream's thinking_done().

    `on_request`, if given, is called with no arguments the moment a request is
    about to go out - the start of the latency this turn is about to spend
    waiting. It exists so a UI can count that wait while it is happening;
    the measured figure arrives afterwards on `on_timing` as "latency".

    `on_timing`, if given, is handed the finished timing dict for each model
    response the moment that response is complete - on_timing(timing), where
    timing is timing.Phases.as_dict(): how long it waited, thought and wrote,
    and how many output tokens the provider owned up to. It fires BEFORE the
    tool call or the answer that response turned out to be, so a page can seal
    the bubble it was streaming into with the numbers already on it rather
    than rewriting it afterwards. The same dict is stored on the turn, so a
    reload shows exactly what the live view showed.

    `inject`, if given, is asked at the top of every pass for text the user has
    sent SINCE this turn started, and folds whatever it returns in as a user
    turn. That is how cli.py lets you keep typing at a working agent: the
    message lands between two passes rather than waiting out the whole turn or
    starting a competing one. The top of a pass is the only safe place for it -
    there, the tool result behind it is already written and the next request
    hasn't been built, so the history is whole either side of the insertion.

    `pinned`, if given, is the calling agent's OWN pinned list (Agent.pinned) -
    tools/skills pinned into just this one chat, appended to its system
    message every pass (see system_text()). None for a caller with no notion
    of pinning (there isn't one today, but nothing requires every call site to
    pass it).

    `safety` and `safety_prompt` are this ONE turn's answer to the safety gate,
    both None (the default) meaning "follow the settings page" - so every caller
    that doesn't care behaves exactly as before. True/False forces the check on
    or off for this turn alone; `safety_prompt` replaces the global vetting
    prompt. A cron job passes both from its own entry in cron.json, which is how
    a job can be checked under looser rules than the chat, or not at all."""
    chosen = settings.load()
    provider_name = provider_name or chosen["provider"]
    model = model or chosen["model"]

    try:
        turns = json.loads(history) if history else []
    except json.JSONDecodeError:
        turns = []  # an old flat-text chat - nothing valid to carry over, start fresh

    # None means "no new message" - a continue, which runs the turn over the
    # history exactly as it stands (see continue_from). Every other caller
    # passes a string, including an empty one, and gets the turn it always got.
    if text is not None:
        turns.append({"role": "user", "content": text})
    bad_json = 0  # consecutive "meant to be a tool call but wouldn't parse" tries
    last_response = None  # the previous pass's raw reply, to catch a stuck loop
    repeats = 0   # how many passes in a row that reply has now been made
    # How many of those in a row are allowed before the turn is halted - see
    # settings.REPEATS_RANGE. Read once, here, rather than per pass: a turn
    # runs under the rules it started on, so a change saved mid-turn cannot
    # move the line the loop is already being measured against.
    max_repeats = chosen["max_repeats"]

    # Published so a /stop landing part-way through can close this transcript
    # out itself rather than waiting for this thread to come back and do it -
    # see _stopped_history(). The SAME list object, not a copy: it is read as it
    # stands at the moment of the stop, so every append below is visible without
    # anything having to re-publish it.
    ctx = turnctx.current()
    if ctx is not None:
        ctx.turns = turns

    def sync():
        if on_save:
            on_save(json.dumps(turns, indent=2))

    sync()

    # Claude Code keeps its own conversation and runs its own tool loop, so
    # this provider does not go through the loop below at all - see
    # claude_session.py, which explains why that exception is worth making and
    # how Uniagent stays the thing deciding what may run. Everything a caller
    # handed in is passed straight through, because the point is that a chat on
    # this provider behaves like a chat on any other.
    if provider.wire_of(provider_name) == "claude-subscription":
        here = live_workspace(chat_id, workspace_id)
        usage = {}
        started = time.time()
        # A session, not a text endpoint: it keeps its own conversation on the
        # CLI's side and cannot be re-asked over a history the way the loop
        # below can. A continue therefore has to SAY so - one sentence into the
        # session - where every other provider resumes without a word.
        said = CONTINUE_NUDGE if text is None else text
        try:
            claude_session.run_turn(
                turns, sync, said, chat_id, provider_name, model,
                system_text(provider_name, model, pinned, here),
                workspace_id, chosen, approve,
                on_text=on_text, on_tool_call=on_tool_call,
                on_tool_result=on_tool_result, on_safety=on_safety,
                on_message=on_message, should_stop=should_stop, usage=usage,
                safety=safety, safety_prompt=safety_prompt,
                safety_threshold=safety_threshold, safety_extra=safety_extra,
                on_reasoning=on_reasoning, on_timing=on_timing,
                on_request=on_request, on_thought=on_thought)
        except BaseException as e:
            stopped = isinstance(e, turnctx.Stopped)
            usage_log.record("turn", provider_name, model, chat=chat_id, usage=usage,
                             ms=(time.time() - started) * 1000,
                             ok=stopped, error=None if stopped else repr(e))
            raise
        usage_log.record("turn", provider_name, model, chat=chat_id, usage=usage,
                         ms=(time.time() - started) * 1000)
        if chat_id:
            _set_usage(chat_id, usage, model_key(provider_name, model))
        return json.dumps(turns, indent=2)

    for _ in range(MAX_TOOL_CALLS):
        # Checked HERE, at the top of a pass, because that is the one point the
        # history is always whole: the pass before it either ended on a plain
        # answer or wrote its tool result. Stopping anywhere else could leave a
        # tool call with no result behind it - the exact half-written state that
        # makes the next turn incoherent.
        if should_stop and should_stop():
            turns.append({"role": "assistant", "content": STOPPED})
            sync()
            break

        # Anything the user typed while this turn was already running, folded
        # in here as its own user turn. Labelled rather than inserted bare,
        # because provider.py's _compat() merges adjacent same-role turns and
        # this one lands directly behind a tool result - unmarked, the model
        # would read the user's sentence as the tail of the tool's output.
        extra = inject() if inject else None
        if extra:
            turns.append({"role": "user", "content": MID_TURN + extra})
            sync()

        # A real system message, then the chat's own turns exactly as they're
        # stored in the chat file - no flattening into "User: .../Uniagent:
        # ..." text. turns is already OpenAI's own message shape (role/
        # content, tool_calls/tool_call_id on the ones that have them), so it
        # goes straight through; provider.py maps it to whatever shape each
        # provider's wire format actually needs. What goes into the system
        # message is this model's own injection list if it has one
        # (models_custom.json), else the shared default - see system_text().
        # Read fresh every pass, so the line telling the model where it is
        # working is right on the pass after a move rather than a turn later.
        here = live_workspace(chat_id, workspace_id)
        system = system_text(provider_name, model, pinned, here)
        messages = [{"role": "system", "content": system}] + turns
        usage = {}
        native_call = {}
        # on_delta rides in the reasoning dict itself (provider._thought), so
        # a thinking model can be watched thinking. _stream wraps this with
        # its own clock before the request goes out.
        reasoning = {"on_delta": on_reasoning} if on_reasoning else {}
        phases = timing.Phases()
        started = time.time()
        try:
            response = _stream(messages, provider_name, model, temperature, on_text, should_stop,
                               usage, native_call, reasoning, phases, on_request,
                               on_thought, on_reclassify)
        except BaseException as e:
            # Written down before it unwinds. A request that died part-way -
            # the provider 500'd, the key is spent, the turn was stopped - had
            # already sent its whole prompt, and is charged for accordingly, so
            # dropping it here would make the ledger quietly cheaper than the
            # bill. A stop is not a failure though: it is the user getting what
            # they asked for, so only a real exception counts as one.
            stopped = isinstance(e, turnctx.Stopped)
            usage_log.record("turn", provider_name, model, chat=chat_id, usage=usage,
                             prompt_text=usage_log.text_of(messages),
                             ms=(time.time() - started) * 1000,
                             ok=stopped, error=None if stopped else repr(e))
            # Keep what the failed response DID produce, before the exception
            # takes the rest with it. A model that thinks for two minutes and
            # is then cut off by its own context limit has done the most
            # expensive part of a turn, and the caller's append_error() only
            # ever wrote the error message - so the thinking, and every
            # measurement of the wait that produced it, vanished. The error
            # turn still lands after this one, exactly as a /stop's marker
            # lands after its partial turn.
            #
            # Not for a stop: that transcript is written by the stopping
            # thread instead (_stopped_history), and doing it here as well
            # would put the same partial response in twice.
            if not stopped:
                # What had streamed before it died, read off the turn context -
                # the same place a stop reads it from, and the only place it
                # exists: `response` is a local of _stream() that went out of
                # scope with the exception.
                live = turnctx.current()
                said = (getattr(live, "partial", "") or "") if live else ""
                thought = reasoning.get("content") or ""
                if said.strip() or thought:
                    turns.append(_partial_turn(said, thought, phases,
                                               provider_name, model, usage))
                    sync()
            raise
        # `response` is what the provider actually sent back, so it is what
        # gets tokenized when the provider declined to say - see usage.py on
        # why that is so often the case on a turn that calls a tool.
        usage_log.record("turn", provider_name, model, chat=chat_id, usage=usage,
                         prompt_text=usage_log.text_of(messages),
                         reply_text=response + (native_call.get("arguments") or ""),
                         ms=(time.time() - started) * 1000)
        if chat_id:
            _set_usage(chat_id, usage, model_key(provider_name, model))

        # What that response cost in time, settled now that the stream is
        # closed. Announced before the branches below decide what the response
        # WAS, because every one of them ends this pass's message one way or
        # another and a UI wants the numbers on the bubble it is still
        # streaming into. The same dict is put on whichever turn gets appended.
        #
        # The response's output tokens are split between the thinking and the
        # reply first, because a rate computed across both is wrong for each -
        # see _split_output, which is the whole of why this exists.
        spent = phases.as_dict(*_split_output(provider_name, model,
                                              reasoning.get("content"), response, usage,
                                              reasoning.get("tokens")))
        if on_timing:
            on_timing(spent)

        def spoke(content):
            """This pass's assistant turn - what the model said, with what it
            cost and what it thought on the way.

            Both extra keys are filtered out before the history goes back to a
            model (provider._NATIVE_KEYS), so neither is ever read as part of
            the conversation. They are here because this is the only place
            that knows them, and because a duration belongs to the message it
            measured - stored on the turn, it moves, survives or dies with it,
            where a sidecar paired by index would slide onto the wrong message
            the first time /compact rewrote the transcript.

            reasoning_content was already kept on turns that made a tool call,
            because DeepSeek's thinking models demand it back (provider.py's
            _REASONING_KEY). Keeping it on plain answers too costs nothing on
            the wire - it is filtered there just the same - and it is what
            lets a reload still show what the model was thinking, instead of
            the thinking existing only for as long as the window stayed open.
            """
            turn = {"role": "assistant", "content": content}
            if spent:
                turn["timing"] = spent
            if reasoning.get("content"):
                turn["reasoning_content"] = reasoning["content"]
            return turn

        # Stopped mid-answer: keep whatever text arrived, but do NOT try to read
        # it as a tool call. A reply cut off partway is usually half a JSON
        # object, and putting it through the bad-JSON path would tell the model
        # to "resend it" - nagging it to retry the very thing the user just
        # cancelled. The check at the top of the next pass ends the turn.
        if should_stop and should_stop():
            if response.strip():
                turns.append(spoke(response))
                sync()
            continue

        call, before, shown_call, retry_msg = _parse_call(response, native_call)

        # Loop-breaker: a stuck model emits the exact same reply pass after pass
        # - same reasoning, same tool call, same result feeding the same reply -
        # and MAX_TOOL_CALLS would run that hundreds of times before giving up.
        # How many in a row is enough to call it stuck is the max_repeats
        # setting: a repeat is not always a mistake, so the same call is
        # allowed that many passes before this steps in.
        # Compared on response+shown_call, not response alone: a native call
        # never embeds in `response` itself (see _stream()'s docstring), so
        # comparing bare `response` would call two DIFFERENT native calls with
        # the same (often empty) preamble "identical" and stop the turn short.
        compare_key = response + shown_call
        repeats = repeats + 1 if compare_key == last_response else 1
        last_response = compare_key

        # Over the line: this pass would be one repeat too many, so it is
        # never run. `repeats` counts THIS reply in, so the comparison is >
        # and not >=: at max_repeats=1 the first reply passes and the second
        # identical one stops the turn, which is what this did before the
        # number was settable.
        if compare_key.strip() and repeats > max_repeats:
            same = ("identical to the previous one" if max_repeats == 1 else
                    "identical to the %d before it" % max_repeats)
            turns.append(spoke(response))
            turns.append({"role": "user", "content":
                          "Tool result: STOPPED - this reply is " + same + ", so "
                          "the agent is repeating itself in a loop. Halting the "
                          "turn. Rephrase the request or check the last tool "
                          "result, which evidently did not move things forward."})
            sync()
            break

        if call is None:
            # Nothing parsed. It's already been shown as it arrived; the only
            # question is what it was. If it clearly MEANT to be a tool call -
            # retry_msg says how - don't silently swallow it as the answer:
            # tell the model and let it resend, a few times before giving up.
            # Otherwise it's a genuine plain answer, so keep it and stop.
            if retry_msg and bad_json < MAX_BAD_JSON:
                bad_json += 1
                turns.append(spoke(response))
                turns.append({"role": "user", "content": retry_msg})
                sync()
                continue
            # No tool call - this is the answer. Keep all of it.
            turns.append(spoke(response))
            sync()
            if on_message and response.strip():
                on_message(response.strip(), "answer")
            break

        bad_json = 0  # a call parsed cleanly - reset the counter

        # There IS a tool call - store it in DeepSeek/OpenAI's own shape: the
        # prose as the assistant's plain content, the call itself as a
        # tool_calls entry with a generated id, so the result below can be tied
        # back to it by that id (same as a real "tool" role message would be).
        #
        # raw_call is the exact text the model actually wrote for the call -
        # DSML tags, plain JSON, whatever its own syntax was that turn. Kept
        # verbatim so provider.py's _compat() can replay THIS instead of
        # reconstructing a synthetic "name(json_args)" text: a model reading
        # back a fake reconstruction of its own history can't tell "this
        # worked" from "this didn't", because both collapse to the exact same
        # shape - which is what was quietly teaching deepseek to keep
        # repeating its own past mistakes instead of the syntax it actually
        # used when it got it right.
        #
        # reasoning_content is stored alongside it when the model produced any
        # - a thinking model's own working, which is never shown and never
        # part of the reply. It is kept for one reason: DeepSeek's thinking
        # models 400 on a replayed turn that made a call without it. Only
        # written when there IS some, so nothing changes for the models that
        # don't think aloud (see provider.py's _REASONING_KEY).
        call_id = "call_" + uuid.uuid4().hex[:8]
        made_call = {
            "role": "assistant",
            "content": before,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": call["tool"], "arguments": json.dumps(call.get("args", {}))},
            }],
            "raw_call": shown_call,
        }
        if reasoning.get("content"):
            made_call["reasoning_content"] = reasoning["content"]
        if spent:
            made_call["timing"] = spent
        turns.append(made_call)
        sync()
        # The exact call text (prose + the JSON, tail trimmed) so a UI can seal
        # the bubble it streamed and open the tool's result block straight away.
        if on_tool_call:
            on_tool_call(before + shown_call)
        # What the model SAID on its way to that call, if anything - the running
        # commentary, without the call itself, which is not something anyone
        # wants read to them. Fired here, before the tool runs, so a UI reading
        # it out gets the whole length of the tool call to do it in.
        if on_message and before.strip():
            on_message(before.strip(), "call")

        # Safety gate. The turn's SAFETY NUMBER decides how the call is
        # treated: 10 runs it unvetted, 0 puts it to the human without asking a
        # model at all, and anything between sends it to the checking model for
        # a 0-10 rating and compares that against the number.
        # tool_validation.check() does all of that and answers with one of
        # three words; the three branches here are that answer.
        #
        # A denial tells the model to STOP AND WAIT, not to find another way.
        #
        # `safety_threshold` and `safety` come from the chat (or a cron job's line
        # in cron.json); both None means nobody said, so the settings page
        # decides. See tool_validation.threshold_for for the full order.
        threshold = tool_validation.threshold_for(safety_threshold, safety, chosen)
        outcome, reason = tool_validation.check(
            call, threshold, prompt=safety_prompt, extra=safety_extra)

        if outcome == tool_validation.SKIP:
            # Nothing was checked, so say so rather than saying nothing: a tool
            # result with no safety row used to be indistinguishable from one
            # whose row simply failed to load, and in a cron job's chat - where
            # the setting comes from cron.json and can differ per job - "was
            # this checked?" is exactly the question you open the chat to
            # answer. It also keeps the log one line per call, which is what
            # the page pairs rows to results by.
            skipped = _log_validation(shown_call, True, None, checked=False)
            if on_safety:
                on_safety(True, skipped, checked=False)
        else:
            safe = outcome == tool_validation.RUN
            _log_validation(shown_call, safe, reason)
            if on_safety:
                on_safety(safe, reason)
            # rstrip because a reason is a sentence from the checking model as
            # often as it is one of ours, and half of them end in a full stop
            # that reads as a stutter in front of " - run it anyway?".
            if not safe and not approve("[safety] " + reason.rstrip(" .")
                                        + " - run it anyway?"):
                turns.append({"role": "tool", "tool_call_id": call_id, "content":
                              "DENIED - the user did not approve this call. Stop "
                              "working on this task: reply with a brief "
                              "acknowledgement of the denial, then wait for the "
                              "user's next instruction. Do not retry the call, "
                              "work around it another way, or carry on with the "
                              "task unasked."})
                sync()
                if on_tool_result:
                    on_tool_result("DENIED - you did not approve this call.",
                                   call["tool"])
                continue

        # chat_id goes with the call so a tool that keeps something per
        # conversation - the terminal's open shell - knows whose it is. It
        # comes from the caller, never from the model's own args. The
        # workspace rides along the same way and for the same reason: which
        # machine and which root a file tool works in is the chat's business,
        # not something the model gets to put in its arguments.
        ran = timing.now()
        result = tool_processor.process(
            call, chat_id, workspace_id=live_workspace(chat_id, workspace_id))
        # How long the tool itself took, which is a different question from how
        # long the model took and often the more interesting one: a turn that
        # felt slow is as likely to have been a 40-second web fetch as a slow
        # model, and until now nothing on the screen could tell you which.
        took = {"ms": timing.ms(ran)}
        turns.append({"role": "tool", "tool_call_id": call_id,
                      "content": result, "timing": took})
        sync()
        if on_tool_result:
            on_tool_result(result, call["tool"], took)

    return json.dumps(turns, indent=2)


def turn(c, text, on_text=None, approve=_approve, provider_name=None, model=None,
         temperature=None, on_tool_call=None, on_tool_result=None, on_safety=None,
         safety=None, safety_prompt=None, inject=None, safety_threshold=None,
         safety_extra=None, on_begin=None, on_message=None,
         on_reasoning=None, on_timing=None, on_request=None, on_thought=None,
         on_reclassify=None):
    """One turn of agent `c` through run(), mirrored to its file as it goes.
    Serialised against other turns of the same agent by its turn slot; turns of
    OTHER agents run in parallel. on_text/approve pass through to run(), so a
    front-end other than the terminal (server.py) can catch the stream and
    answer the safety gate its own way.

    Every callback the caller handed in is wrapped so it goes silent the moment
    this turn is stopped. That is what lets /stop abandon the thread outright
    instead of waiting for it: a stopped turn still running somewhere cannot
    stream text to a page, cannot write the transcript, cannot report a tool
    result, because all three of those are these callbacks. See
    turnctx.guard(), and request_stop() for the other half.

    The model comes from the agent itself: its header is re-read here, at the
    start of the turn, so a /model (or /temperature) on the loaded chat or a
    hand-edit to its file takes effect on this very next request.
    provider_name/model/temperature still override when a caller pins the turn
    explicitly, but nothing needs to any more.

    The whole safety block - safety_threshold, safety_extra, safety_prompt and
    older safety flag - works the same way and in the same order: the caller's
    if it passed any (cron.py passes the job's, read fresh from cron.json), else
    the agent's own from its settings .json, else the settings page. None at
    every level is the normal case and means "as the settings page says".

    Only turns IN THIS PROCESS can be stopped: /stop names an agent. Cron jobs
    are real agents too (their own turn slot, file and listing) but run in the
    separate cron watcher process, so /stop can't reach them from here.

    A cron run has no special case here and doesn't want one: each run gets its
    own chat (see cron.py's new_run), so it starts empty because it IS empty,
    and talking to that chat afterwards is an ordinary turn over an ordinary
    history - the run and everything said since, and nothing from the runs
    before it, because those are other chats."""
    _turn_chat.chat = c

    # Re-read the model from the file's header now, so this turn runs on
    # whatever the chat currently says - not whatever it said when it was opened.
    c.reload_model()
    prov, mod, temp = c.models()
    prov = provider_name or prov
    mod = model or mod
    temp = temperature if temperature is not None else temp
    # reload_model() re-read these too, so a hand-edit to the .json - or the
    # mirror cron.py just wrote - is picked up by this very turn.
    safe_on = safety if safety is not None else c.safety
    safe_prompt = safety_prompt or c.safety_prompt
    # Same order, for the same reason: the caller's if it passed one (cron.py
    # passes the job's, read fresh from cron.json), else the chat's own from
    # its settings .json, else nothing and the settings page decides.
    safe_level = safety_threshold if safety_threshold is not None else c.safety_threshold
    safe_extra = safety_extra or c.safety_extra

    stem = c.id
    ctx = turnctx.TurnContext(stem)
    # Recorded before the chat is even taken, so a stop landing in the gap
    # before run() has built its turns list can still say what was asked.
    ctx.text = text
    # Wait for whatever is running in this chat. A second message sent while a
    # turn is still going starts THIS function on its own thread right away and
    # parks here; a /stop on the turn ahead releases the slot immediately, so
    # the wait ends then rather than whenever that turn's thread unwinds.
    c.slot.acquire(ctx)
    # Published only now that the turn actually owns the chat, so /stop is never
    # ambiguous when two are lined up: it finds the one that is RUNNING, and the
    # one still queued goes on to run normally afterwards. Clearing the stop
    # flag has to wait for the same moment, or a message sent a beat before a
    # /stop would wipe the stop meant for the turn still in flight.
    turnctx.publish(ctx)
    turnctx.bind(ctx)
    clear_stop(stem)
    # The first moment this turn owns the chat and its history is settled -
    # which is the only moment a caller can note where its own turn begins. A
    # second message sent while the first was still going has been sitting in
    # the acquire() above; anything it read before that wait was a snapshot
    # from before the turn ahead of it had written a word. server.py uses this
    # to mark the start of the turn it is about to read out loud.
    if on_begin:
        on_begin(c.history)
    try:
        def keep(updated):
            c.history = updated
            c.save()

        # c.history is read HERE, owning the chat, not on the way in: a second
        # message sent while the first was still working starts this function
        # immediately and then waits right here, so anything read before the
        # wait is a snapshot from before the turn ahead of it finished.
        c.history = run(text, c.history,
                        provider_name=prov, model=mod, temperature=temp,
                        approve=turnctx.guard(ctx, approve),
                        on_save=turnctx.guard(ctx, keep),
                        on_text=turnctx.guard(ctx, on_text),
                        should_stop=lambda: stop_requested(stem),
                        chat_id=stem,
                        on_tool_call=turnctx.guard(ctx, on_tool_call),
                        on_tool_result=turnctx.guard(ctx, on_tool_result),
                        on_safety=turnctx.guard(ctx, on_safety),
                        on_message=turnctx.guard(ctx, on_message),
                        on_reasoning=turnctx.guard(ctx, on_reasoning),
                        on_timing=turnctx.guard(ctx, on_timing),
                        on_request=turnctx.guard(ctx, on_request),
                        on_thought=turnctx.guard(ctx, on_thought),
                        on_reclassify=turnctx.guard(ctx, on_reclassify),
                        pinned=c.pinned, safety=safe_on, safety_prompt=safe_prompt,
                        safety_threshold=safe_level, safety_extra=safe_extra,
                        inject=inject, workspace_id=c.workspace)
        # run() syncs as it goes, so the file is usually already this - but it
        # is the RETURNED history that's authoritative, and leaving the two to
        # agree by convention means anything run() adds after its last sync
        # lives only in memory until some later turn happens to write it out.
        if not ctx.cancelled:
            c.save()
    except turnctx.Stopped:
        # This turn was abandoned. request_stop() has already written the
        # stopped transcript, released the chat and told the front-end, so there
        # is nothing left to do and nothing to report - swallowing it here is
        # what keeps /stop from surfacing as a crash in the caller.
        pass
    finally:
        # Both are no-ops when /stop already did them - unpublish only clears an
        # entry that is still ours, release only releases a slot we still hold -
        # so an abandoned thread arriving here late cannot disturb the turn that
        # has since taken over the chat.
        turnctx.unpublish(ctx)
        c.slot.release(ctx)
    # Only a turn that ended on its own terms clears the flag. An abandoned one
    # arriving here is late by definition - the chat may well be on its next
    # turn, and if THAT one has since been stopped too, clearing here would wipe
    # a stop meant for it and leave stop_requested() saying the opposite of the
    # truth. The next turn clears it for itself as it starts (above), which is
    # the only place that can know it is clearing its own.
    if not ctx.cancelled:
        clear_stop(stem)
    # The context is deliberately left BOUND to this thread. Whoever called
    # this still has cleanup of its own to do - server.py's worker broadcasts
    # the turn as finished - and needs to know whether the turn it just ran was
    # stopped, on the error path as much as the normal one. turnctx.cancelled()
    # answers that, and the next turn on this thread binds its own context over
    # the top before anything else happens.
    return ctx


def prompt(text, on_text=None, approve=_approve):
    """One turn of the CURRENT chat - what typed and spoken input call."""
    turn(current, text, on_text=on_text, approve=approve)


def main():
    global notify
    # UTF-8 out and ANSI escapes understood, before a word is printed. Windows
    # defaults to the system codepage, where the first em dash in a reply is a
    # UnicodeEncodeError rather than a dash.
    termios.setup_console()
    # A report goes back to the chat that spawned it, current or not.
    notify = lambda note, origin: turn(chat(origin), note)
    print("chat: " + str(current.path))
    voice_input.start(prompt)
    while True:
        text = _clean(input("> "))
        if text:
            try:
                prompt(text)
            except Exception as e:
                # Same reasoning as server.py's worker: the provider's own
                # message names the fix (a bad model id, an auth failure), and
                # without this it killed the whole REPL - the agent simply
                # vanished mid-turn. Kept in the history too, so the model sees
                # what went wrong on the next turn.
                msg = type(e).__name__ + ": " + str(e)
                append_error(current, msg)
                print(RED + "error: " + msg + RESET)


if __name__ == "__main__":
    main()
