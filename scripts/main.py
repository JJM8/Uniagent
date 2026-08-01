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
from datetime import date, datetime
import uuid
from pathlib import Path

import provider
import settings
import tokens
import tool_processor
import tool_validation
import voice_input

name = "Uniagent"

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

CONTEXT = Path(__file__).parent.parent / "context"

# What counts as a context file. Anything else in context/ (an image, a stray
# .json) is left alone rather than pasted into the prompt as mojibake.
CONTEXT_SUFFIXES = (".md", ".txt")

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


def _under_chats(path):
    """The folder parts of a chat file's path, relative to chats/ - ('cron',
    'ai-news', '003') for chats/cron/ai-news/003/history.json. None when the
    path isn't under chats/ at all."""
    folder = Path(path).parent
    for base in (CHATS, CHATS.resolve()):
        try:
            return folder.relative_to(base).parts
        except ValueError:
            try:
                return folder.resolve().relative_to(base).parts
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


MAX_TOOL_CALLS = 1000  # cap it so it can't call tools forever

# Closes out a turn that /stop cut short. A constant because it's also matched
# on: a stopped subagent still reports what it managed to write, and finding
# that means stepping back past this line.
STOPPED = "[stopped by the user]"
MAX_BAD_JSON = 5       # how many times to ask the model to fix a broken tool call

# What labels a message the user sent mid-turn (run()'s `inject`). It has to
# say WHEN it was sent: it arrives after a tool result the model is still
# reasoning about, and read as a plain user turn it looks like an answer to a
# question the model never asked.
MID_TURN = "The user sent this while you were working, mid-task: "

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
    conversation with a model behind it. The lock is PER AGENT, so turns of
    different agents run in parallel.

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
    # safety/safety_prompt are this agent's own answer to the tool-call safety
    # gate, both None = follow the settings page:
    #   safety         True/False to force the check on or off for this agent
    #                  alone, regardless of the global "safety_validation"
    #   safety_prompt  this agent's own vetting prompt instead of the global
    #                  "safety_prompt" - must contain "{call}" (see
    #                  tool_validation.validate_tool_use)
    # A cron job's are mirrored here from cron.json by cron.py's _ensure_chats,
    # so opening the job's chat shows what it actually runs under; cron.json
    # stays the source of truth for those.
    # started is when a cron RUN began, written once by cron.new_run(). Only a
    # cron run's chat has one - it is what the chats panel labels the job's
    # older runs by, under its "history" toggle, since a run folder is numbered
    # and a number is not a thing anyone can pick a run out by.
    SETTINGS_KEYS = ("provider", "model", "temperature", "name", "started", "safety",
                     "safety_prompt", "input_tokens",
                     "output_tokens", "tokens_model", "tokens_at",
                     "context_input", "context_max", "context_model",
                     "context_exact", "pinned")

    def __init__(self, path, provider=None, model=None, temperature=None):
        self.path = Path(path)  # history.json; settings.json sits beside it
        self.id = chat_id(self.path)
        # The id a browser knows this chat by - the same string for an ordinary
        # chat, 'cron/<name>' for a cron job. See chat_route().
        self.route = chat_route(self.path)
        self.lock = threading.Lock()
        self.history = self.path.read_text() if self.path.exists() else ""
        cfg = self._read_settings()
        # A model passed in (a cron job knows its from cron.json before its files
        # exist) wins; otherwise the .json; otherwise None = follow the default.
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

    def _settings_path(self):
        return self.path.parent / SETTINGS_FILE

    def _read_settings(self):
        try:
            return json.loads(self._settings_path().read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_settings(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(self, k) for k in self.SETTINGS_KEYS
                if getattr(self, k) is not None}
        self._settings_path().write_text(json.dumps(data, indent=2))

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
        self.safety_prompt = cfg.get("safety_prompt")
        self.pinned = cfg.get("pinned") or []

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
        self.path.write_text(self.history)


class Chat(Agent):
    """A conversation the user types into. Unpinned by default, so a fresh chat
    follows the settings-page model until /model pins it to its own."""

    kind = "chat"


# Every agent opened this run, one object per file. Two callers asking for the
# same one must get the same object - separate copies would each hold their own
# history and lock, and two turns could scramble the file.
_open = {}
_open_lock = threading.Lock()


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
        text = old_md.read_text()
        try:
            turns = json.loads(text)
            if not isinstance(turns, list):
                raise ValueError("not a turns list")
        except (json.JSONDecodeError, ValueError):
            turns = _turns_from_flat(text)  # a legacy flat-text transcript
        (folder / HISTORY_FILE).write_text(json.dumps(turns, indent=2))
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
        text = flat.read_text()
        name = None
        if text.startswith("<!-- uniagent"):  # strip the step-2 header if present
            end = text.find("-->")
            if end != -1:
                for line in text[:end].splitlines():
                    if line.strip().lower().startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                text = text[end + 3:].lstrip("\n")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / HISTORY_FILE).write_text(json.dumps(_turns_from_flat(text), indent=2))
        cfg = {"provider": "deepseek", "model": "deepseek-v4-flash"}
        if name:
            cfg["name"] = name
        (folder / SETTINGS_FILE).write_text(json.dumps(cfg, indent=2))
        flat.unlink()


