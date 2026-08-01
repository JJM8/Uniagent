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
"""

import atexit
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time
import uuid
from pathlib import Path

NAME = "terminal"
DESCRIPTION = ("Really run any shell command on the user's computer - read or change files, "
               "install things, launch apps and windows, run programs. Not just read-only. "
               "The terminal stays open, so cd/exports stick and you can answer prompts "
               "like sudo passwords.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "terminal". Do not explain what the command does first, and do not ask the user to approve it - they are asked automatically. You will be shown the output afterwards, and THAT is when you explain it.

Arguments (all optional - see below for when to use which):
- command:    the shell command(s) to run. Several commands can go in one
              string, e.g. "free -h && ps aux --sort=-%mem | head". Omit it
              entirely (no other argument either) to just read whatever new
              output has appeared since your last call.
- timeout:    seconds to wait for the command before reporting it as STILL
              RUNNING instead of giving up on it. Defaults to 60.
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

IT IS ONE TERMINAL AND IT STAYS OPEN: This is not a fresh shell each time - it is the SAME terminal for this whole conversation, so state carries over exactly like a real one: `cd` once and every command after it runs from there; `export`, `source venv/bin/activate` and shell variables all stick too. Do NOT re-cd on every command and do NOT chain "cd x && ..." out of habit - you are already there. Run `pwd` if you have lost track of where you are.

WHEN A COMMAND IS STILL RUNNING: Slow things (builds, apt, downloads, big copies) are NOT killed at the timeout. You get what has been printed so far, plus "STILL RUNNING". The command is fine and is carrying on. To see more, call again with `command` left out entirely - that reads whatever new output has appeared since. Keep doing that until you see it finish. Or wait longer up front with a bigger `timeout`.

ANSWERING PROMPTS - sudo passwords, y/N, REPLs: Because the terminal stays open, a command that stops to ask a question is still sitting there waiting for the answer. Send it with `input` - e.g. run "sudo apt update", see "[sudo] password for the user:" and STILL RUNNING, then call again with `input` set to the password the user gave you.
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
SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description":
            "The shell command(s) to run. Omit entirely (no other argument "
            "either) to just read whatever new output has appeared since "
            "the last call."},
        "timeout": {"type": "number", "description":
            "Seconds to wait before reporting STILL RUNNING instead of "
            "giving up. Defaults to 60."},
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
MAX_OUTPUT = 8000  # characters handed back; the middle of a flood is dropped

# Where a background job's output goes. It gets a file of its own rather than
# sharing the terminal, because a job that prints as it goes would otherwise
# land in the middle of whatever command runs next - and worse, stall, since a
# terminal only holds about 64KB before the writer blocks waiting to be read.
# A file never fills up, so the job runs at full speed and the output keeps.
BG_DIR = Path(__file__).parent.parent / "background-terminal-output"

# Spelled out in full in the instructions: a relative path breaks the moment
# the model has cd'd somewhere else, which it will have.
INSTRUCTIONS = INSTRUCTIONS.replace("<BGDIR>", str(BG_DIR))

# Colour codes, cursor moves and bare carriage returns (progress bars). Even
# with TERM=dumb some programs emit them, and they are noise in a transcript
# the model has to read.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r(?!\n)")


class _Terminal:
    """One bash, kept open, talked to through a pty."""

    def __init__(self):
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
        # in normal use touches PROMPT_COMMAND.
        #
        # Random per terminal so a command that PRINTS the marker (a grep over
        # this very file, say) can't be mistaken for the marker itself.
        self.mark = "__UNI_" + uuid.uuid4().hex + "__"

        env = dict(os.environ)
        env["TERM"] = "dumb"  # ask politely for no colour codes
        # Blanking PS1 here, every prompt, rather than only at startup: a venv
        # or conda sets it whenever it is activated, and that prefix would then
        # be printed after our marker and read as the head of the NEXT
        # command's output ("(venv) hello" instead of "hello").
        env["PROMPT_COMMAND"] = 'printf "\\n%s%s\\n" "' + self.mark + '" "$?"; PS1=""'
        env["PS1"] = ""
        env["PS2"] = ""       # continuation lines add nothing to read

        # -i so bash prints that prompt at all. --noediting turns off readline,
        # which would otherwise echo back and redraw everything we type.
        # --norc/--noprofile so the user's own prompt, aliases and colours don't
        # end up in the output we have to parse. start_new_session puts bash in
        # its own session, so a Ctrl-C in the real terminal running the agent
        # doesn't also kill every chat's shell.
        self.proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile", "--noediting", "-i"],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
        os.close(slave)  # bash holds its own copy; ours would hold back EOF
        self.master = master
        self.busy = False  # is a command still running in here?
        self.jobs = 0      # background jobs started here, for naming their logs
        self.used = time.monotonic()

        # Swallow the prompt bash prints the moment it starts, so it isn't read
        # as the first command's result.
        self.read_until(self.mark, time.monotonic() + 5)

    def alive(self):
        return self.proc.poll() is None

    def close(self):
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

    def read_until(self, marker, deadline):
        """Everything printed up to `marker`, or up to the deadline. Returns
        (text, done); done False means the command is still going and this is
        only what it has printed so far."""
        out = ""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return out, False
            # select waits for the terminal to have something to say, with a
            # deadline - which is what lets us give up on WAITING without
            # giving up on the command. It carries on running either way.
            ready, _, _ = select.select([self.master], [], [], left)
            if not ready:
                return out, False
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return out, True  # terminal went away, nothing more is coming
            if not chunk:
                return out, True
            out += chunk.decode("utf-8", "replace")
            if marker and marker in out:
                return out, True

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


def _reap():
    """Close terminals nobody has used in a while, and any whose bash has died."""
    now = time.monotonic()
    for key, term in list(_SESSIONS.items()):
        if not term.alive() or now - term.used > IDLE_LIMIT:
            term.close()
            del _SESSIONS[key]


def _session(chat_id):
    """This chat's terminal, opening one if it hasn't got a live one."""
    _reap()
    term = _SESSIONS.get(chat_id)
    if term is None or not term.alive():
        term = _SESSIONS[chat_id] = _Terminal()
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


def _finish(term, raw, done):
    """Turn one read into what the model gets told."""
    output = _cap(_clean(raw, term.mark))

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


def _oneshot(command, timeout):
    """No chat_id, so there is no conversation for a terminal to belong to -
    run it the old way, in a throwaway shell. Cron jobs and anything calling
    the tool directly still work exactly as they did."""
    if command is None:
        return ("ERROR: no command given, and there is no saved terminal to read "
                "from (this call has no chat behind it).")

    print("\n[terminal] wants to run:")
    print("    " + command)

    if command.strip().endswith("&"):
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

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout or DEFAULT_TIMEOUT,
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
        background=False):
    """Run `command` in this chat's terminal and return what it printed.

    chat_id is filled in by tool_processor, never by the model - it is what
    keeps one chat's terminal separate from another's. `input` answers a
    command that is still waiting (a password, a y/N). With neither, we read
    whatever new output has appeared, which is how a slow command gets
    followed. Without a chat_id at all we fall back to a one-shot shell."""
    if chat_id is None:
        return _oneshot(command, timeout)

    if reset:
        term = _SESSIONS.pop(chat_id, None)
        if term is not None:
            term.close()
        if command is None and input is None:
            _session(chat_id)
            return "(terminal closed and reopened - fresh shell, fresh directory)"

    term = _session(chat_id)
    deadline = time.monotonic() + (timeout or DEFAULT_TIMEOUT)

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
        return _finish(term, *term.read_until(term.mark, deadline))

    # No command and no input: catching up on something slow.
    if command is None:
        if not term.busy:
            return (_cap(_clean(term.drain(), term.mark))
                    or "(nothing new - nothing is running in this terminal)")
        return _finish(term, *term.read_until(term.mark, deadline))

    if term.busy:
        # Typing a command at a terminal that is mid-prompt doesn't run it - it
        # gets eaten as that command's input. Better to say so than to silently
        # feed `rm -rf` to a y/N prompt.
        return ('ERROR: nothing was run. A command is STILL RUNNING in this '
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
        term.write(command.rstrip("\n") + " > '" + str(log) + "' 2>&1 &\n")
        raw, done = term.read_until(term.mark, deadline)
        term.busy = not done
        # bash announces a background job as "[1] 12345" - the pid is how the
        # model stops it again.
        started = _clean(raw, term.mark)
        pid = started.split()[-1] if started else "?"
        return ("Started in the background, pid " + pid + ".\n"
                "Its output is going to: " + str(log) + "\n"
                "Read it by calling terminal again with \"command\": \"tail -n 50 "
                + str(log) + "\" - it keeps running while you do other things, "
                "and nothing of it will appear here. Stop it with kill " + pid + ".")

    term.write(command.rstrip("\n") + "\n")
    return _finish(term, *term.read_until(term.mark, deadline))
