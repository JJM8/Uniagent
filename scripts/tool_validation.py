"""Safety check for tool calls.

main.py hands every tool call to validate_tool_use() before running it. If the
verification model calls it safe the tool runs straight away; if not, main.py
falls back to a human y/n. The verification model is chosen separately from the
main agent's model - both on the settings page - so the two can differ, and the
whole layer can be switched off there too. The prompt itself lives in
settings.py's DEFAULTS (key "safety_prompt"), editable from the settings page -
not here - so this file has no prompt text of its own to fall out of sync with it.

A caller can pass its own prompt instead (validate_tool_use's `prompt`): a cron
job's "safety_prompt" in cron.json, or an agent's own settings .json. That
only changes the wording of the question - the verification model, and the
whitelist/blacklist here, stay global.
"""

import json

import provider
import settings

PINK = "\033[95m"
RESET = "\033[0m"

# Set by a front-end that shows the verdict itself. The server wants these
# lines - they are its journal - but cli.py draws the check into the turn it is
# rendering (main.py's on_safety), and the same text arriving a second time as
# a raw print lands in the middle of a half-drawn tool block.
quiet = False


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


def validate_tool_use(call, prompt=None):
    """(safe, reasoning) - the verification model's verdict on the call and its
    one-line why, so the verdict can be shown with its reasoning. Any error is
    treated as unsafe so a broken check fails closed, not open.

    `prompt` is the caller's own vetting prompt - a cron job's `safety_prompt:`,
    an agent's own settings .json - and must contain "{call}", which
    is where the call itself is substituted in. None (the default) uses the
    settings page's "safety_prompt", which is what every ordinary chat turn
    does. A prompt WITHOUT "{call}" would ask the model to judge a call it was
    never shown, and whatever came back would be meaningless, so that falls
    back to the global prompt rather than being run. The verification model
    and the whitelist/blacklist below are global either way: only the wording
    of the question changes, never who answers it.

    `call` is the PARSED {"tool", "args"} dict - the same shape
    tool_processor.process() runs, not the model's raw call text - so this
    works identically whether the call was regexed out of generated text or
    arrived as a provider's native structured tool_call, and so the
    whitelist below matches the actual tool name rather than a substring of
    the whole call (a command whose ARGUMENT happened to contain
    "screenshot_tool" used to false-positive the old text-substring check)."""
    text = _canonical(call)
    call_lower = text.lower()
    tool_name = (call.get("tool") or "").lower()

    # Whitelist check - trusted tools skip the model check entirely and run.
    # Matched on the parsed tool name exactly, not a substring of the call.
    WHITELIST = [
        "screenshot_tool",
    ]
    if tool_name in WHITELIST:
        _note("WHITELISTED TOOL DETECTED: " + tool_name + " - marking SAFE")
        return True, "whitelisted tool (" + tool_name + ")"

    # Blacklist check - automatically mark as unsafe if touching core files.
    # Scanned across the whole canonical text (tool + args), not just the
    # tool name - what this is meant to catch is usually a path ARGUMENT
    # naming a core file, not the tool itself.
    BLACKLIST = [
        #"main.py",
        #"tool_processor.py",
        #"tool_validation.py",
        #"provider.py",
        #"settings.py",
        #"command_processor.py"
    ]

    for filename in BLACKLIST:
        if filename in call_lower:
            _note("BLACKLISTED FILE DETECTED: " + filename + " - marking UNSAFE")
            return False, "This tool call attempts to modify a core Uniagent file (" + filename + ") and has been blocked by the blacklist."

    chosen = settings.load()
    if prompt and "{call}" not in prompt:
        _note("custom safety prompt has no {call} placeholder - using the global one")
        prompt = None
    try:
        reply = provider.get_response(
            (prompt or chosen["safety_prompt"]).replace("{call}", text),
            provider=chosen["verify_provider"],
            model=chosen["verify_model"],
        )
    except Exception as e:
        _note("check failed (" + type(e).__name__ + ": " + str(e) + ") - treating as UNSAFE")
        return False, "the safety check itself failed (" + type(e).__name__ + "), so this fails closed."

    _note("model said: " + reply.strip())
    safe = "{true}" in reply.lower()
    _note("decision: " + ("SAFE" if safe else "UNSAFE"))
    reason = reply.replace("{true}", "").replace("{TRUE}", "").strip()
    return safe, reason or "(no reasoning given)"