_migrate_layout()      # flat chats/<id>.md  -> a folder per chat
_migrate_json_names()  # <id>.md/<id>.json   -> history.json/settings.json


def busy_chats():
    """Ids of every open chat that is mid-turn right now."""
    with _open_lock:
        return sorted(cid for cid, c in _open.items() if c.lock.locked())


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
    if c.lock.locked() or _has_conversation(c):
        return None
    folder = c.path.parent
    if folder.exists():
        # Guard the delete hard - it's the only irreversible thing in here.
        # Anything that isn't one of our own chats/chat-*/ folders is left
        # exactly as it is, registry included: dropping it from _open without
        # deleting it would let the next chat() call build a SECOND Agent for
        # the same files, with its own lock, which is the one thing _open
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

# How a front-end is told a chat's own turn was just asked to stop, called as
# on_stop(stem) the instant /stop lands - BEFORE the worker thread winds down.
# server.py points this at a broadcast that seals the streaming bubble on the
# page right away, so the stop looks instant even though the worker can only
# give up cooperatively a chunk or two later. None when nothing is watching.
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


# Chats whose turn has been asked to stop, by stem. A running turn is a worker
# thread part-way through a tool loop, and a thread can't be safely killed from
# outside - so stopping is COOPERATIVE: the turn checks this at the points it
# can safely give up (between tool-loop passes, and between streamed chunks)
# and returns early, leaving the history complete rather than half-written.
_stops = set()
_stops_lock = threading.Lock()


def request_stop(stem):
    """Ask the turn running in chat `stem` to stop at its next check."""
    with _stops_lock:
        _stops.add(stem)


def stop_requested(stem):
    with _stops_lock:
        return stem in _stops


def clear_stop(stem):
    with _stops_lock:
        _stops.discard(stem)


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
_UNCHECKED_CRON = ("no safety check - this cron job runs with safety: off in "
                   "cron.json, so its tool calls are auto-approved and run "
                   "unvetted. Set safety: on for this job to have them checked.")
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
        with open(VALIDATIONS / (c.id + ".jsonl"), "a") as f:
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
                body = p.read_text().strip()
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
                stripped = p.read_text().strip()
            except OSError:
                continue
            first_line = stripped.splitlines()[0] if stripped else ""
            desc = first_line.lstrip("#").strip() or "(no description)"
            lines.append(p.stem + ": " + desc)

        if lines:
            text = (
                "Memories: individual topic files, one per file, living in "
                + str(MEMORIES) + " - NOT loaded automatically, unlike context/. "
                "If what's being discussed matches one below, or reading it would "
                "help, read that file in full FIRST (read_file or ask_file) before "
                "answering - don't wait to be asked. If something worth keeping "
                "comes up that belongs in one of these, append to it (check it "
                "isn't already there first, don't duplicate). If it's a new fact "
                "specific to a project, person, or topic none of these cover - not "
                "a general fact about the user, which belongs in the memory file "
                "in context/ - create a new "
                "file here with write_file: " + str(MEMORIES) + "/<topic>.md, first "
                "line a short one-line description, so it's listed here next turn.\n"
                + "\n".join(lines)
            )
        else:
            text = ""
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


