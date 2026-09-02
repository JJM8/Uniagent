#!/usr/bin/env python3
"""The Uniagent terminal front-end - `uniagentcli`.

The same agent the web UI drives, in a shell: one chat at a time, the reply
rendered as Markdown as it streams, tool calls shown as their own blocks rather
than as the raw JSON the model actually wrote.

The thing that shapes this file is that the prompt is ALWAYS live - you can
type at a working agent, and what you type decides when it lands:

    enter   send it into the turn that is already running: it is folded in at
            the next pass boundary, which in practice is as soon as the tool
            in flight comes back (main.run's `inject`)
    tab     hold it until the reply is completely finished, then send it as
            its own turn
    esc     stop the running turn (the same thing /stop does)
    /...    commands run immediately, running turn or not

That rules out input() and readline, which own the terminal for as long as they
are waiting. Instead stdin is put in raw mode and read a key at a time on the
MAIN thread, while turns run on a worker - so nothing ever blocks the keyboard.

Everything on screen is therefore drawn by one object, Console, because two
threads are writing to it. It keeps a "transient" block pinned at the bottom -
the reply's unfinished line, the queued-message notices, the input line, and a
status bar carrying the model and how full its context is - redrawn around
anything committed above it, and row by row, touching only what changed, so
typing doesn't flicker the prompt. Committed lines scroll away and are never
touched again. The cursor is left sitting at the caret after every redraw, so
typing looks normal while output streams past.

Bare /chats and /model open that block as a list to arrow through and filter
instead of answering with a wall of text, and loading a chat replays what is in
it rather than just announcing that it moved.

Terminals that aren't interactive (a pipe, `uniagentcli "one question"`) skip
all of that and use the plain line-at-a-time loop at the bottom.
"""

import os
import re
import sys
import json
import time
import _term as termios  # every terminal difference between platforms, in one file
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import main
import provider
import command_processor
import tool_processor
import tool_validation
import cli_md as md
import timing
from cli_md import (RESET, BOLD, NOBOLD, ITAL, TEXT, DIM, ACCENT, MUTE, RED,
                    BORDER, bg, fg, vlen, C)

HISTFILE = os.path.expanduser("~/.uniagent_history")
QUIT = ("/quit", "/exit", "/q", "quit", "exit")

# The accent chip in the banner: the theme's own "on accent" colour, so text on
# top of a filled green block stays readable in both palettes.
ON_ACCENT = "#0d1117" if C is md.DARK else "#ffffff"
CHIP = bg(C["accent"]) + fg(ON_ACCENT) + BOLD

PROMPT = "❯ "
PROMPT_LIT = ACCENT + BOLD + PROMPT + RESET + TEXT  # coloured, for drawing

# When a queued message actually goes. "tool" is enter - into the turn already
# running, at its next pass; "later" is tab - as its own turn once this one is
# completely done.
NOW, TOOL, LATER = "now", "tool", "later"


def user_line(text, cols=None):
    """The user's own message, echoed into the scrollback the moment it is
    really sent - which for a queued message is not when it was typed."""
    cols = cols or md.width()
    return md.wrap(text, cols, first=PROMPT_LIT, cont="  " + TEXT)


# ------------------------------------------------------------------ screen

class Console:
    """The screen. Committed lines above, a transient block pinned below.

    Every write goes through the lock, and every write leaves the terminal in
    the same state: transient block drawn, cursor parked at the caret. That
    invariant is what lets the streaming turn and the keyboard both draw
    whenever they like without agreeing on anything else.

    Each transient line is truncated to one screen row (see _fit) so a row is
    always a line: the arithmetic for walking the cursor back over the block
    stays a subtraction instead of a wrapping calculation, and a line that is
    too long to fit was going to be redrawn on the next keystroke anyway."""

    def __init__(self):
        self.lock = threading.RLock()
        self.tail = []      # the reply's unfinished line, redrawn per chunk
        self.status = []    # queued messages, safety questions, notices
        self.editor = None
        self.picker = None  # a list being chosen from, standing in for the input
        self.bar = None     # () -> the status bar's lines
        self.drawn = []     # exactly what is on screen now, row by row
        self.at = 0         # which of those rows the cursor is parked on
        self.on = False     # is the transient block drawn at all
        # The REAL stdout, captured before Sink takes sys.stdout's place. Every
        # write here has to bypass that redirection, or drawing the screen
        # would feed itself back in as more output to draw.
        self.out = sys.stdout

    # -- writing ----------------------------------------------------------

    def update(self, commit=(), tail=None, status=None):
        """Redraw the block, touching only the rows that actually changed.

        This is deliberately NOT "clear the block, print it again". Clearing
        first means every keystroke blanks the prompt and repaints it a moment
        later, and the eye catches that as the cursor and the line flickering on
        every character. Here the cursor walks to the top of the block and each
        row is either overwritten in one go (\\033[K then the text, so it is
        cleared and rewritten in the same paint) or stepped over untouched. When
        you type, exactly one row differs, so exactly one row is redrawn.

        Committing and replacing the unfinished line happen together for the
        same reason: as two separate redraws, the old trailing line gets drawn
        one more time before being replaced, which flickers on every chunk."""
        with self.lock:
            if tail is not None:
                self.tail = tail
            if status is not None:
                self.status = status
            new, row, col = self._compose()
            if self.drawn:
                if self.at:
                    self.out.write("\033[%dA" % self.at)
                self.out.write("\r")
            for line in commit:
                self.out.write("\033[K" + line + RESET + "\n")
            # Rows the old block had that the new one won't reach. Only when
            # there are any is a clear-to-end needed - and only then does the
            # last row have to be rewritten, since \033[J can only clear from a
            # known cursor position, which means the end of a row we just drew.
            stale = len(self.drawn) - len(commit) - len(new)
            if not new:
                if self.drawn:
                    self.out.write("\033[J")
                self.drawn, self.at = [], 0
                self.out.flush()
                return
            # Committed lines shift everything below them, so nothing under
            # them can be assumed still in place.
            fresh = bool(commit)
            for i, line in enumerate(new):
                if i:
                    self.out.write("\n")
                if fresh or i >= len(self.drawn) or line != self.drawn[i] \
                        or (stale > 0 and i == len(new) - 1):
                    self.out.write("\r\033[K" + line)
            if stale > 0:
                self.out.write("\033[J")
            up = (len(new) - 1) - row
            if up > 0:
                self.out.write("\033[%dA" % up)
            self.out.write("\r")
            if col > 0:
                self.out.write("\033[%dC" % col)
            self.drawn, self.at = new, row
            self.out.flush()

    def commit(self, lines):
        """Print lines that stay - they scroll off, they are never redrawn."""
        if lines:
            self.update(commit=lines)

    def set_tail(self, lines):
        self.update(tail=lines)

    def set_status(self, lines):
        self.update(status=lines)

    def refresh(self):
        self.update()

    def start(self):
        """Begin drawing the transient block - after the banner, once there is
        an editor to show."""
        with self.lock:
            self.on = True
            self.update()

    def stop(self):
        with self.lock:
            self.on = False
            self.update()

    def _compose(self):
        """The transient block and where the caret sits in it.

        Bottom of the screen upwards: the status bar, the hint, the input line
        (or the picker standing in for it), the queued-message notices, and at
        the top the reply's own unfinished line, which is really the tail of the
        scrollback rather than part of the furniture."""
        if not self.on:
            return [], 0, 0
        cols = self.width
        lines = list(self.tail) + list(self.status)
        row, col = max(0, len(lines) - 1), 0
        if self.picker:
            block, row_in, col = self.picker.render(cols)
            row = len(lines) + row_in
            lines += block
        elif self.editor:
            block, row_in, col = self.editor.render(cols)
            row = len(lines) + row_in
            lines += block
            lines += self.editor.hint()
        if self.bar:
            lines += self.bar()
        return [self._fit(l, cols) for l in lines], row, col

    @property
    def width(self):
        # One column short of the real width: a line that exactly fills the
        # terminal leaves the cursor in a state (wrap pending or not) that
        # differs between terminals, and the whole block's arithmetic hangs off
        # knowing which row the cursor is on.
        return max(20, md.width(cap=10 ** 6))

    @staticmethod
    def _fit(line, cols):
        if vlen(line) <= cols:
            return line
        # Truncating a coloured line by slicing plain text loses the colour;
        # keeping the escapes and cutting only visible characters is what this
        # walk does.
        out, seen = "", 0
        for part in re.split(r"(\x1b\[[0-9;]*m)", line):
            if part.startswith("\x1b"):
                out += part
                continue
            for ch in part:
                if seen >= cols - 1:
                    return out + "…"
                out += ch
                seen += 1
        return out


