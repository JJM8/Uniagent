"""Manage scheduled jobs in cron.json - add, list, remove, edit. Self-contained:
it just validates the pieces and edits the one file the cron watcher reads.

cron.json lives at the project root, found relative to this file, so the path is
right on any machine. The agent can also edit cron.json by hand with the terminal;
this tool is the safe, validated way to do the same thing.

A schedule is two unix timestamps in seconds - `start` (the first run) and
`interval` (the gap between runs) - and that is exactly what goes in the file.
This tool will also take a time written the way a person says it ("07:00",
"now", "6h") and convert it, because the model asking for a job at 7am should
not have to do calendar arithmetic to say so.
"""

import datetime
import json
import re
import time
from pathlib import Path

import provider as provider_module

NAME = "cron"
DESCRIPTION = ("Manage scheduled jobs (cron jobs) - things that run automatically on "
               "a timer, like every morning at 7am or every few hours. Use it whenever "
               "the user wants something scheduled, or wants to see/change/remove what's "
               "scheduled. Actions: list, add, remove, edit.")

# Recomputed on every reload (tools/ modules reload every turn), so a key
# added to .env is enforced here on the very next turn - no restart needed.
AVAILABLE_PROVIDERS = provider_module.available()

INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "cron". Do not explain what you are doing first.

Arguments:
- action: REQUIRED. One of "list", "add", "remove", "edit". Everything else
          below depends on which one.

action "list" - show every scheduled job:
- (no other arguments)

action "add" - schedule a new job:
- name:        a short slug (letters, digits, - or _), unique.
- prompt:      what to DO each run, one line of plain text. It's a fresh run
               with its own tools, so if it should save a file or send
               something, SAY so and give the full path.
- start:       REQUIRED. When the FIRST run happens. A unix timestamp in
               seconds (e.g. 1785567600), or any of these, which get converted
               for you: "now", "07:00" (the next time that clock time comes
               round), "2026-08-04 07:00", "2026-08-04".
- interval:    OPTIONAL. Seconds between runs - 86400 for daily, 3600 for
               hourly. Shorthand works too: "6h", "30m", "1d", "1w". LEAVE IT
               OUT for a job that should happen ONCE, at `start`, and never
               again.
- provider:    OPTIONAL. MUST be one of these - the ones actually configured
               on this machine right now, not just any that exists:
<<PROVIDERS>>
- model:       OPTIONAL, one of that provider's models listed above.
- temperature: OPTIONAL, a number 0-2 - 0 is most predictable, higher is more
               random. Omit to use the settings page's default.
- safety:      OPTIONAL, "on" or "off" - whether this job's tool calls are
               vetted by the safety check before they run. Omit to follow the
               settings page, which is nearly always right. Set "off" ONLY if
               the user asks for that job to skip the check: nobody is watching
               when a cron job runs, so an unchecked job runs whatever it
               decides to, unattended. A job can also be given its own, gentler
               safety prompt - a "safety_prompt" field containing {call} -
               which this tool carries through unchanged but does not write;
               use the file tools if the user wants one.

action "remove" - delete a job by name:
- name: the job to remove.

action "edit" - change a job:
- name: the job to change.
- Then only the fields you want to change, using the same field names as
  "add" (start, interval, prompt, provider, model, temperature, safety).
  Passing `interval` as an empty string turns a repeating job into a one-off;
  passing `temperature` or `safety` as an empty string clears it back to the
  default.

