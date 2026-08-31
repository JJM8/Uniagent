"""What a wire IS, as data rather than as a function.

A wire is the shape of an API, not the company behind it - see provider.py's
WIRES comment. Until now each one was a hand-written function: a URL built by
string concatenation, a headers dict, a body dict, and a loop reading the
response. Five of those functions differed only in the first three of those
four things, which is to say they differed only in data.

So the data lives here, in wires.json, and there is one function left.

    {
      "openrouter": {
        "label": "OpenRouter",
        "dialect": "openai",
        "default_base_url": "https://openrouter.ai/api/v1",
        "endpoint": "/chat/completions",
        "auth": {"header": "Authorization", "template": "Bearer $key"},
        "body": {
          "model": "$model",
          "messages": "$messages",
          "temperature": "$temperature",
          "stream": true
        }
      }
    }

That entry is a complete, working provider backend. Nothing in Python needs to
learn the word "openrouter".

THE BODY IS A TEMPLATE, AND ITS ORDER IS THE WIRE'S ORDER. What you write is
what goes out: keys are serialized in the order they appear in the file, because
that is how Python dicts and json.dumps both already behave. A picky endpoint
that wants "model" first gets "model" first, and you can see that it will by
reading the file. Values are either literals (true, 1024, "some string") or
placeholders, which are substituted for this turn's real values:

    $model        the model id, as typed on the settings page
    $messages     this turn's history, already in the dialect's own shape
    $system       the system text, split out for the dialects that want it
                  separately (anthropic, gemini) - "" on the openai dialect,
                  which carries it as the first message instead
    $temperature  the number, 0 included - see _EMPTY below
    $tools        the native tools array, provider-shaped, or nothing at all
    $stop         the stop sequences
    $max_tokens   the reply cap, where a wire insists on one
    $want_usage   true when the caller wants token counts, absent when not -
                  which is how the OpenAI wire's stream_options object appears
                  only on the turns that asked for it
    $key          the API key - for auth and headers, not normally the body
    $setting:NAME a value from this wire's own setup form (see "fields")

AN EMPTY PLACEHOLDER DELETES ITS KEY, and keeps deleting outward. A turn with
no system text must not send "system": "" - several APIs reject that - so an
empty value removes the key holding it, and a container left empty by that
removal is itself removed, all the way up. That is what lets Gemini's

    "systemInstruction": {"parts": [{"text": "$system"}]}

vanish whole on a turn with no system text, while

    "generationConfig": {"temperature": "$temperature", "stopSequences": "$stop"}

keeps its temperature on a turn with no stop sequences. One rule, and both
cases fall out of it rather than being special-cased.

WHAT IS STILL CODE, AND WHY. A spec names a `dialect`, and there are exactly
three: openai, anthropic, gemini. A dialect is the pair of things that genuinely
cannot be expressed as reordered keys - how a conversation's history is built
(a flat messages list with tool_calls, versus content blocks, versus parts with
functionCall) and how the streamed response is read back (where the text is,
where the usage is, whether a tool call arrives as fragments to accumulate or
whole in one event). Those live in provider.py, one reader each.

The split is the whole design. Practically every endpoint in the world speaks
one of those three dialects - the OpenAI wire alone covers OpenRouter, Groq,
Together, xAI, Mistral, Fireworks, Cerebras, Ollama, vLLM and LM Studio - so
"add a provider" is a JSON entry, while "add a genuinely new response format" is
the rare thing that it actually is, and is honest about needing code.

TWO FILES, MERGED. defaults/wires.json ships with Uniagent and is updated by
`git pull` like any other shipped file. wires.json in the project root is
yours: wires you invented, and per-key overrides of shipped ones. The overlay is
merged over the defaults on read, which means an update that fixes Gemini's
endpoint reaches you even though you have edited two other wires, and "revert
this wire" is deleting its key from the overlay rather than trying to remember
what it used to say.
"""

import json
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Shipped, tracked in git, replaced wholesale by an update. Never written to
# here - a wire edited on the settings page is written to the overlay below,
# so that an update can keep fixing the shipped copy underneath it.
DEFAULTS_FILE = ROOT / "defaults" / "wires.json"

# Yours: new wires, and overrides of shipped ones. Gitignored, like every other
# file that is about this install rather than about Uniagent.
OVERLAY_FILE = ROOT / "wires.json"

