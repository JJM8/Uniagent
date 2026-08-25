"""User-side slash commands, checked before anything reaches the model.

Any input line starting with "/" is handled here and answered straight back as
a system line. Commands never enter the history, so the model never sees them
and they cost nothing. process() returns the reply text for a command, or None
when the line is ordinary chat and should run as a turn.

Every command acts on ONE chat, and which one is passed in - process(text,
chat) - rather than read off a global. The terminal passes main.current; the
web passes whichever chat that browser window is showing, which it sends with
the request. Two windows in two different chats therefore get two different
answers to the same /model, and neither disturbs the other.

The two commands that change WHICH chat you are in rather than acting on the
one you are in (/load, /new) can't work that way, since there is no single
"you". They answer with the chat to go to, and the caller decides what that
means: cli.py moves main.current, the web tells that one browser window to
switch. See process()'s return value and NAVIGATION below.

The pending-approval state also lives here: when a turn hits the safety gate,
the worker thread blocks in wait_approval() until the user answers with
/approve y or /approve n - the web front-end's version of the terminal's
blocking input(). Pending approvals are PER CHAT, because turns in different
chats run at the same time - /approve answers the chat it is given.
"""

import re
import threading

import compaction
import claude_session
import cron
import main
import provider
import settings
import tool_validation
import usage
import workspace as workspace_mod

HELP = """commands:
/help - this list
/history - show the raw transcript of the current chat
/chats - list saved chats (newest first)
/load <chat> - continue a saved chat
/new - start a fresh chat
/compact - shorten this chat's history (the full one is kept in chat_archive)
/model - show the main agent's provider/model, and the usable providers
/model <provider> <model> - switch the main agent (any model that provider has)
/name - show this chat's title
/name <text> - set this chat's title (shown instead of its first message)
/name default - clear it, go back to showing the first message
/temperature - show this chat's temperature, and the default
/temperature <0-2> - set this chat's temperature (0 = most predictable)
/temperature default - unpin, follow the settings default temperature
/usage - tokens and requests spent over the last 30 days, by kind, model and chat
/usage today|7d|30d|all - the same over a different window
/usage chat - just the chat you're in (combines with a window)
/workspace - show where this chat's files and terminal work, and the alternatives
/workspace <name> - move this chat to that workspace (its id or its name)
/workspace default - move it back to the default workspace
/approve y|n - answer this chat's pending safety check
/cronsafety - show the safety number each cron job's tool calls run at
/cronsafety <job> 0-10 - the highest danger rating that job runs unasked
/cronsafety <job> default - unset it, follow the cron default again
/continue - pick a stopped or failed turn back up where it left off
/stop - cut this chat's running turn short
/stop <subagent> - stop just that subagent, leaving this chat's turn alone
/restart - restart the web server on new code (not available in the terminal)
/delete chat - delete the chat you're in (lands you back on the previous one)
/delete <chat> - delete another saved chat by name (not gone for good - moved
                 into deleted_chats)"""

_pending = {}  # chat stem -> {"event": Event, "answer": bool, "question": str}


def wait_approval(stem, question=""):
    """Block until this chat's approval is answered - by /approve, a button on
    the page, or the user simply sending another message (which denies it and
    moves on). Called on the worker thread that hit the safety gate."""
    p = {"event": threading.Event(), "answer": False, "question": question}
    _pending[stem] = p
    try:
        p["event"].wait()
    finally:
        _pending.pop(stem, None)
    return p["answer"]


def pending_question(stem):
    """The question this chat is waiting on, or None - so the page can show
    the approval (with its buttons) even after a reload."""
    p = _pending.get(stem)
    return p["question"] if p else None


def deny_pending(stem):
    """Deny this chat's pending approval, if there is one. Used when the user
    moves on - sending a new message while an approval waits answers it no.
    True if something was actually denied."""
    p = _pending.get(stem)
    if p is None:
        return False
    p["answer"] = False
    p["event"].set()
    return True


def _help(arg, chat):
    return HELP


def _history(arg, chat):
    return chat.history if chat.history else "(this chat is empty)"


def _chats(arg, chat):
    # Each chat is a folder now (chats/<id>/history.json); one RUN of a cron
    # job is two levels further down (chats/cron/<job>/<nnn>/history.json).
    files = list(main.CHATS.glob("chat-*/" + main.HISTORY_FILE)) \
        + list((main.CHATS / "cron").glob("*/*/" + main.HISTORY_FILE))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "no saved chats."
    return "\n".join(_chat_id(p) + (" (current)" if p == chat.path else "")
                     for p in files)


