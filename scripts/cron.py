"""Scheduled prompts - a standalone always-on watcher.

This is its OWN process, deliberately separate from the chat (main.py). Run it as:

    python3 scripts/cron.py

or install the user service in scripts/uniagent-cron.service so it's always
running. Because it's a separate process it shares nothing with the chat - no
lock, no history - so a scheduled job runs concurrently, can't make you wait, and
never touches the live conversation. Jobs run through the SAME tool loop as the
chat (main.turn/main.run), so they get tool use and the safety check.

Every RUN of a job is its own real chat, kept at chats/cron/<name>/<nnn>/
exactly like any other chat (same Chat object, same main.turn()) - so it shows
up in the web UI's sidebar and can be opened, tool calls and all, like a normal
chat.

One folder per run, not one per job, is what makes a cron job something you can
talk to. The newest run is the job's chat: it holds that run and everything
said afterwards, right up until the next run opens a new one. So a follow-up
question is an ordinary turn over an ordinary history, with nothing hidden from
the model and nothing from last week replayed to it. The older runs are still
there, listed under the job's "history" in the sidebar.

It reads ../cron.json on a fixed tick and fires any job that's due. One object
per job in the "jobs" list:

    {
      "name": "a-short-name",
      "start": 1785567600,     # unix seconds - the moment of the FIRST run
      "interval": 86400,       # unix seconds between runs (omit/0 = run once)
      "provider": "deepseek",  # optional - omit for the settings page's cron default
      "model": "deepseek-v4-flash",
      "temperature": 0.7,      # optional - omit for the settings page's default
      "safety": 7,             # optional - 0-10, omit for the cron default
      "safety_extra": "This job's own rules for the safety check.",
      "prompt": "What to do."
    }

"safety" is the same 0-10 number a chat runs on (see tool_validation): the
highest danger rating a tool call can be given and still run. Nobody is here to
approve a call above it - _deny answers the gate - so it is denied outright and
the job goes no further, which is why a job's number is resolved against the
settings page's "cron_safety_threshold" and not a chat's stricter default.

What makes a higher number safe is that the check is TOLD WHAT THE JOB IS. The
settings page's "cron_safety_extra" - the task in it - and then the job's own
"safety_extra" are composed here into the one {extra} block a chat uses for its
own rules, so a call that is ordinary work for this job reads as ordinary work
and a call that has nothing to do with it stands out. See _safety_brief().

A job may still replace the whole vetting prompt with a "safety_prompt" of its
own (it must contain {call}) - the escape hatch for a job the shared prompt is
wrong about rather than merely uninformed about. It wins over everything above.

A schedule is two plain numbers, both unix seconds: runs happen at start,
start+interval, start+2*interval, and so on. That means the whole clock is one
subtraction - no timezone, no HH:MM parsing, no "did today's 07:00 already go
past" bookkeeping - and a job can be checked by hand with `date -d @1785567600`.
The trade: an interval is a fixed number of seconds, so a daily job (86400)
lands an hour out after the clocks change until its start is nudged back.
"""

import datetime
import json
import re
import time
from pathlib import Path

import _term
import main
import provider
import service
import settings
import tool_validation

CRON_FILE = Path(__file__).parent.parent / "cron.json"
STATE_FILE = Path(__file__).parent.parent / "cron_state.json"

TICK = 30  # seconds between checks - the finest granularity a schedule can hit

DIM = "\033[2m"
RESET = "\033[0m"

# What a `"safety"` that isn't a number means. This field was a true/false flag
# before it was a 0-10 number, and files written then are still on disk, so the
# old spellings are read rather than refused: "off" was "never check this one",
# which is exactly 10, and "on" was "do check it", which is now whatever the
# cron default says. Nothing writes these any more - see _threshold().
_LEGACY_WORDS = {"on": True, "true": True, "yes": True,
                 "off": False, "false": False, "no": False}


def _note(text):
    print(DIM + "[cron] " + text + RESET)