class Picker:
    """A list you arrow through, standing in for the input line while it's up.

    Filtering is not decoration: /model over a configured account lists several
    hundred models, and arrowing to one of them is not a usable way to find it.
    Typing narrows the list; the arrows then have something short to walk.

    Only a window of the list is drawn, and the window follows the selection, so
    a long list costs the same few rows as a short one."""

    ROWS = 10

    def __init__(self, title, items, choose, hint="↑↓ move · enter select · esc cancel"):
        self.title = title
        self.items = items          # [(key, label, detail)]
        self.choose = choose        # called with the chosen key
        self.hint = hint
        self.filter = ""
        self.at = 0                 # index into the FILTERED list
        self.top = 0                # first filtered row drawn

    def shown(self):
        if not self.filter:
            return self.items
        needles = self.filter.lower().split()
        return [it for it in self.items
                if all(n in (it[1] + " " + it[2]).lower() for n in needles)]

    def move(self, step):
        rows = self.shown()
        if rows:
            self.at = max(0, min(len(rows) - 1, self.at + step))

    def type(self, text):
        self.filter += text
        self.at = self.top = 0

    def backspace(self):
        self.filter = self.filter[:-1]
        self.at = self.top = 0

    def take(self):
        rows = self.shown()
        return rows[self.at][0] if rows else None

    def render(self, cols):
        rows = self.shown()
        # Keep the selection inside the window, scrolling it by the least that
        # puts the selection back in view.
        if self.at < self.top:
            self.top = self.at
        elif self.at >= self.top + self.ROWS:
            self.top = self.at - self.ROWS + 1
        self.top = max(0, min(self.top, max(0, len(rows) - self.ROWS)))

        out = ["  " + BOLD + ACCENT + self.title + RESET + DIM + "   " + self.hint + RESET]
        caret = len(out)
        out.append(PROMPT_LIT + self.filter)
        window = rows[self.top:self.top + self.ROWS]
        if not window:
            out.append("    " + DIM + ITAL + "nothing matches" + RESET)
        for i, (_, label, detail) in enumerate(window, start=self.top):
            on = i == self.at
            room = max(10, cols - 6 - min(28, len(detail) + 2))
            text = label if len(label) <= room else label[:room - 1] + "…"
            line = ("  " + ACCENT + "› " + BOLD if on else "    " + DIM)
            line += text + RESET
            if detail:
                line += DIM + "  " + detail + RESET
            out.append(line)
        more = len(rows) - (self.top + len(window))
        if more > 0:
            out.append("    " + DIM + ITAL + "… %d more" % more + RESET)
        return out, caret, len(PROMPT) + len(self.filter)


class Sink:
    """Stands in for sys.stdout while the CLI owns the screen.

    Several tools announce themselves by printing - terminal.py says what
    command it is about to run, write_file.py which file it is replacing. Those
    are worth seeing, but a bare print lands wherever the cursor happens to be,
    which is somewhere inside the pinned input block, and scrambles it. Routing
    them through Console puts them in the scrollback with everything else.

    Line-buffered because print() writes its text and its newline separately,
    and half a line is not something Console can place."""

    def __init__(self, console):
        self.console = console
        self.buf = ""
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.buf += text
            lines, _, self.buf = self.buf.rpartition("\n")
            if not lines:
                return len(text)
        self.console.commit(["      " + DIM + l.rstrip() + RESET
                             for l in lines.split("\n")])
        return len(text)

    def flush(self):
        self.console.out.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self.console.out.fileno()


# ------------------------------------------------------------------ editor

class Editor:
    """The input line: a buffer, a caret, and history on the up/down arrows.

    Deliberately small. It exists because readline cannot share the terminal
    with a reply being streamed into it - not because the line editing here is
    better. Column arithmetic counts characters, so a double-width glyph in the
    input will sit one column off until the next redraw."""

    def __init__(self, history):
        self.buf = ""
        self.pos = 0
        self.history = history
        self.at = len(history)
        self.stash = ""     # what was being typed before arrowing into history
        self.busy = False   # a turn is running, so the keys mean other things

    # -- editing ----------------------------------------------------------

    def insert(self, text):
        text = text.replace("\n", " ").replace("\r", " ")
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)

    def backspace(self):
        if self.pos:
            self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
            self.pos -= 1

    def delete(self):
        self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]

    def left(self):
        self.pos = max(0, self.pos - 1)

    def right(self):
        self.pos = min(len(self.buf), self.pos + 1)

    def home(self):
        self.pos = 0

    def end(self):
        self.pos = len(self.buf)

    def kill_start(self):
        self.buf, self.pos = self.buf[self.pos:], 0

    def kill_end(self):
        self.buf = self.buf[:self.pos]

    def kill_word(self):
        cut = self.buf[:self.pos].rstrip()
        cut = cut[:cut.rfind(" ") + 1] if " " in cut else ""
        self.buf, self.pos = cut + self.buf[self.pos:], len(cut)

    def word_left(self):
        cut = self.buf[:self.pos].rstrip()
        self.pos = cut.rfind(" ") + 1 if " " in cut else 0

    def word_right(self):
        nxt = self.buf.find(" ", self.pos)
        self.pos = len(self.buf) if nxt < 0 else nxt + 1

    def take(self):
        text, self.buf, self.pos = self.buf.strip(), "", 0
        self.at = len(self.history)
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            self.at = len(self.history)
        return text

    def clear(self):
        self.buf, self.pos = "", 0

    # -- history ----------------------------------------------------------

    def older(self):
        if self.at == len(self.history):
            self.stash = self.buf
        if self.at > 0:
            self.at -= 1
            self.buf = self.history[self.at]
            self.pos = len(self.buf)

    def newer(self):
        if self.at >= len(self.history):
            return
        self.at += 1
        self.buf = self.stash if self.at == len(self.history) else self.history[self.at]
        self.pos = len(self.buf)

    # -- completion -------------------------------------------------------

    def complete(self):
        """Slash commands only, and only on a line that is nothing else yet -
        tab means "send this when the reply ends" the rest of the time, and
        quietly completing a word instead would be a nasty surprise."""
        if not self.buf.startswith("/") or " " in self.buf:
            return False
        names = ["/" + c for c in command_processor.COMMANDS] + ["/exit"]
        hits = sorted(n for n in names if n.startswith(self.buf))
        if not hits:
            return False
        if len(hits) == 1:
            self.buf = hits[0] + " "
        else:
            shared = os.path.commonprefix(hits)
            if len(shared) > len(self.buf):
                self.buf = shared
        self.pos = len(self.buf)
        return True

    # -- drawing ----------------------------------------------------------

    def render(self, cols):
        """(lines, caret row within them, caret column)."""
        plain = PROMPT + self.buf
        room = max(8, cols - 1)
        rows = [plain[i:i + room] for i in range(0, len(plain), room)] or [""]
        row, col = divmod(len(PROMPT) + self.pos, room)
        if row >= len(rows):
            rows.append("")
        lit = [PROMPT_LIT + rows[0][len(PROMPT):]]
        lit += [TEXT + r for r in rows[1:]]
        return lit, row, col

    def hint(self):
        if not self.busy:
            return []
        keys = [("⏎", "send after this tool" if self.buf.strip() else "send"),
                ("⇥", "send when the reply ends"),
                ("esc", "stop")]
        return [DIM + "  " + " · ".join(MUTE + k + DIM + " " + w for k, w in keys) + RESET]