def _chat_id(path):
    """The id used to load a chat - its path under chats/, so a cron run keeps
    the job and run it belongs to and points back into the right place."""
    return main.chat_route(path)


def _load(arg, chat):
    """Go to another chat. NAVIGATION: this one doesn't act on `chat`, it
    replaces it, so it answers with where to go and lets the caller move -
    there is no one global "current chat" left to reassign here."""
    if not arg:
        return "usage: /load <chat> - see /chats for the names", None
    # An id like 'chat-1bb28a87' or 'cron/ai-news' (a stray suffix is tolerated,
    # since older pages and habits still tack one on).
    cid = arg.strip().removesuffix(".md").removesuffix(".json")
    if not re.fullmatch(r"[\w-]+(/[\w-]+){0,2}", cid):
        return "no chat called " + arg + " - see /chats", None
    # Build the path ourselves and confirm it lands inside chats/, so nothing
    # like '../../.bashrc' can be loaded (and later saved over).
    path = main.chat_md(cid).resolve()
    if main.CHATS.resolve() not in path.parents or not path.exists():
        return "no chat called " + arg + " - see /chats", None
    # Fine to go to a chat mid-turn: its turn keeps writing to its own object,
    # and you arrive watching it.
    return "loaded " + cid, cid


def _delete(arg, chat):
    """Delete a chat.

    /delete chat   - delete the one you're in (`chat`)
    /delete <name> - delete another saved chat by id

    Either form can name the chat you're in - deleting it moves it into
    deleted_chats same as any other, and the reply carries the id to go to
    instead (main.landing_chat_id), so no front-end is left pointed at a chat
    that no longer exists. NAVIGATION when that happens, an ordinary reply
    otherwise."""
    if not arg:
        return "usage: /delete chat  (to delete current chat) or /delete <name>", None
    arg = arg.strip().lower()
    if arg == "chat":
        cid = chat.id
        if not cid:
            return "no current chat to delete.", None
    else:
        cid = arg.removesuffix(".md").removesuffix(".json")
        if not re.fullmatch(r"[\w-]+(/[\w-]+){0,2}", cid):
            return "no chat called " + arg + " - see /chats", None
    path = main.chat_md(cid).resolve()
    if main.CHATS.resolve() not in path.parents or not path.exists():
        return "no chat called " + arg + " - see /chats", None
    deleting_current = path == chat.path.resolve()
    deleted_dir = main.CHATS / "deleted_chats"
    deleted_dir.mkdir(exist_ok=True)
    chat_folder = path.parent
    # One RUN of a cron job: chats/cron/<job>/<nnn>/, so the job's name is the
    # folder above this one. Deleting a run deletes that run, not the job - the
    # job is the cron.json entry, and its other runs are their own chats.
    is_cron = chat_folder.parent.parent.name == "cron"
    job_name = chat_folder.parent.name if is_cron else None
    # The id, not the folder name: by folder name alone every job's third run
    # is '003' and they would all want deleted_chats/003, so whichever went
    # second couldn't.
    base = cid.replace("/", "-")
    # Deleting the same name twice is normal, not an error - a cron job's chat
    # is recreated by the watcher every tick while its job is still in cron.json
    # (see cron._ensure_chats), so the same name comes back and gets deleted
    # again. Refusing the second one made the chat permanently undeletable: the
    # × in the web UI just reported the clash, forever. Number it instead.
    destination = deleted_dir / base
    n = 2
    while destination.exists():
        destination = deleted_dir / (base + "-" + str(n))
        n += 1
    chat_folder.rename(destination)
    # The Claude Code session this chat was holding, if any. It lives in memory
    # keyed by the chat id, so a delete that left it open would keep a CLI
    # process alive for a conversation nobody can reach any more.
    claude_session.close(main.chat_id(path))
    # The stamps go with it. They live outside the chat folder (see
    # main.STAMPS), keyed by the chat's id, and a cron job's run numbers come
    # BACK: delete run 003 and the watcher makes another 003 within 30
    # seconds. Its first turn is the job's prompt - the same text, so the same
    # role and the same length - which is exactly what stamp_history reuses a
    # stamp on, and the new run's first message would quietly wear the old
    # run's date. Better no date than a confident wrong one.
    main.clear_stamps(main.chat_id(path))
    # Say so when it's going to come straight back, rather than letting it look
    # like the delete silently failed 30 seconds later.
    note = ""
    if is_cron and any(j["name"] == job_name for j in cron._load_jobs()):
        note = (" - '" + job_name + "' is still a job in cron.json, so it keeps "
                "its other runs, and the watcher gives it a fresh chat within "
                "30s if that was its last one. Delete that job in cron.json "
                "to be rid of it for good.")
    if not deleting_current:
        return "deleted " + cid + " (moved to chats/deleted_chats/)" + note, None
    # `previous` is the terminal's one-level-back, and only means anything when
    # this IS the terminal's chat - a browser window has its own history of
    # where it has been and doesn't share this one.
    prefer = main.previous if chat is main.current else None
    landed = main.landing_chat_id(prefer)
    where = (" - now on " + landed) if landed else " - no chats left"
    return ("deleted " + cid + " (moved to chats/deleted_chats/)"
            + where + note), (landed or "")