# The response formats that exist as code, in provider.py. A spec naming
# anything else is refused by validate() rather than failing at request time,
# because "gemeni" as a typo would otherwise look exactly like a dead endpoint.
DIALECTS = ("openai", "anthropic", "gemini")

# A wire that isn't HTTP-and-JSON at all, and so cannot be a spec: bedrock
# signs with SigV4 through boto3, claude-subscription drives a CLI. Both are
# marked `"native": true` in wires.json, which is what everything here tests;
# this map holds only the sentence explaining WHY a request template would be
# meaningless for them, which the settings page shows in place of the editor.
NATIVE_REASON = {
    "bedrock": "Signs each request with AWS SigV4 through boto3, so there is no "
               "URL or JSON body to shape.",
    "claude-subscription": "Drives the Claude Code CLI as a subprocess rather "
                           "than calling an HTTP endpoint.",
    "piper": "Runs the local piper text-to-speech engine as a subprocess rather "
             "than calling an HTTP endpoint.",
}

_lock = threading.Lock()
_cache = {}          # path -> (mtime, data)
_error = None        # whichever file last failed to parse, as a sentence


# --- reading the two files --------------------------------------------------

def _read(path):
    """One JSON object off disk, cached on mtime, never raising.

    Never raising is the point. This is asked for on every settings page load
    and on the way into every single request, and a stray comma in a file the
    user is editing must not take every provider down with it - it reports
    itself through error() and leaves the last good answer in place."""
    global _error
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.pop(path, None)
        return {}
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _error = path.name + " could not be read: " + str(e)
        return hit[1] if hit else {}
    if not isinstance(data, dict):
        _error = path.name + " should hold a JSON object of wires."
        return hit[1] if hit else {}
    data = {k: v for k, v in data.items()
            if isinstance(v, dict) and not k.startswith("_")}
    _cache[path] = (mtime, data)
    _error = None
    return data


def specs():
    """{wire name: spec} - the shipped wires with your overlay merged over
    them, key by key.

    Key by key, not wire by wire: an overlay that only says {"gemini":
    {"default_base_url": "..."}} repoints Gemini and leaves its body template,
    its dialect and its model listing to the shipped copy, which keeps getting
    updates. Overriding one thing should not mean owning all of it."""
    with _lock:
        merged = {}
        for name, spec in _read(DEFAULTS_FILE).items():
            merged[name] = dict(spec)
        for name, spec in _read(OVERLAY_FILE).items():
            merged[name] = {**merged.get(name, {}), **spec}
        return merged


def error():
    """Whichever wires file last failed to parse, as a sentence for the
    settings page, or None when both are fine."""
    return _error


def spec_for(wire):
    """One wire's spec, or {} for a native wire and for one that isn't there."""
    return specs().get(wire, {})


def names():
    """Every wire that exists, in a stable order - shipped first, in the order
    the shipped file lists them, then yours alphabetically. That order is what
    the settings page's wire dropdown shows, so the familiar names stay at the
    top as your own accumulate below them."""
    shipped = list(_read(DEFAULTS_FILE))
    return shipped + sorted(n for n in specs() if n not in shipped)


def shipped():
    """The wires Uniagent ships, in the order the shipped file lists them.

    Told apart from yours so the settings page can say what a "revert" would
    do: on a shipped wire it restores what Uniagent ships, on one of your own
    it deletes the wire outright. Same button, two quite different outcomes."""
    return list(_read(DEFAULTS_FILE))


def is_native(wire):
    """Whether this wire is a Python function rather than a spec. Such a wire
    has no request to edit, no body to reorder and no endpoint to point
    somewhere else - see NATIVE_REASON for what to tell someone who asks."""
    return bool(spec_for(wire).get("native"))


def is_spec(wire):
    """Whether this wire is driven by a spec, and so can be edited, cloned,
    previewed and pointed at a different host."""
    return wire in specs() and not is_native(wire)


def is_custom(wire):
    """Whether this wire is one of yours - present in the overlay. A shipped
    wire carrying an override counts, since that override is yours to revert."""
    return wire in _read(OVERLAY_FILE)


# --- placeholders -----------------------------------------------------------