def injection_breakdown(provider_name, model, pinned=None):
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
                text = (ROOT / rest).read_text().strip()
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
    return breakdown


def system_text(provider_name, model, pinned=None):
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
    parts = injection_breakdown(provider_name, model, pinned)
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


def _stream(messages, provider_name, model, temperature, on_text, should_stop=None, usage=None,
            native_call=None, reasoning=None):
    """One model response, read as it's written. Returns everything received.

    Shows each piece the moment it arrives - printed here, or handed to on_text
    if the caller wants it somewhere other than the terminal. Stops reading the
    moment a complete tool call has come through: a capable model keeps writing
    after its call, inventing tool results and firing more calls, and none of
    that tail should be shown, remembered, or paid for.

    `should_stop`, if given, is checked between chunks: a stopped turn gives up
    mid-answer rather than making the user wait out a long one, and whatever
    arrived before that is still returned so it can be kept.

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
    a thinking model's own reasoning_content. It is never shown and never part
    of the reply - run() stores it on the turn purely so it can be handed back
    on the next request, which DeepSeek's thinking models demand of any turn
    that made a tool call (see provider.py's _REASONING_KEY).
    """
    response = ""
    in_call = False  # has the call started being written yet?

    #temp guard
    if temperature is None:
        temperature = chosen["temperature"]

    tools = None
    if native_call is not None:
        tools = tool_processor.tools_schema(tool_processor.shape_for(provider_name))

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
        if not in_call:
            in_call = True
            if response and not response.endswith("\n"):
                piece = "\n\n" + piece  # same gap the redraw puts there
        if on_text:
            on_text(piece)
        else:
            print(GREEN + piece + RESET, end="", flush=True)

    for chunk in provider.stream_response(messages, provider=provider_name, model=model,
                                          temperature=temperature, usage=usage,
                                          tools=tools, tool_call=native_call,
                                          reasoning=reasoning,
                                          on_call_delta=show_call):
        if should_stop and should_stop():
            break
        response += chunk
        # Reply text only. The call itself never comes through here - it
        # arrives on the structured channel and is shown by show_call above -
        # so there is nothing to scan, trim or break out of mid-stream: the
        # provider stops on its own once it has decided to call a tool.
        if chunk:
            if on_text:
                on_text(chunk)
            else:
                print(chunk, end="", flush=True)

    if not on_text:
        print(RESET if in_call else "")

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
    text = "Error: the turn failed - " + msg
    try:
        turns = json.loads(c.history) if c.history else []
    except json.JSONDecodeError:
        c.history += text + "\n"
        c.save()
        return
    turns.append({"role": "assistant", "content": text})
    c.history = json.dumps(turns, indent=2)
    c.save()