def _new(arg, chat):
    """A fresh chat. NAVIGATION: it hands back an id and nothing else - no
    folder, no empty history.json (see main.new_chat_id). The chat comes into
    being on the first thing actually written into it, so opening the app or
    hitting "new chat" and then changing your mind leaves nothing behind."""
    cid = main.new_chat_id()
    return "started a fresh chat: " + cid, cid


def _compact(arg, chat):
    """Shorten THIS chat's history: archive it, hand the whole thing to the
    chat's own model to condense, and swap what comes back in as the history.
    See compaction.py - all of it lives there, including why the chat's own
    model does the work rather than one of compaction's own.

    Blocking, unlike every other command here: the user is waiting on the
    result, and there is nothing sensible to say back until the model has
    answered."""
    try:
        return compaction.compact(chat)
    except Exception as e:
        # A provider error must not take the server down with it - and the
        # chat is untouched either way, since nothing is written until the
        # reply is in hand.
        return "compaction failed - " + type(e).__name__ + ": " + str(e)


def _continue(arg, chat):
    """Only ever reached from the model's own uniagent_command tool. Both front
    ends take /continue before the table gets near it, because continuing is
    not a command that answers - it is a turn that starts with no message (see
    main.CONTINUE). Which also makes it the one command the model cannot
    usefully run: it is inside a turn already."""
    return ("/continue picks up a turn that was stopped or that failed, so it "
            "belongs to the user, not to you - you are in a running turn right "
            "now. Carry on with what you were doing.")


def _stop(arg, chat):
    """End a turn. Bare, it ends THIS chat's own turn; given a subagent name, it
    stops just that subagent and leaves the chat alone. The two are deliberately
    separate keys - a subagent is off doing its own long job, and stopping the
    conversation you're in should not kill it (nor the reverse).

    The chat's own turn is over by the time this returns: main.request_stop()
    breaks open whatever the turn was blocked in, writes the stopped transcript
    itself, hands the chat on and tells the front-end - all on THIS thread,
    which is not the one that was busy. The worker is abandoned rather than
    waited for, so how long it takes to notice is nobody's problem: it can no
    longer write anything or reach any screen. That is why this says "stopped"
    outright now, where it used to promise the turn would end shortly.

    A safety check parked on a y/n is denied too. It is a wait on a human
    rather than on a machine, so nothing about it gets broken open - it has to
    be answered, and moving on IS the answer."""
    stem = chat.id

    # A compaction holds the chat's turn slot, so the check below would see a
    # busy chat and report a turn stopped that was never running. It is one
    # request to the model with no safe point to give up at, so say so instead.
    if not arg and compaction.is_compacting(stem):
        return "that chat is being compacted - that can't be stopped part-way."

    if arg:
        tag = "subagent-" + stem + "/" + arg
        if not any(t.name == tag for t in threading.enumerate()):
            return ("no subagent called " + arg + " is running in this chat. "
                    "(/stop on its own stops the chat's own turn.)")
        main.request_stop(tag)
        return ("stopping subagent " + arg + " - it reports back with whatever "
                "it had written.")

    if not chat.slot.held():
        return "nothing is running in this chat."
    # Before the stop, not after: a turn parked on the approval gate is waiting
    # on an answer, and until it gets one it never reaches the point where being
    # cancelled means anything.
    deny_pending(stem)
    if not main.request_stop(stem):
        return "nothing is running in this chat."
    return "stopped - the turn ended here."


