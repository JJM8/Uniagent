"""How long a turn's pieces took - measured here, stored on the turn, drawn
everywhere.

A chat already records WHEN each message landed (main.stamp_history writes
epoch seconds beside the transcript). That answers "when did this happen" and
nothing else: a reply that took forty seconds and one that took two look
identical, and on a local model - where the whole point is that you can watch
it work - that is the most interesting number on the screen.

THE THREE THINGS A RESPONSE IS
------------------------------
A response is not one duration. It is a wait followed by up to two streams,
and lumping them together produces numbers that are not merely imprecise but
actively wrong - which is how a 30 tok/s local model came to report 1000.

    latency   the request goes out -> the first token of anything comes back.
              Nothing is being generated here: the model is queued, or the
              prompt is being processed. On a local server with a long
              conversation this is usually the biggest number of the three,
              and it is the one that grows as the chat does.
    think     the first reasoning token -> the last one. Time the model spent
              working rather than writing, and its own token count.
    write     the first token of the reply -> the last one. The reply, and its
              own token count.

Each of the two streams carries its OWN tokens and its OWN span, and a rate is
only ever computed within one of them. That is the correction: a response that
spent eight seconds thinking 600 tokens and one second writing 40 has two
honest speeds - 75 tok/s and 40 tok/s - and exactly one dishonest one, which
is what you get from dividing every token the response produced by the seconds
it spent writing. Latency is excluded from both, because no token was produced
during it; counted in, a model looks slower the longer its prompt gets, which
says something true about the setup and nothing about the model.

`total` is kept as well and is NOT the other three added up. Latency, thinking
and writing can overlap or leave gaps (a stream can stall mid-sentence; a model
can go back to thinking after writing), so the whole is measured on its own
rather than trusted to reconcile from the parts.

Milliseconds, as plain ints. Nothing here is meaningful below a millisecond,
and a float would put "12.100000000000001s" into a chat file read by hand.

WHERE IT IS STORED
------------------
A timing dict goes onto the history turn itself, as a "timing" key beside
"raw_call" and "reasoning_content" - not into a sidecar file paired by index
the way stamps are. Stamps have to live outside the transcript because they are
reconciled against a history that gets rewritten underneath them (/compact
replaces every turn, a stopped turn rewrites its tail); a duration has no such
problem, because it belongs to the message and moves, survives or dies with it.
It also means a turn dropped by compaction takes its timing with it instead of
sliding every remaining number onto the wrong message.

Nothing here reaches a model. provider._NATIVE_KEYS whitelists the keys that go
over the wire and "timing" is not one of them, the same way raw_call never has
been - see _native_messages.
"""

import time


def now():
    """The clock everything here measures against.

    monotonic, not wall time: a duration must not go negative because NTP
    stepped the clock or the machine came back from suspend mid-request. The
    stamps file is the one that needs a real date, and it takes its own."""
    return time.monotonic()


def ms(since, until=None):
    """A span as whole milliseconds, or None when it never started.

    None rather than 0 for something that did not happen, because the two are
    genuinely different: a model that did no thinking has no think span, and
    drawing that as "0ms" claims a measurement nobody made."""
    if since is None:
        return None
    return int(round(((until if until is not None else now()) - since) * 1000))


