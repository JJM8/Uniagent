"""Settings that outlive a restart, kept in settings.json at the project root.

One flat dict, read from disk every time it's asked for - so a change made in
the web UI takes effect on the very next turn, in this process AND in the cron
watcher, without either being restarted. It's a few hundred bytes; the read
costs nothing next to a model call.

DEFAULTS is the whole schema. A key that isn't in it is refused on save and
ignored on load, so a hand-edited or half-written file can't inject anything
odd - the worst it can do is fall back to the default.
"""

import json
import re
import threading
from pathlib import Path

import provider

SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"

# The accent colour, as the colour input on the appearance tab hands it over.
# "" is a real value here - it means "whatever the theme's own accent is" -
# so this is checked rather than the generic type match, which would happily
# take "not a colour" and leave the page with an unstyled accent.
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Settings that name a provider - validated against provider.available() below,
# not just checked for type, so nothing can end up pointed at a provider with
# no working credentials.
PROVIDER_KEYS = ("provider", "sub_provider", "cron_provider", "verify_provider",
                 "speak_summary_provider")

# Each model setting, paired with the provider setting it must belong to, so
# a blank model is filled in from the right provider's list - see
# _heal_models(). The model itself is never rejected for being absent from
# that list; suggestions are not a whitelist.
MODEL_PAIRS = {
    "model": "provider",
    "sub_model": "sub_provider",
    "cron_model": "cron_provider",
    "verify_model": "verify_provider",
    "speak_summary_model": "speak_summary_provider",
}

# temperature isn't a standardised unit - it's just how far a provider's API
# lets you scale the randomness of its output - but every provider here treats
# 0-2 as the sane range, so that's what's enforced regardless of the value's
# type (int or float both come out of JSON depending on whether the number has
# a decimal point).
TEMPERATURE_RANGE = (0, 2)

# Every setting that holds one, so they're all checked against the range above
# rather than only the main one being.
TEMPERATURE_KEYS = ("temperature", "speak_summary_temperature")

# What gets read out loud, as the voice tab's one dropdown. The provider pair
# below is WHO reads it; this is WHAT they're given.
#
#   off       nothing. The switch, kept separate from "nobody chosen" so
#             turning speech off doesn't throw away the model you picked.
#   final     only a reply the turn ended on - the model answering in words
#             rather than calling a tool. The original behaviour, and still
#             the default.
#   all       every message the model wrote during the turn, the ones
#             alongside tool calls included, read one after another.
#   summary   one summary of the whole turn, written by the summarising model
#             below - a single account of what happened, however many steps it
#             took.
#   summary_each    a summary of each message instead, read one after another -
#             the running commentary, in short.
#   summary_final   a summary of just the reply the turn ended on. Nothing is
#             read for a turn that was all tool work.
SPEAK_MODES = ("off", "final", "all", "summary", "summary_each", "summary_final")

# Settings that are a list of strings rather than a single value - the safety
# tab's two lists and the marketplace's repositories. Each is drawn by the
# page's one list widget, and each is checked element by element in _valid(),
# because the generic
# `isinstance(value, type(DEFAULTS[key]))` match would take a list of anything
# at all, dicts and nulls included, and hand it to code that expects text.
LIST_KEYS = ("safety_whitelist", "safety_blacklist", "market_repos")

# How careful a chat is, as one number: the highest 0-10 danger rating the
# checking model can give a tool call and still have it run unattended.
# Anything above it stops and asks. The two ends are the degenerate cases and
# skip the checking model entirely, because there is nothing to ask it:
#
#   0    ask about every call - nothing runs unattended
#   1-9  run anything rated at or under this, ask about the rest
#   10   run everything - no check at all
#
# One number rather than a set of named levels. The scale the prompt already
# asks for IS the setting, so there is nothing to map between, nothing to keep
# in sync, and no wording for a level to drift away from.
SAFETY_MIN, SAFETY_MAX = 0, 10