def _model(arg, chat):
    """Show or switch the model of THE CHAT THIS RAN IN - just this chat,
    not the others. It is written into that chat's own .md header, so it sticks
    to the chat and survives a restart. Other chats are untouched, and an
    unpinned chat keeps following the settings-page default.

    Takes '<provider> <model>', where model is ANY model that provider
    offers - it is not checked against a list, just passed through, so a model
    newer than provider.MODELS works fine. '<model>' on its own also works
    when that name happens to be one of the known suggestions and so identifies
    its provider unambiguously; otherwise say which provider you mean.

    '/model default' unpins this chat, dropping it back to the settings default.
    Bare, it shows this chat's model and the suggestions per usable provider."""
    default = settings.load()
    usable = provider.available()
    parts = arg.split()

    if not parts:
        prov, mod, _ = chat.models()
        pinned = chat.provider or chat.model
        known = provider.known_models(usable)
        lines = ["this chat: " + prov + " / " + mod
                 + (" (pinned)" if pinned else " (following the default)"),
                 "default:   " + default["provider"] + " / " + default["model"], ""]
        for name in usable:
            lines.append("  " + name + ": " + (", ".join(known.get(name, [])) or "(none listed)"))
        lines.extend([
            "",
            "switch THIS chat with /model <provider> <model>, e.g. /model "
            + (usable[0] if usable else "openai") + " <model>.",
            "'/model default' drops this chat back to the settings default.",
            "Any model the provider offers works - it's tested before switching.",
        ])
        return "\n".join(lines)

    # Unpin: back to following the settings default.
    if len(parts) == 1 and parts[0].lower() in ("default", "clear", "unpin"):
        chat.unpin()
        prov, mod, _ = chat.models()
        return "this chat now follows the settings default: " + prov + " / " + mod + "."

    if len(parts) == 1:
        name = parts[0]
        if name in provider.provider_names():
            # A bare provider name: switch to it, on its default model.
            chosen_provider = name
            chosen_model = provider.default_model(name)
        else:
            # A bare model name. If it's a known suggestion, it identifies its
            # own provider, so switch to that too. Otherwise it's a model id we
            # simply haven't heard of - which is normal and must NOT be an
            # error - so keep the provider already selected and take the name
            # as typed.
            chosen_provider = provider.find_model(name) or chat.models()[0]
            chosen_model = name
    else:
        chosen_provider, chosen_model = parts[0], " ".join(parts[1:])

    if chosen_provider not in usable:
        return ("'" + chosen_provider + "' isn't usable here - it needs an API key or "
                "credentials this machine doesn't have. Available: " + ", ".join(usable))
    if not chosen_model:
        return "/model " + chosen_provider + " <model> - say which model too."

    # Prove the pair works BEFORE switching anything: one tiny real request.
    # The provider's own API is the only authority on which model ids exist -
    # no local list can be. Nothing is saved on failure, so a typo can't leave
    # the chat wedged on a model that doesn't exist.
    error = provider.test_model(chosen_provider, chosen_model)
    if error is not None:
        return ("not switching - " + chosen_provider + " rejected '" + chosen_model
                + "':\n  " + error)

    # It answered, so it's real: pin THIS chat to it (written to its .md header)
    # and remember it for every list/dropdown.
    chat.pin(chosen_provider, chosen_model)
    provider.remember_model(chosen_provider, chosen_model)
    return ("tested and switched: this chat is now on " + chosen_provider
            + " / " + chosen_model + ".")


def _fmt_temp(value):
    """A temperature for display, without a trailing '.0' on whole numbers."""
    return "%g" % value


def _temperature(arg, chat):
    """Show or set the temperature of THE CHAT THIS RAN IN - just this
    chat, not the others. Written into that chat's own .json settings file,
    same as /model, so it sticks to the chat and survives a restart. An
    unpinned chat keeps following the settings-page default.

    Bare, shows this chat's temperature (and the default). A number pins this
    chat to it - 0 is most predictable, higher is more random; providers treat
    0-2 as the sane range, so that's what's enforced here too. '/temperature
    default' unpins this chat, dropping it back to the settings default."""
    lo, hi = settings.TEMPERATURE_RANGE

    if not arg:
        _, _, temp = chat.models()
        pinned = chat.temperature is not None
        default = settings.load()["temperature"]
        return ("this chat: " + _fmt_temp(temp) + (" (pinned)" if pinned else " (following the default)") + "\n"
                + "default:   " + _fmt_temp(default) + "\n\n"
                + "0 = most predictable, higher = more random - usual range "
                + _fmt_temp(lo) + "-" + _fmt_temp(hi) + ". Not a standardised unit, "
                + "just how far this app scales each provider's own randomness knob.\n"
                + "set this chat with /temperature <number>, e.g. /temperature 0.7.\n"
                + "'/temperature default' drops this chat back to the settings default.")

    if arg.lower() in ("default", "clear", "unpin"):
        chat.unpin_temperature()
        _, _, temp = chat.models()
        return "this chat now follows the settings default temperature: " + _fmt_temp(temp) + "."

    try:
        value = float(arg)
    except ValueError:
        return "usage: /temperature <number between " + _fmt_temp(lo) + " and " + _fmt_temp(hi) + ">"
    if not (lo <= value <= hi):
        return ("temperature must be between " + _fmt_temp(lo) + " and " + _fmt_temp(hi)
                + ". Got: " + arg)

    chat.set_temperature(value)
    return "this chat's temperature is now " + _fmt_temp(value) + "."


