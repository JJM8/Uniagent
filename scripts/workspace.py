"""Where a chat's tools do their work, and which machine they do it on.

A workspace is a root directory plus, optionally, an ssh destination. The
objects live in .env and are read by provider.workspaces(); this module turns
one of those into something the tools can actually use:

    ws = workspace.get(chat_workspace_id)
    ws.read_text("notes.md")          # relative to the workspace root
    ws.write_text("/etc/hosts", ...)  # absolute stays absolute
    ws.run("git status")

Every method works the same whether the workspace is on this machine or on the
far end of an ssh connection, which is the whole point: a tool asks the
workspace for a file and does not care where the file is. Nothing here is
imported by tools that don't take a workspace, and nothing here touches the
network until a remote workspace is actually used.

REMOTE WORKSPACES NEED KEY-BASED SSH. Every connection is made with
BatchMode=yes, which means ssh never prompts. A password prompt in the server
process would hang a turn forever with nobody able to see the question, so a
workspace that would need one fails immediately with a message saying so
instead. Set up `ssh-copy-id` once and it is not thought about again.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provider

# The install folder - what a workspace-aware tool falls back to when no
# workspaces are configured at all. This is exactly where the tools resolved
# relative paths before workspaces existed, so a setup with an empty
# WORKSPACES behaves precisely as it always did.
INSTALL_ROOT = Path(__file__).resolve().parent.parent

# How long a one-shot command may take before it is given up on. Long enough
# for a slow `git status` over a phone hotspot, short enough that a hung remote
# does not hold a turn open forever.
RUN_TIMEOUT = 120

# How long an idle ssh connection is kept alive for reuse, in seconds. The
# first call to a remote workspace pays for a TCP handshake and a key exchange;
# every call within this window rides the socket that call opened, which turns
# a ~300ms round trip into a ~10ms one. That difference is what makes a remote
# workspace usable for a tool that reads five files in a turn.
SSH_PERSIST = 300


class WorkspaceError(RuntimeError):
    """Something went wrong reaching the workspace itself - the host is down,
    the key is not set up, the path does not exist over there. Distinct from an
    ordinary file error so tools can say "the workspace is unreachable" rather
    than "no such file", which would send the model hunting for a typo that
    isn't there."""