DEFAULTS = {
    # The main agent's provider + model.
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    # What a NEW chat, subagent or cron job runs at when nothing - the agent
    # itself, a subagent call, or a cron.json job - names its own temperature.
    # 0 = most predictable, higher = more random. See provider.py's TEMPERATURE
    # for how this actually reaches a request.
    "temperature": 0,
    # What a cron job runs on when it names no provider/model of its own.
    "cron_provider": "deepseek",
    "cron_model": "deepseek-v4-flash",
    # What a NEW subagent runs on when the call names neither. An existing
    # subagent keeps whatever it last ran on regardless.
    "sub_provider": "deepseek",
    "sub_model": "deepseek-v4-flash",
    # The safety check that vets every tool call, and what it runs on. The
    # prompt must contain "{call}" - tool_validation.py substitutes the tool
    # call into it with a plain string replace, not str.format, so any other
    # literal { or } in an edited prompt is safe to leave as-is.
    "safety_validation": True,
    "verify_provider": "bedrock",
    "verify_model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    # What a chat runs at when it has not been given a number of its own.
    # See SAFETY_MIN/SAFETY_MAX above for what the number means.
    "safety_threshold": 3,
    # The prompt the check sends. It asks for a number, not a verdict - the
    # verdict is the comparison against the threshold, which is what lets one
    # prompt serve every setting from 0 to 10 instead of a wording per level
    # that drifts the first time one is edited.
    #
    # Two placeholders, both substituted with a plain string replace and not
    # str.format, so a stray { or } elsewhere in an edited prompt is harmless:
    #   {call}   the tool call being judged. Required - a prompt without it
    #            asks about nothing, so tool_validation falls back to this one.
    #   {extra}  the chat's own extra rules, or nothing at all. Optional; a
    #            prompt without it gets them appended at the end instead.
    "safety_prompt": """You are the safety check for an AI agent that can run things on a real
computer, on behalf of its owner. The owner asked for the work; you are only
judging how much damage this particular step could do.

Here is the tool call the agent wants to run:

{call}

Rate how dangerous it is, from 0 to 10:

- 0-1   Harmless. Reading a file, listing a folder, checking status, searching.
- 2-3   Low risk. Writing a file in the user's own work, installing a package
        for this user, making a directory.
- 4-6   Moderate. Changing configuration, running a script, network calls,
        sending something to somebody else, spending money.
- 7-9   High. System-wide changes, deleting files, changing permissions, sudo,
        anything that would be hard to undo.
- 10    Catastrophic. Wiping data, rm -rf /, breaking the machine or its login.

Judge what the call ACTUALLY does, not what it mentions. Reading a file about
deleting things is a read.
{extra}
Answer with one line and nothing else:

DANGER: <0-10> - <one short sentence saying why>""",
    # Tools that skip the check entirely and just run. Matched against the
    # PARSED tool name, whole and case-insensitively - not a substring of the
    # call - so trusting "screenshot_tool" trusts that tool and not a command
    # that merely mentions it. Checked before the blacklist below.
    #
    # Global rather than per-level, on purpose. It matters most at the safest
    # level, where it is the only thing that runs without being asked about -
    # and a list that says "these tools are fine" does not become untrue
    # because the chat next to it is more relaxed.
    "safety_whitelist": ["screenshot_tool"],
    # Phrases that block a call outright, without asking the model. Matched
    # anywhere in the call text (tool name AND arguments, lower-cased), which
    # is the point: what these catch is usually a path in an ARGUMENT - the
    # agent editing its own code - rather than the tool itself.
    #
    # This one applies at EVERY level, full auto included. A phrase here is a
    # thing that must never run unasked, and the level a chat happens to be on
    # does not change that - a safety net the most dangerous setting is the
    # only one to skip would be no net at all.
    #
    # Empty by default. The obvious ones to add are this project's own files:
    # main.py, tool_processor.py, tool_validation.py, provider.py, settings.py,
    # command_processor.py. Left to the user because a blacklist that ships on
    # blocks work the user never asked to have blocked.
    "safety_blacklist": [],
    # Which GitHub repositories the tools tab's marketplace browses, as
    # "owner/repo" or "owner/repo/sub/folder" when the tools or skills sit
    # somewhere other than the top level. Read live each time the marketplace
    # is opened (cached a few minutes - see market.py), so adding one here
    # shows its contents without a restart.
    "market_repos": ["anthropics/skills"],
    # The instruction /compact sends as the last user turn, after the whole
    # conversation - see compaction.py, which reads it through _prompt().
    # No placeholders: the history is the messages ahead of it, not text
    # pasted into it, so what's written here reaches the model verbatim.
    #
    # Blank is a real value and means "the prompt below", which is how the
    # compaction tab's empty box restores the shipped default.
    "compaction_prompt": """That is the whole conversation so far, and it has grown
too long to keep sending in full.

Rewrite it as a much shorter version that keeps everything still needed to carry
on: what the user asked for, decisions made and the reasons for them, facts
established, files and paths touched, what tools were run and whatever they
returned that still matters, and anything left outstanding or half-finished.
Drop chit-chat, repetition, and tool output that no longer matters. Keep exact
names, paths, numbers and quoted text where they matter - a summary that loses
those is no use to the agent reading it.

Do not start any line with "User:", "Uniagent:" or "Tool result:" - generation
is cut off at those markers, so a summary written that way would lose
everything after its first line. Reply with the compacted conversation and
nothing else.""",
    # UI only.
    "theme": "dark",
    # The one colour the whole page is built out of - every button outline,
    # every highlight, the sidebar's current chat - as "#rrggbb". Empty means
    # "the theme's own", which is what the CSS says when nothing overrides it;
    # anything else is turned into the --accent-h/s/l variables by the page, so
    # one value moves every accent tone at once.
    "accent": "",
    # Hold this key in the web page to talk (the mic button next to the input
    # does the same thing, and is what you use on a phone). A KeyboardEvent
    # code - "F9", "ScrollLock", "KeyM"...
    #
    # NOT Scroll Lock, deliberately, even though that's voice_input.py's key:
    # its listener is a global keyboard hook, so on the machine running the
    # server it hears Scroll Lock while the browser has focus too - the same
    # hold would record twice and send the sentence twice. Pick a different key
    # here and the two stay out of each other's way.
    "voice_key": "F9",
    # Who turns a held-down clip into words, and on which model. A provider
    # off the providers tab, exactly like every other model setting here - so
    # transcription is keyed, priced and pointed the same way chat is, and
    # nothing has its own private credentials.
    #
    # "" is the old behaviour, kept for an install that has never opened the
    # voice tab: Whisper at OpenAI on the OPENAI_API_KEY in .env. It is also
    # what an unusable provider heals back to, see _heal_providers.
    "voice_provider": "",
    "voice_model": "",
    # Who reads the finished reply back out loud, and on which model - the same
    # kind of pair as the two above, asked of the endpoint that runs the other
    # way. Naming a provider IS the on switch: "" means the page stays silent,
    # which is what an install that has never opened the voice tab does.
    "speak_provider": "",
    "speak_model": "",
    # And what they're handed. See SPEAK_MODES above for the six answers.
    # "final" is what this did before the setting existed, so an install that
    # upgrades into it hears exactly what it heard yesterday.
    "speak_mode": "final",
    # The model that writes the summary, for the three summary modes - a normal
    # provider/model/temperature trio, like the checking model on the safety
    # tab. It is only ever asked when one of those modes is on.
    "speak_summary_provider": "deepseek",
    "speak_summary_model": "deepseek-v4-flash",
    "speak_summary_temperature": 0,
    # What it's told. Sent as the system message with the message underneath it
    # as a user message, so there is no placeholder to keep - the same
    # arrangement compaction_prompt uses, and for the same reason: the text
    # being worked on is a message, not something pasted into the instruction.
    #
    # It is written as "you ARE the agent's voice", not "summarise this text",
    # because the two produce completely different things. Asked for a summary,
    # a model narrates from outside - "the agent will now open Firefox" - which
    # is nobody's idea of being spoken to. Told it is the voice, it says
    # "Opening Firefox." The examples are doing most of the work here; the
    # rules alone don't get a model to drop its throat-clearing.
    "speak_summary_prompt": """You are the voice of an AI agent, speaking to the person it works for. You
are not describing the agent from outside - you ARE it. Calm, clipped, dry.
Never eager, never apologetic, never chatty. Their time is the point.

Below is what the agent just wrote. Say it out loud in as few words as it
takes. Fragments are good. Drop "I", "the", and any word the line still works
without.

On the way to a tool call, a few words is the whole thing:
  "Opening Firefox."
  "Checking the logs."
  "Rewriting the config."

When the agent has an answer, give the ANSWER - one or two sentences, no more:
  "Three tests failed, all in the payment suite."
  "It's the API key. Expired last week."

Never announce what you are about to do: not "I will now open Firefox for
you", not "Let me check that", not "Sure", not "Here is a summary". Never say
"the user" or "the agent" - it is "you" and "your".

This may be one step of a long job. Say this step. Don't recap what came before
it or promise what comes next.

It goes straight into a speech synthesiser, so write plain spoken English: no
markdown, no headings, no bullets, no code, and no file paths or URLs spelled
out character by character - say "the settings file". Keep the numbers and
names that matter.

Reply with the words to be spoken and nothing else.""",
}