def _workspace_listing(current, by_user=True):
    """Every configured workspace, with the one this chat is in marked. The
    list is provider.workspaces() - what the settings page writes and what the
    picker in the corner of the chat window is filled from - and it is never
    empty: the Uniagent folder itself is always the first entry.

    `by_user` only changes the line at the bottom saying HOW to move, because
    the two readers move differently: a person types /workspace <id> into the
    chat box, and the model calls the `workspace` tool with an id. Telling
    either one the other's way is telling it to do something it cannot."""
    lines = []
    for w in provider.workspaces():
        marks = []
        if w["id"] == current:
            marks.append("current")
        elif not current and w["default"]:
            marks.append("current - the default")
        elif w["default"]:
            marks.append("default")
        if w["ssh"]:
            marks.append("on " + w["ssh"])
        lines.append("  " + w["id"] + " (" + w["name"] + ") - " + w["path"]
                     + (("  [" + ", ".join(marks) + "]") if marks else ""))
    how = ("move this chat with /workspace <id or name>.\n"
           "'/workspace default' moves it back to the default one.\n"
           if by_user else
           "move this chat by calling the workspace tool with that id.\n"
           "id \"default\" moves it back to the default one.\n")
    return ("workspaces:\n" + "\n".join(lines) + "\n\n" + how
            + "New ones are added on the settings page - this only moves between "
            "the ones above.")


def _workspace(arg, chat, by_user=True):
    """Show or change WHERE this chat's tools do their work - which folder, and
    which machine.

    This is a command rather than a tool because it is a thing done TO the
    conversation, like /model: it changes what the next tool call means instead
    of being one. The agent still reaches it, through uniagent_command, which is
    what `by_user` is about - see below.

    Every file tool (read_file, write_file, edit_file, ask_file) and the
    terminal work inside whichever workspace this chat is in. A remote one means
    they genuinely run on that machine over ssh: `pwd` in the terminal is a
    directory over there, and a file written lands on that computer. It takes
    effect from the very next tool call, in this chat only, mid-turn included -
    the terminal gets a fresh shell in the new place, so a process left running
    or a venv activated in the old one stays behind there.

    Bare, it lists the workspaces and marks the one this chat is in. A name or
    an id moves it; 'default' moves it back to whichever is the default. It
    cannot CREATE one - that is the settings page's job, since a new workspace
    needs a path and, for another machine, ssh that is already set up.

    `by_user` is who is doing the moving, and it decides one thing: whether the
    move is written into the chat's history as a note the model reads (see
    main.workspace_note). True - the user typed /workspace, so the model has to
    be told, exactly as it is told a tool's result. False - the model moved
    itself with uniagent_command, and the reply below IS its tool result, so
    writing "the user moved this chat" alongside it would be a plain lie."""
    current = chat.workspace or ""

    if not arg:
        return _workspace_listing(current, by_user)

    wanted = arg.strip().lower()
    if wanted in ("default", "clear", "unpin"):
        target = ""
    else:
        entries = provider.workspaces()
        match = next((w for w in entries if w["id"] == wanted), None)
        if match is None:
            match = next((w for w in entries if w["name"].strip().lower() == wanted), None)
        if match is None:
            return ("there is no workspace called " + arg + ".\n\n"
                    + _workspace_listing(current, by_user))
        target = match["id"]

    ws = workspace_mod.get(target)
    was = workspace_mod.get(current)
    if (target or "") == current:
        return ("this chat is already in " + ws.name + " - " + ws.where
                + ". Nothing to do.")

    try:
        chat.set_workspace(target)
    except OSError as e:
        return "could not save the workspace onto this chat: " + str(e)

    # Naming the workspace a chat was already FOLLOWING (or dropping back onto
    # the very one it was pinned to) changes what it is filed under, not where
    # the work happens. Saved, but nothing said to the model and nothing
    # written into the history: a note there would be a lie by implication,
    # since nothing has moved. No reachability check either - it is the same
    # machine it was already working on.
    if was.id == ws.id:
        return ("this chat " + ("now follows the default workspace, "
                                if not target else "is now pinned to ")
                + ws.name + " - which is where it was already working. "
                "Nothing has moved.")

    # Whether it can actually be reached, said now rather than leaving the next
    # tool call to be the thing that discovers the machine is off.
    ok, message = ws.check()
    if by_user:
        main.workspace_note(chat, ws, ok, message, following_default=not target)
    head = ("this chat now works in " + ws.name + " - " + ws.where
            + ". Files and terminal commands happen there from the next one on.")
    if not ok:
        return (head + "\n\nit is NOT reachable at the moment:\n" + message
                + "\nthe chat has been moved anyway.")
    return head + "\n" + message