# ------------------------------------------------------------------ streaming

# What the start of a tool call looks like: the name(args) text main._stream
# synthesises for a provider-native call, which is the only shape there is now.
# Only ever tested against a whole line, and only outside a ``` fence, so an
# example in a code block is never mistaken for one.
_CALL = re.compile(r'^\s*[A-Za-z_]\w*\(\s*\{')
# A native call arrives as "name(" before its arguments do, and that first
# fragment is a whole line on its own for one frame. Without this it flashes up
# as prose and is replaced a moment later, which reads as a glitch.
_HALF_CALL = re.compile(r"^\s*[A-Za-z_]\w*\(\s*$")
_NAME = re.compile(r'^\s*([A-Za-z_]\w*)\(')


def _maybe_call(text):
    s = text.lstrip()
    return bool(s) and (bool(_CALL.match(s)) or bool(_HALF_CALL.match(s)))


def _call_name(text):
    m = _NAME.search(text.lstrip())
    return next((g for g in m.groups() if g), None) if m else None


class Stream:
    """One turn's output: on_text/on_tool_call/on_tool_result land here.

    Complete lines are committed. The unfinished trailing line is transient, so
    `**bold` becomes bold the moment its closing `**` arrives instead of sitting
    there as asterisks.

    Tool calls arrive through on_text like any other text (main._stream's
    show_call), so a call would otherwise stream past as a wall of JSON before
    the block for it could be drawn. Once a line starts to look like a call it
    is HELD rather than committed, and dropped when on_tool_call confirms it.
    If no call ever materialises - prose that merely opened with a brace - the
    held text is flushed at the end of the turn, so nothing is lost."""

    def __init__(self, console):
        self.console = console
        self.md = md.Renderer()
        self.pending = ""    # the trailing line, still being written
        self.held = ""       # complete lines withheld as a suspected tool call
        self.holding = False
        self.wrote = False
        # The model's own thinking as it arrives, and where the last committed
        # line of it ended. Kept here rather than pushed straight to the screen
        # because it arrives in fragments that are not lines: a reasoning
        # stream sends " the", " user", " wants" and committing each of those
        # would put one word per row.
        self.thinking = ""
        self.thought = 0     # characters of it already on screen
        self.thinking_from = None   # when the first reasoning fragment landed
        self.thought_spent = None   # that stream's numbers, known when it ended
        self.spent = None    # the last response's timing, printed by flush()
        # The request this response is answering, and whether the wait for it
        # has been reported yet. Measured here rather than taken off the
        # server's own figure because the terminal IS that process - the two
        # readings are the same clock - and this one is available at the moment
        # the wait ends, where the server's does not arrive until the whole
        # response is over.
        self.asked = None
        self.said_wait = False
        self.console.set_tail([DIM + "…" + RESET])

    def requested(self):
        """A request has gone out. Everything after this until the first token
        is latency - the model queued, or a long prompt being processed - and
        on a local server it is regularly the longest part of a turn. Saying so
        is the difference between a terminal that is working and one that has
        hung."""
        self.asked = timing.now()
        self.said_wait = False
        self.console.set_tail([DIM + "  waiting for the model…" + RESET])

    def _token(self):
        """The first token of this response, on whichever stream. Reports the
        wait, once."""
        if self.said_wait or self.asked is None:
            return
        self.said_wait = True
        words = timing.waited({"latency": timing.ms(self.asked)})
        if words:
            self.console.update(commit=[MUTE + "  " + DIM + words + RESET], tail=[])

    def thought(self, spent):
        """The thinking stream's finished numbers, handed over at the moment it
        ended. Held for _end_thinking below, which is about to run: the same
        first token of the reply produces both, this one first."""
        self.thought_spent = spent

    def _end_thinking(self):
        """Close the thinking block off, because the reply has started.

        Here, at the first word of the answer, rather than when the response
        finishes - "thinking" has to stop saying thinking the moment it stops
        being true, or the block sits there in the present tense for the whole
        length of the reply.

        The line carries the rate as well as the duration, because both are
        known by now: the server counts the thinking as that stream closes
        rather than waiting for the provider's final usage event (see
        main._stream's thinking_done). Only if that never arrived does this
        fall back to the terminal's own reading of the clock, which is honest
        about the duration and silent about the speed."""
        if not self.thinking:
            return
        rest = self.thinking[self.thought:].rstrip()
        done = [MUTE + "  │ " + DIM + ITAL + rest + RESET] if rest else []
        spent = self.thought_spent or {"think": timing.ms(self.thinking_from)}
        done.append(MUTE + "  └ " + DIM + timing.thought(spent) + RESET)
        self.console.update(commit=done, tail=[])
        self.thinking, self.thought, self.thinking_from = "", 0, None
        self.thought_spent = None

    def __call__(self, chunk):
        if not chunk:
            return
        self._token()
        self._end_thinking()
        self.pending += chunk
        done = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if not self.holding and not self.md.fence and _CALL.match(line):
                self.holding = True
            if self.holding:
                self.held += line + "\n"
            else:
                done += self.md.feed(line)
        if self.holding or (not self.md.fence and _maybe_call(self.pending)):
            name = _call_name(self.held + self.pending)
            tail = [MUTE + "▸ " + DIM + (name or "tool call") + " …" + RESET]
        else:
            tail = self.md.partial(self.pending)
        self.wrote = self.wrote or bool(done)
        self.console.update(commit=done, tail=tail)

    def reasoning(self, text):
        """A fragment of the model's thinking, shown as it is written.

        Committed line by line and dimmed, above the reply, rather than held
        back and printed as a block at the end. The whole value of watching a
        thinking model is watching it: a local reasoning model can spend forty
        seconds here, and forty seconds of a blank terminal is indistinguishable
        from a hang. The unfinished trailing fragment sits in the tail, so a
        sentence appears as it is written rather than a word per row.

        The reply itself is untouched by any of this - thinking arrives on its
        own callback (main.run's on_reasoning) and never through on_text, so
        there is nothing here to filter back out of the answer."""
        if not text:
            return
        self._token()
        if self.thinking_from is None:
            self.thinking_from = timing.now()
        self.thinking += text
        done = []
        while "\n" in self.thinking[self.thought:]:
            cut = self.thinking.index("\n", self.thought)
            done.append(MUTE + "  │ " + DIM + ITAL
                        + self.thinking[self.thought:cut].rstrip() + RESET)
            self.thought = cut + 1
        tail = self.thinking[self.thought:]
        self.console.update(
            commit=done,
            tail=[MUTE + "  │ " + DIM + ITAL + tail[-md.width() + 6:] + RESET]
            if tail.strip() else [DIM + "  thinking…" + RESET])

    def measured(self, spent):
        """This response's numbers, once it is complete.

        One dim line under the reply - the same total and rate the web UI puts
        under a bubble, plus the phase breakdown, which a terminal has room for
        and no hover to hide it behind.

        Held rather than printed, because at this point the response's own last
        lines are not on screen yet: run() announces the timing the moment the
        stream closes and only then decides whether what it read was a tool
        call or the answer. Whichever it turns out to be prints this first -
        tool_call() before the call block, flush() at the end of the turn - so
        every message of a multi-call turn wears its own numbers instead of the
        turn wearing only the last one's."""
        self.spent = spent
        # A backstop for the response that thought and then said nothing at
        # all. Normally the first word of the reply closes the block (see
        # _end_thinking), and on a turn that ends in a tool call that word is
        # the call being typed - but a response that produced neither would
        # otherwise leave the block hanging open.
        self._end_thinking()

    def _took(self):
        """The held timing as lines, and forget it. [] when there is none."""
        if not self.spent:
            return []
        line = timing.summary(self.spent)
        detail = timing.detail(self.spent)
        if detail:
            line += "   (" + detail + ")"
        self.spent = None
        return [MUTE + "  " + line + RESET] if line.strip() else []

    def tool_call(self, shown, name=None, id=None):
        # `name` and `id` are which tool this is and the id its result will
        # come back under. Accepted and unused, exactly as tool_result below
        # accepts `name`: the terminal prints a call and then its result
        # directly underneath, in the order they happen, so it has nothing to
        # pair up. They are taken so a caller that knows them can always say
        # so - the web UI needs both to draw a call before its result exists.
        raw = (self.held + self.pending).strip() or shown
        self.held, self.pending, self.holding = "", "", False
        self.md = md.Renderer()  # a fence cannot span a tool call
        self.wrote = True
        self.console.update(commit=self._took() + [""] + _call_block(raw), tail=[])

    def tool_result(self, result, name=None, spent=None, id=None):
        # `name` is which call this answered and `id` is that call's id. Both
        # accepted and unused: the terminal prints results in the order they
        # happen directly under the call that made them, so there is nothing
        # here for it to disambiguate - they are taken so that a caller which
        # knows them can always say so.
        #
        # `spent` is how long the call took ({"ms": n}), which is worth a line
        # for the same reason it is worth a label in the web UI: a turn that
        # felt slow is as likely to have been a 40-second fetch as a slow
        # model, and the result block is where you go to find out.
        took = timing.human((spent or {}).get("ms"))
        self._commit(_result_block(result)
                     + ([MUTE + "  └ " + DIM + took + RESET] if took else [])
                     + [""])

    def safety(self, safe, reason, checked=True):
        # Only a flagged call is worth a line here. `checked` False is the gate
        # being off, which the terminal already knows it turned off and doesn't
        # need told once per tool call - the web UI shows that as a row on the
        # result, where it answers a question you can't otherwise ask.
        if checked and not safe:
            self._commit([RED + "  ⚠ " + reason.strip() + RESET])

    def flush(self, quiet=False):
        """`quiet` drops the "(no reply)" placeholder. It is for the failed
        turn: an error line is about to be printed saying exactly why nothing
        arrived, and "(no reply)" directly above it is the same news, told
        worse."""
        rest = (self.held + self.pending).rstrip("\n")
        self.held = self.pending = ""
        self.holding = False
        # Held text that still looks like a call is a call that never finished -
        # a turn stopped part-way through writing one. There is nothing useful
        # in half a JSON object, and dumping it raw is exactly what holding it
        # back was for.
        if _maybe_call(rest):
            rest = ""
        done = []
        for line in rest.split("\n") if rest else []:
            done += self.md.feed(line)
        done += self.md.flush()
        if done:
            self.wrote = True
        elif not self.wrote and not quiet:
            done = [DIM + "(no reply)" + RESET]
        # What the reply cost, under it. The full breakdown rather than the web
        # UI's summary-plus-hover: a terminal has the width for it and no hover
        # to put the rest behind, and this is the one line that answers "why
        # did that take so long" without another request.
        done = done + self._took()
        self.console.update(commit=done, tail=[])

    def _commit(self, lines):
        if lines:
            self.wrote = True
            self.console.commit(lines)