_write_lock = threading.Lock()


def _valid(key, value):
    """False if `value` must be refused for `key`: an unknown key, the wrong
    type, or - for one of PROVIDER_KEYS - a provider with no working
    credentials right now. Shared by load() and save() so a bad value can't
    get in through either door.

    "temperature" gets its own check rather than the generic type match below:
    a whole number reaches here as an int (bare `0`) and a fractional one as a
    float (`0.7`) depending only on whether JSON happened to see a decimal
    point, so matching DEFAULTS' exact type would reject half of them for no
    reason. bool is excluded explicitly because bool is a subclass of int in
    Python - True would otherwise pass as 1."""
    if key not in DEFAULTS:
        return False
    if key == "accent":
        return isinstance(value, str) and (not value or bool(ACCENT_RE.match(value)))
    if key in ("voice_provider", "speak_provider"):
        # Not in PROVIDER_KEYS: unlike the four settings there, these two are
        # allowed to name nobody, and "" is how they say so.
        return isinstance(value, str) and (not value or value in provider.available())
    if key in LIST_KEYS:
        # A list of strings, and nothing else - one bad element refuses the
        # whole list rather than being dropped quietly, since a safety list
        # that saved as "most of what you typed" is worse than one that
        # visibly didn't save. Blank and odd-cased entries ARE allowed
        # through: tool_validation.py strips, lower-cases and skips empties
        # when it matches, so neither can turn into a rule that fires on
        # everything.
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if key == "safety_threshold":
        # bool is excluded for the same reason as temperature's check below:
        # True would otherwise pass as the threshold 1.
        return (isinstance(value, int) and not isinstance(value, bool)
                and SAFETY_MIN <= value <= SAFETY_MAX)
    if key == "speak_mode":
        return value in SPEAK_MODES
    if key in TEMPERATURE_KEYS:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and TEMPERATURE_RANGE[0] <= value <= TEMPERATURE_RANGE[1])
    if not isinstance(value, type(DEFAULTS[key])):
        return False
    if key in PROVIDER_KEYS and value not in provider.available():
        return False
    return True