def _number(value):
    """A schedule field as an int of seconds. None when it isn't a number at
    all - JSON gives us the real type, so this only has to reject the wrong
    one (and True, which is an int in Python and never a timestamp)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _threshold(value, job_name):
    """A job's own `"safety"` as a 0-10 number, or None for a job that hasn't
    got one of its own and should follow the cron default.

    Three kinds of value reach here. A number is the answer, clamped. One of
    the old true/false spellings is translated (see _LEGACY_WORDS) and noted,
    so a file written before the numbers keeps working while saying out loud
    that it should be rewritten. Anything else is a typo, and a typo must never
    quietly disarm the check: it is ignored, noted, and the job follows the
    default - which is a check, not the absence of one."""
    if value is None:
        return None

    number = tool_validation.clamp(value)
    if number is not None:
        return number

    word = _LEGACY_WORDS.get(str(value).strip().lower()) \
        if isinstance(value, (bool, str)) else None
    if word is None:
        _note("'" + job_name + "' has safety: " + repr(value) + " - expected a "
              "number from 0 to 10, so following the cron default instead")
        return None
    if word:
        _note("'" + job_name + "' has safety: " + repr(value) + " - that's the old "
              "on/off form; it now follows the cron default, so write the number "
              "you want")
        return None
    _note("'" + job_name + "' has safety: " + repr(value) + " - that's the old "
          "on/off form for 'never check this job', which is now safety: "
          + str(settings.SAFETY_MAX))
    return settings.SAFETY_MAX


def _safety_brief(own, task, chosen):
    """Everything the check is told about this job beyond the call itself, as one
    block of text for tool_validation's {extra} - the same channel a chat's own
    rules use, so there is one prompt in force and one place it is composed.

    The settings page's "cron_safety_extra" is the whole shape of it: the fixed
    words about being a scheduled job, with this job's task written into its
    {task} and this job's own "safety_extra" into its {rules}. Nothing about the
    wording lives here, so improving how scheduled calls are judged is an edit
    on the safety tab and not a release.

    Either placeholder the text doesn't name is appended at the end instead -
    the same tolerance _compose() has for a prompt with no {extra}, and for the
    same reason: a task silently dropped would leave the check judging a
    scheduled call with no idea what it was scheduled to do, which is the one
    thing this block exists to tell it.

    Emptying the addendum on the settings page, though, means exactly that: no
    framing and no task, just the job's own words. An emptied box that sprang
    back to the shipped text would be a box that cannot be emptied."""
    frame = (chosen["cron_safety_extra"] or "").strip()
    own = (own or "").strip()
    if not frame:
        return own or None

    # The lead-in is for the APPENDED case only: a text that names the
    # placeholder has already introduced it in its own words, and saying it
    # again would read as a stutter.
    for mark, text, lead in (("{task}", task, "The job was told to do this, and "
                                              "only this:\n\n"),
                             ("{rules}", own, "")):
        if mark in frame:
            frame = frame.replace(mark, text)
        elif text:
            frame += "\n\n" + lead + text
    # A placeholder the job had nothing for leaves the blank line it stood on.
    return re.sub(r"\n{3,}", "\n\n", frame).strip()


def parse_jobs(data):
    """The decoded cron.json as a list of job dicts, each one filled in with the
    settings-page defaults it didn't name. Takes the PARSED object, not text, so
    a caller that already has the file open (the tool, the server) doesn't parse
    it twice and can't disagree with this about what it says.

    A job missing a name, a prompt or a start is a real mistake and gets a note
    rather than being skipped in silence - there is no prose in this file for it
    to be mistaken for."""
    if isinstance(data, dict):
        data = data.get("jobs")
    if not isinstance(data, list):
        return []

    # Read per parse - so changing the cron defaults on the settings page is
    # picked up on the next tick, without restarting the watcher.
    chosen = settings.load()

    jobs = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        job_name = str(raw.get("name", "")).strip()
        text_prompt = raw.get("prompt")
        text_prompt = text_prompt.strip() if isinstance(text_prompt, str) else ""
        if not job_name or not text_prompt:
            _note("skipping a job with no " + ("name" if not job_name else "prompt"))
            continue

        # The whole schedule: when the first run is, and how far apart the runs
        # are. No interval (or 0) is a job that happens once and then never
        # again - a reminder, not a routine.
        start = _number(raw.get("start"))
        if start is None:
            _note("skipping '" + job_name + "' - needs a 'start' in unix seconds")
            continue
        interval = _number(raw.get("interval")) or 0
        if interval < 0:
            _note("'" + job_name + "' has a negative interval - running it once instead")
            interval = 0

        # A job's own provider (or the settings default) - then its own model,
        # or the settings default model ONLY if that default is actually for
        # THIS provider. A job naming its own provider without its own model
        # must not inherit a model meant for a different provider.
        job_provider = raw.get("provider") or chosen["cron_provider"]
        job_model = raw.get("model")
        if not job_model:
            if job_provider == chosen["cron_provider"]:
                job_model = chosen["cron_model"]
            else:
                job_model = provider.default_model(job_provider) or None

        # An invalid temperature in cron.json (a typo, out of range) falls back
        # to the settings default rather than blocking the whole job.
        job_temperature = None
        if raw.get("temperature") is not None:
            try:
                job_temperature = float(raw["temperature"])
            except (TypeError, ValueError):
                job_temperature = None
            else:
                lo, hi = settings.TEMPERATURE_RANGE
                if not (lo <= job_temperature <= hi):
                    job_temperature = None
        if job_temperature is None:
            job_temperature = chosen["temperature"]

        # This job's own words for the check, and its own number. The number is
        # resolved here rather than at the gate so that everything downstream -
        # the run, the chat it is mirrored into, /cronsafety's listing - is
        # looking at the same figure, and "own" keeps the one thing resolving
        # loses: whether the file said it or the settings page did.
        own_threshold = _threshold(raw.get("safety"), job_name)
        safety_extra = raw.get("safety_extra")
        safety_extra = safety_extra.strip() if isinstance(safety_extra, str) else ""

        # This job's own vetting prompt, an ordinary field like every other.
        safety_prompt = raw.get("safety_prompt")
        safety_prompt = safety_prompt.strip() if isinstance(safety_prompt, str) else ""

        # A prompt with no {call} placeholder can't show the check what it is
        # judging, so it is dropped here rather than being handed on - the job
        # is then checked under the global prompt, which is the safe half of
        # the two. Caught at parse time so the note names the job.
        if safety_prompt and "{call}" not in safety_prompt:
            _note("'" + job_name + "' has a safety_prompt with no {call} "
                  "placeholder - using the settings page's prompt instead")
            safety_prompt = ""

        jobs.append({
            "name": job_name,
            # Off switch. ABSENT MEANS ON - every job written before this
            # existed, and every job the tool adds without thinking about it,
            # has to keep running exactly as it did. Only an explicit false (or
            # anything else falsy somebody hand-typed) turns a job off.
            "enabled": bool(raw.get("enabled", True)),
            "start": start,
            "interval": interval,
            "provider": job_provider,
            "model": job_model,
            "temperature": job_temperature,
            # The 0-10 number this run is judged against, already resolved
            # against the cron default (and the old global on/off switch, which
            # threshold_for still honours) - so it is always a number.
            "safety": tool_validation.threshold_for(
                own_threshold, chosen=chosen,
                default_key="cron_safety_threshold"),
            # Whether that number is the job's own or the default it happens to
            # be following. For anything that reports the setting back.
            "safety_own": own_threshold is not None,
            # The job's own words, verbatim as the file has them, and then the
            # whole brief the check is given: the settings page's framing, this
            # job's task, and those words. Both, because they answer different
            # questions - "has this job said anything of its own?" (reported by
            # /cronsafety and the cron tab) and "what does the check see?" (what
            # actually runs, and what gets mirrored into the run's chat).
            "safety_extra": safety_extra or None,
            "safety_brief": _safety_brief(safety_extra, text_prompt, chosen),
            "safety_prompt": safety_prompt or None,
            "prompt": text_prompt,
        })
    return jobs


def load_file():
    """cron.json decoded, or None if it isn't there or isn't valid JSON. The
    one place that failure is turned into a log line, so every caller below can
    just ask for jobs and get none."""
    try:
        return json.loads(CRON_FILE.read_text(encoding="utf-8"))
    except OSError:
        return None
    except json.JSONDecodeError as e:
        _note("could not read " + str(CRON_FILE) + " - it isn't valid JSON ("
              + str(e) + "), so nothing is scheduled until it's fixed")
        return None


def _load_jobs():
    data = load_file()
    return parse_jobs(data) if data is not None else []


def set_job_safety(name, threshold):
    """Set one job's "safety" in cron.json - a 0-10 number, or None to remove it
    and go back to following the cron default. Returns an error string, or None
    when it worked.

    This is the write half of the "safety" field parsed above, kept here beside
    it so the two can't drift. It changes that one key on that one job and hands
    the rest of the decoded file straight back to json.dump, which is the whole
    reason this file is JSON: every other field, every other job, and anything
    somebody added that this code has never heard of, all survive by default
    rather than by careful editing.

    Used by /cronsafety; the watcher itself never writes cron.json."""
    data = load_file()
    if data is None:
        return "could not read " + str(CRON_FILE)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return str(CRON_FILE) + " has no 'jobs' list"

    found = next((j for j in jobs
                  if isinstance(j, dict) and str(j.get("name", "")).strip() == name), None)
    if found is None:
        return "no cron job called '" + name + "'"

    if threshold is None:
        found.pop("safety", None)
    else:
        number = tool_validation.clamp(threshold)
        if number is None:
            return ("a safety number has to be a whole number from "
                    + str(settings.SAFETY_MIN) + " to " + str(settings.SAFETY_MAX))
        found["safety"] = number

    return _write_file(data)


def set_job_enabled(name, enabled):
    """Turn one job in cron.json on or off. Returns an error string, or None
    when it worked.

    On removes the key rather than writing "enabled": true - on is the absence
    of the switch, so a file nobody has ever turned anything off in stays as
    clean as it was. Same load-modify-write as set_job_safety above, and for
    the same reason: every other field, every other job, and anything this code
    has never heard of survive by default.

    Used by POST /cron/enabled; the watcher itself never writes cron.json."""
    data = load_file()
    if data is None:
        return "could not read " + str(CRON_FILE)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return str(CRON_FILE) + " has no 'jobs' list"

    found = next((j for j in jobs
                  if isinstance(j, dict) and str(j.get("name", "")).strip() == name), None)
    if found is None:
        return "no cron job called '" + name + "'"

    if enabled:
        found.pop("enabled", None)
    else:
        found["enabled"] = False

    return _write_file(data)


def _write_file(data):
    """cron.json rewritten from a decoded object. Returns an error string, or
    None. Written whole, so a caller must have loaded the file first and be
    handing back everything it read."""
    try:
        CRON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    except OSError as e:
        return "could not write " + str(CRON_FILE) + " - " + str(e)


def _load_state():
    """Per-job bookkeeping: the last scheduled moment each job has fired for, in
    unix seconds. A fresh/corrupt file just means 'nothing has run yet', which
    is safe."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        _note("could not save state - " + str(e))


