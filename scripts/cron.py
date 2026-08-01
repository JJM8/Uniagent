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
      "safety": false,         # optional - omit to follow the settings page
      "safety_prompt": "This job's own vetting prompt. Must contain {call}.",
      "prompt": "What to do."
    }

A schedule is two plain numbers, both unix seconds: runs happen at start,
start+interval, start+2*interval, and so on. That means the whole clock is one
subtraction - no timezone, no HH:MM parsing, no "did today's 07:00 already go
past" bookkeeping - and a job can be checked by hand with `date -d @1785567600`.
The trade: an interval is a fixed number of seconds, so a daily job (86400)
lands an hour out after the clocks change until its start is nudged back.
"""

import datetime
import json
import time
from pathlib import Path

import main
import provider
import settings

CRON_FILE = Path(__file__).parent.parent / "cron.json"
STATE_FILE = Path(__file__).parent.parent / "cron_state.json"

TICK = 30  # seconds between checks - the finest granularity a schedule can hit

DIM = "\033[2m"
RESET = "\033[0m"

# What a written-out `"safety": <this>` means, for a file edited by hand and for
# /cronsafety's argument. Anything else is a typo, and a typo must never quietly
# disarm the check - an unrecognised value is ignored and the job falls back to
# the settings page (see _safety()).
_SAFETY_WORDS = {"on": True, "true": True, "yes": True, "1": True,
                 "off": False, "false": False, "no": False, "0": False}


def _note(text):
    print(DIM + "[cron] " + text + RESET)


def _number(value):
    """A schedule field as an int of seconds. None when it isn't a number at
    all - JSON gives us the real type, so this only has to reject the wrong
    one (and True, which is an int in Python and never a timestamp)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _safety(value, job_name):
    """false -> False, true -> True. None for a job that said nothing, and also
    for a job that said something unrecognised - which is noted, because
    `"safety": "of"` silently meaning "checked" is a surprise worth one line in
    the log, and silently meaning "unchecked" would be worse than a surprise.

    Strings are taken too ("on"/"off"), so a hand-written file that says what
    the old cron.md said still works."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    word = _SAFETY_WORDS.get(str(value).strip().lower())
    if word is None:
        _note("'" + job_name + "' has safety: " + repr(value)
              + " - expected true or false, so following the settings page instead")
    return word


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
            "start": start,
            "interval": interval,
            "provider": job_provider,
            "model": job_model,
            "temperature": job_temperature,
            # None for both = this job says nothing, so the settings page
            # decides, exactly as before these fields existed.
            "safety": _safety(raw.get("safety"), job_name),
            "safety_prompt": safety_prompt or None,
            "prompt": text_prompt,
        })
    return jobs


def load_file():
    """cron.json decoded, or None if it isn't there or isn't valid JSON. The
    one place that failure is turned into a log line, so every caller below can
    just ask for jobs and get none."""
    try:
        return json.loads(CRON_FILE.read_text())
    except OSError:
        return None
    except json.JSONDecodeError as e:
        _note("could not read " + str(CRON_FILE) + " - it isn't valid JSON ("
              + str(e) + "), so nothing is scheduled until it's fixed")
        return None


def _load_jobs():
    data = load_file()
    return parse_jobs(data) if data is not None else []


def set_job_safety(name, value):
    """Set one job's "safety" in cron.json - True, False, or None to remove it
    and go back to following the settings page. Returns an error string, or None
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

    if value is None:
        found.pop("safety", None)
    else:
        found["safety"] = bool(value)

    return _write_file(data)


def _write_file(data):
    """cron.json rewritten from a decoded object. Returns an error string, or
    None. Written whole, so a caller must have loaded the file first and be
    handing back everything it read."""
    try:
        CRON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        return "could not write " + str(CRON_FILE) + " - " + str(e)


def _load_state():
    """Per-job bookkeeping: the last scheduled moment each job has fired for, in
    unix seconds. A fresh/corrupt file just means 'nothing has run yet', which
    is safe."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
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
    flagged call, so it fails safe rather than open."""
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
    # Both together: they're one setting in two parts, and writing only the
    # changed half would leave that .json disagreeing with cron.json about the
    # other. _fire passes these to the turn directly anyway - the mirror is so
    # the chat shows what it runs under.
    if missing or (c.safety, c.safety_prompt) != (job["safety"], job["safety_prompt"]):
        c.set_safety(job["safety"], job["safety_prompt"])


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
    there's no human here to ask - unless the job's own `safety:`/
    "safety_prompt" in cron.json says otherwise, which is passed straight
    through here, read fresh this tick."""
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
              safety=job["safety"], safety_prompt=job["safety_prompt"])


def watch():
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