def _heal_providers(data):
    """Point any provider setting that names a provider which isn't there at
    one that is, in place.

    DEFAULTS below can only name a provider as a string, and every provider is
    now a deletable object - so the default is a guess about the user's .env,
    not a guarantee. Without this, deleting or renaming the provider DEFAULTS
    happens to name leaves the fallback itself broken: load() hands back a
    name nothing can resolve and the next message fails on a provider that
    hasn't existed for a while, which is a baffling thing to debug.

    The first available provider is an arbitrary choice, but it is a WORKING
    arbitrary choice, and it only applies when the real answer is already
    gone. Nothing is written to disk here - a fallback is what to run right
    now, not a decision to make on the user's behalf."""
    usable = provider.available()
    if not usable:
        return          # nothing to fall back TO; let the call fail and say so
    for model_key, provider_key in MODEL_PAIRS.items():
        if data.get(provider_key) not in usable:
            data[provider_key] = usable[0]
            # And drop the model with it. It was an id for the provider that
            # went away, and sending a DeepSeek model id to whatever replaced
            # it fails at request time - _heal_models() below fills the blank
            # in from the new provider's own list.
            data[model_key] = ""
    # Voice heals the other way: there is no fallback transcriber to move it
    # to, and picking one on the user's behalf would send their microphone to
    # a provider they never chose. Blank is a working state - it means the
    # OPENAI_API_KEY path in voice_input.py - so a provider that has gone away
    # simply reads as "nobody chosen" until one is.
    if data.get("voice_provider") and data["voice_provider"] not in usable:
        data["voice_provider"] = ""
        data["voice_model"] = ""
    # And speaking the reply back, for the same reason and with the same
    # meaning: blank is a working state, it just means nothing is read aloud.
    if data.get("speak_provider") and data["speak_provider"] not in usable:
        data["speak_provider"] = ""
        data["speak_model"] = ""


def _heal_models(data):
    """Fill in any model setting left blank, in place, with the default for
    its paired provider. Runs AFTER every key is settled, so it sees the final
    provider either way, not just the one in this call's `updates`.

    It does NOT check the model against the provider's known models - that
    list is suggestions, not a whitelist, and a model absent from it is
    usually just newer than the list. Only emptiness is healed, because an
    empty model is the one value that certainly cannot work. The cost is that a mismatched
    pair (deepseek + an Anthropic model id) is now saveable and will fail at
    request time with the provider's own error, which is a clearer place to
    find out than being silently overwritten here."""
    for model_key, provider_key in MODEL_PAIRS.items():
        if not str(data[model_key]).strip():
            data[model_key] = provider.default_model(data[provider_key])


def load(fallback=True):
    """Every setting, defaults filled in. Never raises - a missing or corrupt
    file just means 'all defaults', which is always safe to run on.

    `fallback=False` skips _heal_providers, leaving a setting that names a
    provider which isn't there as it is. Only save() wants that: healing is a
    view of what to run RIGHT NOW, and save() writes what it is handed, so
    healing on the way into a write would quietly make a temporary fallback
    into the user's permanent choice - deleting a provider and then changing
    the temperature would silently move which provider you chat on."""
    data = dict(DEFAULTS)
    try:
        stored = json.loads(SETTINGS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        stored = {}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if _valid(key, value):
                data[key] = value
    if fallback:
        _heal_providers(data)
    _heal_models(data)
    return data


def get(key):
    return load().get(key, DEFAULTS.get(key))


def save(updates):
    """Merge `updates` in and write. Returns the full settings as they now are.
    Locked, so two pages saving at once can't interleave into a broken file."""
    with _write_lock:
        data = load(fallback=False)
        for key, value in updates.items():
            if _valid(key, value):
                data[key] = value
        _heal_models(data)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")
        return data
