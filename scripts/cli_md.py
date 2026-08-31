#!/usr/bin/env python3
"""Terminal colours and a Markdown renderer for the CLI.

Split out of cli.py because it is a self-contained thing: text in, ANSI out,
no knowledge of chats or turns. The feature list is deliberately the same one
web/index.html's renderText() supports - fences, inline code, headings,
bold/italic/strikethrough, links, images, pipe tables and -/*/1. lists - so the
terminal and the browser show the same reply the same way, and the palette
below is the web theme's own CSS variables converted to ANSI.

The renderer is a CLASS rather than a function because a reply arrives a chunk
at a time and lines have to be drawn as they land: whether a line is code
depends on a ``` seen several lines earlier, so that state has to live
somewhere between calls. feed() takes one COMPLETE line and returns the
physical terminal lines to print; partial() renders an unfinished line without
touching any state, for the trailing piece that is still being typed by the
model and will be redrawn on the next chunk.
"""

import os
import re
import shutil

# ---------------------------------------------------------------- colours

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Terminals that can't do 24-bit colour get the nearest xterm-256 cell, so the
# theme degrades instead of disappearing. COLORTERM is what actually advertises
# truecolour; TERM only ever says "xterm-256color" even on terminals that do.
_TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _cube(r, g, b):
    """The xterm-256 index closest to an RGB triple - the 24-step grey ramp for
    anything near-grey, the 6x6x6 colour cube for everything else."""
    if max(r, g, b) - min(r, g, b) < 12:
        v = (r + g + b) // 3
        if v < 8:
            return 16
        if v > 248:
            return 231
        return 232 + round((v - 8) / 247 * 23)
    step = lambda c: 0 if c < 48 else 1 if c < 115 else min(5, (c - 35) // 40)
    return 16 + 36 * step(r) + 6 * step(g) + step(b)


def fg(h):
    r, g, b = _rgb(h)
    return "\033[38;2;%d;%d;%dm" % (r, g, b) if _TRUECOLOR else "\033[38;5;%dm" % _cube(r, g, b)


def bg(h):
    r, g, b = _rgb(h)
    return "\033[48;2;%d;%d;%dm" % (r, g, b) if _TRUECOLOR else "\033[48;5;%dm" % _cube(r, g, b)


# The web theme's two palettes, straight off :root / [data-theme=light] in
# web/index.html with the hsl() accents resolved to hex.
DARK = {"text": "#c9d1d9", "dim": "#8b949e", "accent": "#3fb950", "pop": "#61c970",
        "mute": "#32933f", "panel": "#161b22", "border": "#21262d", "red": "#f85249",
        "link": "#58a6ff"}
LIGHT = {"text": "#1f2328", "dim": "#656d76", "accent": "#1a7f37", "pop": "#115525",
         "mute": "#1a7f37", "panel": "#f6f8fa", "border": "#d0d7de", "red": "#ce222d",
         "link": "#0969da"}


def _pick_theme():
    """Dark unless told otherwise. UNIAGENT_THEME wins; failing that COLORFGBG,
    which terminals set to "fg;bg" as palette indexes - a high background index
    (7, 15, or the literal "default" on a light profile) means a light terminal,
    where the dark palette's greys are unreadable."""
    want = os.environ.get("UNIAGENT_THEME", "").strip().lower()
    if want in ("light", "dark"):
        return LIGHT if want == "light" else DARK
    parts = os.environ.get("COLORFGBG", "").split(";")
    if len(parts) >= 2 and parts[-1].strip().isdigit() and int(parts[-1]) >= 7:
        return LIGHT
    return DARK


C = _pick_theme()

RESET = "\033[0m"
BOLD, NOBOLD = "\033[1m", "\033[22m"
ITAL, NOITAL = "\033[3m", "\033[23m"
UNDER, NOUNDER = "\033[4m", "\033[24m"
STRIKE, NOSTRIKE = "\033[9m", "\033[29m"

TEXT = fg(C["text"])
DIM = fg(C["dim"])
ACCENT = fg(C["accent"])
POP = fg(C["pop"])
MUTE = fg(C["mute"])
RED = fg(C["red"])
LINK = fg(C["link"])
PANEL = bg(C["panel"])
NOBG = "\033[49m"
# The web's --border (#21262d) is tuned against the page's own #0d1117 panel;
# a terminal's background is whatever the user picked, and against a pure black
# one that colour is invisible. Nudged up to the next border shade so rules and
# table frames actually read, whatever the profile.
BORDER = fg("#30363d") if C is DARK else fg(C["border"])


def strip(s):
    """The text with its colour codes taken back out."""
    return _ANSI.sub("", s)


def vlen(s):
    """How many columns a string actually occupies once colour codes are
    discounted - what every width calculation here has to measure, since the
    escapes are several bytes each and print as nothing."""
    return len(strip(s))


def width(cap=100):
    """The usable text width: the terminal's, minus a column of breathing room,
    capped so a maximised window doesn't stretch prose into unreadably long
    lines."""
    return max(20, min(cap, shutil.get_terminal_size((80, 24)).columns - 1))


# ---------------------------------------------------------------- wrapping

def _sgr_after(state, chunk):
    """The colour codes still in force at the end of `chunk`, given those in
    force before it. Kept as a list rather than a parsed state because
    re-emitting the sequence verbatim reproduces the state exactly - including
    the "off" codes (22/23/29), which cancel their own attribute and leave
    everything else standing. A bare reset empties the list."""
    out = list(state)
    for m in _ANSI.finditer(chunk):
        code = m.group(0)
        out = [] if code[2:-1] in ("", "0") else out + [code]
    return out


def wrap(text, cols, first="", cont=None):
    """`text` broken to `cols` columns, `first` in front of the first line and
    `cont` in front of the rest (hanging indents for list items and quotes).

    Colour-aware in both directions: the prefixes and the text itself may carry
    escapes, which are measured as zero width, and any styling still open at a
    line break is closed and reopened on the next line - otherwise a wrapped
    bold sentence bleeds its bold into the line's own indent."""
    cont = first if cont is None else cont
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words:
        return [first.rstrip()] if strip(first).strip() else [""]
    lines, cur, curw, prefix, state = [], "", 0, first, []
    for w in words:
        ww = vlen(w)
        room = max(8, cols - vlen(prefix))
        if cur and curw + 1 + ww > room:
            lines.append(prefix + cur + (RESET + TEXT if state else ""))
            prefix, cur, curw = cont, "".join(state) + w, ww
        else:
            cur, curw = (w, ww) if not cur else (cur + " " + w, curw + 1 + ww)
        state = _sgr_after(state, w)
    lines.append(prefix + cur + (RESET if state else ""))
    return lines


def _hard_wrap(text, cols):
    """Break on the column, not on a space - for code, where the whitespace is
    the content and a re-flow would be a lie about what the line says."""
    if not text:
        return [""]
    return [text[i:i + cols] for i in range(0, len(text), cols)] or [""]


# ---------------------------------------------------------------- inline

_STASH = re.compile("\x00(\\d+)\x00")


def inline(text):
    """`**bold**`, `*italic*`, links, images and `code` turned into escapes.

    Code spans are pulled out FIRST and put back LAST, behind a null-byte
    placeholder, so a stray * or _ inside `some_code*here` is never read as
    emphasis - the same trick, for the same reason, as the web renderer's.

    Every span closes with its own "off" code (22/23/29) rather than a full
    reset, so styles nest: the reset in `**bold with *italic* inside**` would
    have ended the bold at the italic's closing marker and left the rest of the
    sentence plain."""
    held = []

    def stash(s):
        held.append(s)
        return "\x00%d\x00" % (len(held) - 1)

    text = re.sub(r"`([^`\n]+)`",
                  lambda m: stash(PANEL + POP + " " + m.group(1) + " " + NOBG + TEXT), text)
    # Images before links: ![alt](src) also matches the link pattern, which
    # would leave a stray "!" in front of it.
    text = re.sub(r"!\[([^\]\n]*)\]\(\s*<?([^)\n]+?)>?\s*\)",
                  lambda m: stash(DIM + "[image " + (m.group(1) or m.group(2)) + "]" + TEXT), text)
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)",
                  lambda m: stash(UNDER + LINK + m.group(1) + NOUNDER + TEXT
                                  + DIM + " (" + m.group(2) + ")" + TEXT), text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: BOLD + POP + m.group(1) + NOBOLD + TEXT, text)
    text = re.sub(r"~~([^~\n]+)~~", lambda m: STRIKE + DIM + m.group(1) + NOSTRIKE + TEXT, text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", lambda m: ITAL + m.group(1) + NOITAL, text)
    return _STASH.sub(lambda m: held[int(m.group(1))], text)


# ---------------------------------------------------------------- renderer

_FENCE = re.compile(r"^\s*```(\S*)")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_ROW = re.compile(r"^\s*\|")
_SEP = re.compile(r"^\s*\|?[\s:|-]*\|[\s:|-]*$")

BULLETS = ("•", "◦", "▪")  # bullet, white bullet, small square


class Renderer:
    """One reply's worth of Markdown, fed a line at a time.

    Two things are held between calls: whether a ``` fence is open, and the
    rows of a pipe table that has started but not finished. A table can't be
    drawn until its last row is in, because the column widths depend on every
    cell - so its lines come out all at once, when a non-table line (or flush())
    finally ends it."""

    def __init__(self, cols=None):
        self._cols = cols
        self.fence = False
        self.lang = ""
        self.table = []
        # A heading opens with a blank line to stand off the text above it -
        # but the source usually has one there already, and two in a row reads
        # as a gap rather than as spacing. Tracked here so the second is
        # dropped; True at the start so a reply opening on a heading doesn't
        # begin with an empty line either.
        self.blank = True

    @property
    def cols(self):
        # Read live rather than cached, so resizing the window mid-reply is
        # picked up by the very next line instead of the next run.
        return self._cols or width()

    def feed(self, line):
        """One complete source line -> the terminal lines to print for it."""
        out = []
        if not _ROW.match(line) and self.table:
            out += self._flush_table()
        out += self._line(line, keep=True)
        if out:
            self.blank = not strip(out[-1]).strip()
        return out

    def flush(self):
        """Anything still held - a table left open by the end of the reply."""
        return self._flush_table() if self.table else []

    def partial(self, text):
        """An unfinished trailing line, rendered without changing any state.
        Redrawn from scratch on every chunk, so it must never consume a fence
        or start a table it can't finish."""
        if self.fence:
            return self._code(text)
        if _ROW.match(text) or self.table:
            return [DIM + text.rstrip() + RESET]
        return self._line(text, keep=False)

    # -- one line ---------------------------------------------------------

    def _line(self, line, keep):
        cols = self.cols
        line = line.replace("\t", "    ").rstrip("\n")

        fence = _FENCE.match(line)
        if fence and (self.fence or not line.strip().startswith("````")):
            if self.fence:
                if keep:
                    self.fence, self.lang = False, ""
                return [MUTE + "╰" + "─" * (cols - 1) + RESET]
            lang = fence.group(1)
            if keep:
                self.fence, self.lang = True, lang
            label = " " + lang + " " if lang else ""
            bar = "─" * max(0, cols - 2 - vlen(label))
            return [MUTE + "╭─" + (DIM + label + MUTE if label else "") + bar + RESET]

        if self.fence:
            return self._code(line)

        if not line.strip():
            return [""]

        if _RULE.match(line):
            return [BORDER + "─" * cols + RESET]

        if _ROW.match(line):
            if keep:
                self.table.append(line)
                return []
            return [DIM + line.rstrip() + RESET]

        head = _HEAD.match(line)
        if head:
            return self._heading(len(head.group(1)), head.group(2), cols)

        quote = _QUOTE.match(line)
        if quote:
            bar = MUTE + "▏ " + DIM + ITAL
            return wrap(inline(quote.group(1)) if quote.group(1) else "", cols,
                        first=bar, cont=bar)

        num = _NUMBER.match(line)
        if num:
            depth = len(num.group(1)) // 2
            pad = "  " * depth
            mark = pad + ACCENT + num.group(2) + "." + TEXT + " "
            return wrap(inline(num.group(3)), cols, first=mark,
                        cont=pad + " " * (len(num.group(2)) + 2))

        bullet = _BULLET.match(line)
        if bullet:
            depth = len(bullet.group(1)) // 2
            pad = "  " * depth
            mark = pad + ACCENT + BULLETS[depth % len(BULLETS)] + TEXT + " "
            return wrap(inline(bullet.group(2)), cols, first=mark, cont=pad + "  ")

        # Leading spaces are kept and carried onto the wrapped continuation:
        # plain text that lines up its own columns (a /command's answer, a tool
        # listing) is common enough that reflowing it flush-left would be a
        # visible regression, and prose is unindented anyway so it costs it
        # nothing.
        pad = line[:len(line) - len(line.lstrip(" "))]
        return wrap(inline(line), cols, first=pad + TEXT, cont=pad + TEXT)

    def _heading(self, level, text, cols):
        body = inline(text.strip())
        gap = [] if self.blank else [""]
        if level == 1:
            return gap + wrap(BOLD + POP + body, cols) + [BORDER + "─" * cols + RESET]
        if level == 2:
            return gap + wrap(BOLD + ACCENT + body, cols)
        return gap + wrap(BOLD + TEXT + body, cols)

    def _code(self, line):
        """A line inside a fence: a coloured left rail, then the code on the
        panel background, padded out so the block reads as one solid slab
        rather than a ragged right edge."""
        cols = self.cols
        room = cols - 2
        return [MUTE + "│" + PANEL + TEXT + " " + part.ljust(room)[:room] + NOBG + RESET
                for part in _hard_wrap(line, room)]

    # -- tables -----------------------------------------------------------

    def _flush_table(self):
        rows, self.table = self.table, []
        cells = [self._cells(r) for r in rows]
        align = ["left"] * max((len(c) for c in cells), default=0)
        head = None
        # What makes a run of pipe lines a TABLE rather than prose is the
        # |---|:--:| line under the header. Without one there is nothing to
        # align to and no header to bolden, so the rows are left as they were.
        if len(rows) >= 2 and _SEP.match(rows[1]) and "-" in rows[1]:
            head, align = cells[0], [self._align(c) for c in cells[1]]
            cells = cells[2:]
        if head is None and not any(_SEP.match(r) for r in rows):
            return [TEXT + r.strip() + RESET for r in rows]

        grid = ([head] if head else []) + cells
        n = max(len(r) for r in grid)
        grid = [r + [""] * (n - len(r)) for r in grid]
        align += ["left"] * (n - len(align))
        widths = [max(vlen(inline(r[i])) for r in grid) for i in range(n)]

        # Too wide for the window: take the columns down proportionally and let
        # the cells ellipsise, rather than letting the table wrap into rubble.
        cols = self.cols
        over = sum(widths) + 3 * n + 1 - cols
        while over > 0 and max(widths) > 4:
            widths[widths.index(max(widths))] -= 1
            over -= 1

        line = lambda l, m, r: BORDER + l + m.join("─" * (w + 2) for w in widths) + r + RESET
        out = [line("┌", "┬", "┐")]
        if head:
            out.append(self._row(head, widths, align, bold=True))
            out.append(line("├", "┼", "┤"))
        for row in cells:
            out.append(self._row(row, widths, align))
        out.append(line("└", "┴", "┘"))
        return out

    def _row(self, row, widths, align, bold=False):
        pipe = BORDER + "│" + RESET
        out = []
        for i, w in enumerate(widths):
            text = row[i] if i < len(row) else ""
            body = inline(text)
            if vlen(body) > w:
                body = strip(body)[:max(1, w - 1)] + "…"
            pad = w - vlen(body)
            if align[i] == "right":
                body = " " * pad + body
            elif align[i] == "center":
                body = " " * (pad // 2) + body + " " * (pad - pad // 2)
            else:
                body = body + " " * pad
            style = BOLD + ACCENT if bold else TEXT
            out.append(" " + style + body + RESET + " ")
        return pipe + pipe.join(out) + pipe

    @staticmethod
    def _cells(row):
        """"| a | b |" -> ["a", "b"]. The outer pipes are padding rather than
        empty cells, and a \\| inside a cell is a literal pipe, not a split."""
        cells, cur, i = [], "", 0
        while i < len(row):
            if row[i] == "\\" and i + 1 < len(row) and row[i + 1] == "|":
                cur, i = cur + "|", i + 2
                continue
            if row[i] == "|":
                cells.append(cur)
                cur = ""
            else:
                cur += row[i]
            i += 1
        cells.append(cur)
        if cells and not cells[0].strip():
            cells.pop(0)
        if cells and not cells[-1].strip():
            cells.pop()
        return [c.strip() for c in cells]

    @staticmethod
    def _align(spec):
        spec = spec.strip()
        left, right = spec.startswith(":"), spec.endswith(":")
        return "center" if left and right else "right" if right else "left"


def render(text, cols=None):
    """A whole string of Markdown as one block of terminal lines. For anything
    already complete - a /command's answer, a tool result - where there is no
    stream to follow."""
    r = Renderer(cols)
    out = []
    for line in text.split("\n"):
        out += r.feed(line)
    return out + r.flush()
