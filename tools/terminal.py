"""A real terminal the agent keeps open, one per chat.

Every chat - the main one, a cron job, a subagent - gets its own bash that
stays alive between tool calls, so `cd`, `export` and `source venv/bin/activate`
carry over from one command to the next the way they would for a person. It
runs behind a pty (a fake terminal), which is what makes sudo, y/N prompts and
REPLs work: programs that insist on a real terminal get one, and the agent can
write back into a command that is still sitting there waiting.

chat_id comes from tool_processor, never from the model. Without one there is
no chat to own a terminal, so we fall back to the old behaviour: a throwaway
subprocess.run, exactly as this tool worked before.

WINDOWS: the same idea, with the same class interface, over PowerShell instead
of bash. Windows has real pseudo-terminals too (ConPTY), they are just not in
Python's standard library - pywinpty is the binding. pty/termios/fcntl are
POSIX-only and are imported only where they exist: at module level they raised
ModuleNotFoundError on Windows, which made tool_processor list this whole tool
as BROKEN, so the agent had no shell at all there.
"""

import atexit
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

WINDOWS = os.name == "nt"

if WINDOWS:
    import shutil
else:
    import fcntl
    import pty
    import select
    import struct
    import termios

NAME = "terminal"
DESCRIPTION = ("Really run any shell command on the user's computer - read or change files, "
               "install things, launch apps and windows, run programs. Not just read-only. "
               "The terminal stays open, so cd/exports stick and you can answer prompts "
               "like sudo passwords.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "terminal". Do not explain what the command does first, and do not ask the user to approve it - they are asked automatically. You will be shown the output afterwards, and THAT is when you explain it.

Arguments (all optional - see below for when to use which):
- command:    the shell command(s) to run. Several commands can go in one
              string, e.g. "free -h && ps aux --sort=-%mem | head". Leave it
              out, or send it empty (""), to WAIT ON and read whatever is
              already running - see below.
- timeout:    seconds to wait before reporting the command as STILL RUNNING
              instead of giving up on it. Defaults to 60, maximum 600.
- input:      text to send to a command that is sitting there waiting for an
              answer - a password, a y/N, a line for a REPL. Send the single
              Ctrl-C control character if something is stuck.
- background: true to run `command` in the background instead of the normal
              terminal - for anything that keeps printing (servers, builds,
              installs, tail -f). Defaults to false.
- reset:      true to throw this whole terminal away and open a fresh one -
              clean shell, clean directory. Defaults to false.

WHAT THIS TOOL ACTUALLY DOES: These commands REALLY RUN on the user's real computer. This is not a simulation and it is not read-only. Commands do not just print text back - they CHANGE THE MACHINE. You can install packages, create/edit/delete files, launch GUI applications, start background processes, change settings, and anything else a person sitting at that computer could do in a terminal.

So if the user asks you to DO something on their machine, you almost certainly CAN. Do not say "I can only run shell commands, I can't do that" - opening windows, launching apps and changing files ARE shell commands. Work out the command and run it. Only say you can't if there is genuinely no way to do it via a shell.

IT IS ONE TERMINAL AND IT STAYS OPEN: This is not a fresh shell each time - it is the SAME terminal for this whole conversation, so state carries over exactly like a real one: `cd` once and every command after it runs from there; `export`, `source venv/bin/activate` and shell variables all stick too. You are ALREADY in that directory. Re-running `cd` to a place you have already been is pure waste - it burns tokens and changes nothing - so NEVER do it, and never wrap every command in "cd x && ..." out of habit. If you have lost track of where you are, run `pwd` once and move on.

WHEN A COMMAND IS STILL RUNNING: Slow things (builds, apt, downloads, big copies) are NOT killed at the timeout. You get what has been printed so far, plus "STILL RUNNING". The command is fine and is carrying on. To see more, call again with no `command` (left out, or "") and a `timeout` - that is a real WAIT: it sits there for the whole timeout and comes back the moment the command finishes, or the moment it stops at a question. A long quiet patch is NOT a reason for it to return early, so one "timeout": 300 wait is the right way to see out a slow scan - not five short ones in a row. Or wait longer up front with a bigger `timeout` on the command itself.

A command that stops to ASK you something comes back marked "WAITING FOR INPUT" instead, straight away rather than at the timeout. That one is not working away in the background - it is parked on the question shown on the last line and will sit there forever until you answer it, so do NOT keep reading it and do NOT run the command again. Answer it, as below.

ANSWERING PROMPTS - sudo passwords, y/N, REPLs: Because the terminal stays open, a command that stops to ask a question is still sitting there waiting for the answer. Send it with `input` - e.g. run "sudo apt update", see "[sudo] password for the user:" and WAITING FOR INPUT, then call again with `input` set to the password the user gave you.
NEVER guess, invent or brute-force a password. If something asks for one and the user has not given it to you, say so and ask them - do not try candidates. `input` answers anything, not just passwords: "y" for a confirmation, a line of Python for a REPL you started, "q" to get out of a pager.

LAUNCHING APPS AND WINDOWS - put "&" on the end of `command`, e.g. "gnome-terminal &". This is real bash, so "&" backgrounds it and hands the terminal straight back. Use it for anything with a window - browsers, editors, games - so it does not sit there holding the terminal. The app IS open: do not run it a second time and do not go hunting for proof it worked.

ANYTHING THAT KEEPS PRINTING - use `background` instead of "&": servers, builds, installs, training runs, tail -f - anything that goes on printing while it runs. Do NOT put those in the normal terminal, with or without "&": their output would pour into the middle of whatever you run next, and they seize up once the terminal fills.
You get back a pid and a FILE its output is going to. It runs in this same terminal, so it keeps the directory you cd'd to and any venv you activated, but nothing it prints ever comes back here. Read it whenever you want with a plain `command` like "tail -n 50 <that file>". Stop it with `kill <pid>`. If you have lost the file, every background job this conversation started is in one place, newest first: "ls -t <BGDIR>/*/".

READING THE RESULT HONESTLY: Exit code 0 always means the command succeeded. A non-zero exit code means whatever that particular program decided it means - it is NOT proof of an error. grep, pkill, diff and test all return 1 to mean "nothing matched", which is an ANSWER, not a failure. Read it for what it is and tell the user the truth: "nothing was running" is a perfectly good result.

DO NOT pad commands to manufacture a success message. Never append things like "; echo 'Done!'" to the end of a command. That forces the exit code to 0 and prints a message whether or not the real command did anything, so it hides the truth from you and makes you tell the user you did something you did not do. Run the bare command and report what actually came back.

If the user refuses you get back a string starting with "DENIED" and nothing was executed - do not retry the same command, ask them what they would prefer."""

# For native provider tool-calling (models_custom.json "tool_syntax": "native")
# - a standalone JSON Schema, usable directly as OpenAI's function.parameters
# or Anthropic's input_schema. chat_id is deliberately absent: it's injected
# by tool_processor.process(), never supplied by the model - see the module
# docstring above.
# Runs on its own, never alongside another tool call in the same batch.
# There is one shell per chat and it is a real terminal, not a queue.
# Two commands running in it at once would write into each other's
# output and neither would be read back correctly - and `cd`, `export`
# and a half-finished prompt all mean the ORDER commands ran in.
# See tool_processor.parallel_safe().
PARALLEL = False

SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description":
            "The shell command(s) to run. Leave out, or send empty (\"\"), to "
            "wait on and read whatever is already running - that call blocks "
            "for the whole timeout and returns as soon as the command "
            "finishes or stops at a question. To save tokens and the users time, attempt to run multiple commands in one string, e.g. \"free -h && ps aux --sort=-%mem | head\"."},
        "timeout": {"type": "number", "description":
            "Seconds to wait before reporting STILL RUNNING instead of "
            "giving up. Defaults to 60, capped at 600."},
        "input": {"type": "string", "description":
            "Text to send to a command sitting there waiting for an answer "
            "- a password, a y/N, a line for a REPL. Send the single Ctrl-C "
            "control character if something is stuck."},
        "background": {"type": "boolean", "description":
            "True to run `command` in the background instead of the normal "
            "terminal - for anything that keeps printing (servers, builds, "
            "installs, tail -f). Defaults to false."},
        "reset": {"type": "boolean", "description":
            "True to throw this whole terminal away and open a fresh one - "
            "clean shell, clean directory. Defaults to false."},
    },
    "required": [],
}


# The live terminals, one per chat id. Read back out of globals() rather than
# just assigned, because tool_processor reloads every tool on EVERY turn - a
# plain "= {}" would re-run and wipe this, losing the sessions and leaking the
# bash processes behind them. On the first import there is nothing to find, so
# it starts empty, which is what we want.
_SESSIONS = globals().get("_SESSIONS", {})

# How long a terminal may sit untouched before it is closed. One nobody has
# used in half an hour is worth less than the process it holds open.
IDLE_LIMIT = 30 * 60

DEFAULT_TIMEOUT = 60
# A wait really does block the whole turn now (see PROMPT_ONLY), so an absurd
# timeout would wedge the conversation rather than merely being ignored.
MAX_TIMEOUT = 600

# watch_prompts, third setting. False is a setup read that must swallow its
# marker; True is a command being run, where a mid-line silence is worth
# reporting; PROMPT_ONLY is an explicit WAIT - the model asked for the whole
# timeout, so silence means nothing and only a real event ends it early.
PROMPT_ONLY = "prompt-only"
# How often a PROMPT_ONLY wait looks up from select() to check the terminal is
# still there. Only reason it isn't one long sleep: a reset (or a shell that
# died) has to end the wait, and closing a fd underneath a blocked select does
# not reliably wake it.
WAIT_TICK = 1.0

# Set in every shell this tool opens, and re-asserted after the user's own
# startup files have had their say (see _source_user_env). LESS is there for
# the times something asks for less BY NAME rather than through $PAGER: -F
# quits straight away if the text fits one screen, and -X stops it wiping that
# screen on the way out, so the output stays in the transcript.
_NO_PAGER = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "SYSTEMD_PAGER": "cat",
    "MANPAGER": "cat",
    "GH_PAGER": "cat",
    "LESS": "-FRX",
}
MAX_OUTPUT = 8000  # characters handed back; the middle of a flood is dropped

# Where a background job's output goes. It gets a file of its own rather than
# sharing the terminal, because a job that prints as it goes would otherwise
# land in the middle of whatever command runs next - and worse, stall, since a
# terminal only holds about 64KB before the writer blocks waiting to be read.
# A file never fills up, so the job runs at full speed and the output keeps.
BG_DIR = Path(__file__).parent.parent / "background-terminal-output"

# The instructions above are written for bash. On Windows the shell is
# PowerShell, and a model told to use bash syntax writes commands that fail -
# so the handful of shell-specific sentences are swapped out. Only these lines
# differ; everything else about the tool behaves the same on both. This runs
# BEFORE <BGDIR> is filled in, so the patterns can still match on it.
if WINDOWS:
    for _bash, _ps in [
        ('`cd` once and every command after it runs from there; `export`, '
         '`source venv/bin/activate` and shell variables all stick too.',
         '`cd` once and every command after it runs from there; `$env:VAR`, '
         '`.\\.venv\\Scripts\\Activate.ps1` and variables all stick too.'),
        ('LAUNCHING APPS AND WINDOWS - put "&" on the end of `command`, e.g. '
         '"gnome-terminal &". This is real bash, so "&" backgrounds it and hands '
         'the terminal straight back.',
         'LAUNCHING APPS AND WINDOWS - use Start-Process, e.g. "Start-Process '
         'notepad". This is real PowerShell, and Start-Process hands the terminal '
         'straight back instead of waiting for the app to close.'),
        ('ANYTHING THAT KEEPS PRINTING - use `background` instead of "&":',
         'ANYTHING THAT KEEPS PRINTING - use `background` instead of Start-Process:'),
        ('Do NOT put those in the normal terminal, with or without "&":',
         'Do NOT put those in the normal terminal:'),
        ('a plain `command` like "tail -n 50 <that file>". Stop it with `kill <pid>`.',
         'a plain `command` like "Get-Content -Tail 50 <that file>". Stop it with '
         '`Stop-Process -Id <pid>`.'),
        ('every background job this conversation started is in one place, newest '
         'first: "ls -t <BGDIR>/*/".',
         'every background job this conversation started is in one place, newest '
         'first: "Get-ChildItem <BGDIR> | Sort-Object LastWriteTime -Descending".'),
        ('grep, pkill, diff and test all return 1 to mean "nothing matched"',
         'Select-String and findstr return 1 to mean "nothing matched"'),
        ('run "sudo apt update", see "[sudo] password for the user:" and WAITING '
         'FOR INPUT, then call again with `input` set to the password the user gave you.',
         'run something that stops to ask a question, see WAITING FOR INPUT, then '
         'call again with `input` set to the answer.'),
    ]:
        INSTRUCTIONS = INSTRUCTIONS.replace(_bash, _ps)

# Spelled out in full in the instructions: a relative path breaks the moment
# the model has cd'd somewhere else, which it will have.
INSTRUCTIONS = INSTRUCTIONS.replace("<BGDIR>", str(BG_DIR))

# Colour codes, cursor moves and bare carriage returns (progress bars). Even
# with TERM=dumb some programs emit them, and they are noise in a transcript
# the model has to read.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r(?!\n)")

# Questions that stop a command dead until somebody types an answer.
#
# Why this table exists: bash only prints the marker when a command FINISHES,
# so a command sitting at a password prompt looks exactly like a slow one - and
# we used to sit there for the whole timeout before handing back a prompt that
# had been on screen since the first 50ms. A minute of nothing, every single
# sudo. Matching the prompt itself is what turns that minute into a moment.
#
# Matched against the LAST line only and anchored to the end of it (see
# _quiet_gap), so a line that merely mentions a password somewhere in the
# middle is not a prompt. Case-insensitive, so [Y/n] and [y/N] both come from
# the one pattern. Keep them ending where the real prompt ends - no "$", the
# anchor is added once around the whole list.
PROMPT_PATTERNS = [
    r"\[sudo\] password for [^:\n]*:",              # sudo, the one that hurts
    r"password[^:\n]*:",                            # ssh, su, mysql, gpg, most others
    r"(passphrase|pass phrase)[^:\n]*:",             # ssh keys
    r"(username|user name|login|account)[^:\n]*:",   # logins
    r"enter [^:\n]*:",                               # "Enter something:", installers
    r"\[y/n[^\]]*\]|\(y/n\)|\[yes/no\]|\(yes/no\)",  # apt, dpkg, rm -i, ssh's yes/no
    r"\?",                                          # any question: rm -i, "Are you sure?"
    r"press (\[?enter\]?|return|any key)[^\n]*",     # pagers, installers
    r"^:|\(END\)",                                   # less/more sitting on a page
    r">>>|In \[\d+\]:|\(gdb\)|\(pdb\)",            # python, ipython, debuggers
    r"(mysql|sqlite|ftp|sftp|redis[^>\n]*)>",        # database and transfer REPLs
    r"irb\([^)]*\)[^>\n]*>",                        # ruby
]

# One regex from the table: any pattern, at the very end of the line, with
# trailing spaces allowed because nearly every prompt has one after the colon.
_PROMPT_RE = re.compile("(?:" + "|".join(PROMPT_PATTERNS) + r")[ \t]*$", re.I)

# How long the output has to have STOPPED before silence means "waiting for an
# answer" rather than "still working". Two speeds, because the two signals are
# not equally trustworthy: a matched prompt is near enough proof and only needs
# long enough to be sure no more output is on its way, while an unfinished line
# on its own is a guess and is given time to turn out to be a slow program
# mid-sentence.
PROMPT_QUIET = 0.3
IDLE_QUIET = 3.0
# The third and weakest reading: it printed something, ended the line tidily,
# matched nothing in the table, and then said nothing whatsoever. Usually that
# is a slow step doing its work - which is why this wait is five times the
# others - but it is also every stuck thing the table has never heard of: a
# pager showing a full screen, a REPL that opened with a banner, an installer
# asking in words nobody predicted. Before this there was no reading at all
# for that shape, so the answer was the entire timeout and then a guess.
SILENT_QUIET = 15.0


def _quiet_gap(text):
    """(how long a silence must last for `text` to mean "waiting", what kind of
    guess that is) - or (None, None) if this output cannot mean it however long
    it goes quiet.

    Nothing printed yet is None: a command that has said nothing is just slow
    (`sleep 30` prints nothing either), and there is no prompt to answer.
    Otherwise it comes down to the last line - a known prompt ("prompt", near
    enough certain), or at least an unfinished one ("idle", a guess: a program
    stopped mid-line with the cursor after it is USUALLY waiting on somebody,
    but it might equally be "Building... " with five seconds of work to go).
    The two are kept apart all the way to the wording the model is given, so a
    guess is never handed over as a certainty."""
    if not text:
        return None, None
    clean = _ANSI.sub("", text.replace("\r\n", "\n"))
    lines = [ln for ln in clean.split("\n") if ln.strip()]
    tail = lines[-1].rstrip() if lines else ""
    # Both signals matter, and they multiply. A real prompt leaves the cursor
    # sitting on the line it asked on - that is what a prompt IS - so a pattern
    # that matches a line the program has already finished and moved on from is
    # much weaker evidence than the same pattern with the cursor still on it.
    # Requiring both for "prompt" is what lets the table above be as broad as a
    # bare "?" without a passing "Did you mean x?" in a build log being
    # answered as though it were a question.
    unfinished = not clean.endswith("\n")
    matched = bool(tail and _PROMPT_RE.search(tail))
    if matched and unfinished:
        return PROMPT_QUIET, "prompt"
    if matched or unfinished:
        # One signal, not both: it might be waiting, it might be working. Worth
        # coming back for, not worth claiming anything about. This is also what
        # catches the prompt written the other way round - `echo "Continue?
        # [y/n]"; read a` - where the question ends in a newline and only the
        # pattern is left to go on.
        return IDLE_QUIET, "idle"
    # Nothing to go on but the silence itself, so it is given the longest wait
    # and handed over with the same careful wording as any other guess.
    return SILENT_QUIET, "idle"


class _PosixTerminal:
    """One bash, kept open, talked to through a pty.

    With a remote workspace the bash is on the other machine and the pty holds
    an ssh session instead - same marker, same reading, same everything below.
    That is deliberate: a remote terminal you can `cd` in, run sudo in and
    leave a REPL sitting in is worth having, and a second implementation of all
    this for the remote case would be two things to keep in step."""

    def __init__(self, workspace=None):
        # openpty() gives a linked pair: the slave IS the terminal as far as
        # bash is concerned, the master is our end to read and write. This is
        # the part that makes sudo and REPLs work - they get a real terminal.
        master, slave = pty.openpty()

        # A pty echoes back everything written to it, which would put a copy of
        # every command in its own output. Turn that off at the source rather
        # than trying to strip it out afterwards.
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO  # index 3 is lflags
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        # Claim a wide window, or anything that formats to the terminal width
        # wraps at 80 columns and the output is a mess to read.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))

        # How we know a command has finished, given bash itself never exits: we
        # get bash to print a marker every time it is ready for the next
        # command, with $? for how the last one went.
        #
        # It has to come from bash rather than from a line we write after the
        # command, because a command that reads stdin - sudo, `read`, a REPL -
        # would eat that line as its own input. Ask bash for a password and it
        # would be handed our sentinel.
        #
        # PROMPT_COMMAND rather than PS1, because PS1 is not ours: `source
        # venv/bin/activate` prepends "(venv) " to it, and conda and plenty of
        # prompt themes rewrite it outright. That silently mangles the marker
        # and every result afterwards is the PREVIOUS command's output. Nothing
        # in normal use touches PROMPT_COMMAND - but a user's .bashrc is
        # exactly the kind of thing that might, which is why we re-assert it
        # after sourcing their files in _source_user_env below.
        #
        # Random per terminal so a command that PRINTS the marker (a grep over
        # this very file, say) can't be mistaken for the marker itself.
        self.mark = "__UNI_" + uuid.uuid4().hex + "__"
        prompt_cmd = 'printf "\\n%s%s\\n" "' + self.mark + '" "$?"; PS1=""'

        env = dict(os.environ)
        env["TERM"] = "dumb"  # ask politely for no colour codes
        # No pagers. `git log`, `systemctl status`, `man`, `gh` and friends
        # pipe THEMSELVES into `less` the moment they see a terminal - and
        # ours always is one, which is the entire point of the pty. A person
        # in a gnome-terminal sees the pager and presses q; from here it is a
        # command that prints nothing, never finishes and never prints the
        # marker, so the whole timeout goes by on a `git log` that would have
        # taken 30ms. This was the bug: not the finish detection, which works,
        # but a shell that had been taken over by less and was never going to
        # reach its next prompt at all. `cat` is what these same programs use
        # when they are piped into something, which is what we are really
        # doing with their output.
        env.update(_NO_PAGER)
        # Blanking PS1 here, every prompt, rather than only at startup: a venv
        # or conda sets it whenever it is activated, and that prefix would then
        # be printed after our marker and read as the head of the NEXT
        # command's output ("(venv) hello" instead of "hello").
        env["PROMPT_COMMAND"] = prompt_cmd
        env["PS1"] = ""
        env["PS2"] = ""       # continuation lines add nothing to read

        # -i so bash prints that prompt at all. --noediting turns off readline,
        # which would otherwise echo back and redraw everything we type.
        # --norc/--noprofile keep the user's startup files OUT of the initial
        # prompt: they are sourced deliberately, and under control, as the
        # first command (see _source_user_env) rather than at bash startup.
        # That way anything they print is swallowed with the startup output,
        # and anything they set that would break the marker is undone
        # immediately afterwards. start_new_session puts bash in its own
        # session, so a Ctrl-C in the real terminal running the agent doesn't
        # also kill every chat's shell.
        self.workspace = workspace
        if workspace is not None and workspace.is_remote:
            # Over ssh the local `env=` never arrives - sshd builds the remote
            # environment itself and only forwards what its AcceptEnv allows,
            # which by default is nothing that matters here. So the marker
            # variables are set ON the remote command line instead, where they
            # are part of what bash is started with.
            #
            # -tt forces a terminal on the far side even though our own stdin
            # is already a pty. Without it the remote bash gets a pipe, and
            # sudo, ssh-agent prompts and every REPL stop working - which is
            # the entire reason this class uses a pty in the first place.
            remote = ("PROMPT_COMMAND=" + shlex.quote(prompt_cmd)
                      + " PS1= PS2= TERM=dumb "
                      + " ".join(k + "=" + shlex.quote(v)
                                 for k, v in _NO_PAGER.items())
                      + " bash --norc --noprofile --noediting -i")
            argv = list(workspace.ssh_argv("-tt", command=remote))
        else:
            argv = ["bash", "--norc", "--noprofile", "--noediting", "-i"]
        self.proc = subprocess.Popen(
            argv,
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
        os.close(slave)  # bash holds its own copy; ours would hold back EOF
        self.master = master
        self.busy = False  # is a command still running in here?
        self.closed = False  # set by close(), so a wait in flight can stop
        self.jobs = 0      # background jobs started here, for naming their logs
        self.used = time.monotonic()

        # Swallow the prompt bash prints the moment it starts, so it isn't read
        # as the first command's result.
        self.read_until(self.mark, time.monotonic() + 5)

        # The far side's pty does its own echoing, so without this every
        # command comes back in its own output - the exact thing ECHO was
        # turned off for locally, arriving from the other end of the wire.
        if self.workspace is not None and self.workspace.is_remote:
            self.write("stty -echo\n")
            self.read_until(self.mark, time.monotonic() + 10)

        # Behave like the user's own terminal: bring in ~/.profile and
        # ~/.bashrc so PATH, aliases and exports are the ones the user actually
        # lives with (this is what puts ~/.local/bin - edagent, hfetch - on
        # PATH). Done as a command rather than at bash startup so we control
        # it: the files' stdout lands in the swallow above, stderr is silenced,
        # stdin is /dev/null so a stray `read` can't hang the shell, and the
        # marker machinery is re-asserted afterwards in case either file
        # touched PROMPT_COMMAND or PS1.
        self._source_user_env(prompt_cmd)

        # Start where the work is. Without this a local terminal opens in
        # scripts/ - the server's own directory, which is nobody's idea of
        # where their project lives - and a remote one opens in the far
        # account's home rather than the workspace it was configured with.
        if self.workspace is not None:
            self.write("cd " + shlex.quote(self.workspace.root) + " 2>/dev/null\n")
            self.read_until(self.mark, time.monotonic() + 10)

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        self.closed = True
        # Kill the whole SESSION, not just bash's process group. With job
        # control on, every background job gets a process group of its own, so
        # killpg would take out bash and leave `npm run dev` running forever -
        # it really does, it was left orphaned in testing. bash was started
        # with start_new_session, which makes it the session leader, so its pid
        # is the session id and this reaches everything it ever started.
        try:
            subprocess.run(["pkill", "-9", "-s", str(self.proc.pid)], timeout=5)
        except Exception:
            pass
        try:
            self.proc.kill()  # in case pkill isn't installed
        except OSError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass

    def write(self, text):
        os.write(self.master, text.encode())

    def read_until(self, marker, deadline, watch_prompts=False):
        """Everything printed up to `marker`, or up to the deadline. Returns
        (text, done, waiting): done False means the command is still going and
        this is only what it has printed so far; waiting means it has stopped
        at a question and there is no point waiting any longer.

        watch_prompts is off by default so the reads that set this terminal up
        keep the old behaviour exactly - they MUST swallow their marker, and a
        read that gives up early would leave it to be read as the head of the
        first real command's output. PROMPT_ONLY is the explicit wait: see the
        note where the gap is thrown away below."""
        out = ""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return out, False, None
            # How long a silence would mean this has stopped to ask something.
            # None when the output so far cannot mean that, in which case there
            # is nothing to wake up early for.
            gap, kind = _quiet_gap(out) if watch_prompts else (None, None)
            if watch_prompts == PROMPT_ONLY and kind != "prompt":
                # An explicit wait, and this is only the guess that silence
                # MIGHT mean a question. Coming back on it is what made a
                # "timeout": 300 return in fifteen seconds while a disk scan
                # was quietly working - so silence ends nothing here. A matched
                # prompt still does: that one is parked forever and waiting out
                # the rest of the timeout would buy nothing.
                gap, kind = None, None
            # select waits for the terminal to have something to say, with a
            # deadline - which is what lets us give up on WAITING without
            # giving up on the command. It carries on running either way. The
            # wait is cut to `gap` so silence is noticed when it happens rather
            # than slept through: one long select would not come back until the
            # timeout, which is the whole bug.
            wait = left if gap is None else min(left, gap)
            if watch_prompts == PROMPT_ONLY:
                # Not impatience - see WAIT_TICK. The loop below tells the tick
                # apart from the deadline and carries on.
                wait = min(wait, WAIT_TICK)
            ready, _, _ = select.select([self.master], [], [], wait)
            if not ready:
                # A select that timed out IS the silence: it can only return
                # this way after `wait` seconds with nothing said, and any
                # output at all would have come back ready and reset the clock
                # on the next time round. So the silence counts only if we
                # waited the whole `gap` for it - with a `timeout` shorter than
                # that, what just arrived was the deadline, and calling that a
                # prompt would be inventing quiet we never actually watched.
                if gap is not None and wait >= gap:
                    return out, False, kind
                if watch_prompts == PROMPT_ONLY and left > wait:
                    # A tick, not the deadline. The only things that end a wait
                    # early are the terminal going away under it - a "reset"
                    # from elsewhere closes it, a killed shell stops being
                    # alive - and either way there is nothing more coming.
                    if self.closed or not self.alive():
                        return out, True, "gone"
                    continue
                return out, False, None
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return out, True, None  # terminal went away, nothing more is coming
            if not chunk:
                return out, True, None
            out += chunk.decode("utf-8", "replace")
            if marker and marker in out:
                return out, True, None

    def drain(self):
        """Whatever has piled up since we last looked, without waiting.

        A timeout of 0 asks select "is there anything right now?" and returns
        either way, so this never blocks - it can't go through read_until,
        whose deadline would already have passed and which would read nothing
        at all."""
        out = ""
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                return out
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return out
            if not chunk:
                return out
            out += chunk.decode("utf-8", "replace")

    def _source_user_env(self, prompt_cmd):
        """Bring the user's own shell environment into this terminal.

        Runs as the first command, once, when the terminal opens: source
        ~/.profile then ~/.bashrc, then put the marker machinery back the way
        it was in case either file set PROMPT_COMMAND or PS1. stdin comes from
        /dev/null so a stray `read` in one of the files can't leave the shell
        sitting there; stderr is silenced so warnings don't pollute the first
        command's output; anything they print to stdout is swallowed by the
        read that follows this write."""
        bootstrap = (
            "[ -f ~/.profile ] && source ~/.profile </dev/null 2>/dev/null; "
            "[ -f ~/.bashrc ] && source ~/.bashrc </dev/null 2>/dev/null; "
            "PROMPT_COMMAND=" + shlex.quote(prompt_cmd) + "; PS1=''; PS2=''; "
            # Same reason PROMPT_COMMAND is set again here: a .bashrc that
            # sets PAGER=less, or an alias for git that adds one back, would
            # otherwise undo the whole point of _NO_PAGER above.
            + "export " + " ".join(k + "=" + shlex.quote(v)
                                   for k, v in _NO_PAGER.items()) + "; true"
        )
        self.write(bootstrap + "\n")
        self.read_until(self.mark, time.monotonic() + 5)


def _shell_exe():
    """The PowerShell to run on Windows: 7 if it is installed, else the 5.1
    that ships with every copy of Windows. One answer for both the persistent
    terminal and the throwaway one, so a command means the same thing in each.
    """
    return (shutil.which("pwsh") or shutil.which("powershell")
            or "powershell.exe")


def _ps_quote(text):
    """One PowerShell single-quoted string. PowerShell escapes a quote inside
    single quotes by doubling it, and does nothing else in there - no backslash
    escapes, no variable expansion - which is exactly what we want for a path
    or a command we are passing through untouched."""
    return "'" + str(text).replace("'", "''") + "'"


class _WindowsTerminal:
    """One PowerShell, kept open, talked to through a ConPTY.

    The same shape as _PosixTerminal - write/read_until/drain/alive/close - so
    everything below this point treats the two identically. The differences are
    all forced by the platform:

    - ConPTY instead of pty. Windows has had real pseudo-consoles since Windows
      10 1809; pywinpty is the binding, since Python's stdlib has none.
    - A reader thread instead of select(). On Windows select() only accepts
      sockets, never pipes or console handles, and pywinpty's read() blocks. So
      one daemon thread reads forever into a buffer and read_until watches the
      buffer, which gives the same "wait with a deadline" behaviour.
    - The `prompt` function instead of PROMPT_COMMAND. It is PowerShell's
      equivalent hook - it runs before each prompt is shown - so the marker
      comes from the shell itself rather than from a line we write after the
      command, for the same reason spelled out in _PosixTerminal: a command
      sitting on Read-Host would eat that line as its own input.
    """

    def __init__(self, workspace=None):
        try:
            import winpty
        except ImportError as e:  # pragma: no cover - Windows-only path
            raise RuntimeError(
                "The terminal needs pywinpty on Windows (it is the binding for "
                "ConPTY, the Windows pseudo-console). Install it with:  "
                "pip install pywinpty"
            ) from e

        self.workspace = workspace
        self.mark = "__UNI_" + uuid.uuid4().hex + "__"

        shell = self.shell = _shell_exe()

        env = dict(os.environ)
        env["TERM"] = "dumb"
        env.update(_NO_PAGER)   # see _NO_PAGER: git pages on Windows as well

        # -NoLogo: no banner to swallow. -NoProfile for the same reason bash
        # gets --norc/--noprofile: the user's profile is sourced deliberately
        # below, after the marker is in place, so nothing it prints or sets can
        # break the very first result.
        self.proc = winpty.PtyProcess.spawn(
            [shell, "-NoLogo", "-NoProfile"],
            dimensions=(50, 200),   # same wide window as the pty gets
            env=env,
        )

        self._buf = ""
        self._lock = threading.Lock()
        self._echo = ""
        self.busy = False
        self.closed = False  # set by close(), so a wait in flight can stop
        self.jobs = 0
        self.used = time.monotonic()

        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

        # Swallow the startup banner/prompt, then take the shell over.
        self.read_until(None, time.monotonic() + 3)
        self._setup()

    # --- the bits that differ from POSIX ---------------------------------

    def _pump(self):
        """Read forever into the buffer. A thread, because pywinpty's read()
        blocks and there is no select() on Windows to wait on instead."""
        while True:
            try:
                data = self.proc.read(65536)
            except Exception:
                return          # EOFError when the console closes, and friends
            if data:
                with self._lock:
                    self._buf += data
            elif not self.proc.isalive():
                return
            else:
                time.sleep(0.01)  # alive but quiet - don't spin the CPU

    def _setup(self):
        """Put the marker in place and make PowerShell behave like a pipe.

        Remove-Module PSReadLine is the counterpart of bash's --noediting: left
        loaded it redraws and colours the line as it is 'typed', which lands in
        the output as gibberish. ProgressPreference kills the progress bars that
        would otherwise repaint over everything.

        $? has to be read before anything else in the prompt function, because
        every statement resets it. $LASTEXITCODE is the exit code of the last
        native .exe and is null until one has run, so the two are combined: a
        cmdlet that failed has $? false and no exit code, which is a 1.

        THE MARKER IS BUILT FROM TWO HALVES AND NEVER TYPED WHOLE. A console
        echoes back everything written to it and, unlike a pty, there is no
        ECHO flag to turn off. Writing the marker literally meant this very
        line came straight back with the marker in it - read_until matched THAT
        instead of the prompt, _unecho then stripped the line it had matched,
        and the read returned empty while the real prompt marker stayed in the
        buffer. Every command afterwards read the previous command's marker:
        the first two results came back as "(the command ran successfully but
        printed nothing)" and every one after that was the output of the
        command before it. Split in half, no line ever contains the whole
        string, so the only thing that can match is the prompt itself.
        """
        head, tail = self.mark[:10], self.mark[10:]
        # $global: so the prompt function can still see it - a function reads
        # the enclosing scope, but a profile that runs in its own scope would
        # otherwise be able to shadow it.
        prompt = (
            "function prompt { $o=$?; $c=$LASTEXITCODE; "
            "if ($null -eq $c) { $c = 0 }; "
            "if (-not $o -and $c -eq 0) { $c = 1 }; "
            '"`n$($global:UNIMARK)$c`n" }'
        )
        self.write("Remove-Module PSReadLine -ErrorAction SilentlyContinue; "
                   "$ProgressPreference='SilentlyContinue'; "
                   "$global:UNIMARK = '" + head + "' + '" + tail + "'; "
                   + prompt + "\n")
        self.read_until(self.mark, time.monotonic() + 5)

        # Now the user's own profile, the way _source_user_env does on POSIX,
        # then the prompt again in case the profile replaced it.
        self.write("if (Test-Path $PROFILE) { . $PROFILE }; " + prompt + "\n")
        self.read_until(self.mark, time.monotonic() + 10)

        # Start where the work is, the same as the POSIX terminal does - a
        # workspace that doesn't set the shell's directory isn't a workspace.
        if self.workspace is not None:
            self.write("Set-Location -LiteralPath "
                       + _ps_quote(self.workspace.root) + "\n")
            self.read_until(self.mark, time.monotonic() + 10)

    # --- the shared interface --------------------------------------------

    def alive(self):
        return self.proc.isalive()

    def write(self, text):
        # Remembered so read_until can drop the copy ConPTY echoes back. A real
        # console echoes what is typed at it and, unlike a pty, there is no
        # ECHO flag to turn off - so it comes off the output instead.
        self._echo = text.strip()
        # ConPTY/PSReadLine only treats CR as Enter - a bare "\n" is not
        # submitted at all, it just sits in the edit buffer. Every write()
        # after that then lands on top of the SAME unsubmitted line, forever -
        # which looked like PowerShell stuck showing ">>" continuation prompts
        # that kept growing, and the marker never appeared. pywinpty's own
        # write() does no newline translation (confirmed by reading
        # ptyprocess.py), so it has to happen here.
        self.proc.write(re.sub(r"\r?\n", "\r\n", text))

    def _take(self):
        with self._lock:
            out, self._buf = self._buf, ""
        return out

    def read_until(self, marker, deadline, watch_prompts=False):
        """Everything printed up to `marker`, or up to the deadline. Same
        contract as _PosixTerminal.read_until, including the
        (text, done, waiting) triple where done False means it is still running
        and waiting means it has stopped at a question.

        There is no select() here to give a deadline to - this end is a
        background thread filling a buffer - so the silence is timed directly
        instead, from the last moment anything arrived."""
        out = ""
        quiet_since = time.monotonic()
        while True:
            got = self._take()
            if got:
                out += got
                quiet_since = time.monotonic()
            if marker and marker in out:
                return self._unecho(out), True, None
            if self.closed or not self.alive():
                return (self._unecho(out), True,
                        "gone" if watch_prompts == PROMPT_ONLY else None)
            now = time.monotonic()
            if now >= deadline:
                return self._unecho(out), False, None
            if watch_prompts:
                gap, kind = _quiet_gap(self._unecho(out))
                if watch_prompts == PROMPT_ONLY and kind != "prompt":
                    gap = None      # same reason as the POSIX read: see there
                if gap is not None and now - quiet_since >= gap:
                    return self._unecho(out), False, kind
            time.sleep(0.02)

    def drain(self):
        """Whatever has piled up since we last looked, without waiting."""
        return self._unecho(self._take())

    def _unecho(self, text):
        """Drop the echoed copy of the command from the front of the output.
        Best effort and deliberately conservative: only the first line, only
        when it matches exactly, so output that merely resembles the command is
        left alone."""
        if not self._echo:
            return text
        first = self._echo.split("\n", 1)[0]
        stripped = text.lstrip("\r\n")
        if first and stripped.startswith(first):
            return stripped[len(first):].lstrip("\r\n")
        return text

    def close(self):
        self.closed = True
        # taskkill /T is the tree kill: PowerShell's own children go with it,
        # which is what pkill -s does for the session on POSIX. Without /T a
        # background job outlives the shell that started it.
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass


# The switch. Everything below calls _Terminal() and neither knows nor cares
# which one it got - the two classes expose the same handful of methods.
_Terminal = _WindowsTerminal if WINDOWS else _PosixTerminal


def _reap():
    """Close terminals nobody has used in a while, and any whose bash has died."""
    now = time.monotonic()
    for key, term in list(_SESSIONS.items()):
        if not term.alive() or now - term.used > IDLE_LIMIT:
            term.close()
            del _SESSIONS[key]


def _key(chat_id, workspace):
    """What a terminal is filed under: the chat AND the workspace it is in.

    Both, because a chat that switches workspace has to get a different shell -
    the old one is a bash on the old machine, sitting in a directory that may
    not exist on this one. Keying on the chat alone would hand it straight back
    and every command after the switch would quietly run in the old place."""
    return (chat_id, getattr(workspace, "id", "") or "")


def _session(chat_id, workspace=None):
    """This chat's terminal in this workspace, opening one if it hasn't got a
    live one."""
    _reap()
    key = _key(chat_id, workspace)
    term = _SESSIONS.get(key)
    if term is None or not term.alive():
        term = _SESSIONS[key] = _Terminal(workspace)
    term.used = time.monotonic()
    return term


def _log_path(chat_id, command, term):
    """Where this background job's output goes: one file per job, named by the
    tool rather than by the model. A name the model picked could collide with
    a job already running, or truncate a real file - "> " destroys whatever
    was there - and it would have no way to tell a stale log from a live one."""
    term.jobs += 1
    # A subagent's chat id has a "/" in it (subagent-chat-x/name), which would
    # quietly turn into a nested folder.
    chat = str(chat_id).replace("/", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", command.lower()).strip("-")[:30] or "job"
    folder = BG_DIR / chat
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (str(term.jobs) + "-" + slug + ".log")


def _clean(text, marker=None):
    """Output as it should be read: no escape codes, no sentinel."""
    if marker:
        text = text.split(marker)[0]
    # A terminal ends its lines "\r\n" where a pipe would just use "\n". Left
    # in, every line of every result carries a stray \r into the transcript.
    return _ANSI.sub("", text.replace("\r\n", "\n")).strip()


def _cap(text):
    """Keep a flood from swallowing the conversation: head and tail, with the
    middle counted rather than kept."""
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    dropped = len(text) - MAX_OUTPUT
    return (text[:half] + "\n\n... [" + str(dropped) + " characters cut from the "
            "middle - narrow it down with grep/head/tail if you need them] ...\n\n"
            + text[-half:])


def _exit_code(raw, marker):
    """The command's exit code - bash's prompt is the marker with $? on it."""
    if not marker or marker not in raw:
        return 0
    digits = re.match(r"\s*(\d+)", _ANSI.sub("", raw.split(marker, 1)[1]))
    return int(digits.group(1)) if digits else 0


def _report(code, output):
    """One finished command, said the way the old tool said it."""
    if code == 0:
        return output or "(the command ran successfully but printed nothing)"
    if output:
        return output + "\n(exit code " + str(code) + ")"
    return ("(no output, exit code " + str(code) + ". Careful: plenty of tools "
            "use a non-zero exit code to mean 'nothing found' rather than 'error' "
            "- grep, pkill and diff all return 1 when nothing matched.)")


def _finish(term, raw, done, waiting=None):
    """Turn one read into what the model gets told."""
    output = _cap(_clean(raw, term.mark))

    setattr(term, "parked", waiting == "prompt")

    if waiting == "prompt":
        # Said differently from STILL RUNNING on purpose. The two look the same
        # from here - neither has finished - but they need opposite next moves:
        # this one wants an answer typed into it, and telling the model to
        # "read more" would have it sit and watch a prompt that will never
        # change on its own.
        term.busy = True
        return (output + "\n\n(WAITING FOR INPUT - it has stopped and is sitting "
                "at the question on the last line above. This is NOT an error, it "
                "has NOT finished and it was NOT killed. Answer it by calling "
                "terminal again with just an \"input\" argument. If it wants a "
                "password you have not been given, stop and ask the user for it - "
                "never guess one.)").strip()

    if waiting == "idle":
        # All we actually know is that it stopped mid-line and went quiet, which
        # is a question about as often as it is a slow step. Handing that over
        # as "WAITING FOR INPUT" would be a guess dressed as a fact, and the
        # model would answer a build that never asked anything. So it gets both
        # readings and the move for each.
        term.busy = True
        return (output + "\n\n(STOPPED PRINTING and gone quiet - NOT finished, "
                "NOT an error, NOT killed. Either it is waiting for an answer to "
                "the last line above, or it is just slow and has more to print. "
                "If it reads as a question, answer it by calling terminal again "
                "with just an \"input\" argument; if it reads as work in progress, "
                "call terminal again with no arguments at all to keep "
                "reading.)").strip()

    if waiting == "gone":
        # The wait ended because the terminal did: a "reset" from elsewhere, or
        # the shell died under it. Not a timeout and not a finished command -
        # reporting exit code 0 for a command nobody ever heard the end of
        # would be an invented success.
        term.busy = False
        return ((output + "\n\n" if output else "")
                + "(the terminal was closed while waiting - either it was reset "
                "or the shell went away. Whatever was running is gone and "
                "nothing more is coming from it. The next command opens a fresh "
                "shell.)").strip()

    if not done:
        # NOT killed - it is still going. Say so plainly, because the obvious
        # wrong read of a short reply is "it failed", and the model would then
        # run the whole thing again on top of the copy already running.
        term.busy = True
        return (output + "\n\n(STILL RUNNING after the timeout - this is NOT an "
                "error and it was NOT killed. If it is waiting on a question, "
                "answer it by calling terminal again with just an \"input\" "
                "argument. Otherwise read more by calling terminal again with "
                "no arguments at all.)").strip()

    term.busy = False
    return _report(_exit_code(raw, term.mark), output)


def _oneshot(command, timeout, workspace=None):
    """No chat_id, so there is no conversation for a terminal to belong to -
    run it the old way, in a throwaway shell. Cron jobs and anything calling
    the tool directly still work exactly as they did."""
    if command is None:
        return ("ERROR: no command given, and there is no saved terminal to read "
                "from (this call has no chat behind it).")

    print("\n[terminal] wants to run:")
    print("    " + command)

    if workspace is not None and workspace.is_remote:
        # One command, one ssh round trip, on the connection the workspace
        # already keeps open. Backgrounding is left to the remote shell here:
        # there is no local process to hold onto, so the "&" means what it
        # means over there.
        print("    " + workspace.where)
        try:
            code, out = workspace.run(command, timeout=timeout or DEFAULT_TIMEOUT)
        except Exception as e:
            return "ERROR: " + str(e)
        return _report(code, _cap(_clean(out)))

    # POSIX only: on Windows "&" is cmd's command separator, not backgrounding,
    # so stripping it off would change what the command means.
    if not WINDOWS and command.strip().endswith("&"):
        # The trailing "&" comes off and Popen does the backgrounding instead.
        # Left on, the SHELL backgrounds the job and exits 0 straight away, so
        # the process we are holding is the shell rather than the command - and
        # poll() reports the shell's success no matter how badly the command
        # went. Dropped, the thing we are watching is the command itself.
        proc = subprocess.Popen(
            command.strip()[:-1], shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Look before claiming it worked. Popen succeeds even when the command
        # doesn't exist - the shell starts fine and then dies - so without this
        # a typo'd "gnome-termnial &" reported success just the same.
        time.sleep(0.2)
        if proc.poll() not in (None, 0):
            return ("(it exited immediately with code " + str(proc.returncode)
                    + " - it did NOT start. Check the command is spelled right "
                    "and that it exists.)")
        return "(launched in the background - it is running now, this WORKED)"

    # Which shell interprets this. On Windows shell=True means cmd.exe, and the
    # instructions above teach PowerShell - so a cron job or a subagent (both
    # of which come through here) would have been writing Get-ChildItem at a
    # shell that has never heard of it. Same shell as the persistent terminal,
    # so a command means the same thing however it is run.
    argv, use_shell = command, True
    if WINDOWS:
        argv = [_shell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", command]
        use_shell = False

    try:
        result = subprocess.run(
            argv, shell=use_shell, capture_output=True,
            # Said out loud rather than left to the locale: Windows would
            # otherwise decode with the ANSI codepage, and a single odd byte
            # from a program that prints in another one would raise instead of
            # handing back the output it managed.
            text=True, encoding="utf-8", errors="replace",
            # No stdin. capture_output only redirects the OUTPUT, so without
            # this the command inherits the server's own stdin - and anything
            # that asks a question there (sudo when it can find a terminal, a
            # stray `read`) hangs for the whole timeout with nobody able to
            # answer it. Handed /dev/null it fails immediately and says why,
            # which is the useful answer. There is no chat behind this call, so
            # unlike the terminal above there is nobody to answer a prompt.
            stdin=subprocess.DEVNULL,
            timeout=timeout or DEFAULT_TIMEOUT,
            # A local workspace still decides where a one-shot command runs.
            cwd=(workspace.root if workspace is not None else None),
        )
    except subprocess.TimeoutExpired as e:
        # Hand back what it managed before the timeout rather than nothing -
        # the partial output is usually the whole point of a slow command.
        got = (e.stdout or b"") + (e.stderr or b"")
        if isinstance(got, bytes):
            got = got.decode("utf-8", "replace")
        return (_cap(_clean(got)) + "\n\n(timed out and was killed after "
                + str(timeout or DEFAULT_TIMEOUT) + "s.)").strip()

    return _report(result.returncode, _cap(_clean(result.stdout + result.stderr)))


def _close_all():
    """Shut every terminal down when the agent exits. Without this they are
    orphaned: bash is in its own session, so it and everything it started keep
    running after Uniagent has quit."""
    for term in list(_SESSIONS.values()):
        term.close()
    _SESSIONS.clear()


# Guarded, because this module is reloaded on every turn and would otherwise
# register the same handler over and over.
if not globals().get("_ATEXIT_DONE"):
    atexit.register(_close_all)
_ATEXIT_DONE = True


def run(command=None, chat_id=None, timeout=None, input=None, reset=False,
        background=False, workspace=None):
    """Run `command` in this chat's terminal and return what it printed.

    chat_id is filled in by tool_processor, never by the model - it is what
    keeps one chat's terminal separate from another's. workspace arrives the
    same way and decides WHERE the shell is: a directory on this machine, or a
    bash on another machine over ssh. `input` answers a command that is still
    waiting (a password, a y/N). With neither, we read whatever new output has
    appeared, which is how a slow command gets followed. Without a chat_id at
    all we fall back to a one-shot shell."""
    if workspace is not None and workspace.is_remote and WINDOWS:
        # The Windows terminal is built around PowerShell's own prompt
        # function for its marker, and a remote bash under it would have no
        # working marker at all - every result would be the previous command's
        # output. Better to say so plainly than to return convincing nonsense.
        return ("ERROR: remote workspaces need the Uniagent server to be on "
                "Linux or macOS - this one is on Windows. Files still work over "
                "ssh; it is only the terminal that cannot open a remote shell "
                "from here.")
    # "" is what the model actually sends when it means "no command" - it has
    # a command field in front of it and fills it in. Left as an empty string
    # it is not None, so it went down the RUN path and was refused instantly
    # with "nothing was run" instead of waiting: three turns of a real chat
    # burned that way, and a working disk scan abandoned as stuck.
    if command is not None and not command.strip():
        command = None

    if chat_id is None:
        return _oneshot(command, timeout, workspace)

    if reset:
        # "reset" IS a real command (it clears the screen), so a model asking
        # for a fresh shell the obvious way - both the flag and the word - used
        # to reset the terminal and then run reset in the new one.
        if command is not None and command.strip() == "reset":
            command = None
        term = _SESSIONS.pop(_key(chat_id, workspace), None)
        if term is not None:
            term.close()
        if command is None and input is None:
            _session(chat_id, workspace)
            return "(terminal closed and reopened - fresh shell, fresh directory)"

    term = _session(chat_id, workspace)
    # Clamped, not just defaulted: a wait now really holds the turn open for
    # as long as it says.
    deadline = time.monotonic() + min(timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)

    # Answering something that is waiting - a password, a y/N, a line for a REPL.
    if input is not None:
        print("\n[terminal] sends input")
        # Ctrl-C and friends are single control characters: they go through
        # as-is, with no newline, or the program just sees a blank line.
        term.write(input if len(input) == 1 and input < " " else input.rstrip("\n") + "\n")
        if not term.busy:
            # Nothing was waiting on it. Don't sit here until the timeout for a
            # prompt that isn't coming - say what came back and leave it.
            return _cap(_clean(term.drain(), term.mark)) or "(sent - nothing came back)"
        return _finish(term, *term.read_until(term.mark, deadline, watch_prompts=True))

    # No command and no input: catching up on something slow. This is also the
    # only call that WAITS - it sits here until the command finishes, stops at
    # a question, or the timeout runs out. Anything else and a model asking for
    # 300 seconds got fifteen.
    if command is None:
        if not term.busy:
            # Nothing is running, so nothing can arrive however long we sit
            # here. Say so at once rather than serving a timeout of guaranteed
            # silence - to actually pass time, run `sleep N` as a command.
            return (_cap(_clean(term.drain(), term.mark))
                    or "(nothing new - nothing is running in this terminal)")
        # Anything that arrived since the last call, before deciding anything:
        # it may have finished, and its marker would then be sitting in the
        # buffer with nobody to read it.
        fresh = term.drain()
        if term.mark in fresh:
            return _finish(term, fresh, True, None)
        if getattr(term, "parked", False) and not _clean(fresh, term.mark):
            # It stopped at a question last time and has not said a word since,
            # so it is still on that question and will be in five minutes too.
            # Waiting out the timeout would only make the model wait to be told
            # what it was told before.
            return ("(WAITING FOR INPUT - nothing has changed and nothing will: "
                    "it is still sitting at the question it asked in the output "
                    "above, and it does not time out. Answer it by calling "
                    "terminal again with just an \"input\" argument, or clear it "
                    "out with \"reset\": true.)")
        print("\n[terminal] waiting up to "
              + str(int(min(timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)))
              + "s for what is running")
        raw, done, waiting = term.read_until(term.mark, deadline,
                                             watch_prompts=PROMPT_ONLY)
        return _finish(term, fresh + raw, done, waiting)

    if term.busy:
        # It has very likely finished since. "busy" is set by every early
        # return - the timeout, a prompt, a quiet patch - and none of those
        # kill the command: it carries on, prints its marker, and that marker
        # sits in the buffer with nobody reading it. So look, without waiting,
        # before refusing anything: the terminal is only really busy if the
        # marker still has not turned up. Without this a single slow command
        # left the shell wedged for the rest of the conversation, and every
        # command after it came back "nothing was run" no matter how long the
        # model waited.
        leftover = term.drain()
        if term.mark in leftover:
            term.busy = False

    if term.busy:
        # Typing a command at a terminal that is mid-prompt doesn't run it - it
        # gets eaten as that command's input. Better to say so than to silently
        # feed `rm -rf` to a y/N prompt. What the drain above took comes back
        # here rather than being thrown away - it is the newest thing the
        # running command has said, and it is exactly what the model needs to
        # tell a question it has to answer from work it has to wait out.
        since = _cap(_clean(leftover, term.mark))
        return ((since + "\n\n" if since else "")
                + 'ERROR: nothing was run. A command is STILL RUNNING in this '
                'terminal and would have swallowed this as its input. Read it by '
                'calling terminal again with no arguments to see what it wants, '
                'answer it with "input", or clear it out with "reset": true.')

    print("\n[terminal] wants to run:")
    print("    " + command)
    # Approval is handled centrally in main.py (safety validation + y/n), so by
    # the time we get here the command has already been cleared to run.

    # Anything left over from before would otherwise read as this one's output.
    term.drain()

    if background:
        # Started in THIS terminal, so it keeps the cd and the venv - a job
        # launched from a shell of its own would start in the wrong directory
        # with the wrong python. What keeps it out of the way is the redirect,
        # not a separate shell: its output goes to the file from the moment it
        # starts, so it never writes here at all.
        log = _log_path(chat_id, command, term)
        if WINDOWS:
            # Start-Process hands back a real pid, and "*>" sends every stream
            # to the one file so nothing reaches this terminal. -WorkingDirectory
            # is not optional: Set-Location moves PowerShell's idea of where it
            # is but NOT the process's, so without this the job would start in
            # whatever directory the shell was launched in.
            inner = command.rstrip("\n") + " *> " + _ps_quote(log)
            term.write("(Start-Process -FilePath " + _ps_quote(term.shell)
                       + " -ArgumentList '-NoProfile','-Command'," + _ps_quote(inner)
                       + " -WorkingDirectory $PWD.Path -NoNewWindow -PassThru).Id\n")
        else:
            term.write(command.rstrip("\n") + " > '" + str(log) + "' 2>&1 &\n")
        raw, done, _ = term.read_until(term.mark, deadline)
        term.busy = not done
        # bash announces a background job as "[1] 12345", PowerShell prints the
        # bare id - either way the pid is the last thing on the line, and it is
        # how the model stops it again.
        started = _clean(raw, term.mark)
        pid = started.split()[-1] if started else "?"
        read_cmd = ("Get-Content -Tail 50 " if WINDOWS else "tail -n 50 ") + str(log)
        stop_cmd = ("Stop-Process -Id " if WINDOWS else "kill ") + pid
        return ("Started in the background, pid " + pid + ".\n"
                "Its output is going to: " + str(log) + "\n"
                "Read it by calling terminal again with \"command\": \"" + read_cmd
                + "\" - it keeps running while you do other things, "
                "and nothing of it will appear here. Stop it with " + stop_cmd + ".")

    term.write(command.rstrip("\n") + "\n")
    return _finish(term, *term.read_until(term.mark, deadline, watch_prompts=True))