# ------------------------------------------------------------------ blocks

def _parse_call(raw):
    """(name, args) out of a stored call's text, or (None, {}) if it won't
    parse - in which case the raw text is shown as it was, which beats
    pretending we understood it. One shape to read: the "name({...})" a native
    call is rendered as (main._parse_call's shown_call)."""
    raw = raw.strip()
    m = re.match(r"^([A-Za-z_]\w*)\(\s*(\{.*\})\s*\)$", raw, re.S)
    if m:
        try:
            return m.group(1), json.loads(m.group(2))
        except json.JSONDecodeError:
            return m.group(1), {}
    return None, {}


def _one_line(value, room):
    """An argument as a single line: tools are usually handed whole files and
    multi-line scripts, and printing one verbatim buries everything else."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    value = " ⏎ ".join(value.strip().split("\n"))
    return value if len(value) <= room else value[:max(4, room - 1)] + "…"


def _call_block(raw):
    cols = md.width()
    name, args = _parse_call(raw)
    if name is None:
        return [MUTE + "▸ " + DIM + l + RESET for l in raw.split("\n")[:6]]
    lines = ["  " + ACCENT + "▸ " + BOLD + name + NOBOLD + RESET]
    pad = max((len(k) for k in args), default=0)
    for key, value in args.items():
        lines.append("      " + DIM + key.ljust(pad) + "  " + RESET
                     + TEXT + _one_line(value, cols - pad - 8) + RESET)
    return lines


def _result_block(result, keep=8):
    """A tool's output, dimmed and clipped - enough to see that it worked, not
    so much that a directory listing pushes the conversation off screen."""
    text = (result or "").rstrip()
    if not text:
        return ["      " + DIM + ITAL + "(no output)" + RESET]
    colour = RED if re.match(r"^\s*(error|denied|traceback)\b", text, re.I) else DIM
    cols = md.width()
    lines = text.split("\n")
    shown = [l[:cols - 10] + ("…" if len(l) > cols - 10 else "") for l in lines[:keep]]
    block = ["      " + MUTE + "│ " + colour + l + RESET for l in shown]
    if len(lines) > keep:
        block.append("      " + MUTE + "│ " + DIM + ITAL
                     + "… %d more lines" % (len(lines) - keep) + RESET)
    return block


# ------------------------------------------------------------------ the bar

def _short(n):
    """12400 -> '12.4k'. The bar has one line and the exact digit count of a
    context window is never the thing being read off it."""
    if n is None:
        return "?"
    if n < 1000:
        return str(n)
    if n < 100000:
        return ("%.1fk" % (n / 1000)).replace(".0k", "k")
    return "%dk" % round(n / 1000)


def _meter(used, cap, cells=12):
    if not cap or not used:
        return MUTE + "░" * cells + RESET
    share = min(1.0, used / cap)
    fill = max(1, round(cells * share)) if share else 0
    colour = RED if share >= 0.9 else ACCENT
    return colour + "█" * fill + MUTE + "░" * (cells - fill) + RESET


def status_bar(info):
    """The always-on bottom line: which model this chat is on, and how full its
    context is - the two numbers the web UI keeps on screen permanently, and
    the two you want when deciding whether to /compact or switch."""
    model = (info.get("provider") or "?") + "/" + (info.get("model") or "?")
    used, cap = info.get("input"), info.get("max")
    share = ("%d%%" % round(100 * used / cap)) if used and cap else ""
    count = ("~" if info.get("exact") is False else "") + _short(used) + "/" + _short(cap)
    parts = [MUTE + "▌" + RESET + DIM + model + RESET, _meter(used, cap),
             DIM + count + RESET]
    if share:
        parts.append(DIM + share + RESET)
    if info.get("settled") is False:
        parts.append(DIM + ITAL + "counting…" + RESET)
    return [" " + "  ".join(parts)]


# ------------------------------------------------------------------ lists

# The first user turn in a history.json, matched against the HEAD of the file
# rather than a parse of all of it. There can be hundreds of chats and some of
# them are megabytes; json.loads on every one of them to read a label off the
# top is the difference between the list opening instantly and it hanging.
_FIRST_SAID = re.compile(r'"role":\s*"user",\s*"content":\s*"((?:[^"\\]|\\.)*)"')


def _chat_label(path):
    """What to call a chat in the picker: its own /name if it has one, else the
    first thing actually typed into it, which is what the web's sidebar shows."""
    try:
        name = json.loads((path.parent / main.SETTINGS_FILE)
                          .read_text(encoding="utf-8")).get("name")
        if name:
            return name
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except OSError:
        return ""
    for m in _FIRST_SAID.finditer(head):
        try:
            said = json.loads('"' + m.group(1) + '"')
        except json.JSONDecodeError:
            continue
        if said and not said.startswith(("Subagent ", "Tool result: ", main.MID_TURN,
                                         main.WORKSPACE_NOTE)):
            return " ".join(said.split())[:90]
    return ""


