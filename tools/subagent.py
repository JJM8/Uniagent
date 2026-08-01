"""Spawn a background subagent - a separate conversation that works a task
through the same tool loop as the chat (main.run) on its own history, then
reports back into the chat as a new message.

Subagents are NAMED, and they belong to the chat that spawned them. One tool,
two arguments: a name the main agent picks, and a prompt. A name that hasn't
been used yet IN THIS CHAT creates that subagent; a name that has been used
here sends the prompt to that same subagent, which carries on with its whole
history intact. The same name in a different chat is a different subagent. The
history lives in chats/subagents/<chat>/<name>.md - the file IS the subagent's
memory, so it survives restarts and can be watched live, and the folder ties
it to its parent chat.

Every prompt gets a standing instruction appended telling the subagent to end
with a complete report - the report is the only thing that crosses back into
the chat; the full transcript stays in the file, readable on demand.

The report is delivered by handing it to the front-end's registered turn
runner (main.notify) from the worker thread. That takes the same turn lock as
typed and spoken input, and a turn
holds that lock from before anything is sent to the provider until the loop
ends on a plain answer - so the report can only land when the chat is idle:
no response in flight, and not mid tool-loop.

Reload-proofing: tool modules are re-imported every turn, so nothing here can
keep state in module globals. The worker threads themselves are the registry -
each is named "subagent-<name>", and liveness is checked by scanning threads."""

import json
import re
import sys
import threading
from pathlib import Path

import provider as provider_module

NAME = "subagent"
DESCRIPTION = ("Hand a task to a named background subagent - a separate agent with its "
               "own conversation and full tool use. It works in parallel while you carry "
               "on, and reports back as a message when it finishes. Prompting the same "
               "name again continues that subagent, history intact.")

INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "subagent". Do not explain what you are doing first.

Arguments:
- name:   a short slug YOU choose (letters, digits, - or _), e.g. "researcher".
          A name not used before IN THIS CHAT creates that subagent. A name
          you already used here sends this prompt to THAT subagent, which
          continues with its whole history intact - so pick a fresh name for
          a fresh start, and reuse a name to follow up. Subagents belong to
          this chat; another chat's subagents are not reachable from here.
- prompt: what it should do, in plain text. A NEW subagent starts with NO
          memory of this conversation, so include every path, fact and
          constraint it needs. You do not need to ask for a report - that is
          added for you.
- provider: OPTIONAL, what to run the subagent on. MUST be one of these - the
          ones actually configured on this machine right now, not just any
          that exists:
<<PROVIDERS>>
          Omit it (and model) and a subagent keeps whatever it last ran on; a
          brand new one uses the defaults. Only set this when asked to, or
          when a task clearly needs a stronger/cheaper model.
- model:  OPTIONAL, one of that provider's models listed above. Same omit
          behaviour as provider.
- temperature: OPTIONAL, a number 0-2 - 0 is most predictable, higher is more
          random. Omit and a subagent keeps whatever it last ran on; a brand
          new one uses the settings page's default.
- if_busy: OPTIONAL, what to do when that subagent is STILL WORKING on its last
          prompt. Ignored when it is idle (or brand new), so it is only ever
          about a follow-up landing mid-work:
          - "error" (the default) - the call fails and nothing is sent, so you
            can decide what to do knowing the work is untouched.
          - "queue" - the prompt is held and delivered the moment the current
            work finishes. It still reports on what it is doing now first, then
            picks this up with its whole history intact. Use this for a
            follow-up that can wait.
          - "interrupt" - the current work is stopped where it stands (it saves
            what it has and reports it as stopped), then this prompt goes in on
            top of that history. Use this when what it is doing is wrong or no
            longer wanted - it loses the rest of that work.

