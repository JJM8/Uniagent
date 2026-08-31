"""Who is running the server and the cron watcher, and how to restart them.

Uniagent's two long-lived processes are kept alive by something outside them,
and what that something is depends on the machine:

    Linux     two systemd user services, restarted with `systemctl --user`.
    Windows   run-server.ps1, a supervisor loop that starts each process and
              starts it again when it exits.
    neither   somebody ran `python scripts/server.py` in a terminal.

That third case is not an edge case - it is how the app is run while it is
being worked on - and the difference matters, because "restart" means something
different in each. Under a supervisor the way to restart is to STOP: exit, and
be started again a moment later on the new code. With nobody watching, exiting
means the app is gone, so a replacement has to be launched first.

Getting that backwards is not a small bug. On Windows os.execv does not replace
the process the way it does on POSIX - it starts a new one and ends this one,
under a new pid. The supervisor sees the pid it was watching disappear and
starts a server of its own, so an update left two servers fighting over one
port, and which of them won was a coin toss.

WHAT IS IN HERE

  supervised()      whether something will restart us if we exit
  write_pidfile()   record this process, so another one can find it
  stop(name)        end that process and let the supervisor bring it back
  restart_self()    the right kind of restart for wherever this is running
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "logs"

WINDOWS = os.name == "nt"

# Set by run-server.ps1 on every process it starts. Its presence is the whole
# test: it is set by the thing doing the supervising, so it cannot be true when
# there is nothing there.
SUPERVISED_ENV = "UNIAGENT_SUPERVISED"

# What a supervised process exits with to ask for a restart. Any code brings it
# back - the supervisor restarts on any exit - but a distinct one makes the
# reason legible in a log rather than looking like a crash.
RESTART_CODE = 7


def supervised():
    """Whether something outside this process will start it again if it exits.

    True under run-server.ps1 (which sets the variable) and under systemd
    (Restart=always, which sets INVOCATION_ID on every unit it starts)."""
    return bool(os.environ.get(SUPERVISED_ENV)
                or os.environ.get("INVOCATION_ID"))


# --- pid files -------------------------------------------------------------

def pidfile(name):
    return RUN_DIR / (name + ".pid")


def write_pidfile(name):
    """Record this process under `name`, and clear it again on the way out.

    This is how one process reaches another: the server restarts the cron
    watcher, and an update stops both. On Linux systemd knows their pids
    already and this is only a fallback; on Windows there is no service manager
    holding that knowledge, so the file is the only answer.
    """
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        pidfile(name).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return  # a read-only folder is not worth failing to start over
    import atexit
    atexit.register(_clear_pidfile, name, os.getpid())


def _clear_pidfile(name, pid):
    """Remove our own pidfile, and only ours - a later process may have
    replaced it, and deleting its file would strand it."""
    try:
        if read_pid(name) == pid:
            pidfile(name).unlink()
    except OSError:
        pass


def read_pid(name):
    """The recorded pid for `name`, or None if there isn't a usable one."""
    try:
        return int(pidfile(name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    """Whether that pid is a process that exists right now."""
    if pid is None:
        return False
    if WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq " + str(pid), "/NH"],
                                 capture_output=True, timeout=10,
                                 text=True, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)   # signal 0 asks "is it there?" and changes nothing
        return True
    except OSError:
        return False


# --- stopping and restarting ----------------------------------------------

def stop(name, timeout=10):
    """End the process recorded under `name`. True if it is gone afterwards.

    Under a supervisor this IS the restart: the process ends and is started
    again seconds later, on whatever code is on disk by then. Asked for
    politely first - the server has a certificate and open sockets to let go
    of - and insisted on if that is ignored.
    """
    pid = read_pid(name)
    if not alive(pid):
        return True

    if WINDOWS:
        # /T so the tree goes with it: a python that has started a shell would
        # otherwise leave that shell holding the port.
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)

    if not WINDOWS:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return not alive(pid)


def python():
    """The interpreter this install actually runs on - the venv's, if an
    installer built one, because that is where the dependencies live."""
    for rel in (".venv/bin/python3", ".venv/bin/python", ".venv/Scripts/python.exe"):
        p = ROOT / rel
        if p.exists():
            return str(p)
    return sys.executable


def spawn(script):
    """Start one of our scripts as a process of its own, outliving this one.

    For the unsupervised case only: with nothing waiting to restart us, a
    replacement has to exist before we go.
    """
    argv = [python(), str(ROOT / "scripts" / script)]
    kwargs = {"cwd": str(ROOT / "scripts")}
    if WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console of its own,
        # and out of this process's group so it is not killed along with it.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
        return True
    except OSError:
        return False


def restart_self(script="server.py", delay=0.4):
    """Come back on the code that is on disk now.

    Python reads every .py once at startup and never looks again, so updated
    code only takes effect in a new process. Which KIND of new process depends
    on the platform:

      POSIX     execv, which REPLACES this process: same pid, same terminal,
                same everything, running the new code. systemd sees no exit at
                all, so it neither counts a failure nor races its own
                Restart=always, and a server started by hand in a terminal
                stays in that terminal. There is no case here where it is
                wrong.

      Windows   execv does not replace anything - it starts a new process and
                ends this one, under a NEW pid. run-server.ps1 is watching the
                old pid, sees it disappear, and starts a server of its own: two
                servers, one port, and which of them wins is a coin toss. So
                under the supervisor we simply exit and let it do the starting,
                and with no supervisor we start the replacement ourselves.

    Runs on its own thread after a beat, so the HTTP response that asked for
    the restart is actually written before the process is gone.
    """
    import threading

    def go():
        time.sleep(delay)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        if not WINDOWS:
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return
        if not supervised():
            spawn(script)
        os._exit(RESTART_CODE)

    threading.Thread(target=go, daemon=True).start()
