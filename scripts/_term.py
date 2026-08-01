"""termios, made portable.

termios is POSIX-only. On Windows there is no such module, and cli.py and
main.py both import it at the top of the file - which is fine on Linux and a
crash on Windows even for code paths that never touch a terminal.

This shim exposes the same names under the same spelling, so the callers just
do `import _term as termios`. On POSIX it IS termios, unchanged. On Windows the
functions are no-ops and POSIX is False, so interactive() can refuse the raw
keyboard mode it cannot have and fall back to a simpler interface instead.
"""

import sys

if sys.platform == "win32":
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
        pass
else:
    import termios as _t
    POSIX = True
    ICANON, ECHO, ISIG = _t.ICANON, _t.ECHO, _t.ISIG
    VMIN, VTIME = _t.VMIN, _t.VTIME
    TCIFLUSH, TCSADRAIN = _t.TCIFLUSH, _t.TCSADRAIN
    tcgetattr, tcsetattr, tcflush = _t.tcgetattr, _t.tcsetattr, _t.tcflush