def _ago(when):
    gap = max(0, time.time() - when)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if gap >= size:
            return "%d%s ago" % (gap // size, unit)
    return "just now"


def chat_items():
    files = list(main.CHATS.glob("chat-*/" + main.HISTORY_FILE)) \
        + list((main.CHATS / "cron").glob("*/*/" + main.HISTORY_FILE))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    here = main.current.id if main.current else None
    items = []
    for path in files:
        cid = command_processor._chat_id(path)
        label = _chat_label(path) or cid
        detail = _ago(path.stat().st_mtime) + ("  (current)" if cid == here else "")
        items.append((cid, label, detail))
    return items


def model_items():
    """Every model of every usable provider, the current one first so enter on
    an unfiltered list is a no-op rather than a surprise switch."""
    usable = provider.available()
    known = provider.known_models(usable)
    try:
        now = "%s %s" % main.current.models()[:2]
    except Exception:
        now = None
    items = []
    for name in usable:
        for model in known.get(name, []):
            key = name + " " + model
            items.append((key, name + " / " + model, "current" if key == now else ""))
    items.sort(key=lambda it: it[0] != now)
    return items


# ------------------------------------------------------------------ replay

def transcript(chat, keep=30):
    """A loaded chat's past turns, drawn the way they were drawn live.

    Opening a chat and being shown nothing but its name is the wrong answer -
    the whole point of going back to one is what is in it. The same renderers
    do the work as during a turn, so a reply reads identically whether you
    watched it arrive or came back to it a day later.

    Only the last `keep` turns: a long chat is tens of thousands of lines, and
    replaying all of it to reach the end is slower and less useful than /history,
    which is still there for the raw thing."""
    try:
        turns = json.loads(chat.history) if chat.history else []
    except json.JSONDecodeError:
        return [DIM + ITAL + "  (this chat's history isn't readable)" + RESET]
    if not isinstance(turns, list) or not turns:
        return [DIM + ITAL + "  (nothing in this chat yet)" + RESET]

    out = []
    skipped = max(0, len(turns) - keep)
    if skipped:
        out += [DIM + ITAL + "  … %d earlier turns not shown - /history for all of it"
                % skipped + RESET, ""]
    for t in turns[skipped:]:
        if not isinstance(t, dict):
            continue
        role, content = t.get("role"), t.get("content") or ""
        if role == "user":
            # A mid-turn message was stored with the label main.run puts on it
            # for the model's benefit; what the user actually typed is the rest.
            said = content[len(main.MID_TURN):] if content.startswith(main.MID_TURN) else content
            if said.startswith("Tool result: "):
                continue  # an older chat's tool result, kept as a user turn
            if said.startswith(main.WORKSPACE_NOTE):
                # The chat being moved to another workspace: a user turn so the
                # model reads it (see main.note_turn), but not a line anybody
                # typed - drawn as the aside it is.
                out += [DIM + ITAL + "  " + " ".join(said.split()) + RESET, ""]
                continue
            out += user_line(said) + [""]
        elif role == "tool":
            took = timing.human((t.get("timing") or {}).get("ms"))
            out += _result_block(content)
            if took:
                out += [MUTE + "  └ " + DIM + took + RESET]
            out += [""]
        elif role == "assistant":
            # What it thought on the way, before what it said - one folded line
            # rather than the reasoning itself. The full text is on the turn
            # (reasoning_content) and the web UI opens it; a redrawn terminal
            # transcript is for finding your place, and a page of somebody
            # else's working is the fastest way to lose it.
            waited = timing.waited(t.get("timing"))
            if waited:
                out += [MUTE + "  " + DIM + waited + RESET]
            if t.get("reasoning_content"):
                out += [MUTE + "  └ " + DIM + ITAL
                        + timing.thought(t.get("timing")) + RESET]
            if content.strip():
                out += md.render(content)
            for call in t.get("tool_calls") or []:
                fn = call.get("function", {})
                out += [""] + _call_block(t.get("raw_call")
                                          or fn.get("name", "") + "(" + fn.get("arguments", "") + ")")
            spent = timing.summary(t.get("timing"))
            if spent:
                out += [MUTE + "  " + spent + RESET]
            out += [""]
    return out


def banner(console):
    cols = md.width()
    try:
        provider, model, _ = main.current.models()
    except Exception:
        provider, model = "?", "?"
    console.commit([
        "",
        " " + CHIP + " Uniagent " + RESET + DIM + "  cli" + RESET,
        " " + DIM + "model  " + RESET + TEXT + provider + DIM + " / " + RESET + TEXT + model,
        " " + DIM + "chat   " + RESET + TEXT + (main.current.id if main.current else "-"),
        " " + DIM + "type while it works · /help for commands · /exit to quit" + RESET,
        BORDER + "─" * cols + RESET,
        "",
    ])


# ------------------------------------------------------------------ keyboard

_CSI = re.compile(rb"\x1b(\[[0-9;?]*[A-Za-z~]|O[A-Za-z])")

# The control bytes a raw-mode terminal delivers, named. Everything else that
# isn't an escape sequence is text.
_CTRL = {b"\r": "enter", b"\n": "enter", b"\t": "tab", b"\x7f": "backspace",
         b"\x08": "backspace", b"\x03": "ctrl-c", b"\x04": "ctrl-d",
         b"\x01": "home", b"\x05": "end", b"\x0b": "kill-end", b"\x15": "kill-start",
         b"\x17": "kill-word", b"\x02": "left", b"\x06": "right", b"\x0c": "clear"}
_SEQ = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
        b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left",
        b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b[1~": "home", b"\x1b[4~": "end",
        b"\x1b[3~": "delete", b"\x1b[Z": "shift-tab",
        b"\x1b[1;5C": "word-right", b"\x1b[1;5D": "word-left"}

PASTE_ON, PASTE_OFF = b"\x1b[200~", b"\x1b[201~"


class Keys:
    """Bytes from a raw terminal, turned into ("key", name) / ("text", str).

    A bare escape and the start of an arrow key are the same byte, so a lone
    \\x1b at the end of the buffer is not decided here - _flushable() says it is
    still ambiguous and the reader waits a few milliseconds for the rest of the
    sequence before calling it an escape.

    Bracketed paste is turned on by the caller, which matters: without it a
    pasted paragraph arrives as text with carriage returns in it, and every one
    of those would submit the line."""

    def __init__(self):
        self.buf = b""
        self.pasting = False

    def feed(self, data, final=False):
        self.buf += data
        out = []
        while self.buf:
            if self.pasting:
                end = self.buf.find(PASTE_OFF)
                if end < 0:
                    if len(self.buf) > len(PASTE_OFF):
                        keep = len(PASTE_OFF) - 1
                        out.append(("text", self._text(self.buf[:-keep])))
                        self.buf = self.buf[-keep:]
                    break
                out.append(("text", self._text(self.buf[:end])))
                self.buf = self.buf[end + len(PASTE_OFF):]
                self.pasting = False
                continue
            if self.buf.startswith(PASTE_ON):
                self.buf = self.buf[len(PASTE_ON):]
                self.pasting = True
                continue
            head = self.buf[:1]
            if head in _CTRL:
                out.append(("key", _CTRL[head]))
                self.buf = self.buf[1:]
                continue
            if head == b"\x1b":
                if PASTE_ON.startswith(self.buf[:len(PASTE_ON)]) and not final:
                    break
                m = _CSI.match(self.buf)
                if m:
                    out.append(("key", _SEQ.get(m.group(0), "unknown")))
                    self.buf = self.buf[m.end():]
                    continue
                if len(self.buf) > 1 or final:
                    out.append(("key", "esc"))
                    self.buf = self.buf[1:]
                    continue
                break  # a lone escape, still ambiguous
            # Plain text: decode as much as decodes cleanly, keep any partial
            # UTF-8 sequence for the next read.
            end = len(self.buf)
            for stop in (b"\x1b", b"\r", b"\n", b"\t"):
                at = self.buf.find(stop, 1)
                if at >= 0:
                    end = min(end, at)
            chunk, self.buf = self.buf[:end], self.buf[end:]
            text, rest = self._decode(chunk)
            self.buf = rest + self.buf
            if text:
                out.append(("text", text))
            elif not text and rest:
                break
        return out

    def ambiguous(self):
        return self.buf == b"\x1b"

    @staticmethod
    def _decode(chunk):
        for cut in range(0, min(4, len(chunk))):
            head = chunk[:len(chunk) - cut] if cut else chunk
            try:
                return head.decode("utf-8"), chunk[len(head):]
            except UnicodeDecodeError:
                continue
        return "", chunk

    @staticmethod
    def _text(raw):
        return raw.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")


# ------------------------------------------------------------------ the app

class App:
    """Keyboard on the main thread, turns on a worker, one Console between."""

    def __init__(self):
        self.console = Console()
        self.editor = Editor(_load_history())
        self.console.editor = self.editor
        self.cv = threading.Condition()
        self.pending = []     # texts to run as their own turn, in order
        self.inject = []      # texts to fold into the turn already running
        self.busy = False
        self.alive = True
        self.stopping = False
        self.chat_id = None   # the chat the running turn belongs to
        self.ask = None       # a safety question waiting on a keystroke
        self.picker = None    # a list being chosen from
        self.speak = threading.Lock()  # only one turn may draw at a time
        # What the status bar draws. Seeded from what the chat already has on
        # disk so the bar is right from the first frame, then kept up to date
        # by the refresher thread - which does the expensive part (re-reading
        # every injected file, running the tokenizer) off the drawing path.
        self.info = self._seed_info()
        self.recount = threading.Event()
        self.console.bar = self.bar

    @staticmethod
    def _seed_info():
        try:
            prov, model, _ = main.current.models()
            return {"provider": prov, "model": model,
                    "input": main.current.context_input,
                    "max": main.current.context_max or provider.context_window(prov, model)}
        except Exception:
            return {}

    def bar(self):
        return status_bar(self.info)

    # -- the token count --------------------------------------------------

    def usage_loop(self):
        """Recount in the background, never on the keystroke path. Woken by
        anything that can change the number - a finished turn, a tool result, a
        model or chat switch - and on a slow heartbeat besides, which is what
        picks up a network tokenizer that had not settled yet."""
        while self.alive:
            try:
                chat = main.current
                prov, model, _ = chat.models()
                injected = main.injection_breakdown(prov, model, chat.pinned,
                                                    profile=chat.profile)
                self.info = dict(main.context_usage(chat, prov, model, injected),
                                 provider=prov, model=model)
                self.console.refresh()
            except Exception:
                pass  # a bar that is briefly stale is not worth a crash
            self.recount.wait(timeout=20)
            self.recount.clear()

    def recount_soon(self):
        self.recount.set()

    # -- pickers ----------------------------------------------------------

    def open_picker(self, picker):
        self.picker = self.console.picker = picker
        self.console.refresh()

    def close_picker(self):
        self.picker = self.console.picker = None
        self.console.refresh()

    def pick(self, title, build, choose, empty):
        """Open a picker, once its list exists.

        Built on a thread, never inline: /chats stats and reads every chat on
        disk, and /model's catalogue can go and ask each provider what it
        offers. Either would freeze the keyboard for as long as it took, which
        is the one thing this UI is not allowed to do."""
        def work():
            try:
                items = build()
            except Exception as e:
                return self.console.commit([RED + "  " + str(e) + RESET, ""])
            if not items:
                return self.console.commit([DIM + "  " + empty + RESET, ""])
            self.open_picker(Picker(title, items, choose))
        threading.Thread(target=work, daemon=True).start()

    def pick_chat(self):
        self.pick("open a chat", chat_items,
                  lambda cid: self.run_command("/load " + cid), "no saved chats.")

    def pick_model(self):
        self.pick("switch this chat's model", model_items,
                  lambda key: self.run_command("/model " + key), "no usable providers.")

    def picker_key(self, name):
        picker = self.picker
        if name in ("esc", "ctrl-c"):
            self.close_picker()
            return
        if name == "up":
            picker.move(-1)
        elif name == "down":
            picker.move(1)
        elif name == "backspace":
            picker.backspace()
        elif name in ("enter", "tab"):
            chosen = picker.take()
            self.close_picker()
            if chosen is not None:
                picker.choose(chosen)
            return
        self.console.refresh()

    # -- queueing ---------------------------------------------------------

    def submit(self, text, when):
        """A line the user just sent. `when` decides where it goes; a command
        never queues at all, since /stop is worthless if it has to wait for the
        thing it is stopping."""
        if not text:
            return
        if text.lower() in QUIT:
            self.quit()
            return
        if text.strip().lower() == main.CONTINUE:
            # Not a command, despite the slash: it starts a turn rather than
            # answering (see main.CONTINUE), so it joins the queue the way a
            # message does - as None, which main.turn reads as "carry on from
            # where the last one stopped".
            # Asked before the rewind, which takes the marker carrying it away:
            # the model this picks up is the one the chat says now, which after
            # a failure is very often not the one that failed. See
            # main.model_switch().
            switch = main.model_switch(main.current)
            why = main.continue_from(main.current)
            if why is not None:
                self.console.commit(md.render(why) + [""])
                return
            note = main.switch_note(switch)
            if note:
                self.console.commit(md.render(note) + [""])
            with self.cv:
                self.pending.append(None)
                self.cv.notify_all()
            self.show_status()
            return
        if text.startswith("/"):
            name, _, arg = text[1:].partition(" ")
            # Bare /chats and /model answer with a wall of text the web UI shows
            # as a list you click. Here they open the same list as something to
            # arrow through; with an argument they stay the plain commands, so
            # nothing scripted or habitual breaks.
            if not arg.strip():
                if name.lower() == "chats":
                    return self.pick_chat()
                if name.lower() in ("model", "models"):
                    return self.pick_model()
            self.run_command(text)
            return
        with self.cv:
            if when == TOOL and self.busy:
                self.inject.append(text)
            elif when == LATER and self.busy:
                self.pending.append(text)
            else:
                self.pending.append(text)
            self.cv.notify_all()
        self.show_status()

    def drain_inject(self):
        """main.run asks this between passes. Everything waiting goes at once -
        holding one back for a tool call that may never come would strand it."""
        with self.cv:
            if not self.inject:
                return None
            texts, self.inject = self.inject, []
        for text in texts:
            self.console.commit(user_line(text))
        self.show_status()
        return "\n\n".join(texts)

    def stop_turn(self):
        if self.busy and self.chat_id:
            main.request_stop(self.chat_id)
            self.stopping = True
            self.show_status()

    def quit(self):
        with self.cv:
            self.alive = False
            self.cv.notify_all()
        self.recount.set()  # so the refresher stops waiting and sees `alive`
        if self.busy and self.chat_id:
            main.request_stop(self.chat_id)

    # -- status -----------------------------------------------------------

    def show_status(self):
        lines = []
        if self.ask:
            lines.append(RED + "  ⚠ " + RESET + TEXT + self.ask["question"]
                         + DIM + "  [y/N]" + RESET)
        with self.cv:
            queued = ([(TOOL, t) for t in self.inject]
                      + ([(LATER, t) for t in self.pending] if self.busy else []))
        for when, text in queued:
            label = "after this tool  " if when == TOOL else "when the reply ends  "
            # None is a queued /continue - it has no text of its own, so it is
            # named by what it does.
            lines.append(MUTE + "  ⧗ " + DIM + label + RESET + TEXT
                         + (text if text is not None else "continue"))
        if self.stopping:
            lines.append(DIM + "  stopping…" + RESET)
        self.console.set_status(lines)

    # -- running turns ----------------------------------------------------

    def worker(self):
        while True:
            with self.cv:
                while self.alive and not self.pending:
                    self.cv.wait()
                if not self.alive:
                    return
                text = self.pending.pop(0)
                self.busy = True
                self.stopping = False
                self.chat_id = main.current.id
            self.editor.busy = True
            self.show_status()
            # A continue has no message to echo back - nobody said anything.
            if text is not None:
                self.console.commit(user_line(text))
            self.say(main.current, text)
            with self.cv:
                # Anything queued for the turn that just ended but never landed
                # in it - the reply finished before another tool call - becomes
                # the next turn instead, ahead of whatever tab queued after it.
                self.pending[:0] = self.inject
                self.inject = []
                self.busy = bool(self.pending)
                self.stopping = False
            self.editor.busy = self.busy
            self.show_status()
            self.recount_soon()

    def say(self, chat, text):
        with self.speak:
            stream = Stream(self.console)
            try:
                main.turn(chat, text, on_text=stream, approve=self.approve,
                          on_tool_call=stream.tool_call,
                          on_tool_result=stream.tool_result,
                          on_safety=stream.safety, inject=self.drain_inject,
                          on_reasoning=stream.reasoning, on_timing=stream.measured,
                          on_request=stream.requested, on_thought=stream.thought)
                stream.flush()
            except Exception as e:
                stream.flush(quiet=True)
                self.console.commit([RED + "  " + type(e).__name__ + ": " + str(e) + RESET])
            self.console.commit([""])

    def approve(self, question):
        """The safety gate, answered by a keystroke rather than by input() -
        which cannot be used here, since the keyboard belongs to the main
        thread's raw-mode reader."""
        done = threading.Event()
        self.ask = {"question": question.strip(), "answer": False, "done": done}
        self.show_status()
        done.wait()
        answer = self.ask["answer"]
        self.ask = None
        self.show_status()
        self.console.commit([(ACCENT if answer else RED) + "  ⚠ "
                             + ("approved" if answer else "denied") + RESET])
        return answer

    def report(self, note, origin):
        """A subagent's report coming back into the chat that asked for it. It
        waits for the speak lock rather than drawing over a turn in progress."""
        if not self.alive:
            return
        with self.speak:
            self.console.commit(["", MUTE + "▸ " + DIM + "report from " + origin + RESET])
            stream = Stream(self.console)
            try:
                main.turn(main.chat(origin), note, on_text=stream, approve=self.approve,
                          on_tool_call=stream.tool_call, on_tool_result=stream.tool_result,
                          on_safety=stream.safety,
                          on_reasoning=stream.reasoning, on_timing=stream.measured,
                          on_request=stream.requested, on_thought=stream.thought)
                stream.flush()
            except Exception as e:
                stream.flush(quiet=True)
                self.console.commit([RED + "  report failed: " + str(e) + RESET])

    def run_command(self, text):
        """Commands go on their own thread, never inline: /compact and /model
        both ask a model and take seconds, and running one here would freeze the
        keyboard for that long - in a UI whose whole premise is that the
        keyboard never blocks. It also keeps /stop instant, which is the point
        of commands not queueing behind a turn in the first place."""
        threading.Thread(target=self.command, args=(text,), daemon=True).start()

    def command(self, text):
        result = command_processor.process(text)
        if result is None:
            return
        reply, goto = result
        if reply:
            self.console.commit(md.render(reply) + [""])
        if goto:
            main.load(main.chat_md(goto))
            cols = md.width()
            self.console.commit([BORDER + "─" * cols + RESET,
                                 " " + DIM + "chat   " + RESET + TEXT + main.current.id
                                 + (DIM + "   " + main.current.name if main.current.name else ""),
                                 BORDER + "─" * cols + RESET, ""]
                                + transcript(main.current))
        elif goto == "":
            main.new_chat()
        # /model, /load, /new and /compact all move the number in the bar.
        self.recount_soon()

    # -- keys -------------------------------------------------------------

    def key(self, name):
        ed = self.editor
        if self.ask and name in ("enter", "esc"):
            return self._answer(False)
        if self.picker and not self.ask:
            return self.picker_key(name)
        if name == "ctrl-c":
            if self.ask:
                return self._answer(False)
            if self.busy:
                return self.stop_turn()
            if ed.buf:
                ed.clear()
                return self.console.refresh()
            return self.quit()
        if name == "ctrl-d":
            if not ed.buf:
                return self.quit()
            ed.delete()
        elif name == "enter":
            text = ed.take()
            self.console.refresh()
            self.submit(text, TOOL if self.busy else NOW)
            return
        elif name == "tab":
            if ed.complete():
                pass
            elif ed.buf.strip():
                self.submit(ed.take(), LATER)
            else:
                return
        elif name == "esc":
            if self.busy:
                return self.stop_turn()
            ed.clear()
        elif name == "backspace":
            ed.backspace()
        elif name == "delete":
            ed.delete()
        elif name == "left":
            ed.left()
        elif name == "right":
            ed.right()
        elif name == "word-left":
            ed.word_left()
        elif name == "word-right":
            ed.word_right()
        elif name == "home":
            ed.home()
        elif name == "end":
            ed.end()
        elif name == "up":
            ed.older()
        elif name == "down":
            ed.newer()
        elif name == "kill-start":
            ed.kill_start()
        elif name == "kill-end":
            ed.kill_end()
        elif name == "kill-word":
            ed.kill_word()
        elif name == "clear":
            sys.stdout.write("\033[H\033[2J")
            self.console.rows = self.console.at = 0
        self.console.refresh()

    def text(self, s):
        if self.ask:
            for ch in s:
                if ch.lower() in ("y", "n"):
                    return self._answer(ch.lower() == "y")
            return
        if self.picker:
            self.picker.type(s)
        else:
            self.editor.insert(s)
        self.console.refresh()

    def _answer(self, yes):
        self.ask["answer"] = yes
        self.ask["done"].set()

    # -- the loop ---------------------------------------------------------

    def read_loop(self):
        # _term.KeySource is the same object on both platforms: a pty read
        # behind select() on POSIX, the console polled through msvcrt on
        # Windows, both handing back the same escape sequences. Which is why
        # there is no platform in this loop.
        source = termios.KeySource()
        keys = Keys()
        while self.alive:
            data = source.read(0.2)
            if data is None:
                self.quit()
                break
            if not data:
                continue
            events = keys.feed(data)
            if keys.ambiguous():
                # A lone \x1b: an escape, or the first byte of an arrow key. The
                # rest of a real sequence is already in the input buffer, so a
                # few milliseconds settles it.
                more = source.read(0.04) or b""
                events += keys.feed(more, final=not more)
            for kind, value in events:
                if kind == "key":
                    self.key(value)
                else:
                    self.text(value)


# ------------------------------------------------------------------ history

def _load_history():
    try:
        with open(HISTFILE, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.strip()][-500:]
    except (OSError, UnicodeDecodeError):
        return []


def _save_history(history):
    try:
        with open(HISTFILE, "w", encoding="utf-8") as f:
            f.write("\n".join(history[-500:]) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ entry

def interactive():
    # This whole interface is drawn with escape codes, so a console that will
    # not obey them can only produce a screenful of gibberish. Windows 10 and
    # 11 do obey them once asked (setup_console does the asking); anything
    # older is sent to the simpler interface instead of a broken one.
    if not termios.ansi_ok():
        print("  this terminal does not support the escape codes the "
              "full-screen interface is drawn with.")
        print("  Use  uniagentcli \"a question\"   for a single turn,")
        print("       echo text | uniagentcli     for piped input,")
        print("  or the web UI at https://localhost:"
              + str(provider.port("UNIAGENT_HTTPS_PORT", 8764)))
        return 1
    app = App()
    main.notify = app.report
    worker = threading.Thread(target=app.worker, daemon=True)
    worker.start()
    threading.Thread(target=app.usage_loop, daemon=True).start()
    banner(app.console)
    try:
        # Keys as they are pressed rather than lines as they are finished. The
        # context manager puts the terminal back even if the body raises, which
        # is the one case where a half-configured terminal would otherwise be
        # left behind for the shell to inherit.
        with termios.raw_mode():
            sys.stdout = Sink(app.console)
            sys.stdout.write("\033[?2004h")  # bracketed paste on
            termios.on_resize(app.console.refresh)
            app.console.start()
            app.read_loop()
    finally:
        app.quit()
        worker.join(timeout=2)
        app.console.stop()
        sys.stdout = app.console.out
        sys.stdout.write("\033[?2004l" + RESET + "\n")
        _save_history(app.editor.history)
    print(DIM + "  bye" + RESET)
    return 0


def once(text):
    """One turn, no terminal games - `uniagentcli "a question"`, or stdin that
    isn't a terminal at all (a pipe, a script). Whatever is typed here cannot
    be queued anywhere, because there is nothing to type at."""
    console = Console()
    # No editor and nothing to type at, but on a terminal the reply's
    # unfinished line is still worth drawing - it is the difference between
    # watching the answer arrive and watching nothing until a line ends.
    console.on = sys.stdout.isatty()
    if text.strip().lower() == main.CONTINUE:
        switch = main.model_switch(main.current)
        why = main.continue_from(main.current)
        if why is not None:
            console.commit(md.render(why))
            return 1
        note = main.switch_note(switch)
        if note:
            console.commit(md.render(note))
        text = None  # run the turn over the history as it stands
    result = command_processor.process(text) if text is not None else None
    if result is not None:
        reply, goto = result
        if reply:
            console.commit(md.render(reply))
        if goto:
            main.load(main.chat_md(goto))
        elif goto == "":
            main.new_chat()
        return 0
    stream = Stream(console)
    try:
        main.turn(main.current, text, on_text=stream, approve=lambda q: False,
                  on_tool_call=stream.tool_call, on_tool_result=stream.tool_result,
                  on_safety=stream.safety,
                  on_reasoning=stream.reasoning, on_timing=stream.measured,
                  on_request=stream.requested, on_thought=stream.thought)
    except KeyboardInterrupt:
        main.request_stop(main.current.id)
    except Exception as e:
        stream.flush(quiet=True)
        console.commit([RED + "  " + type(e).__name__ + ": " + str(e) + RESET])
        return 1
    stream.flush()
    print()
    return 0


def dumb_loop():
    """stdin is a pipe or a dumb terminal: one line in, one turn out."""
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text.lower() in QUIT:
            break
        once(text)
    return 0


# NOT called main(): `import main` at the top of this file binds that name to
# the conversation engine, and a function of the same name here would rebind
# it, turning every main.something above into an AttributeError on a module
# that is actually a function.
def run():
    # Before anything is printed: UTF-8 out and ANSI escapes understood. On
    # Linux both are already true and this changes nothing; on Windows neither
    # is, and without it the first em dash in a reply ends the process.
    termios.setup_console()
    # The safety layer's own log lines would otherwise land in the middle of
    # the tool block being drawn; Stream.safety shows the flagged ones instead.
    tool_validation.quiet = True
    if len(sys.argv) > 1:
        return once(" ".join(sys.argv[1:]))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return dumb_loop()
    return interactive()


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        sys.stdout.write(RESET)