def due_at(job, now):
    """The most recent moment this job was SUPPOSED to run, at or before `now`.
    None while its start is still in the future.

    The runs are start, start+interval, start+2*interval, ... so the one we are
    in is a subtraction and a floor divide - no calendar, no timezone, and
    nothing that has to be recomputed when the watcher restarts. A job with no
    interval has exactly the one moment, its start."""
    start, interval = job["start"], job["interval"]
    if now < start:
        return None
    if interval <= 0:
        return start
    return start + ((now - start) // interval) * interval


def next_at(job, now):
    """When this job runs next, in unix seconds - or None for a one-off whose
    moment has already gone. For reporting only; the watcher fires off due_at."""
    if now < job["start"]:
        return job["start"]
    if job["interval"] <= 0:
        return None
    return due_at(job, now) + job["interval"]


def _due(job, state, now):
    """Is this job due to fire now, and what to write back to its state once it
    does? Returns (due, mark).

    Due means a scheduled moment has arrived that this job hasn't fired for
    yet. Comparing the MOMENT rather than "how long since the last run" is what
    keeps a job on its start's rhythm: a run that takes twenty minutes, or a
    watcher that was off for an hour, doesn't shift every later run by that
    much."""
    moment = due_at(job, now)
    if moment is None:
        return False, None
    last = state.get(job["name"])
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return False, None  # never seen - watch() arms it instead of firing
    return moment > last, moment


def _deny(_question):
    """How cron answers the safety gate: deny. There's no human here to approve a
    flagged call, so it fails safe rather than open.

    This is what a job's number is really setting. A call rated above it isn't
    held for someone to look at in the morning - it never runs, and the job
    carries on without it. Hence the cron default sitting higher than a chat's,
    and hence _safety_extra: the way to avoid denying real work is to tell the
    check what the real work is, not to raise the number until nothing is
    denied."""
    return False


# How a run's folder is numbered inside its job's folder. Zero-padded so the
# folders sort in the order they happened, by name, in a file manager and in
# any glob - 010 must not come between 001 and 002.
_RUN_DIGITS = 3


def job_dir(name):
    """A job's own folder, chats/cron/<name>/. It holds one folder per run, not
    a transcript: the job is a series of chats, not one."""
    return main.CHATS / "cron" / name


def runs(name):
    """Every run of this job that has a chat on disk, oldest first."""
    folder = job_dir(name)
    if not folder.is_dir():
        return []
    return sorted(p.parent for p in folder.glob("*/" + main.HISTORY_FILE))


def run_route(name, run):
    """The chat id for one run of a job - 'cron/ai-brief/003'."""
    return "cron/" + name + "/" + run


def current_chat(name):
    """The job's LIVE chat: its most recent run. This is what the sidebar opens
    when you click the job, and what a message typed at the job goes to. None
    when the job has never run and has no chat yet."""
    found = runs(name)
    return main.chat(found[-1] / main.HISTORY_FILE) if found else None


def _mirror(c, job):
    """Keep a run's settings .json in step with cron.json, which is the source of
    truth for a cron job's model, temperature and safety - so the chat, opened
    in the web UI, shows what it actually ran under.

    Checks whether the settings FILE is there, not just whether the values in
    memory are right. Delete a chat and the watcher recreates the folder - but
    save() writes the transcript only, and the checks below compare against the
    same long-lived Agent object, whose values still match cron.json. So the file
    that went with the folder was never written back, and the chat showed no
    model or safety at all."""
    missing = not (c.path.parent / main.SETTINGS_FILE).exists()
    if missing or (c.provider, c.model) != (job["provider"], job["model"]):
        c.pin(job["provider"], job["model"])
    if missing or c.temperature != job["temperature"]:
        c.set_temperature(job["temperature"])
    # The safety block, in the two writes the chat itself uses for it: the number
    # the slider writes, then the words the dropdown writes. Compared as a whole
    # and written as a whole, because writing only the changed part would leave
    # that .json disagreeing with cron.json about the rest.
    #
    # `safety` (the old True/False flag) is cleared by set_safety_threshold, and
    # that matters here: a run folder written before the numbers has one on file,
    # and left there it would go on answering for a job whose number this is now.
    #
    # _fire passes all three to the turn directly anyway - the mirror is so the
    # chat SHOWS what it ran under, and so a follow-up question typed into it is
    # judged the way the run was.
    if missing or (c.safety_threshold, c.safety_extra, c.safety_prompt, c.safety) \
            != (job["safety"], job["safety_brief"], job["safety_prompt"], None):
        c.set_safety_threshold(job["safety"])
        c.set_safety_text(extra=job["safety_brief"], prompt=job["safety_prompt"])


def new_run(job):
    """A brand new chat for a run that is about to start: the next number in the
    job's folder, stamped with the time, settings mirrored from cron.json.

    This is what makes a cron job's chat a chat you can hold a conversation in.
    A run gets a clean history because it IS a clean history - not because
    anything hides the earlier ones from the model. So talking to the job
    afterwards is an ordinary turn over an ordinary transcript: this run, and
    whatever you have said since. Yesterday's run is a different chat, sitting
    under the job's `history` in the sidebar, still there to open and read."""
    found = runs(job["name"])
    nxt = 1
    if found:
        try:
            nxt = int(found[-1].name) + 1
        except ValueError:
            # A folder that isn't a number at all (hand-made, or restored from
            # somewhere). Count instead of parsing, so a run still gets made.
            nxt = len(found) + 1
    c = main.chat(main.chat_md(run_route(job["name"], str(nxt).zfill(_RUN_DIGITS))))
    c.save()  # the folder and an empty transcript, so it lists immediately
    c.set_started(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    _mirror(c, job)
    return c


def _ensure_chats(jobs):
    """Give every job in cron.json a chat, whether or not it has ever fired, and
    keep its current run's settings in step with cron.json.

    Being in cron.json is enough to exist: the job should be there in the sidebar,
    named, openable and empty, from the moment it is written - not appear out of
    nowhere hours later the first time it happens to run. That first chat is a
    real run folder, so anything you say to the job before it has ever fired is
    kept, and the job's first actual run opens the next one."""
    for job in jobs:
        c = current_chat(job["name"])
        if c is None:
            c = new_run(job)
            _note("created chat for '" + job["name"] + "'")
        else:
            _mirror(c, job)


def _fire(job):
    """Run the job's prompt through the shared tool loop, AS A REAL CHAT: a new
    one per run (see new_run), opened through main.chat() same as any chat
    window, so it gets the same lock, the same object, the same everything -
    the web UI lists it, opens it, and shows its tool calls exactly like a
    normal chat. It gets full tool use and the safety check, deny-only since
    there's no human here to ask (see _deny), at the number and with the words
    this job's line in cron.json resolves to - passed straight through here,
    read fresh this tick."""
    # Reuse the chat waiting for this job when nothing has been said in it yet.
    # _ensure_chats makes one the moment a job appears in cron.json, so the job
    # can be seen and talked to before it has ever fired; without this, its
    # first real run would open a second folder and leave that one empty for
    # good. Anything actually said in it means it is somebody's conversation,
    # and the run gets its own.
    c = current_chat(job["name"])
    if c is None or main._has_conversation(c):
        c = new_run(job)
    else:
        c.set_started(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    main.turn(c, job["prompt"], approve=_deny,
              provider_name=job["provider"], model=job["model"],
              temperature=job["temperature"],
              safety_threshold=job["safety"], safety_extra=job["safety_brief"],
              safety_prompt=job["safety_prompt"])


def watch():
    # This process's stdout is a log file under both service managers. On
    # Windows it would otherwise be written in the system codepage, so a job
    # whose reply contains an em dash would raise on the way to the log.
    _term.setup_console()
    # So the server can find this process to restart it - on Windows that
    # pidfile is the only handle on it there is.
    service.write_pidfile("cron")
    _note("watching " + str(CRON_FILE))
    while True:
        state = _load_state()
        now = int(time.time())

        # Re-read cron.json every tick so edits and new jobs are picked up live.
        jobs = _load_jobs()
        # Done every tick, not just at startup, so a job added to cron.json shows
        # up in the sidebar within 30 seconds rather than at its first run.
        _ensure_chats(jobs)

        for job in jobs:
            key = job["name"]

            # Switched off: never fires, but is kept ARMED at the moment it is
            # in - the same expression the first-sighting branch below uses.
            # Without that its state would stay frozen at the last run it did,
            # and switching it back on a week later would find a scheduled
            # moment it had "missed" and fire instantly. Off means off, and on
            # means "carry on from here", not "make up for lost time" - the
            # same reason a restart never catches up on a schedule that has
            # already gone past.
            if not job["enabled"]:
                moment = due_at(job, now)
                state[key] = job["start"] - 1 if moment is None else moment
                continue

            last = state.get(key)
            if not isinstance(last, (int, float)) or isinstance(last, bool):
                # First time we've seen this job (or its state is from an older
                # format). Arm it at the moment it is in now, so a schedule that
                # has already gone past is never "caught up" on startup - the
                # next one fires. A job whose start is still ahead is armed just
                # short of it, so that first run does happen.
                moment = due_at(job, now)
                state[key] = job["start"] - 1 if moment is None else moment
                continue

            due, mark = _due(job, state, now)
            if due:
                _note("firing '" + key + "'")
                # Record the run BEFORE running it, and persist immediately, so a
                # crash mid-job doesn't re-fire it on the next tick.
                state[key] = mark
                _save_state(state)
                try:
                    _fire(job)
                except Exception as e:
                    _note("job '" + key + "' errored - "
                          + type(e).__name__ + ": " + str(e))

        _save_state(state)
        time.sleep(TICK)


if __name__ == "__main__":
    watch()