def run(text, history, provider_name=None, model=None, temperature=0, approve=_approve,
        on_save=None, on_text=None, should_stop=None, chat_id=None,
        on_tool_call=None, on_tool_result=None, on_safety=None, pinned=None,
        safety=None, safety_prompt=None, inject=None):
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
    whole turn. `should_stop`, if given, is checked between tool-loop passes and
    between streamed chunks: when it goes true the turn gives up early and
    returns the history as it stands, which is how /stop cuts a turn short
    (a worker thread can't be killed from outside, so it has to agree to stop).

    `on_tool_call`, `on_tool_result` and `on_safety`, if given, fire the moment
    a call is parsed, its result comes back, and the safety verdict is known - so
    a UI can draw the tool's own block (the call, safety row and result dropdown)
    as it happens, instead of waiting out the whole turn for a redraw.

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

    turns.append({"role": "user", "content": text})
    bad_json = 0  # consecutive "meant to be a tool call but wouldn't parse" tries
    last_response = None  # the previous pass's raw reply, to catch a stuck loop

    def sync():
        if on_save:
            on_save(json.dumps(turns, indent=2))

    sync()

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
        system = system_text(provider_name, model, pinned)
        messages = [{"role": "system", "content": system}] + turns
        usage = {}
        native_call = {}
        reasoning = {}
        response = _stream(messages, provider_name, model, temperature, on_text, should_stop,
                           usage, native_call, reasoning)
        if chat_id:
            _set_usage(chat_id, usage, model_key(provider_name, model))

        # Stopped mid-answer: keep whatever text arrived, but do NOT try to read
        # it as a tool call. A reply cut off partway is usually half a JSON
        # object, and putting it through the bad-JSON path would tell the model
        # to "resend it" - nagging it to retry the very thing the user just
        # cancelled. The check at the top of the next pass ends the turn.
        if should_stop and should_stop():
            if response.strip():
                turns.append({"role": "assistant", "content": response})
                sync()
            continue

        call, before, shown_call, retry_msg = _parse_call(response, native_call)

        # Loop-breaker: a stuck model emits the exact same reply pass after pass
        # - same reasoning, same tool call, same result feeding the same reply -
        # and MAX_TOOL_CALLS would run that hundreds of times before giving up.
        # Compared on response+shown_call, not response alone: a native call
        # never embeds in `response` itself (see _stream()'s docstring), so
        # comparing bare `response` would call two DIFFERENT native calls with
        # the same (often empty) preamble "identical" and stop the turn short.
        compare_key = response + shown_call
        if compare_key.strip() and compare_key == last_response:
            turns.append({"role": "assistant", "content": response})
            turns.append({"role": "user", "content":
                          "Tool result: STOPPED - this reply is identical to the "
                          "previous one, so the agent is repeating itself in a loop. "
                          "Halting the turn. Rephrase the request or check the last "
                          "tool result, which evidently did not move things forward."})
            sync()
            break
        last_response = compare_key

        if call is None:
            # Nothing parsed. It's already been shown as it arrived; the only
            # question is what it was. If it clearly MEANT to be a tool call -
            # retry_msg says how - don't silently swallow it as the answer:
            # tell the model and let it resend, a few times before giving up.
            # Otherwise it's a genuine plain answer, so keep it and stop.
            if retry_msg and bad_json < MAX_BAD_JSON:
                bad_json += 1
                turns.append({"role": "assistant", "content": response})
                turns.append({"role": "user", "content": retry_msg})
                sync()
                continue
            # No tool call - this is the answer. Keep all of it.
            turns.append({"role": "assistant", "content": response})
            sync()
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
        turns.append(made_call)
        sync()
        # The exact call text (prose + the JSON, tail trimmed) so a UI can seal
        # the bubble it streamed and open the tool's result block straight away.
        if on_tool_call:
            on_tool_call(before + shown_call)

        # Safety gate: a call the verification model calls safe runs straight
        # away; anything it flags goes to `approve`, with the model's reasoning
        # so whoever answers knows what worried it. A denial tells the model to
        # STOP AND WAIT, not to find another way. With the gate off the call
        # runs unvetted - which is logged too, so the chat can show that it was
        # never checked instead of showing nothing at all.
        # `safety` is this turn's own override (a cron job's `safety:` line, an
        # agent's settings .json); None means nobody said, so the settings page
        # decides as it always has.
        if not (safety if safety is not None else chosen["safety_validation"]):
            # The check is OFF for this turn, so this call ran unvetted. Say so,
            # rather than saying nothing: a tool result with no safety row used
            # to be indistinguishable from one whose row simply failed to load,
            # and in a cron job's chat - where the setting comes from cron.json
            # and can differ per job - "was this checked?" is exactly the
            # question you open the chat to answer. It also keeps the log one
            # line per call, which is what the page pairs rows to results by.
            skipped = _log_validation(shown_call, True, None, checked=False)
            if on_safety:
                on_safety(True, skipped, checked=False)
        else:
            safe, reason = tool_validation.validate_tool_use(call, prompt=safety_prompt)
            _log_validation(shown_call, safe, reason)
            if on_safety:
                on_safety(safe, reason)
            if not safe and not approve("[safety] flagged as possibly unsafe: "
                                        + reason + " - run it anyway?"):
                turns.append({"role": "tool", "tool_call_id": call_id, "content":
                              "DENIED - the user did not approve this call. Stop "
                              "working on this task: reply with a brief "
                              "acknowledgement of the denial, then wait for the "
                              "user's next instruction. Do not retry the call, "
                              "work around it another way, or carry on with the "
                              "task unasked."})
                sync()
                if on_tool_result:
                    on_tool_result("DENIED - you did not approve this call.")
                continue

        # chat_id goes with the call so a tool that keeps something per
        # conversation - the terminal's open shell - knows whose it is. It
        # comes from the caller, never from the model's own args.
        result = tool_processor.process(call, chat_id)
        turns.append({"role": "tool", "tool_call_id": call_id, "content": result})
        sync()
        if on_tool_result:
            on_tool_result(result)

    return json.dumps(turns, indent=2)


def turn(c, text, on_text=None, approve=_approve, provider_name=None, model=None,
         temperature=None, on_tool_call=None, on_tool_result=None, on_safety=None,
         safety=None, safety_prompt=None, inject=None):
    """One turn of agent `c` through run(), mirrored to its file as it goes.
    Serialised against other turns of the same agent by its lock; turns of
    OTHER agents run in parallel. on_text/approve pass through to run(), so a
    front-end other than the terminal (server.py) can catch the stream and
    answer the safety gate its own way.

    The model comes from the agent itself: its header is re-read here, at the
    start of the turn, so a /model (or /temperature) on the loaded chat or a
    hand-edit to its file takes effect on this very next request.
    provider_name/model/temperature still override when a caller pins the turn
    explicitly, but nothing needs to any more.

    safety/safety_prompt work the same way and in the same order: the caller's
    if it passed any (cron.py passes the job's, read fresh from cron.json), else
    the agent's own from its settings .json, else the settings page. None at
    every level is the normal case and means "as the settings page says".

    Only turns IN THIS PROCESS can be stopped: /stop names an agent. Cron jobs
    are real agents too (their own lock, file and listing) but run in the
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

    stem = c.id
    # Clear ONLY once this turn actually holds the lock - not before. A second
    # message sent while a turn is still running starts THIS function on its
    # own thread right away, which used to clear_stop() here before ever
    # blocking on c.lock - wiping out a /stop meant for the turn still in
    # flight a moment before its own should_stop() check could see it, so the
    # turn you tried to stop just carried on regardless. Waiting until the
    # lock is actually held means nothing else for this chat is running, so
    # clearing here can only ever affect THIS turn about to start.
    with c.lock:
        clear_stop(stem)

        def keep(updated):
            c.history = updated
            c.save()

        # c.history is read HERE, holding the lock, not on the way in: a second
        # message sent while the first was still working starts this function
        # immediately and then waits right here, so anything read before the
        # wait is a snapshot from before the turn ahead of it finished.
        c.history = run(text, c.history,
                        provider_name=prov, model=mod, temperature=temp,
                        approve=approve, on_save=keep,
                        on_text=on_text, should_stop=lambda: stop_requested(stem),
                        chat_id=stem, on_tool_call=on_tool_call,
                        on_tool_result=on_tool_result, on_safety=on_safety,
                        pinned=c.pinned, safety=safe_on, safety_prompt=safe_prompt,
                        inject=inject)
        # run() syncs as it goes, so the file is usually already this - but it
        # is the RETURNED history that's authoritative, and leaving the two to
        # agree by convention means anything run() adds after its last sync
        # lives only in memory until some later turn happens to write it out.
        c.save()
    clear_stop(stem)


def prompt(text, on_text=None, approve=_approve):
    """One turn of the CURRENT chat - what typed and spoken input call."""
    turn(current, text, on_text=on_text, approve=approve)


def main():
    global notify
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