A run happens at start, then start+interval, then start+2*interval, and so on.
A schedule that has already gone past is never caught up: a job added at 9am
with start "07:00" first runs at 7am TOMORROW. Say the time back to the user in
plain words ("every day at 07:00"), never as a raw timestamp.""".replace(
    "<<PROVIDERS>>", provider_module.options_text())

# For native provider tool-calling. `action` fully changes which other fields
# are meaningful/required (list needs none; add/remove/edit each need their
# own subset - see INSTRUCTIONS above for exactly which), so only `action`
# itself is schema-required; the rest stay optional with the condition
# spelled out in their own description rather than a oneOf per action.
# `provider`'s enum is AVAILABLE_PROVIDERS, live - this module reloads every
# turn (tool_processor.load_tools()), so it's never stale.
#
# start/interval are typed as strings so a model can send either a raw number
# of seconds or "07:00"/"6h" without the provider rejecting it on type; both
# forms are parsed below.
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list", "add", "remove", "edit"],
                    "description": "Which operation to perform."},
        "name": {"type": "string", "description":
            "A short slug (letters, digits, - or _), unique. Required for add/remove/edit."},
        "prompt": {"type": "string", "description":
            "What to DO each run, one line of plain text. Required for add; "
            "optional for edit. It's a fresh run with its own tools, so if it "
            "should save a file or send something, say so and give the full path."},
        "start": {"type": "string", "description":
            "When the first run happens. A unix timestamp in seconds, or "
            "\"now\", \"07:00\" (next time that clock time comes round), "
            "\"2026-08-04 07:00\", \"2026-08-04\". Required for add."},
        "interval": {"type": "string", "description":
            "Seconds between runs - \"86400\" daily, \"3600\" hourly - or "
            "shorthand \"6h\", \"30m\", \"1d\", \"1w\". Omit for a job that "
            "runs once at `start` and never again. For edit, an empty string "
            "makes it a one-off."},
        "provider": {"type": "string", "enum": AVAILABLE_PROVIDERS, "description":
            "Optional - which provider the job runs on. Omit to use the settings default."},
        "model": {"type": "string", "description":
            "Optional, one of the chosen provider's models."},
        "temperature": {"type": "number", "description":
            "Optional, 0-2 - 0 is most predictable, higher is more random. "
            "For edit, an empty string clears it back to the default."},
        "safety": {"type": "string", "enum": ["on", "off"], "description":
            "Optional - whether this job's tool calls go through the safety "
            "check. Omit to follow the settings page. Only set \"off\" if the "
            "user asks for it: the job then runs unattended with nothing "
            "vetting what it does. For edit, an empty string clears it."},
    },
    "required": ["action"],
}

CRON_FILE = Path(__file__).resolve().parent.parent / "cron.json"

# The order a job's keys are written in, so a hand-read file always looks the
# same: what it is, when it runs, what it runs on, then the long text.
_ORDER = ["name", "start", "interval", "provider", "model", "temperature",
          "safety", "safety_prompt", "prompt"]

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# --- times in, times out ---------------------------------------------------
#
# The file only ever holds unix seconds. These two turn what a person says into
# that, and back again for anything shown to one.

def _parse_start(value):
    """A `start` argument as unix seconds. Returns (seconds, error string)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value), None
    text = str(value or "").strip()
    if not text:
        return None, "'start' is required - when should the first run be?"
    if re.fullmatch(r"\d{9,}", text):  # already a timestamp
        return int(text), None
    if text.lower() == "now":
        return int(time.time()), None

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return None, "'start' time must be between 00:00 and 23:59. Got: " + repr(value)
        now = datetime.datetime.now()
        when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # Today's time if it's still ahead, otherwise tomorrow's - a schedule
        # is never set in the past, where it would just never fire.
        if when <= now:
            when += datetime.timedelta(days=1)
        return int(when.timestamp()), None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(text, fmt).timestamp()), None
        except ValueError:
            pass
    return None, ("'start' must be unix seconds, \"now\", \"HH:MM\", or "
                  "\"YYYY-MM-DD HH:MM\". Got: " + repr(value))


def _parse_interval(value):
    """An `interval` argument as seconds. Returns (seconds, error string);
    (None, None) means "no interval", i.e. run once."""
    if value is None:
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (int(value), None) if value > 0 else (None, None)
    text = str(value).strip().lower()
    if not text:
        return None, None
    if re.fullmatch(r"\d+", text):
        return (int(text) or None), None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", text)
    if m:
        seconds = int(float(m.group(1)) * _UNITS[m.group(2)])
        return (seconds or None), None
    return None, ("'interval' must be seconds (86400) or shorthand like \"6h\", "
                  "\"30m\", \"1d\". Got: " + repr(value))