def _name(arg, chat):
    """Show or set the title of THE CHAT THIS RAN IN - just this chat.
    Written into its own .json settings file, same as /model and
    /temperature, so it sticks to the chat and survives a restart.

    Bare, shows this chat's title if it has one. A chat with no title shows
    its first message as a label instead (see server.py's _chats()) - that's
    the default for every new chat; /name only matters when you want
    something more specific than that. '/name default' (or 'clear'/'unpin')
    drops back to the auto-derived label."""
    if not arg:
        if chat.name:
            return "this chat's title: " + chat.name
        return ("this chat has no title set - the chats panel shows its first "
                "message instead.\nset one with /name <text>.")
    if arg.lower() in ("default", "clear", "unpin"):
        chat.clear_name()
        return "title cleared - back to showing this chat's first message."
    chat.rename(arg)
    return "this chat is now titled: " + arg


def _num(n):
    """A token count at a glance. Thousands and millions are shortened because
    the interesting comparison is between rows, not to the last token - 3.8M
    against 398k is read in one look where 3812440 against 398210 is not."""
    n = int(n or 0)
    if n >= 1_000_000:
        return format(n / 1_000_000, ".2f").rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return format(n / 1_000, ".1f").rstrip("0").rstrip(".") + "k"
    return str(n)


def _provenance(counters, side):
    """The parenthetical that keeps a total honest: how much of it we measured
    ourselves, and how many requests contributed nothing at all. Empty when
    every number on that side came from the provider, which is the only case
    where a bare figure would be the whole truth."""
    notes = []
    estimated = counters.get(side + "_est", 0)
    unknown = counters.get(side + "_unknown", 0)
    if estimated:
        notes.append(_num(estimated) + " estimated")
    if unknown:
        notes.append(str(unknown) + " request" + ("s" if unknown != 1 else "")
                     + " unknown")
    return ("  (" + ", ".join(notes) + ")") if notes else ""


def _usage_rows(rows, label_of, limit=8):
    lines = []
    for row in rows[:limit]:
        lines.append("  " + label_of(row).ljust(38)
                     + str(row["requests"]).rjust(6) + " req"
                     + _num(row["in"]).rjust(9) + " in"
                     + _num(row["out"]).rjust(9) + " out")
    if len(rows) > limit:
        lines.append("  ... and " + str(len(rows) - limit) + " more")
    return lines