class Phases:
    """The clock on one model response.

    Fed by main._stream as the stream arrives - thinking() and writing() are
    each safe to call on every fragment, and only the bookkeeping that has to
    happen once does. That is what lets the call sites be one line at the top
    of a branch they were already in, rather than a state machine.
    """

    def __init__(self):
        self.began = now()
        self.first_at = None      # the first token of anything came back
        self.think_from = None
        self.think_to = None
        self.write_from = None
        self.write_to = None
        self.last = None          # which stream was live most recently
        self.ended = None

    def restart(self):
        """Move the start of the clock to now, discarding what came before.

        Called once the request is genuinely about to go out. A Phases is made
        by the caller, before the prompt has even been built - and building it
        is real work (the tool schemas are assembled and serialised on every
        pass, which measured at over a hundred milliseconds here). Left in,
        that time lands in `latency`, where it reads as the provider being slow
        to answer. It is Uniagent being slow to ask, which is a different
        problem with a different fix, and a number that quietly blames the
        wrong one is worse than no number at all."""
        self.began = now()
        return self

    def _first(self):
        if self.first_at is None:
            self.first_at = now()

    def thinking(self):
        """A reasoning token arrived. The end of the span is moved on every
        time, since the last one is only known to be the last once something
        else comes - or once the stream closes, see end()."""
        self._first()
        at = now()
        if self.think_from is None:
            self.think_from = at
        self.think_to = at
        self.last = "think"

    def writing(self):
        """A token of the reply itself arrived - prose, or the text of a tool
        call being typed, which is equally the model writing."""
        self._first()
        at = now()
        if self.write_from is None:
            self.write_from = at
        self.write_to = at
        self.last = "write"

    def end(self):
        """The stream is closed.

        Whichever of the two streams was live when it closed ran until it
        closed, so its end is moved out to here. Without this a reply that
        arrived as a single chunk would measure zero milliseconds and report no
        speed at all, when what actually happened is that it generated for as
        long as the stream was open."""
        if self.ended is None:
            self.ended = now()
            if self.last == "think":
                self.think_to = self.ended
            elif self.last == "write":
                self.write_to = self.ended
        return self

    def reclassify(self):
        """What was collected as thinking turned out to BE the reply.

        One provider quirk needs this: some local builds stream an entire
        answer as reasoning_content and never send a content chunk, so it is
        only at the end of the stream that anyone can tell (see
        provider._read_openai's tail). Everything measured as the think stream
        was therefore the write stream, and is moved across - otherwise the
        response reports minutes of thinking and a reply written in no time at
        all, which is the exact opposite of what happened."""
        if self.think_from is None:
            return
        self.write_from = (self.think_from if self.write_from is None
                           else min(self.write_from, self.think_from))
        self.write_to = (self.think_to if self.write_to is None
                         else max(self.write_to, self.think_to))
        self.think_from = self.think_to = None
        self.last = "write"

    def as_dict(self, think_tokens=None, write_tokens=None):
        """What goes on the turn: only the phases that actually happened.

        A key that isn't there means "this didn't occur", which every reader
        below draws as nothing at all - never as a zero.

        The two token counts are the response's output split between its two
        streams (main._split_output). They are stored rather than the rates
        derived from them, so that a rate can always be recomputed against the
        span a reader thinks is fair, and so a chat file never carries a
        derived number that has drifted out of step with the two it came
        from."""
        self.end()
        out = {"total": ms(self.began, self.ended)}
        latency = ms(self.began, self.first_at)
        if latency is not None:
            out["latency"] = latency
        for key, since, until, tokens in (
                ("think", self.think_from, self.think_to, think_tokens),
                ("write", self.write_from, self.write_to, write_tokens)):
            span = ms(since, until)
            if span is None:
                continue
            out[key] = span
            if isinstance(tokens, int) and tokens > 0:
                out[key + "_tok"] = tokens
        return out