# $name, ${name}, or $setting:NAME.
#
# The colon form is spelled out as its own branch rather than as an optional
# ":arg" on the general one, and that is not tidiness. Gemini's endpoint is
# "/models/$model:streamGenerateContent?alt=sse", where the colon belongs to
# GOOGLE'S URL, not to this syntax - a general ":arg" swallowed it whole and
# quietly produced "/models/gemini-2.5-pro?alt=sse", a URL that is a perfectly
# well-formed request to the wrong endpoint. Only "setting" takes an argument.
#
# The braces exist for the one case that needs them - a placeholder butted
# straight against a word character, as in "${model}s" - and are otherwise
# noise, so both forms are accepted.
_PLACEHOLDER = re.compile(
    r"\$\{?(?:setting:(?P<setting>[A-Za-z0-9_]+)|(?P<name>[a-z_]+))\}?")

# The sentinel a placeholder resolves to when it has nothing to say, and the
# thing _prune deletes on sight. A class rather than None, because None is a
# value a template is entitled to send deliberately.
class _Omit:
    def __repr__(self):
        return "<omit>"


OMIT = _Omit()

def _value(match, ctx):
    """One matched placeholder's value out of `ctx`, or OMIT when it has none.

    "Nothing to say" is None, or an empty string/list/dict. 0 and False are
    deliberately values: "temperature": 0 is the most-used temperature in this
    project and must survive, and a false a template states outright is an
    instruction rather than an absence."""
    setting = match.group("setting")
    if setting:
        value = (ctx.get("setting") or {}).get(setting)
    else:
        value = ctx.get(match.group("name"))
    if value is None or (isinstance(value, (str, list, dict, tuple)) and not value):
        return OMIT
    return value


def _substitute(node, ctx):
    """`node` with every placeholder in it replaced, OMIT where a value is
    absent. Structure is walked, so a placeholder nested anywhere is found.

    A string that is EXACTLY one placeholder becomes that value as an object -
    "$messages" is the messages list, not the text "[...]". A string that
    merely contains one is interpolated as text, which is what "Bearer $key"
    needs. An interpolation with an absent value is OMIT in its entirety, so an
    empty key removes its whole Authorization header rather than sending the
    word "Bearer" and nothing else."""
    if isinstance(node, dict):
        return {k: _substitute(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, ctx) for v in node]
    if not isinstance(node, str):
        return node

    whole = _PLACEHOLDER.fullmatch(node)
    if whole:
        return _value(whole, ctx)

    missing = False

    def one(m):
        nonlocal missing
        value = _value(m, ctx)
        if value is OMIT:
            missing = True
            return ""
        return value if isinstance(value, str) else json.dumps(value)

    text = _PLACEHOLDER.sub(one, node)
    return OMIT if missing else text