def _usage(arg, chat):
    """What has been spent, from the ledger usage.py keeps.

    Every request the app makes is written down as it happens - chat turns,
    the safety check on each tool call, compactions, spoken summaries - so this
    reads a file rather than asking any provider anything. It costs nothing and
    works offline.

    Bare, it reports the last 30 days across every chat, then today and this
    chat as one-liners so the common questions are answered without a second
    command. '/usage today|7d|30d|all' picks the window, and '/usage chat'
    narrows to the chat you are in; the two combine in either order.

    Counts are labelled wherever they aren't the provider's own: a model that
    reports no usage has its tokens measured locally and shown as estimated,
    and a request that could not be measured at all is counted as unknown
    rather than as zero. See usage.py on why that is so common - a turn ending
    in a tool call usually never receives the provider's final usage event."""
    parts = arg.lower().split()
    window = "30d"
    here = False
    for part in parts:
        if part in usage.RANGES:
            window = part
        elif part in ("chat", "here", "this"):
            here = True
        else:
            return ("/usage [today|7d|30d|all] [chat] - unknown option '" + part
                    + "'.\n/usage chat reports only the chat you are in.")

    stem = chat.id if (here and chat is not None) else None
    if here and stem is None:
        return "no chat here to report on - /usage on its own covers everything."
    data = usage.summary(window, chat=stem)
    totals = data["totals"]

    if not totals["requests"]:
        if data["logging_since"]:
            return ("no requests recorded in this window (usage has been logged "
                    "since " + data["logging_since"] + ").")
        return ("nothing recorded yet. Usage is logged from the moment this "
                "feature was installed - there is no history from before it, "
                "and the first request will start the ledger.")

    span = ("today" if window == "today" else
            "all time" if window == "all" else
            "last " + window.rstrip("d") + " days")
    head = "usage - " + span
    if data["since"] and window != "today":
        head += " (" + data["since"] + " to " + data["until"] + ")"
    if stem:
        head += ", this chat only"

    lines = [head, ""]
    failed = totals["errors"]
    lines.append("  requests  " + str(totals["requests"]).rjust(10)
                 + (("  (" + str(failed) + " failed)") if failed else ""))
    lines.append("  input     " + _num(totals["in"]).rjust(10) + _provenance(totals, "in"))
    lines.append("  output    " + _num(totals["out"]).rjust(10) + _provenance(totals, "out"))
    if totals["cache_read"] or totals["cache_write"]:
        lines.append("  cached    " + _num(totals["cache_read"]).rjust(10)
                     + "  read from cache, part of the input above"
                     + ((", " + _num(totals["cache_write"]) + " written")
                        if totals["cache_write"] else ""))

    if data["by_kind"]:
        lines += ["", "by kind:"] + _usage_rows(data["by_kind"],
                                                lambda r: r["kind"] or "(none)")
    if data["by_model"]:
        lines += ["", "by model:"] + _usage_rows(
            data["by_model"],
            lambda r: (r["provider"] or "?") + " / " + (r["model"] or "?"))
    if not stem and data["by_chat"]:
        lines += ["", "by chat:"] + _usage_rows(data["by_chat"],
                                                lambda r: r["chat"], limit=5)

    # The two one-liners that save a second command. Skipped when they would
    # just repeat what is already above - which is why they are collected
    # first and only then given their blank line: either may be the one that
    # isn't there.
    footnotes = []
    if window != "today" and not stem:
        today = usage.summary("today")["totals"]
        footnotes.append("today: " + str(today["requests"]) + " requests, "
                         + _num(today["in"]) + " in, " + _num(today["out"]) + " out")
    if not stem and chat is not None:
        mine = usage.summary(window, chat=chat.id)["totals"]
        if mine["requests"]:
            footnotes.append("this chat: " + str(mine["requests"]) + " requests, "
                             + _num(mine["in"]) + " in, " + _num(mine["out"]) + " out")
    if footnotes:
        lines += [""] + footnotes

    lines += ["", "/usage today|7d|30d|all changes the window, /usage chat "
              "reports just this chat."]
    return "\n".join(lines)


def _cronsafety(arg, chat):
    """Show or set the safety NUMBER a cron job's tool calls run at - the same
    0-10 scale as the slider in the corner of a chat, and the same meaning: the
    highest danger rating a call can be given and still run unasked.

    Per job, written as a "safety" field on that job in cron.json - which is the
    source of truth for everything about a cron job, and is re-read by the
    watcher every 30 seconds, so a change here lands on the next run with no
    restart. A job with no number of its own follows the settings page's cron
    default.

    Bare, lists every job with its number and where that number came from. The
    per-job WORDS aren't editable from here - a job's own "safety_extra", and the
    task the check is told about, live in cron.json and the safety tab. This does
    say which jobs have added something of their own.

    The number bites harder here than in a chat: no one is watching at 03:00, and
    the watcher answers a flagged call by denying it, so a number set too low is
    a job that quietly doesn't finish and """ + str(settings.SAFETY_MAX) + """ is
    a job that runs whatever it decides to, unattended."""
    parts = arg.split()
    jobs = cron._load_jobs()
    chosen = settings.load()
    default = tool_validation.threshold_for(
        chosen=chosen, default_key="cron_safety_threshold")

    if not parts:
        if not jobs:
            return "no cron jobs in " + str(cron.CRON_FILE) + " yet."
        lines = []
        for job in sorted(jobs, key=lambda j: j["name"]):
            state = str(job["safety"]) + " - " + tool_validation.says(job["safety"]) + (
                " (set in cron.json)" if job["safety_own"]
                else " (following the cron default)")
            if job["safety_extra"]:
                state += ", own rules"
            if job["safety_prompt"]:
                state += ", own prompt"
            lines.append("  " + job["name"] + " - " + state)
        return ("safety numbers on cron jobs:\n" + "\n".join(lines)
                + "\n\ncron default (settings > safety): " + str(default)
                + "\nset one with /cronsafety <job> 0-" + str(settings.SAFETY_MAX)
                + ", or 'default' to unset it."
                + "\nthe check is told what each job was asked to do, and a job can "
                "add rules of its own with a safety_extra field in cron.json.")

    if len(parts) != 2:
        return ("usage: /cronsafety <job> 0-" + str(settings.SAFETY_MAX)
                + "|default   (bare, it lists every job)")

    name, word = parts[0], parts[1].lower()
    if word in ("default", "clear", "unpin"):
        value = None
    else:
        try:
            value = int(word)
        except ValueError:
            return ("usage: /cronsafety " + name + " 0-"
                    + str(settings.SAFETY_MAX) + "|default")
        if not settings.SAFETY_MIN <= value <= settings.SAFETY_MAX:
            return ("a safety number has to be from " + str(settings.SAFETY_MIN)
                    + " to " + str(settings.SAFETY_MAX) + ".")

    error = cron.set_job_safety(name, value)
    if error:
        known = ", ".join(sorted(j["name"] for j in jobs)) or "none"
        return error + ".\njobs: " + known

    if value is None:
        return ("'" + name + "' now follows the cron default: safety "
                + str(default) + " - " + tool_validation.says(default) + ".")
    if value >= settings.SAFETY_MAX:
        return ("'" + name + "' now runs its tool calls WITHOUT the safety check - "
                "unattended, with nothing vetting what it does.")
    return ("'" + name + "' now runs at safety " + str(value) + " - "
            + tool_validation.says(value) + ". Anything rated higher is denied, not asked about, "
            "since nobody is there to ask.")


