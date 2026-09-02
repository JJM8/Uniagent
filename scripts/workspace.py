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

AND THE KEY HAS TO BE FOUND WITHOUT AN AGENT. This is the part that bites.
Uniagent runs as a background service, and a service does not inherit the
ssh-agent your terminal is talking to - on a desktop Linux session it commonly
inherits a *different*, empty one. So a key that your shell uses without a
thought is invisible here, and ssh falls back to the handful of default
filenames it tries on its own (id_rsa, id_ed25519, ...). A key called anything
else - id_ed25519_josh, work_key - is then never offered at all, and the far
end says "Permission denied (publickey)" for a login that plainly works when
you type it yourself. That is not a broken key, it is a key nobody handed over.

So identities are named explicitly on every command line rather than left to
the agent: the file on the workspace if one is set, otherwise whatever keys are
actually sitting in ~/.ssh. Nothing here depends on the environment the service
happened to start in.
"""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import filecache

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

# The identity filenames ssh already tries by itself. Naming these again would
# only make it attempt the same key twice, so discovery skips them - they are
# the ones that were never the problem.
SSH_DEFAULT_KEYS = ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519",
                    "id_ed25519_sk", "id_xmss", "id_dsa")

# Newest and smallest first when there is a choice, purely so the likeliest key
# is offered before the others.
KEY_PREFERENCE = ("ed25519", "ecdsa", "rsa")

# How many discovered keys are offered at most. Every key offered is an
# authentication attempt, and sshd cuts the connection off after MaxAuthTries
# (6 by default) - so a drawer full of old keys must not be allowed to burn the
# budget before the right one is reached.
MAX_KEYS = 3

# Whether ssh here can multiplex. Win32 OpenSSH has no ControlMaster: the
# feature is built on unix sockets and simply is not implemented, so passing
# ControlPath there produces an error on every single connection rather than a
# faster second one. Windows pays a fresh handshake per call instead, which is
# slower but works - and working is the requirement.
CAN_MULTIPLEX = os.name != "nt"


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
        self.key = str(cfg.get("key") or "").strip()
        self.root = str(cfg.get("path") or INSTALL_ROOT)

    # --- the shape of it ---------------------------------------------------

    @property
    def is_remote(self):
        return bool(self.ssh)

    def identities(self):
        """The private keys this workspace offers, in the order ssh will try
        them. The key named on the workspace if there is one, otherwise what is
        actually in ~/.ssh.

        Named rather than left to an agent on purpose - see the module
        docstring. The service's agent is not your terminal's agent, and a key
        whose filename is not one of ssh's defaults is invisible without
        this."""
        if self.key:
            return [str(Path(self.key).expanduser())]
        return _discovered_keys()

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
            "-o", "ConnectTimeout=10",
            # A first connection to a new machine should work without someone
            # having to go and type "yes" at a prompt nobody can see. The key
            # is still pinned from then on, so a later change still fails loudly.
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if CAN_MULTIPLEX:
            # Connection reuse. %C is ssh's own hash of user/host/port, which
            # keeps the socket path short - a unix socket path is capped near
            # 104 characters, and a workspace name in there would blow it.
            argv += [
                "-o", "ControlMaster=auto",
                "-o", "ControlPath=" + str(_control_dir() / "uniagent-%C"),
                "-o", "ControlPersist=" + str(SSH_PERSIST),
            ]
        # The keys, named outright. A workspace that sets one means it, so
        # IdentitiesOnly keeps the agent's keys out of the way rather than
        # letting them be offered first and spend sshd's attempt budget ahead
        # of the one that was asked for. It does not silence ~/.ssh/config -
        # an IdentityFile configured for this host is still a configured
        # identity and still gets tried, which is the right call: someone who
        # wrote that line meant it too. Discovered keys get no IdentitiesOnly
        # at all; they are a guess, so the agent and ssh's defaults stay
        # welcome alongside them.
        for identity in self.identities():
            argv += ["-i", identity]
        if self.key:
            argv += ["-o", "IdentitiesOnly=yes"]
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
        return Path(target).read_text(errors="replace", encoding="utf-8")

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
        p.write_text(content, encoding="utf-8")
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


def _ssh_dir():
    return Path.home() / ".ssh"


# The discovered key list, kept against the mtime of ~/.ssh so that adding a
# key is picked up on the next call without re-reading the directory on every
# one. Scanning a directory per ssh command would be a silly thing to pay for
# when the answer changes about once a year.
_keys_cache = (None, [])


def _discovered_keys():
    """Private keys sitting in ~/.ssh, best guess first.

    A private key is identified by its public half being next to it, which is
    what ssh-keygen always writes and what avoids mistaking config, known_hosts
    or a stray note for a key. The names ssh already tries by itself are left
    out - they are found with or without us, and naming them again would just
    spend an authentication attempt twice."""
    global _keys_cache
    directory = _ssh_dir()
    try:
        stamp = directory.stat().st_mtime
    except OSError:
        # No ~/.ssh at all. Not an error worth raising here: ssh will say so
        # far more precisely when the connection is actually attempted.
        return []
    if _keys_cache[0] == stamp:
        return _keys_cache[1]
    found = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        entries = []
    for pub in entries:
        if pub.suffix != ".pub":
            continue
        private = pub.with_suffix("")
        if private.name in SSH_DEFAULT_KEYS or not private.is_file():
            continue
        found.append(private)
    found.sort(key=_key_rank)
    keys = [str(p) for p in found[:MAX_KEYS]]
    _keys_cache = (stamp, keys)
    return keys


def _key_rank(path):
    """Sort order for discovered keys: by algorithm, newest family first, then
    by name so the order is at least stable between runs."""
    name = path.name.lower()
    for i, algorithm in enumerate(KEY_PREFERENCE):
        if algorithm in name:
            return (i, name)
    return (len(KEY_PREFERENCE), name)


def _ssh_hint(ws, output):
    """An ssh failure turned into something worth reading. The raw text is kept
    on the end, because the specific line ssh printed is usually the answer."""
    text = (output or "").strip()
    low = text.lower()
    if "permission denied" in low or "publickey" in low:
        offered = ws.identities()
        if offered:
            hint = (ws.ssh + " refused the keys Uniagent offered ("
                    + ", ".join(offered) + "). The public half of one of them has to "
                    "be in ~/.ssh/authorized_keys over there: run  ssh-copy-id -i "
                    + offered[0] + ".pub " + ws.ssh + "  once from this machine.")
        else:
            hint = ("ssh to " + ws.ssh + " was refused, and Uniagent found no key in "
                    "~/.ssh to offer. Make one with  ssh-keygen -t ed25519  and send it "
                    "over with  ssh-copy-id " + ws.ssh + ".")
        # The case that looks like witchcraft and is worth naming outright,
        # because the obvious conclusion - "but it works when I type it!" - is
        # the one that leads nowhere.
        hint += ("\nIf this same login works in your own terminal, the key is only in "
                 "your terminal's ssh-agent, which this service does not share. Set "
                 "the key file on the workspace and it stops depending on that.")
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
    there is one - it is already per-user and cleaned at logout - and the
    system temp directory otherwise, which is where a service without a session
    lands. Asked for rather than hardcoded to /tmp, which on Windows would mean
    creating C:\\tmp; only ever reached where multiplexing exists at all."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
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
    key = ((cfg["id"], cfg["path"], cfg["ssh"], cfg["port"], cfg.get("key", ""))
           if cfg else None)
    if key not in _cache:
        # A config edit makes a new key rather than replacing an old one, so
        # without this the dict would grow by one every time a path is retyped
        # on the settings page. Nothing here is expensive to rebuild.
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = Workspace(cfg)
    return _cache[key]


_described = {}


# Memo for catalogue() below, on the same filecache signature describe() uses.
_catalogued = {}


def catalogue():
    """Every workspace that exists, with its device and path - and NOTHING
    about which one any particular chat is in.

    That omission is the entire point. This text goes into the workspace
    TOOL'S schema description (see tools/workspace_tool.live_description), and
    a tool schema is the first thing on the wire: provider.wire_segments
    renders "the tool schemas first", and the cache breakpoint at the end of
    the system message covers them, so caching is a prefix match starting
    here. Text that varied per chat would give every chat a different prefix
    and cost each of them the whole cached prompt on every turn. The list of
    workspaces is the same for everybody, so it costs nothing to carry and
    changes only when .env does - dynamic in that it is read live, frozen in
    that every chat sees the identical bytes.

    Which workspace a chat is in reaches the model the other way, through the
    conversation rather than the prompt: main.workspace_note writes a turn
    when the user moves it, and a move the model makes comes back as its own
    tool result. A chat that has never moved is in the default workspace,
    which is the machine Uniagent runs on - the safe assumption, and the one
    the model would make anyway."""
    stamp = filecache.signature()
    held = _catalogued.get("text")
    if held is not None and held[0] == stamp:
        return held[1]
    lines = []
    for w in provider.workspaces():
        lines.append("  " + w["id"] + " (" + w["name"] + " - "
                     + (("on " + w["ssh"] + ", ") if w["ssh"] else "")
                     + w["path"] + ")")
    text = ("\n\nTHE WORKSPACES THAT EXIST (a workspace is a device and a "
            "directory; only these exist and you cannot invent one):\n"
            + "\n".join(lines)
            + "\nThis list does NOT say which one this chat is in. You are "
            "told that when it changes - the user moving this chat says so in "
            "the conversation, and a move you make comes back as your own "
            "result - and a chat that has never been moved is in the default "
            "workspace, on the machine running Uniagent. If you are not sure "
            "where you are, call this tool with no id and it will tell you "
            "before you touch a file.")
    _catalogued["text"] = (stamp, text)
    return text


def describe(wsid=None, tools=None):
    """The workspace part of the system prompt: which device and directory this
    chat is working in, which others it can be moved to, and how to move.

    The model has to know where it is working - especially that it may not be
    the machine Uniagent runs on - or it will confidently give you paths from
    the wrong computer.

    THE OTHERS ARE LISTED FOR THE SAME REASON. "Check the logs on the Pi" is
    only actionable if the Pi is a name the model has already been given: a list
    it would have to call a tool to see is a list it never thinks to ask for, so
    it answers from the machine it is on and is wrong without ever knowing there
    was a choice. They cost a line each, and they are what turns "another
    device" from a thing the user has to explain into a thing the model can
    simply do."""
    # This is built into the system prompt on every pass of every turn, and
    # its answer only moves when the workspace list in .env does - which
    # filecache's signature already tracks. Memoised on that rather than on a
    # timer, so an edited workspace is described correctly on the very next
    # turn and an unedited one costs a dict lookup.
    # Keyed on the tool list as well as the workspace, because that list is
    # now part of the sentence: two profiles in the same workspace get two
    # different descriptions, and one memo slot would hand each of them the
    # other's.
    key = (wsid, tuple(tools) if tools is not None else None)
    stamp = filecache.signature()
    held = _described.get(key)
    if held is not None and held[0] == stamp:
        return held[1]

    ws = get(wsid)
    lines = []
    # Which of the place-sensitive tools this chat ACTUALLY has. Passed in
    # rather than looked up, because the module that knows the answer
    # (tool_processor) already imports this one and asking it back would close
    # the loop. None means "don't know" - every caller that has not been
    # taught about profiles - and keeps the original wording.
    place = [t for t in ("read_file", "write_file", "edit_file", "ask_file")
             if tools is None or t in tools]
    has_terminal = tools is None or "terminal" in tools
    # Whether this profile can MOVE, which is a different question from
    # whether it can touch files. The paragraph below tells the model to use
    # the workspace tool by name, and a profile that cannot call it must not
    # be told to - see the "invitation to try them" note at the call site in
    # main.injection_breakdown. The "chat" profile is exactly this case:
    # read_file and three others, no workspace tool, and it was still being
    # handed the move instructions every turn.
    has_move = tools is None or "workspace" in tools
    # Nothing here is about anywhere: no file tool, no terminal, and no way to
    # move. There is no place-sensitive capability left for a location to be
    # the location OF, so the honest output is nothing at all and the caller
    # drops the section. A bare profile gets no workspace line rather than one
    # saying "any tool that touches files works in ..." when it has none.
    if not place and not has_terminal and not has_move:
        _described[key] = (stamp, "")
        return ""
    named = ("the file tool (" if len(place) == 1 else "the file tools (") \
        + ", ".join(place) + ")"
    plural = False
    if place and has_terminal:
        which, plural = named + " and the terminal", True
    elif place:
        which, plural = named, len(place) > 1
    elif has_terminal:
        which, plural = "the terminal", False
    else:
        # No file tool and no terminal, but it can still move - the only way
        # to reach this now. There is no tool here for a location to be the
        # location OF, so the sentence names the place and stops rather than
        # claiming "any tool that touches files works in ..." to a profile
        # that has none.
        which = None
    verb = "work" if plural else "works"
    runs = "RUN ON" if plural else "RUNS ON"
    if which is None:
        lines.append(
            "Workspace: this chat is working in " + ws.name + " - "
            + (ws.ssh + " over ssh, rooted at " if ws.is_remote else "")
            + ws.root + ("." if ws.is_remote
                         else ", on the machine running Uniagent."))
    elif ws.is_remote:
        lines.append(
            "Workspace: this chat is working in " + ws.name + " - " + which
            + " " + runs + " "
            + ws.ssh + " over ssh, rooted at " + ws.root + ". Relative paths, and "
            "anything the terminal does, are on THAT device - not on the machine "
            "running Uniagent, and Uniagent's own folder (memories/, context/, "
            "skills/, tools/) is not there either.")
    else:
        lines.append(
            "Workspace: this chat is working in " + ws.name + " - " + which
            + " " + verb + " in "
            + ws.root + ", on the machine running Uniagent. Relative paths are "
            "resolved from there.")
    others = [w for w in provider.workspaces() if w["id"] != ws.id]
    # Only if this profile can actually make the move - see has_move above.
    if others and has_move:
        lines.append(
            "Other workspaces this chat can be moved to: "
            + "; ".join(w["id"] + " (" + w["name"] + " - "
                        + (("on " + w["ssh"] + ", ") if w["ssh"] else "")
                        + w["path"] + ")" for w in others)
            + ". A workspace is a device and a directory: moving to one is how you "
            "work somewhere else. When the user talks about another device they "
            "have - its files, its logs, running something on it - move this chat "
            "to that device's workspace with the workspace tool (its id as the "
            "`id` argument) and carry on there, rather than answering from "
            "where you happen to be. Only these exist; you cannot invent one.")
    text = "\n".join(lines)
    _described[key] = (stamp, text)
    return text