def _clock(seconds):
    """A unix timestamp as local time a person can read."""
    try:
        return datetime.datetime.fromtimestamp(int(seconds)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _gap(seconds):
    """An interval in seconds as the shortest exact shorthand - 86400 -> '1d',
    5400 -> '90m'. Falls back to plain seconds when nothing divides evenly."""
    if not seconds:
        return "once"
    for unit in ("w", "d", "h", "m"):
        size = _UNITS[unit]
        if seconds >= size and seconds % size == 0:
            return str(seconds // size) + unit
    return str(seconds) + "s"


def _when(job):
    """One job's schedule in words, for anything a person reads."""
    start, interval = job.get("start"), job.get("interval") or 0
    if not interval:
        gone = isinstance(start, (int, float)) and start < time.time()
        return "once at " + _clock(start) + (" (already gone - won't run)" if gone else "")
    return "every " + _gap(interval) + " from " + _clock(start) + ", next " + _clock(_next(job))


def _next(job):
    """When this job runs next, in unix seconds. Mirrors cron.due_at/next_at:
    runs land on start + n*interval, and one that has gone past is not caught
    up, so the next one is the next multiple after now."""
    start, interval = int(job.get("start") or 0), int(job.get("interval") or 0)
    now = time.time()
    if now < start or interval <= 0:
        return start
    return start + ((int(now) - start) // interval + 1) * interval


# --- little validators, all return an error string or None ---

def _check_name(name):
    if not re.fullmatch(r"[\w-]+", (name or "").strip()):
        return "'name' must be a simple slug (letters, digits, - or _). Got: " + repr(name)


def _check_provider(provider):
    if provider and provider.strip().lower() not in AVAILABLE_PROVIDERS:
        return ("'" + provider + "' is not available - it needs an API key/credentials "
                "this machine doesn't have. Retry with one of these:\n"
                + provider_module.options_text())


def _check_temperature(temperature):
    """None (not given, or given blank to clear it back to the default) is
    fine - only a value that was actually supplied gets range-checked."""
    if temperature is None or (isinstance(temperature, str) and not temperature.strip()):
        return None
    try:
        t = float(temperature)
    except (TypeError, ValueError):
        return "'temperature' must be a number between 0 and 2. Got: " + repr(temperature)
    if not (0 <= t <= 2):
        return "'temperature' must be between 0 and 2. Got: " + repr(temperature)


def _check_safety(safety):
    """None or "" (not given, or given blank to clear it back to the settings
    page's setting) is fine - only an actual value has to be on/off."""
    if safety is None or not str(safety).strip():
        return None
    if str(safety).strip().lower() not in ("on", "off"):
        return "'safety' must be \"on\" or \"off\". Got: " + repr(safety)


# --- reading and writing cron.json -----------------------------------------

def _read():
    """(whole decoded file, its jobs list, error string). The file is kept whole
    so a write puts back everything it isn't changing - other jobs, other
    fields, anything hand-written this tool has never heard of."""
    if not CRON_FILE.exists():
        return {"jobs": []}, [], None
    try:
        data = json.loads(CRON_FILE.read_text())
    except OSError as e:
        return None, None, "ERROR: could not read " + str(CRON_FILE) + " - " + str(e)
    except json.JSONDecodeError as e:
        return None, None, ("ERROR: " + str(CRON_FILE) + " is not valid JSON (" + str(e)
                            + "). Fix it by hand before scheduling anything - the "
                              "watcher can't read it either, so nothing is running.")
    if not isinstance(data, dict):
        data = {"jobs": data if isinstance(data, list) else []}
    jobs = data.setdefault("jobs", [])
    if not isinstance(jobs, list):
        return None, None, "ERROR: 'jobs' in " + str(CRON_FILE) + " is not a list."
    return data, [j for j in jobs if isinstance(j, dict)], None


def _write(data):
    try:
        CRON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        return "ERROR: could not write " + str(CRON_FILE) + " - " + str(e)


def _tidy(job):
    """One job with its keys in the house order, and empty ones dropped -
    an unset field is an ABSENT field, so nothing has to remember that "" means
    "follow the settings page". Unknown keys are kept, at the end."""
    out = {k: job[k] for k in _ORDER if k in job and job[k] not in (None, "")}
    out.update({k: v for k, v in job.items() if k not in _ORDER})
    return out


def run(action=None, name=None, start=None, interval=None, prompt=None, provider=None,
        model=None, temperature=None, safety=None):
    action = (action or "").strip().lower()
    if action not in ("list", "add", "remove", "edit"):
        return "ERROR: 'action' must be one of list, add, remove, edit. Got: " + repr(action)

    data, jobs, err = _read()
    if err:
        return err
    found = next((j for j in jobs if str(j.get("name", "")).strip() == (name or "").strip()),
                 None)

    if action == "list":
        if not jobs:
            return "No cron jobs in " + str(CRON_FILE) + " yet."
        lines = ["Cron jobs (" + str(CRON_FILE) + "):"]
        for j in jobs:
            who = (j.get("provider") or "default") + "/" + (j.get("model") or "default")
            # Only shown when the job says something - a job following the
            # settings page has nothing of its own to report here.
            safe = ""
            if j.get("safety") is not None:
                safe = ", safety " + ("on" if j["safety"] else "off")
            if j.get("safety_prompt"):
                safe += ", own safety prompt"
            lines.append("- " + str(j.get("name", "?")) + "  [" + _when(j) + ", " + who
                         + safe + "]\n    " + str(j.get("prompt", ""))[:140])
        return "\n".join(lines)

    if action == "remove":
        if _check_name(name):
            return "ERROR: give the 'name' of the job to remove."
        if not found:
            return ('ERROR: no job called "' + str(name).strip() + '". Existing: '
                    + (", ".join(str(j.get("name")) for j in jobs) or "none"))
        data["jobs"] = [j for j in data["jobs"] if j is not found]
        return _write(data) or ('Removed cron job "' + name.strip() + '".')

    if action == "add":
        for err in (_check_name(name), _check_provider(provider),
                    _check_temperature(temperature), _check_safety(safety)):
            if err:
                return "ERROR: " + err
        if found:
            return ('ERROR: a job called "' + name.strip() + '" already exists. Use '
                    'action "edit" to change it, or pick another name.')
        if not (prompt or "").strip():
            return "ERROR: 'prompt' is empty - say what the job should do each run."
        at, err = _parse_start(start)
        if err:
            return "ERROR: " + err
        gap, err = _parse_interval(interval)
        if err:
            return "ERROR: " + err

        job = _tidy({
            "name": name.strip(),
            "start": at,
            "interval": gap,
            "provider": (provider or "").strip(),
            "model": (model or "").strip(),
            "temperature": float(temperature) if temperature not in (None, "") else None,
            "safety": {"on": True, "off": False}.get((safety or "").strip().lower()),
            "prompt": " ".join(prompt.split()),
        })
        data["jobs"].append(job)
        err = _write(data)
        if err:
            return err
        note = ""
        if not gap and at < time.time():
            note = (" WARNING: that one-off time has already passed, so it will never "
                    "fire - give a 'start' in the future.")
        return ('Added cron job "' + job["name"] + '" - runs ' + _when(job)
                + ". The watcher picks it up within ~30 seconds; no restart needed." + note)

    # action == "edit"
    if _check_name(name):
        return "ERROR: give the 'name' of the job to edit."
    if not found:
        return ('ERROR: no job called "' + str(name).strip() + '". Existing: '
                + (", ".join(str(j.get("name")) for j in jobs) or "none"))
    if all(v is None for v in (start, interval, prompt, provider, model, temperature, safety)):
        return ("ERROR: nothing to change - pass start, interval, prompt, provider, "
                "model, temperature and/or safety.")

    if start is not None:
        at, err = _parse_start(start)
        if err:
            return "ERROR: " + err
        found["start"] = at
    if interval is not None:
        gap, err = _parse_interval(interval)
        if err:
            return "ERROR: " + err
        # "" (or 0) makes it a one-off, the same way a blank clears the fields
        # below back to their default.
        found["interval"] = gap
    if prompt is not None:
        if not prompt.strip():
            return "ERROR: 'prompt' can't be blank."
        found["prompt"] = " ".join(prompt.split())
    if provider is not None:
        if _check_provider(provider):
            return "ERROR: " + _check_provider(provider)
        found["provider"] = provider.strip()  # "" clears it back to the default
    if model is not None:
        found["model"] = model.strip()
    if temperature is not None:
        err = _check_temperature(temperature)
        if err:
            return "ERROR: " + err
        # "" clears it back to the default, same as provider/model above.
        found["temperature"] = None if (isinstance(temperature, str)
                                        and not temperature.strip()) else float(temperature)
    if safety is not None:
        err = _check_safety(safety)
        if err:
            return "ERROR: " + err
        # "" clears it back to the settings page, same as the fields above.
        found["safety"] = {"on": True, "off": False}.get(str(safety).strip().lower())

    # Rebuilt in place, so the job keeps its position in the file and its own
    # safety_prompt (and anything else hand-written on it) rides along. Found
    # by identity, not ==, which would land on the first IDENTICAL job.
    where = next(i for i, j in enumerate(data["jobs"]) if j is found)
    data["jobs"][where] = _tidy(found)
    return _write(data) or ('Updated cron job "' + name.strip() + '" - now runs '
                            + _when(found) + ".")