class Workspace:
    """One workspace, local or remote. Cheap to construct and safe to keep -
    holds no connection of its own, since ssh's own multiplexing does that."""

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.id = cfg.get("id", "")
        self.name = cfg.get("name") or "install folder"
        self.ssh = cfg.get("ssh", "")
        self.port = cfg.get("port", 0)
        self.root = str(cfg.get("path") or INSTALL_ROOT)

    # --- the shape of it ---------------------------------------------------

    @property
    def is_remote(self):
        return bool(self.ssh)

    @property
    def where(self):
        """A short phrase for messages and tool output - "on pi (joshy@10.0.0.5)"
        or "in /home/you/projects"."""
        if self.is_remote:
            return "on " + self.name + " (" + self.ssh + ")"
        return "in " + self.root

    def __repr__(self):
        return "<Workspace " + (self.id or "install") + " " + self.where + ">"

    def resolve(self, path):
        """A path as the workspace sees it. Relative resolves against the root;
        absolute is left alone.

        Absolute stays absolute deliberately. A workspace is a working
        directory, not a sandbox - the same as every other tool in here, which
        has always accepted an absolute path. Confining the agent to a subtree
        is a safety-system job, and pretending a path helper does it would be
        the worse kind of security: the kind that is believed."""
        text = str(path or "").strip()
        if self.is_remote:
            # No pathlib on a remote path: this process may be Windows, where
            # PurePath would happily turn /home/you into \home\you.
            if text.startswith("/"):
                return text
            root = self.root.rstrip("/")
            return (root + "/" + text) if text else root
        p = Path(text).expanduser()
        return str(p if p.is_absolute() else Path(self.root) / p)

    # --- running things ----------------------------------------------------

    def _ssh_argv(self, *options, command=None):
        """The ssh command line. Options BEFORE the destination, the remote
        command after it - that order is not style, it is the syntax: ssh
        treats everything following the destination as the command to run, so
        a flag placed after it is sent to the far machine as a word to execute
        rather than read as a flag. `ssh host -tt "cmd"` tries to run a program
        called -tt over there, and the connection dies immediately."""
        argv = [
            "ssh",
            # Never prompt. See the module docstring: an unanswerable prompt in
            # a background service is worse than a clean failure.
            "-o", "BatchMode=yes",
            # Connection reuse. %C is ssh's own hash of user/host/port, which
            # keeps the socket path short - a unix socket path is capped near
            # 104 characters, and a workspace name in there would blow it.
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=" + str(_control_dir() / "uniagent-%C"),
            "-o", "ControlPersist=" + str(SSH_PERSIST),
            "-o", "ConnectTimeout=10",
            # A first connection to a new machine should work without someone
            # having to go and type "yes" at a prompt nobody can see. The key
            # is still pinned from then on, so a later change still fails loudly.
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.port:
            argv += ["-p", str(self.port)]
        argv += list(options)
        argv.append(self.ssh)
        return argv + ([command] if command is not None else [])

    def ssh_argv(self, *options, command=None):
        """The ssh command line for this workspace, for a caller that needs to
        drive the connection itself rather than run one thing and read the
        output - the terminal tool, which keeps a shell open inside it."""
        if not self.is_remote:
            raise WorkspaceError("workspace " + (self.id or "install") + " is local")
        return self._ssh_argv(*options, command=command)

    def run(self, command, timeout=RUN_TIMEOUT, stdin=None, cwd=None):
        """Run a shell command in the workspace. Returns (exit_code, output)
        with stdout and stderr merged, which is what the model wants to read.

        `cwd` defaults to the workspace root, so `run("ls")` means "list the
        workspace", here and over there alike."""
        where = self.resolve(cwd) if cwd else self.root
        if self.is_remote:
            # cd first so a relative command behaves the same as it does
            # locally; `exec` keeps the shell from adding a layer.
            wrapped = "cd " + shlex.quote(where) + " && " + command
            argv = self._ssh_argv(command=wrapped)
            proc_kwargs = {}
        else:
            argv = command
            proc_kwargs = {"shell": True, "cwd": where}
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, errors="replace",
                timeout=timeout, input=stdin, **proc_kwargs)
        except subprocess.TimeoutExpired:
            raise WorkspaceError(
                "timed out after " + str(timeout) + "s running in the workspace "
                + self.where)
        except OSError as e:
            raise WorkspaceError("could not run anything " + self.where + ": " + str(e))
        out = (done.stdout or "") + (done.stderr or "")
        if self.is_remote and done.returncode == 255 and "ssh" not in command:
            # 255 is ssh's own "I could not connect", as opposed to the remote
            # command's exit code. Worth translating, because "exit 255" tells
            # nobody anything.
            raise WorkspaceError(_ssh_hint(self, out))
        return done.returncode, out

    # --- files -------------------------------------------------------------

    def exists(self, path):
        if self.is_remote:
            code, _ = self.run("test -e " + shlex.quote(self.resolve(path)))
            return code == 0
        return Path(self.resolve(path)).exists()

    def is_dir(self, path):
        if self.is_remote:
            code, _ = self.run("test -d " + shlex.quote(self.resolve(path)))
            return code == 0
        return Path(self.resolve(path)).is_dir()

    def listdir(self, path):
        """Names in a directory, folders marked with a trailing slash - the
        shape read_file already prints when handed a directory."""
        target = self.resolve(path)
        if self.is_remote:
            # -p is POSIX and marks directories with a slash, so one call does
            # the whole job rather than a stat per entry over the network.
            code, out = self.run("ls -Ap -- " + shlex.quote(target))
            if code != 0:
                raise WorkspaceError(out.strip() or ("cannot list " + target))
            return sorted(line for line in out.splitlines() if line.strip())
        return sorted(p.name + ("/" if p.is_dir() else "")
                      for p in Path(target).iterdir())

    def read_text(self, path):
        target = self.resolve(path)
        if self.is_remote:
            code, out = self.run("cat -- " + shlex.quote(target))
            if code != 0:
                raise WorkspaceError(out.strip() or ("cannot read " + target))
            return out
        return Path(target).read_text(errors="replace")

    def write_text(self, path, content):
        """Write a whole file, creating parent directories. Returns the
        resolved path, which is what the tools echo back to the model."""
        target = self.resolve(path)
        if self.is_remote:
            parent = target.rsplit("/", 1)[0] or "/"
            # The content goes over stdin rather than inside the command line:
            # an argument list has a length limit measured in kilobytes, and a
            # file the agent just wrote is routinely bigger than that.
            code, out = self.run(
                "mkdir -p " + shlex.quote(parent) + " && cat > " + shlex.quote(target),
                stdin=content)
            if code != 0:
                raise WorkspaceError(out.strip() or ("cannot write " + target))
            return target
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return target

    # --- reachability ------------------------------------------------------

    def check(self):
        """(ok, message) - whether the workspace can actually be used right
        now. The settings page's test button, and worth calling before a long
        piece of work rather than discovering it on the third tool call."""
        if not self.is_remote:
            p = Path(self.root)
            if not p.exists():
                return False, "there is no directory at " + self.root
            if not p.is_dir():
                return False, self.root + " is a file, not a directory"
            if not os.access(self.root, os.W_OK):
                return False, "no write access to " + self.root
            return True, "ready - " + self.root
        try:
            code, out = self.run("test -d " + shlex.quote(self.root)
                                 + " && echo ok || echo missing", timeout=20)
        except WorkspaceError as e:
            return False, str(e)
        if code != 0:
            return False, _ssh_hint(self, out)
        if "missing" in out:
            return False, ("connected to " + self.ssh + ", but there is no directory at "
                           + self.root + " over there")
        return True, "connected to " + self.ssh + " - " + self.root + " is there"