def _prune(node):
    """`node` with every OMIT taken out, and every container that emptying left
    behind taken out with it.

    The outward part is what makes one rule serve both of the cases in this
    module's docstring. Dropping "text" from {"parts": [{"text": OMIT}]} would
    otherwise leave {"parts": [{}]} - a systemInstruction with no instruction
    in it, which is worse than the empty string it was avoiding."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            value = _prune(value)
            if value is not OMIT:
                out[key] = value
        return out if out else OMIT
    if isinstance(node, list):
        out = [v for v in (_prune(v) for v in node) if v is not OMIT]
        return out if out else OMIT
    return node


def render(node, ctx):
    """A template rendered against this turn's values: placeholders filled in,
    absent ones and whatever they emptied removed. {} rather than OMIT at the
    top, so a caller always gets something it can send."""
    out = _prune(_substitute(node, ctx))
    return {} if out is OMIT else out


# --- building a request -----------------------------------------------------

def base_url(spec, given=""):
    """Where this wire points: what the provider's own card says, falling back
    to the spec's default. Trailing slash stripped once here so no endpoint
    template has to think about it."""
    return (given or spec.get("default_base_url") or "").rstrip("/")


def build(spec, ctx):
    """(url, headers, body) for one turn on this wire.

    Everything a request needs and nothing about how to read the reply, which
    is the dialect's business. Split out from the sending so that the settings
    page can show exactly what would go on the wire without sending it - see
    server.py's wire preview, which is the difference between "it doesn't work"
    and "look, it's putting the key in the wrong header"."""
    url = base_url(spec, ctx.get("base_url", "")) + str(
        render(spec.get("endpoint") or "", ctx) or "")

    headers = {}
    for key, value in (render(spec.get("headers") or {}, ctx) or {}).items():
        headers[key] = value if isinstance(value, str) else json.dumps(value)

    auth = spec.get("auth") or {}
    token = render(auth.get("template", "$key"), ctx)
    # A wire with no key sends no auth header at all, rather than an empty one.
    # Some servers that would happily serve an anonymous request reject a
    # malformed "Bearer " outright, so this distinction is load-bearing for
    # every local model server.
    if token and isinstance(token, str):
        if auth.get("header"):
            headers[auth["header"]] = token
        elif auth.get("query"):
            sep = "&" if "?" in url else "?"
            url += sep + auth["query"] + "=" + token

    return url, headers, render(spec.get("body") or {}, ctx)


def models_request(spec, ctx):
    """(url, headers) for this wire's model-listing call, or (None, None) when
    it hasn't got one.

    The same auth and header machinery as build(), against the spec's "models"
    block - a catalogue endpoint authenticates the same way the chat endpoint
    does, on every API anyone has built."""
    listing = spec.get("models") or {}
    if not listing.get("endpoint"):
        return None, None
    sub = {**spec, "endpoint": listing["endpoint"],
           "headers": listing.get("headers", spec.get("headers")),
           "body": {}}
    url, headers, _ = build(sub, ctx)
    return url, headers


# Substrings that mark a model as not-for-chat in an unfiltered catalogue.
# Erring on keeping: an unfamiliar id is far more likely to be a new chat model
# than a new embedding model, and a missing model is more annoying than an
# extra one in a dropdown.
_NOT_CHAT = ("embed", "whisper", "tts", "dall-e", "audio", "moderation",
             "realtime", "transcribe", "image", "davinci", "babbage")


def parse_models(spec, payload):
    """The model ids out of a catalogue response, per the spec's "models" block:

      list          the key holding the array ("data", "models")
      id            the key on each entry holding its id ("id", "name")
      strip_prefix  cut off the front of each id (Gemini's "models/")
      requires      [key, value] - keep only entries whose named key contains
                    that value, which is how Gemini's list is narrowed to the
                    models that can actually generate content
      filter_chat   drop the obviously-not-chat entries an unfiltered
                    catalogue includes, which the OpenAI wire's /models needs
                    and the two curated catalogues do not

    Anything shaped unexpectedly yields nothing rather than raising: the caller
    treats an empty list as "no live catalogue" and falls back to the wire's
    suggestions, which is a working provider with a shorter dropdown."""
    listing = spec.get("models") or {}
    entries = payload.get(listing.get("list") or "data")
    if not isinstance(entries, list):
        return []
    id_key = listing.get("id") or "id"
    prefix = listing.get("strip_prefix") or ""
    need_key, need_value = (listing.get("requires") or ["", ""] if
                            isinstance(listing.get("requires"), list)
                            else ["", ""])
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if need_key and need_value not in (entry.get(need_key) or []):
            continue
        value = entry.get(id_key)
        if isinstance(value, str) and value:
            out.append(value[len(prefix):] if prefix and value.startswith(prefix)
                       else value)
    if listing.get("filter_chat"):
        out = [i for i in out if not any(n in i for n in _NOT_CHAT)]
    return sorted(out) if listing.get("filter_chat") else out


# --- which endpoint of a model to use ---------------------------------------
#
# A router - OpenRouter is the one everybody uses - serves the SAME model id
# from a dozen different companies, and they are not the same thing to run on:
# they quantise differently, cap the context differently, charge differently
# and go down at different times. So a wire may say how to ask which endpoints
# a model has, and how to name one in a request:
#
#     "routes": {
#       "endpoint": "/models/$model/endpoints",
#       "list": "data.endpoints",
#       "id": "tag",
#       "label": "provider_name",
#       "send": {"provider": {"order": ["$route"], "allow_fallbacks": false}}
#     }
#
# `id` is what gets SENT (OpenRouter's provider slug, "deepinfra/fp4"), `label`
# what a person reads ("DeepInfra"), and "send" is merged into the body of any
# turn whose model has a route chosen for it - and only then, so a wire with
# this block behaves exactly as before until somebody picks an endpoint.
#
# A wire without the block simply cannot route, which is every ordinary
# provider: asking OpenAI which companies serve gpt-5 is not a question.


def routes_spec(spec):
    """This wire's "routes" block, or {} - the one place anything asks whether
    a wire can be pointed at a particular endpoint at all."""
    block = spec.get("routes")
    return block if isinstance(block, dict) else {}


def routes_request(spec, ctx):
    """(url, headers) for "which endpoints serve this model", or (None, None).

    models_request's sibling, and the same reasoning: a catalogue endpoint
    authenticates the way the chat endpoint does. $model is filled in from ctx,
    since this question is asked about one model at a time."""
    block = routes_spec(spec)
    if not block.get("endpoint"):
        return None, None
    sub = {**spec, "endpoint": block["endpoint"],
           "headers": block.get("headers", spec.get("headers")),
           "body": {}}
    url, headers, _ = build(sub, ctx)
    return url, headers


def _dig(payload, path):
    """The value at a dotted path ("data.endpoints"), or None. One level is the
    ordinary case; OpenRouter nests its endpoints one deeper than its models,
    and a path costs less than a second parser."""
    node = payload
    for step in str(path or "").split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


# What an endpoint entry may say about itself beyond its name. Read by key
# where it exists and skipped where it does not, because these are what a
# person actually chooses between - not decoration.
_ROUTE_NOTES = (("quantization", "{}"),
                ("context_length", "{:,} ctx"),
                ("uptime_last_30m", "{:.0f}% up"))


def parse_routes(spec, payload):
    """The endpoints of one model, per the spec's "routes" block, as
    [{"id", "label", "note"}] - id being what a request names, label what the
    list shows, note the few facts worth choosing on (quantisation, context,
    price, uptime).

    Anything shaped unexpectedly yields nothing rather than raising, exactly as
    parse_models does: the caller treats an empty list as "this provider does
    not route", which leaves the box a person can still type a slug into."""
    block = routes_spec(spec)
    entries = _dig(payload, block.get("list") or "data")
    if not isinstance(entries, list):
        return []
    id_key = block.get("id") or "id"
    label_key = block.get("label") or id_key
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get(id_key)
        if not isinstance(value, str) or not value:
            continue
        notes = []
        for key, shape in _ROUTE_NOTES:
            fact = entry.get(key)
            if isinstance(fact, (int, float)) and not isinstance(fact, bool):
                notes.append(shape.format(fact))
            elif isinstance(fact, str) and fact:
                notes.append(fact)
        price = entry.get("pricing")
        if isinstance(price, dict):
            try:
                notes.append("${:g}/${:g} per M".format(
                    float(price.get("prompt") or 0) * 1e6,
                    float(price.get("completion") or 0) * 1e6))
            except (TypeError, ValueError):
                pass
        out.append({"id": value,
                    "label": str(entry.get(label_key) or value),
                    "note": " \u00b7 ".join(notes)})
    return out


def route_body(spec, route):
    """What to merge into a turn's body to send it to `route`, or {}.

    The shape is the wire's own (OpenRouter's provider.order pair), rendered
    from the spec rather than written here, so a router with a different shape
    for the same idea is a JSON edit."""
    if not route:
        return {}
    block = routes_spec(spec)
    rendered = render(block.get("send") or {}, {"route": route})
    return rendered if isinstance(rendered, dict) else {}

# --- the settings form, and validating an edit ------------------------------

def fields(spec):
    """The extra boxes this wire asks for beyond a URL and a key, each settled
    into a full dict so no caller has to guess at a missing key.

    A field with no "env" is dropped: that name is what the value is saved and
    looked up under, and is the one thing that cannot be defaulted."""
    out = []
    for f in spec.get("fields") or []:
        if not isinstance(f, dict):
            continue
        env = str(f.get("env") or "").strip()
        if not env:
            continue
        out.append({
            "env": env,
            "label": str(f.get("label") or env),
            "help": str(f.get("help") or ""),
            "secret": bool(f.get("secret")),
            "required": bool(f.get("required")),
            "default": str(f.get("default") or ""),
            "placeholder": str(f.get("placeholder") or ""),
        })
    return out


def problems(spec):
    """Everything wrong with `spec`, as sentences to show next to the editor.

    A list rather than a raised exception, and a list rather than a bool: the
    point is to tell someone what to fix while they are looking at the thing
    that needs fixing. Called before a save, so a wire that would fail every
    request is refused at the moment it is written rather than at the moment it
    is next used, which could be days later inside a cron job."""
    out = []
    if not isinstance(spec, dict):
        return ["A wire must be a JSON object."]
    if spec.get("native"):
        return []       # nothing here describes it - see NATIVE_REASON

    dialect = spec.get("dialect")
    if not dialect:
        out.append("No dialect. Pick one of: " + ", ".join(DIALECTS) + ".")
    elif dialect not in DIALECTS:
        out.append('Unknown dialect "' + str(dialect) + '". Uniagent can read '
                   + ", ".join(DIALECTS) + ".")

    if not spec.get("endpoint"):
        out.append("No endpoint - the path appended to the base URL, "
                   'such as "/chat/completions".')

    body = spec.get("body")
    if not isinstance(body, dict) or not body:
        out.append("No body template, so the request would carry nothing.")
    else:
        flat = json.dumps(body)
        # $model is looked for in the ENDPOINT too, not only the body. Gemini
        # names the model in its URL and never mentions it in the JSON, which
        # is a perfectly good wire and must not be reported as a broken one.
        if "$model" not in flat + str(spec.get("endpoint") or ""):
            out.append("Neither the endpoint nor the body uses $model, so the "
                       "request would not say which model to use.")
        if "$messages" not in flat:
            out.append("The body never uses $messages, so the request would "
                       "not say which conversation to answer.")

    # Every template on the spec, not just the body: a typo in the auth header
    # is the one that costs an afternoon, because it fails as a 401 that looks
    # exactly like a wrong key.
    everywhere = json.dumps([spec.get(k) for k in
                             ("endpoint", "body", "headers", "auth", "models",
                              "routes")])
    for m in _PLACEHOLDER.finditer(everywhere):
        name = m.group("setting") and "setting" or m.group("name")
        if name not in _KNOWN:
            out.append('Unknown placeholder "$' + str(m.group("name")) + '".')

    auth = spec.get("auth")
    if auth is not None:
        if not isinstance(auth, dict):
            out.append('"auth" must be an object with a header or query name.')
        elif not auth.get("header") and not auth.get("query"):
            out.append('"auth" names neither a header nor a query parameter, '
                       "so the key would not be sent.")
    return out


# Placeholder names build() knows how to fill. Kept next to problems() because
# its only job is telling someone they typed one that doesn't exist - the
# renderer itself needs no list, it just finds nothing and omits the key, which
# is a silently keyless request and exactly the failure worth catching early.
_KNOWN = ("model", "messages", "system", "temperature", "tools", "stop",
          "max_tokens", "want_usage", "key", "setting", "route")


# --- writing the overlay ----------------------------------------------------

def _write_overlay(data):
    OVERLAY_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _cache.pop(OVERLAY_FILE, None)


def save(name, spec):
    """Write one wire into the overlay. Returns [] on success, or the problems
    that stopped it.

    Only ever touches the one key, so saving a wire cannot disturb another, and
    the shipped file is never written at all - a shipped wire edited here
    becomes an entry in the overlay sitting on top of it."""
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name or ""):
        return ["A wire name must be lower-case letters, digits, dots, dashes "
                "or underscores, starting with a letter or digit."]
    if is_native(name):
        return [NATIVE_REASON.get(name, "This wire is a Python function.")
                + " It cannot be edited as a spec."]

    # Validate what the wire WILL BE, not what was typed. An override is
    # allowed to be a fragment - {"default_base_url": "..."} to point Gemini at
    # a proxy is the whole point of the overlay - and judging that fragment on
    # its own would reject it for having no dialect, no endpoint and no body,
    # every one of which it is inheriting from the shipped wire underneath it.
    # A brand-new wire has nothing underneath, so for that case this is exactly
    # the same check as before.
    shipped_spec = _read(DEFAULTS_FILE).get(name, {})
    found = problems({**shipped_spec, **spec})
    if found:
        return found

    # Store only what actually DIFFERS from the shipped wire.
    #
    # The settings page hands back the whole wire, because the whole wire is
    # what it showed you. Writing all of it would be quietly destructive: every
    # key you did not touch would become yours, frozen at today's value, and a
    # later update that fixed this wire's endpoint would reach everyone except
    # the person who once renamed its label. Keeping the difference means an
    # override stays an override, however it was edited.
    #
    # A brand-new wire has nothing to differ from, so all of it is kept.
    trimmed = {k: v for k, v in spec.items()
               if k not in shipped_spec or shipped_spec[k] != v}

    data = dict(_read(OVERLAY_FILE))
    if shipped_spec and not trimmed:
        # Edited back to exactly what Uniagent ships. That is a revert, and
        # leaving an empty entry behind would keep claiming ownership of a wire
        # nothing has changed about.
        data.pop(name, None)
    else:
        data[name] = trimmed
    _write_overlay(data)
    return []


def delete(name):
    """Take a wire out of the overlay - which removes a custom wire entirely,
    and reverts a shipped one to exactly what Uniagent ships. Returns whether
    there was anything there to remove."""
    data = dict(_read(OVERLAY_FILE))
    if name not in data:
        return False
    del data[name]
    _write_overlay(data)
    return True