The subagent runs in the background: you get confirmation back IMMEDIATELY, so
answer the user or carry on working - do NOT wait or stall for it, and NEVER
invent its report. When it finishes, its complete report arrives as a new
message labelled "Subagent <name> finished", delivered only once the chat is
idle. Its full transcript is saved to chats/subagents/<chat>/<name>.md - the
confirmation gives the exact path - read that file if you need more detail
than the report gives.""".replace("<<PROVIDERS>>", provider_module.options_text())

SUBAGENTS = Path(__file__).resolve().parent.parent / "chats" / "subagents"

# For native provider tool-calling. AVAILABLE_PROVIDERS mirrors add_cron.py's
# approach - recomputed on every reload since tools/ modules reload every turn.
AVAILABLE_PROVIDERS = provider_module.available()

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description":
            "A short slug YOU choose (letters, digits, - or _). A name not "
            "used before in this chat creates a new subagent; reusing a name "
            "continues that subagent with its whole history intact."},
        "prompt": {"type": "string", "description":
            "What it should do, in plain text. A NEW subagent starts with NO "
            "memory of this conversation, so include every path, fact and "
            "constraint it needs."},
        "provider": {"type": "string", "enum": AVAILABLE_PROVIDERS, "description":
            "Optional. Omit (and model) to keep whatever the subagent last "
            "ran on, or the settings default for a brand new one."},
        "model": {"type": "string", "description":
            "Optional, one of that provider's models. Same omit behaviour as provider."},
        "temperature": {"type": "number", "description":
            "Optional, 0-2 - 0 is most predictable, higher is more random. "
            "Same omit behaviour as provider."},
        "if_busy": {"type": "string", "enum": ["error", "queue", "interrupt"],
                    "description":
            "Optional, only used when that subagent is still working on its "
            "last prompt. \"error\" (default) sends nothing and fails; "
            "\"queue\" delivers this prompt the moment the current work "
            "finishes; \"interrupt\" stops the current work where it stands "
            "(it reports what it had) and sends this prompt instead."},
    },
    "required": ["name", "prompt"],
}

# What a NEW subagent runs on when the call names no provider/model comes from
# the settings page - see _defaults(). An existing subagent ignores it: it keeps
# whatever it last ran on (kept in _meta.json, per name) until a call overrides it.


def _defaults():
    """(provider, model, temperature) for a subagent that was given none of
    them. Read per call, so changing it on the settings page needs no
    restart. There's no separate 'sub_temperature' setting - a new subagent
    follows the same default temperature a new chat does."""
    import settings
    chosen = settings.load()
    return chosen["sub_provider"], chosen["sub_model"], chosen["temperature"]

# Standing instruction appended to EVERY prompt a subagent gets. The report is
# the only thing the chat ever sees of the work, so it must be complete enough
# to stand alone - a subagent that ends with "Done." is useless.
REPORT = ("\nWhen you are completely finished, end with a COMPLETE report of what "
          "you did, what you found, and anything that failed or was left undone - "
          "that report is all anyone will see of your work.")


def _host():
    """The LIVE main module. Under the chat, main.py is running as __main__ -
    'import main' there would build a second copy with its own empty history
    and lock, and the report would land in a conversation nobody can see. So
    find the instance that's already running."""
    # Fingerprinted on two things only main.py has, so __main__ being some other
    # script (cron.py, a REPL) can't be mistaken for it. Both must be things
    # main.py actually keeps - a marker that gets refactored away silently sends
    # every report into a second, invisible copy of the chat.
    for key in ("main", "__main__"):
        m = sys.modules.get(key)
        if m is not None and callable(getattr(m, "run", None)) \
                and callable(getattr(m, "turn_chat", None)):
            return m
    import main
    return main


def _deny(_question):
    """No human sits behind a subagent, so a safety-flagged call fails safe -
    same as cron."""
    return False


def _dir(host):
    """The spawning chat's own subagent folder - host.turn_chat() is the chat
    whose turn this tool call belongs to, which with several chats running at
    once is not always the loaded one. Subagents are children of that chat:
    the same name in a different chat is a different subagent.

    turn_chat() hands back the chat's FILE, not the chat object (notify() is
    called with that path too), so the id comes from host.chat_id() - the
    folder's name. Reading .id straight off it was an AttributeError on every
    subagent call once chats moved into folders of their own."""
    return SUBAGENTS / host.chat_id(host.turn_chat())


def _tag(host, name):
    """The worker-thread name for this chat's subagent `name` - chat-scoped,
    so a busy 'x' here does not block an 'x' in another chat."""
    return "subagent-" + host.chat_id(host.turn_chat()) + "/" + name