def _ssh_hint(ws, output):
    """An ssh failure turned into something worth reading. The raw text is kept
    on the end, because the specific line ssh printed is usually the answer."""
    text = (output or "").strip()
    low = text.lower()
    if "permission denied" in low or "publickey" in low:
        hint = ("ssh to " + ws.ssh + " was refused. Uniagent never types a password, "
                "so this needs key-based login: run  ssh-copy-id " + ws.ssh
                + "  once from this machine.")
    elif "could not resolve" in low or "name or service not known" in low:
        hint = "cannot find the host " + ws.ssh + " - check the name or use its IP."
    elif "connection refused" in low or "connection timed out" in low or "no route" in low:
        hint = ws.ssh + " is not answering on ssh - is the machine up and reachable?"
    elif "host key verification failed" in low:
        hint = ("the host key for " + ws.ssh + " has changed since it was first seen. "
                "If that is expected, remove its line from ~/.ssh/known_hosts.")
    else:
        hint = "could not reach " + ws.ssh + " over ssh."
    return hint + (("\n" + text) if text else "")


def _control_dir():
    """Somewhere to keep the ssh multiplexing sockets. The runtime dir when
    there is one - it is already per-user and cleaned at logout - and /tmp
    otherwise, which is where a service without a session lands."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
    except OSError:
        base = "/tmp"
    return Path(base)


# --- getting one -----------------------------------------------------------

# Workspaces are looked up per tool call, and .env is read on every lookup so a
# change on the settings page lands on the next turn. The objects themselves
# are cached under everything that defines them, so repeated calls in one turn
# reuse the object rather than rebuilding it - and a config edit produces a
# different key, so nothing stale is ever handed out.
_cache = {}


def get(wsid=None):
    """The Workspace for an id, or the default when the id is None or unknown.

    Never returns None. With no workspaces configured at all this is a local
    workspace rooted at the install folder, which is exactly what the tools did
    before any of this existed."""
    cfg = provider.workspace(wsid)
    key = (cfg["id"], cfg["path"], cfg["ssh"], cfg["port"]) if cfg else None
    if key not in _cache:
        # A config edit makes a new key rather than replacing an old one, so
        # without this the dict would grow by one every time a path is retyped
        # on the settings page. Nothing here is expensive to rebuild.
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = Workspace(cfg)
    return _cache[key]


def describe(wsid=None):
    """One line naming the workspace, for the system prompt. The model has to
    know where it is working - especially that it may not be this machine -
    or it will confidently give you paths from the wrong computer."""
    ws = get(wsid)
    if ws.is_remote:
        return ("Workspace: " + ws.name + " - files and terminal commands run on "
                + ws.ssh + ", rooted at " + ws.root
                + ". Relative paths are on THAT machine, not this one.")
    return "Workspace: " + ws.name + " - rooted at " + ws.root + " on this machine."