def _restart(arg, chat):
    """Restart the front-end process (execv's a fresh python, so edited code
    takes effect) - only wired up where main.on_restart is set. server.py
    answers this request first, since once the process goes there is nothing
    left to reply with; cli.py registers no hook, so /restart there just says
    it can't."""
    if main.on_restart is None:
        return "restart isn't available here - only the web server supports it."
    main.on_restart()
    return "restarting - reconnect in a moment."


def _approve(arg, chat):
    p = _pending.get(chat.id)
    if p is None:
        return "nothing is waiting for approval in this chat."
    if arg.lower() not in ("y", "n", "yes", "no"):
        return "usage: /approve y|n"
    p["answer"] = arg.lower() in ("y", "yes")
    p["event"].set()
    return "approved - running it." if p["answer"] else "denied."


COMMANDS = {
    "help": _help,
    "history": _history,
    "chats": _chats,
    "load": _load,
    "new": _new,
    "compact": _compact,
    "model": _model,
    "name": _name,
    "temperature": _temperature,
    "usage": _usage,
    "workspace": _workspace,
    "approve": _approve,
    "cronsafety": _cronsafety,
    "continue": _continue,
    "stop": _stop,
    "delete": _delete,
    "restart": _restart,
}


# The commands that answer with a chat to GO TO rather than acting on the one
# they were given, so their handlers return (reply, chat_id) instead of a bare
# reply. Kept as a set so process() can normalise the two shapes in one place
# and every other handler stays a plain function of (arg, chat) -> str.
NAVIGATION = {"load", "new", "delete"}

# The commands that care WHO ran them, and so take a third argument - the
# `by_user` process() was given. Only /workspace does today: a move the user
# made has to be written into the history for the model to read, and a move the
# model made itself already comes back to it as its own tool result. Kept as a
# set for the same reason as NAVIGATION - one place normalises the shapes, and
# every other handler stays a plain function of (arg, chat) -> str.
ACTOR_AWARE = {"workspace"}


def process(text, chat=None, by_user=True):
    """Run a /command against `chat` and return (reply, goto), or None if
    `text` isn't a command at all.

    `chat` is the Agent the command acts on - main.current for the terminal,
    the chat a browser window named in its request for the web. It defaults to
    main.current so a caller with genuinely one chat (cli.py) needn't pass it.

    `goto` is the chat id to switch to, or None for "stay where you are" -
    which is every command except the three in NAVIGATION. An empty string
    means "there is nowhere left to go", which /delete answers with when it
    has just removed the last chat on disk. The CALLER decides what switching
    means: cli.py moves main.current, server.py hands the id back to the one
    browser window that asked, and no other window is disturbed.

    `by_user` is whether a person typed this, which is the default and true of
    every front-end. The one caller that passes False is the uniagent_command
    tool, where the model is running the command on itself - see ACTOR_AWARE
    below for the only thing that changes."""
    if not text.startswith("/"):
        return None
    name, _, arg = text[1:].partition(" ")
    handler = COMMANDS.get(name.lower())
    if handler is None:
        return "unknown command /" + name + "\n" + HELP, None
    target = chat if chat is not None else main.current
    if name.lower() in ACTOR_AWARE:
        result = handler(arg.strip(), target, by_user)
    else:
        result = handler(arg.strip(), target)
    # Only the NAVIGATION handlers return the pair; everything else returns the
    # reply on its own and never moves anyone.
    return result if name.lower() in NAVIGATION else (result, None)
