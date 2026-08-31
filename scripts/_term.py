"""The terminal, made portable - one place for every difference between them.

termios, raw keyboard mode, ANSI colour and hidden password entry all exist on
both platforms and are reached completely differently on each. Rather than
scatter `if os.name == "nt"` through cli.py, main.py and setup_wizard.py, all of
it lives here and the callers get one spelling that works in both places.

WHAT IS IN HERE

  termios         POSIX's module, unchanged. On Windows the same names exist as
                  no-ops and POSIX is False, so `import _term as termios` works
                  everywhere and code that needs the real thing can ask.
  setup_console() UTF-8 output and working ANSI escapes. Call it once, early,
                  in anything that prints.
  raw_mode(fd)    cbreak on POSIX, the equivalent console mode on Windows.
  KeySource       raw keypresses as bytes, with a timeout, the same on both.
  hide_input()    typing that doesn't appear on screen, for a password.
  on_resize()     a callback when the window changes size.

WHY UTF-8 HAS TO BE ASKED FOR

Windows decides text encoding from the system codepage, which is cp1252 on a
Western install. Every model reply that contains an em dash, a curly quote or an
emoji - which is most of them - then raises UnicodeEncodeError on the way to the
screen and takes the turn down with it. setup_console() switches the console to
UTF-8 and tells Python's own streams to use it too, with errors="replace" so a
character that still cannot be shown becomes a "?" instead of an exception.
"""

import os
import sys

WINDOWS = sys.platform == "win32"

# --- termios, or a stand-in for it -----------------------------------------

if WINDOWS:
    POSIX = False

    # Names cli.py and main.py reference. On Windows they are only ever read,
    # never used for anything real, so the values are placeholders.
    ICANON = ECHO = ISIG = 0
    VMIN = VTIME = 6
    TCIFLUSH = 0
    TCSADRAIN = 0

    def tcgetattr(fd):
        return None

    def tcsetattr(fd, when, attrs):
        pass

    def tcflush(fd, q):
        # Drop anything typed but not yet read, so a pasted line cannot answer
        # a y/n question that hasn't been asked yet. msvcrt is the only way to
        # see the console's input queue from Python.
        try:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        except Exception:
            pass
else:
    import termios as _t
    POSIX = True
    ICANON, ECHO, ISIG = _t.ICANON, _t.ECHO, _t.ISIG
    VMIN, VTIME = _t.VMIN, _t.VTIME
    TCIFLUSH, TCSADRAIN = _t.TCIFLUSH, _t.TCSADRAIN
    tcgetattr, tcsetattr, tcflush = _t.tcgetattr, _t.tcsetattr, _t.tcflush


# --- the Windows console API, as much of it as we need ---------------------

# Output modes.
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
# Input modes.
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

_STD_INPUT, _STD_OUTPUT, _STD_ERROR = -10, -11, -12


def _kernel32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _console_mode(which):
    """(kernel32, handle, mode) for one standard stream, or None if it is not a
    console - a redirected stdout is a pipe, and has no mode to set."""
    try:
        import ctypes
        k = _kernel32()
        handle = k.GetStdHandle(which)
        if handle in (0, -1, None):
            return None
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return k, handle, mode.value
    except Exception:
        return None


# --- setup_console ---------------------------------------------------------

_console_ready = False


def setup_console():
    """Make this process's console usable, and say whether ANSI works.

    Safe to call more than once and from anywhere - it does the work the first
    time and answers from memory afterwards. Does no harm when the output is a
    file or a pipe: those get the UTF-8 reconfigure (which is what keeps a log
    file readable) and nothing else.
    """
    global _console_ready
    if _console_ready:
        return _ansi_ok
    _console_ready = True
    _utf8_streams()
    if WINDOWS:
        _windows_console()
    return _ansi_ok


# Whether escape codes will be understood rather than printed literally. True
# everywhere except an old Windows console that refuses virtual terminal mode.
_ansi_ok = True