def human(milliseconds):
    """A duration as something worth reading, in as few characters as it can
    honestly be said in.

    Three bands, because a number is only useful at the precision you can act
    on: under a second is whole milliseconds ("840ms"), under a minute is one
    decimal place ("12.1s"), and above that is minutes and seconds ("2m 04s") -
    a reply that took 124.7 seconds is a reply that took two minutes, and the
    tenth of a second is noise at that length.

    None in, "" out, so every caller can pass a phase that may not exist
    straight through without checking for it first. Zero goes the same way, and
    for the same reason: a span that measured zero milliseconds is below the
    resolution of the measurement, so "0ms" would claim a precision nobody has.
    It happens for real - a tool call arrives from the provider as one blob, so
    the write stream of a turn that only called a tool begins and ends in the
    same instant - and "0ms" under that bubble is worse than the blank it now
    gets."""
    if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
        return ""
    if milliseconds < 1000:
        return str(int(round(milliseconds))) + "ms"
    seconds = milliseconds / 1000.0
    if seconds < 60:
        return ("%.1f" % seconds) + "s"
    return "%dm %02ds" % (int(seconds // 60), int(round(seconds % 60)))


def rate(milliseconds, tokens):
    """Tokens per second within one stream, or None when either half is
    missing. Never across streams, and never over a span that includes the
    latency - see the header."""
    if not isinstance(tokens, int) or not isinstance(milliseconds, (int, float)):
        return None
    if tokens <= 0 or milliseconds <= 0:
        return None
    return tokens * 1000.0 / milliseconds


def part(timing, key):
    """One stream as its own line: "3.7s - 40 tok/s".

    The duration alone when the token count is missing, which is common on a
    turn that called a tool - the provider stops generating at the call and
    often never sends its final usage event, so there is nothing to divide.
    "" when that stream did not happen at all."""
    if not isinstance(timing, dict):
        return ""
    span = timing.get(key)
    shown = human(span)
    if not shown:
        return ""
    per_second = rate(span, timing.get(key + "_tok"))
    return shown + (" - " + ("%.0f" % per_second) + " tok/s" if per_second else "")


def waited(timing):
    """The latency, said the way the thinking line says thinking: "waited for
    1.2s". This is the request sitting in a queue or a prompt being processed -
    no token exists yet - which is why it is a line of its own above the
    message rather than anything folded into a rate."""
    shown = human((timing or {}).get("latency"))
    return "waited for " + shown if shown else ""


def thought(timing):
    """The thinking line once thinking is over: "thought for 8.2s - 76 tok/s".

    Falls back to the bare past tense rather than "" when there is no span to
    report. A thinking stream that arrived in one piece has a span of zero -
    the time it took to produce is in the latency, because nothing came back
    until it was finished - and "thought" is then the whole of what can
    honestly be said. Callers only ask when there is thinking to label, so
    there is nothing here for an empty answer to do."""
    shown = part(timing, "think")
    return "thought for " + shown if shown else "thought"


def summary(timing):
    """The short line under a message bubble - the WRITE stream and nothing
    else, because that bubble is the write stream. What the model waited for
    is on its own line above, and what it thought is on the thinking block."""
    return part(timing, "write")


def detail(timing):
    """The whole response in one line, for a hover or a terminal:
    "waited 1.2s - thought 8.2s (620 tok, 76 tok/s) - wrote 3.7s (148 tok,
    40 tok/s) - 13.1s in total".

    Every phase that happened, in the order they happen in, and nothing that
    didn't. "" when there is nothing measured at all."""
    if not isinstance(timing, dict):
        return ""
    bits = []
    if human(timing.get("latency")):
        bits.append("waited " + human(timing["latency"]))
    for key, label in (("think", "thought"), ("write", "wrote")):
        span = human(timing.get(key))
        if not span:
            continue
        tokens = timing.get(key + "_tok")
        per_second = rate(timing.get(key), tokens)
        extra = ""
        if isinstance(tokens, int):
            extra = " (" + str(tokens) + " tok"
            extra += ", " + ("%.0f" % per_second) + " tok/s)" if per_second else ")"
        bits.append(label + " " + span + extra)
    # A tool result carries a duration and nothing else - one key, its own
    # sentence, and none of the above applies to it.
    if human(timing.get("ms")):
        bits.append("ran in " + human(timing["ms"]))
    if human(timing.get("total")) and len(bits) > 1:
        bits.append(human(timing["total"]) + " in total")
    return " - ".join(bits)


# How long a silence has to be before a transcript says so out loud. Ten
# minutes: shorter than that is somebody reading the reply and typing the next
# message, which is not news. Longer than that and "what happened here" is a
# real question, and the answer - you went to lunch, the cron job ran overnight
# - is worth a line.
GAP = 10 * 60


def gap(before, after):
    """The words for a long silence between two messages ("3h 12m later"), or
    "" when the two are close enough together that nothing needs saying.

    Both arguments are epoch seconds off the stamps file, and either may be
    missing - a chat older than stamps has no times at all, and guessing one
    would put a confident, wrong silence into every old conversation."""
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return ""
    seconds = after - before
    if seconds < GAP:
        return ""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return str(days) + "d " + str(hours) + "h later"
    if hours:
        return str(hours) + "h " + str(minutes) + "m later"
    return str(minutes) + "m later"