def _running(host, name):
    """The worker threads working under this chat's subagent `name` right now.
    Normally none or one - there can be more only while a queued/interrupting
    follow-up (which takes the same thread name, so the subagent still counts
    as busy from the moment it is accepted) waits for the run ahead of it."""
    tag = _tag(host, name)
    return [t for t in threading.enumerate() if t.name == tag]


def _busy(host, name):
    return bool(_running(host, name))


def _meta(folder):
    """Which provider/model each of this chat's subagents last ran on, by name.
    On disk next to the transcripts (module globals don't survive the per-turn
    tool reload), so it also survives restarts, same as the histories."""
    try:
        return json.loads((folder / "_meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _remember(folder, name, provider_name, model, temperature):
    meta = _meta(folder)
    meta[name] = {"provider": provider_name, "model": model, "temperature": temperature}
    try:
        (folder / "_meta.json").write_text(json.dumps(meta, indent=2))
    except OSError:
        pass  # worst case the next call falls back to the defaults


def _answer(host, history):
    """The subagent's closing report: run() appends every model response after
    the agent's name, so the text after the LAST name marker is the final
    plain answer.

    A stopped run ends with the stop marker on its own line, which is not an
    answer - drop it first, so a subagent you stopped still hands back the work
    it actually managed instead of just reporting that it was stopped."""
    lines = history.rstrip().splitlines()
    if lines and lines[-1].strip() == host.name + ": " + host.STOPPED:
        lines.pop()
    history = "\n".join(lines)

    at = history.rfind("\n" + host.name)
    if at == -1:
        return history.strip()
    return history[at + 1 + len(host.name):].strip().lstrip(":").strip()


def _report(host, note, origin):
    """Hand the note to the chat that spawned the work - `origin`, captured at
    spawn time, NOT whichever chat is loaded when the note lands. `notify` is
    whichever turn runner the running front-end registered; it takes the
    origin chat's own turn lock, so the report lands only when that chat is
    idle: no response in flight, not mid tool-loop. Under cron nothing
    registers notify - there is no live chat, and the transcript file is the
    whole record."""
    fn = getattr(host, "notify", None)
    if callable(fn):
        fn(note, origin)


def _after(ahead, host, name, path, prompt, provider_name, model, temperature, origin):
    """_work, but not until the run(s) already going under this name are done -
    what a queued or interrupting follow-up runs as. This thread carries the
    subagent's tag as its own name from the moment it is created, so the
    subagent reads as busy for the whole wait, and its transcript (which the
    work below picks up from the file) is whatever the run ahead left."""
    for t in ahead:
        t.join()
    # An interrupt asks to stop by the TAG, which is shared - the run ahead
    # clears it as it unwinds, but if it had already finished by then nobody
    # would, and this prompt would stop the instant it started.
    host.clear_stop(threading.current_thread().name)
    _work(host, name, path, prompt, provider_name, model, temperature, origin)


def _work(host, name, path, prompt, provider_name, model, temperature, origin):
    # This thread's own name IS its stop key - _tag() built it, it's unique per
    # chat+subagent, and it's what /stop names. Read it here rather than passing
    # it in: turn_chat() can't be used on this thread (no turn is registered on
    # it), so recomputing the tag here would give the wrong chat.
    tag = threading.current_thread().name
    history = path.read_text() if path.exists() else ""
    try:
        # on_save mirrors the history to the file as it grows, so a subagent can
        # be watched live. on_text swallows the stream - the default would print
        # it into the chat's terminal, scrambled over whatever the chat is doing.
        # A subagent is stopped on its OWN key, so stopping the parent chat's
        # turn leaves its subagents running, and vice versa.
        history = host.run(prompt, history, provider_name=provider_name, model=model,
                           temperature=temperature,
                           approve=_deny, on_save=path.write_text,
                           on_text=lambda _chunk: None,
                           should_stop=lambda: host.stop_requested(tag),
                           chat_id=tag)
        if host.stop_requested(tag):
            # Stopping halts the work, it does not throw it away - the chat
            # still gets what the subagent managed, just flagged as incomplete.
            note = ("Subagent " + name + " was STOPPED part-way through. What it "
                    "had written so far:\n" + _answer(host, history))
        else:
            note = "Subagent " + name + " finished. Report:\n" + _answer(host, history)
    except Exception as e:
        note = ("Subagent " + name + " CRASHED - " + type(e).__name__ + ": "
                + str(e) + ". Its transcript so far is in " + str(path))
    finally:
        # Always clear, or a stopped name could never be reused in this chat.
        host.clear_stop(tag)
    _report(host, note, origin)


def run(name=None, prompt=None, provider=None, model=None, temperature=None,
        if_busy=None):
    if threading.current_thread().name.startswith("subagent-"):
        return "ERROR: a subagent cannot spawn or re-prompt subagents."
    name = (name or "").strip()
    if not re.fullmatch(r"[\w-]+", name):
        return ("ERROR: 'name' must be a short slug (letters, digits, - or _). "
                "Got: " + repr(name))
    if not (prompt or "").strip():
        return "ERROR: 'prompt' is empty - say what the subagent should do."

    if provider and provider.strip().lower() not in provider_module.available():
        return ("ERROR: '" + provider + "' is not available - it needs an API key/"
                "credentials this machine doesn't have. Retry with one of these:\n"
                + provider_module.options_text())

    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            return "ERROR: 'temperature' must be a number between 0 and 2. Got: " + repr(temperature)
        if not (0 <= temperature <= 2):
            return "ERROR: 'temperature' must be between 0 and 2. Got: " + repr(temperature)

    if_busy = (if_busy or "error").strip().lower()
    if if_busy not in ("error", "queue", "interrupt"):
        return ("ERROR: 'if_busy' must be \"error\", \"queue\" or \"interrupt\". "
                "Got: " + repr(if_busy))

    host = _host()
    # Held from here on: what this call has to wait for (or stop) before its
    # own prompt can go in. Empty when the subagent is idle or brand new, and
    # then if_busy means nothing at all.
    ahead = _running(host, name)
    if ahead and if_busy == "error":
        return ("ERROR: subagent " + name + " is still working - wait for its "
                "report before sending it more. Or call again with "
                "if_busy=\"queue\" to have this prompt delivered the moment it "
                "finishes, or if_busy=\"interrupt\" to stop what it is doing "
                "now and send this instead.")

    folder = _dir(host)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".md")
    new = not path.exists()

    # Per field: what the call says, else what this subagent last ran on, else
    # the settings page's default. The result is remembered as this subagent's
    # new setup.
    last = _meta(folder).get(name, {})
    sub_provider, sub_model, sub_temperature = _defaults()
    provider_name = (provider or last.get("provider") or sub_provider).strip().lower()
    model = (model or last.get("model") or sub_model).strip()
    if temperature is None:
        last_temperature = last.get("temperature")
        temperature = last_temperature if last_temperature is not None else sub_temperature
    _remember(folder, name, provider_name, model, temperature)

    if ahead and if_busy == "interrupt":
        # Cooperative, exactly like /stop: the run gives up at the next point
        # it safely can, keeps what it has written, and reports it as stopped.
        # So this prompt does not start the instant this call returns - it
        # starts when that run actually unwinds, which _after() waits for.
        host.request_stop(_tag(host, name))

    args = (host, name, path, prompt.strip() + REPORT, provider_name, model,
            temperature, host.turn_chat())
    threading.Thread(
        target=_work if not ahead else _after,
        args=args if not ahead else (ahead,) + args,
        name=_tag(host, name), daemon=True,
    ).start()

    if ahead:
        state = ("queued behind the work it is doing now - it will report on that "
                 "first, then pick this up with its history intact"
                 if if_busy == "queue" else
                 "interrupting the work it is doing now - that work stops where it "
                 "stands and is reported as stopped, then this prompt goes in")
    else:
        state = ("created and started" if new else "re-prompted") + " in the background"

    return ("Subagent " + name + " " + state
            + ". Carry on - its report will arrive as a message "
            "once it finishes and the chat is idle. Transcript: " + str(path))