def _utf8_streams():
    """Read and write UTF-8 whatever the system codepage says.

    errors="replace" on the way out, because a character the font or the
    codepage cannot show must never be the thing that ends a conversation.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # not a text stream, already closed, or a pythonw process
    try:
        # Input is decoded strictly nowhere else, so replace here too - a byte
        # from a pasted line is not worth a traceback.
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _windows_console():
    """Turn on UTF-8 and ANSI escape handling in the console itself."""
    global _ansi_ok
    try:
        k = _kernel32()
        k.SetConsoleOutputCP(65001)   # UTF-8 out
        k.SetConsoleCP(65001)         # UTF-8 in
    except Exception:
        pass

    # Windows 10 1511 and later understand ANSI escapes, but only once asked.
    # Without this every colour code in the CLI is printed as literal text.
    ok = False
    for which in (_STD_OUTPUT, _STD_ERROR):
        got = _console_mode(which)
        if got is None:
            continue
        k, handle, mode = got
        if mode & _ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            ok = True
            continue
        if k.SetConsoleMode(handle, mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            ok = True
    # A redirected stdout has no console mode at all, and escape codes in a log
    # file are the caller's business, not ours - so that case stays True.
    if _console_mode(_STD_OUTPUT) is not None:
        _ansi_ok = ok


def ansi_ok():
    """Whether colour and cursor codes will be obeyed. Callers that draw a
    whole screen should refuse to on a console that cannot do it."""
    setup_console()
    return _ansi_ok


# --- raw keyboard mode -----------------------------------------------------

class raw_mode:
    """Keys as they are pressed, not lines as they are finished.

    A context manager, so the terminal is always put back - including when the
    body raises, which is exactly when a half-configured terminal would
    otherwise be left behind for the shell to inherit.

    POSIX gets cbreak rather than full raw: the output side's newline
    translation stays on, so a plain "\\n" from anything that prints still
    returns the carriage. Windows gets the same shape - line editing and echo
    off, Ctrl-C delivered as a byte instead of an exception - plus virtual
    terminal input, which is what makes the arrow keys arrive as the same
    escape sequences they are on POSIX.
    """

    def __init__(self, fd=None):
        self.fd = fd if fd is not None else _stdin_fd()
        self.saved = None
        self.handle = None

    def __enter__(self):
        if WINDOWS:
            got = _console_mode(_STD_INPUT)
            if got is not None:
                k, handle, mode = got
                self.handle, self.saved = handle, mode
                k.SetConsoleMode(handle, (mode & ~(
                    _ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT
                    | _ENABLE_PROCESSED_INPUT))
                    | _ENABLE_VIRTUAL_TERMINAL_INPUT)
            return self
        self.saved = tcgetattr(self.fd)
        raw = list(self.saved)
        raw[3] &= ~(ICANON | ECHO | ISIG)
        raw[6] = list(raw[6])
        raw[6][VMIN], raw[6][VTIME] = 1, 0
        tcsetattr(self.fd, TCSADRAIN, raw)
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            return False
        if WINDOWS:
            try:
                _kernel32().SetConsoleMode(self.handle, self.saved)
            except Exception:
                pass
        else:
            tcsetattr(self.fd, TCSADRAIN, self.saved)
        return False


def _stdin_fd():
    try:
        return sys.stdin.fileno()
    except Exception:
        return 0


# --- reading keys ----------------------------------------------------------

# What the Windows console reports for a key that has no character - a two-part
# read, 0x00 or 0xE0 followed by a scan code - spelled as the escape sequence a
# POSIX terminal would have sent. That way cli.py's key parser sees one input
# language and needs to know nothing about any of this.
_WIN_SCANCODES = {
    "H": b"\x1b[A",      # up
    "P": b"\x1b[B",      # down
    "M": b"\x1b[C",      # right
    "K": b"\x1b[D",      # left
    "G": b"\x1b[H",      # home
    "O": b"\x1b[F",      # end
    "S": b"\x1b[3~",     # delete
    "s": b"\x1b[1;5D",   # ctrl-left
    "t": b"\x1b[1;5C",   # ctrl-right
}


class KeySource:
    """Raw keypresses as bytes, with a timeout. The same object on both
    platforms, so the read loop that uses it has no platform in it at all.

    read(timeout) returns whatever has been typed within `timeout` seconds,
    b"" if nothing was, and None when the input has ended for good (a closed
    pipe, a Ctrl-D on an empty POSIX line).
    """

    def __init__(self, fd=None):
        self.fd = fd if fd is not None else _stdin_fd()
        self._pending = ""   # a lone high surrogate waiting for its partner

    if WINDOWS:
        def read(self, timeout):
            # No select() for the console on Windows - it only ever accepts
            # sockets - so this polls msvcrt, which is the one way Python can
            # ask "has anything been typed?" without blocking on the answer.
            import time
            import msvcrt
            deadline = time.monotonic() + timeout
            out = b""
            while True:
                while msvcrt.kbhit():
                    out += self._one(msvcrt.getwch())
                if out or time.monotonic() >= deadline:
                    return out
                time.sleep(0.01)

        def _one(self, ch):
            """One character from the console, as the bytes POSIX would send."""
            if ch in ("\x00", "\xe0"):
                # A key with no character of its own: the scan code is the very
                # next read, and it is always already there.
                import msvcrt
                code = msvcrt.getwch()
                return _WIN_SCANCODES.get(code, b"")
            if "\ud800" <= ch <= "\udbff":
                self._pending = ch       # first half of an emoji - wait
                return b""
            if self._pending:
                ch, self._pending = self._pending + ch, ""
                try:
                    return ch.encode("utf-16", "surrogatepass").decode(
                        "utf-16").encode("utf-8")
                except Exception:
                    return b""
            return ch.encode("utf-8", "replace")
    else:
        def read(self, timeout):
            import errno
            import select
            try:
                ready, _, _ = select.select([self.fd], [], [], timeout)
            except OSError as e:
                if e.errno == errno.EINTR:
                    return b""
                raise
            if not ready:
                return b""
            try:
                data = os.read(self.fd, 4096)
            except OSError as e:
                if e.errno == errno.EINTR:
                    return b""
                raise
            return data if data else None


# --- hidden input ----------------------------------------------------------

def read_hidden(stream):
    """One line, typed without appearing on screen. For a password or an API
    key, which should not be left in the scrollback of a shared machine.

    Falls back to a visible read wherever the echo cannot be turned off, since
    a question that cannot be answered is worse than one answered in public.
    """
    if WINDOWS:
        try:
            import msvcrt
        except ImportError:
            return stream.readline()
        chars = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                return "".join(chars) + "\n"
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x08":                       # backspace
                if chars:
                    chars.pop()
                continue
            if ch in ("\x00", "\xe0"):             # a key with no character
                msvcrt.getwch()
                continue
            chars.append(ch)
    try:
        fd = stream.fileno()
        saved = tcgetattr(fd)
        quiet = tcgetattr(fd)
        quiet[3] &= ~ECHO                          # index 3 is lflags
        tcsetattr(fd, TCSADRAIN, quiet)
        try:
            answer = stream.readline()
        finally:
            tcsetattr(fd, TCSADRAIN, saved)
        print()                                    # the Enter that wasn't echoed
        return answer
    except Exception:
        return stream.readline()


# --- window size -----------------------------------------------------------

def on_resize(callback):
    """Call `callback` whenever the window changes size.

    POSIX has a signal for it. Windows has no equivalent a console program can
    catch, so a thread watches the size instead - a second's lag on a resize
    nobody does often, rather than a screen that never reflows.
    """
    if not WINDOWS:
        import signal
        try:
            signal.signal(signal.SIGWINCH, lambda *_: callback())
            return
        except (ValueError, AttributeError, OSError):
            pass  # not the main thread, or no SIGWINCH here - fall through
    import shutil
    import threading
    import time

    def watch():
        last = shutil.get_terminal_size()
        while True:
            time.sleep(0.5)
            now = shutil.get_terminal_size()
            if now != last:
                last = now
                try:
                    callback()
                except Exception:
                    return

    threading.Thread(target=watch, daemon=True).start()
