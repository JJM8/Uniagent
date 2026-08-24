"""Bring this checkout up to the latest code, and leave everything of yours alone.

    python3 update.py             update now
    python3 update.py --check     say what an update would bring, change nothing
    python3 update.py --json      machine-readable result (what the web UI reads)
    python3 update.py --no-restart    update, but leave the services alone

WHAT IS SAFE HERE, AND WHY

git only ever writes files it tracks. Everything personal in a Uniagent install
is either gitignored - .env, chats/, context/, memories/, settings.json,
models_custom.json, cron.json, mcp.json, workspace/, certs/, the logs - or it is
simply untracked, which is what a tool or a skill you added yourself is. So this
never resets, never force-checks-out over the tree, and NEVER runs `git clean`.
A fast-forward merge is the whole of the update, and a fast-forward cannot touch
a file that is not in the repo.

The one place that is not true is enabling and disabling. tools/, skills/ and
disabled/ are all tracked, and the switch in the tools tab MOVES a bundle
between them (tool_processor.set_enabled). To git that reads as "deleted over
here, and some untracked files turned up over there" - so a plain `git pull`
happily updates the shipped copy at its shipped path and leaves the one you
moved sitting where you put it, stale, and now duplicated. A skill you had
switched off comes back on, with a second stale copy of itself in disabled/.
That is not hypothetical; it is what `git pull --ff-only` does to this repo.

So the moves are undone before the merge and redone after it: each moved file
goes back to the path git knows it by, git updates it in place along with
everything else, and then it is put back where you had it. The list is written
to update_state.json BEFORE anything moves, so a machine that loses power in
the middle can put everything back on the next run.

WHAT IT WILL NOT DO

It will not overwrite an edit of yours to a shipped file. If you have changed a
tracked file and the update also changes that file, it stops and names it rather
than picking a winner. Same for local commits: the update fast-forwards or it
does nothing. And it never writes to .env - if the new version documents keys
you don't have yet, it lists them and leaves them to you.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service  # noqa: E402  - needs the line above to be importable

ROOT = Path(__file__).parent.parent

# Written before the first file moves and deleted once they are all back. Its
# presence on startup means a previous run died holding things out of place.
STATE_FILE = ROOT / "update_state.json"

# tools/, skills/ and their disabled/ mirrors, in both directions. A tracked
# file missing from one side and present on the other is a bundle the user
# switched off (or on) - see the module docstring.
MIRRORS = (
    ("tools/", "disabled/tools/"),
    ("skills/", "disabled/skills/"),
    ("disabled/tools/", "tools/"),
    ("disabled/skills/", "skills/"),
)

# Only for the "new keys in .env.example" note at the end. Nothing is written.
ENV_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

# The last line the log gets, followed by "restarting" or "idle". The web UI
# tails update.log to follow the update, and an update that restarts the server
# takes the log's own reader down with it - so "the log stopped" cannot mean
# "the update finished". This can. See main().
DONE = "::uniagent-update-done::"


def say(msg=""):
    """Progress, one line at a time. The web UI tails this as a file, so it has
    to be flushed as it happens rather than when the process ends."""
    print(msg, flush=True)


def git(*args, check=False):
    """(exit code, stdout, stderr), always run against this checkout."""
    r = subprocess.run(["git", "-C", str(ROOT)] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError("git " + " ".join(args) + ": " + (r.stderr.strip() or "failed"))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# --- what is where -----------------------------------------------------------

def _mirror(rel):
    """The other place a tools/ or skills/ file can legitimately be, or None if
    this path isn't one that enable/disable ever moves."""
    for here, there in MIRRORS:
        if rel.startswith(here):
            return there + rel[len(here):]
    return None


def find_moves():
    """Every tracked file that is not where git thinks it is because it was
    switched on or off: {"home": path git knows, "away": where it is now}.

    Working per file rather than per bundle is deliberate - it is exactly the
    set of paths the merge could collide with, and a file you added inside a
    shipped skill's folder is untracked and so is never in this list."""
    code, out, _ = git("ls-files", "--", "tools", "skills", "disabled")
    if code != 0:
        return []
    moves = []
    for rel in out.splitlines():
        away = _mirror(rel)
        if not away:
            continue
        if not (ROOT / rel).exists() and (ROOT / away).exists():
            moves.append({"home": rel, "away": away})
    return moves


def _shift(src_rel, dst_rel):
    """Move one file inside the checkout. Never overwrites: a name already
    taken at the destination means something is there that we did not put
    there, and losing it is exactly what this whole script exists to avoid."""
    src, dst = ROOT / src_rel, ROOT / dst_rel
    if not src.exists():
        return False
    if dst.exists():
        say("    ! " + dst_rel + " is already there - left " + src_rel + " alone")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    # Tidy the folder it came out of, but only while it is genuinely empty -
    # anything of yours left inside keeps it alive.
    parent = src.parent
    while parent != ROOT and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return True


def normalise(moves):
    """Put every switched-off bundle back at its shipped path so the merge can
    see it."""
    for m in moves:
        _shift(m["away"], m["home"])


def restore(moves):
    """Put them back where the user had them. A file the update deleted
    upstream simply isn't there to move, and that is the right outcome - it
    does not ship any more."""
    for m in moves:
        _shift(m["home"], m["away"])


def _save_state(moves):
    STATE_FILE.write_text(json.dumps({"moves": moves, "at": time.time()}, indent=2),
                          encoding="utf-8")


def _clear_state():
    STATE_FILE.unlink(missing_ok=True)


def recover():
    """A previous run died between normalise() and restore(). Put its moves
    back before doing anything else, or this run would read the tree as though
    the user had enabled everything they had switched off."""
    if not STATE_FILE.exists():
        return
    try:
        moves = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("moves", [])
    except (OSError, ValueError):
        _clear_state()
        return
    say("==> An earlier update stopped halfway. Putting " + str(len(moves))
        + " file(s) back first.")
    restore(moves)
    _clear_state()


# --- reading the situation ---------------------------------------------------

def target_ref(explicit=None):
    """What we are updating TO. The branch this checkout is on, followed on the
    remote - so a checkout pinned to a tag or a branch stays pinned to it."""
    if explicit:
        return explicit
    code, up, _ = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code == 0 and up:
        return up
    code, branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and branch and branch != "HEAD":
        return "origin/" + branch
    return "origin/main"


def _commit(rev):
    code, out, _ = git("show", "-s", "--format=%h\x1f%cI\x1f%s", rev)
    if code != 0 or not out:
        return None
    sha, when, subject = (out.split("\x1f") + ["", "", ""])[:3]
    return {"sha": sha, "date": when, "subject": subject}


def _env_new_keys():
    """Keys the new .env.example documents that your .env has never heard of.
    Reported only - .env holds your API keys and is never written by an update."""
    example, live = ROOT / ".env.example", ROOT / ".env"
    if not example.exists() or not live.exists():
        return []
    def keys(path):
        out = []
        for line in path.read_text(errors="replace", encoding="utf-8").splitlines():
            m = ENV_KEY.match(line)
            if m:
                out.append(m.group(1))
        return out
    have = set(keys(live))
    return [k for k in dict.fromkeys(keys(example)) if k not in have]


def survey(ref, fetch=True):
    """Everything needed to decide, having changed nothing on disk."""
    if fetch:
        code, _, err = git("fetch", "--quiet", "--prune", "origin")
        if code != 0:
            return {"ok": False, "error": "could not reach the remote: " + (err or "fetch failed")}

    code, _, _ = git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if code != 0:
        return {"ok": False, "error": "no such ref here: " + ref}

    here, there = _commit("HEAD"), _commit(ref)
    _, behind_raw, _ = git("rev-list", "--count", "HEAD.." + ref)
    _, ahead_raw, _ = git("rev-list", "--count", ref + "..HEAD")
    behind = int(behind_raw or 0)
    ahead = int(ahead_raw or 0)

    # What the update would bring, newest first, for the "what's new" list.
    _, log, _ = git("log", "--no-merges", "--format=%h\x1f%s", "HEAD.." + ref)
    commits = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\x1f")
        commits.append({"sha": sha, "subject": subject})

    # Files the update changes, and files you have changed. Where those two
    # overlap the merge would have to pick a winner, so we stop instead.
    #
    # Three dots, not two: "HEAD...ref" is merge-base-to-ref, which is what the
    # update would actually bring. Two dots diffs the two trees, so on a
    # checkout carrying local commits it lists YOUR files as incoming and then
    # reports them as conflicts with themselves.
    _, incoming_raw, _ = git("diff", "--name-only", "HEAD..." + ref)
    incoming = set(incoming_raw.splitlines())
    moves = find_moves()
    moved_homes = {m["home"] for m in moves}
    _, dirty_raw, _ = git("diff", "--name-only", "HEAD")
    # A switched-off bundle reads as a local deletion. That is not an edit, and
    # normalise() is about to undo it - so it is not a reason to refuse.
    dirty = {p for p in dirty_raw.splitlines() if p not in moved_homes}
    blocked = sorted(incoming & dirty)

    return {
        "ok": True,
        "ref": ref,
        "current": here,
        "latest": there,
        "behind": behind,
        "ahead": ahead,
        "commits": commits,
        "files": sorted(incoming),
        "moves": moves,
        "blocked": blocked,
        # Local commits of your own AND new code to take: a fast-forward is
        # impossible, and merging the two is a judgement call that is not ours
        # to make. Commits of your own with nothing new upstream are just a
        # checkout that is ahead - nothing to do, and nothing to warn about.
        "diverged": ahead > 0 and behind > 0,
        "deps_changed": "requirements.txt" in incoming or "requirements-voice.txt" in incoming,
        "up_to_date": behind == 0,
    }


# --- doing it ----------------------------------------------------------------

def _python():
    """The interpreter the install actually runs on - the venv's, if install.sh
    or install.ps1 built one, because that is where the dependencies live."""
    return service.python()


def install_deps():
    py = _python()
    say("==> Dependencies changed - re-applying them with " + py)
    r = subprocess.run([py, "-m", "pip", "install", "--quiet", "-r",
                        str(ROOT / "requirements.txt")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        say("    ! pip failed: " + (r.stderr.strip()[:400] or "unknown"))
        return False
    # Voice extras need system headers a server install won't have. Same as the
    # installer: try quietly, and a failure here is not a failure.
    subprocess.run([py, "-m", "pip", "install", "--quiet", "-r",
                    str(ROOT / "requirements-voice.txt")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    say("    dependencies up to date.")
    return True


def restart_services():
    """Bring the server and the cron watcher back on the new code.

    Nothing is waited on. This process is very likely a child of the server, so
    restarting that unit kills this one - systemd stops the whole cgroup. The
    request is already queued with systemd by then, so the restart happens
    either way, but it does mean this must be the LAST thing done and that
    nothing may be written afterwards."""
    if os.name == "nt":
        return _restart_windows()

    if not shutil.which("systemctl"):
        say("==> Update finished. Restart the server yourself to run the new code.")
        return False
    units = [u for u in ("uniagent-server.service", "uniagent-cron.service")
             if subprocess.run(["systemctl", "--user", "cat", u],
                               capture_output=True).returncode == 0]
    if not units:
        say("==> Update finished. Restart the server yourself to run the new code.")
        return False
    say("==> Restarting " + " and ".join(u.split(".")[0] for u in units)
        + ". The page will reconnect on its own.")
    sys.stdout.flush()
    # --no-block: return as soon as systemd has the job, rather than waiting
    # for a restart that is going to kill this process.
    subprocess.run(["systemctl", "--user", "restart", "--no-block"] + units,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    return True


def _restart_windows():
    """Bring both processes back, on Windows.

    Windows keeps them alive with run-server.ps1 rather than a service manager:
    it starts each one and starts it again whenever it exits. So a restart is
    a stop - the supervisor does the rest, and the process it starts reads the
    new code because Python reads a .py once, at startup.

    NOT `schtasks /Run /TN Uniagent`, which is what this used to do. The task is
    already running by definition (it is what started the server that spawned
    this), and a scheduled task refuses to start a second copy of itself - so
    that call reported success having done nothing at all, and Uniagent carried
    on running the old code until the next logon.

    This process was started detached by the server, so stopping the server
    does not take it with it and there is time to stop the cron watcher too.
    """
    stopped = []
    for name, script in (("cron", "cron.py"), ("server", "server.py")):
        pid = service.read_pid(name)
        if not service.alive(pid):
            continue
        if service.stop(name):
            stopped.append(name)
            # Nothing is watching, so nothing will bring it back but us.
            if not service.supervised():
                service.spawn(script)

    if stopped:
        say("==> Restarting " + " and ".join(stopped)
            + ". The page will reconnect on its own.")
        return True

    # No pidfiles and nothing running under them. Either Uniagent was started
    # some other way, or this is an install from before pidfiles existed - in
    # which case the scheduled task is still the honest way to start it, since
    # by now there is nothing running for it to collide with.
    r = subprocess.run(["schtasks", "/Run", "/TN", "Uniagent"],
                       capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        say("==> Starting Uniagent. The page will reconnect on its own.")
        return True
    say("==> Update finished. Restart Uniagent yourself to run the new code.")
    return False


def run(ref=None):
    """The update itself, up to but not including the restart. Returns the
    result dict the web UI reads. The restart is main()'s job, because the
    finished marker has to be in the log BEFORE this process can be killed by
    the very restart it asked for - see DONE and restart_services()."""
    recover()

    ref = target_ref(ref)
    say("==> Checking " + ref + " for new code...")
    s = survey(ref)
    if not s["ok"]:
        say("xx  " + s["error"])
        return dict(s, updated=False)

    say("    you are on " + s["current"]["sha"] + " - " + s["current"]["subject"])
    if s["up_to_date"]:
        say("Already up to date. Nothing to do.")
        return dict(s, updated=False)

    if s["diverged"]:
        say("xx  This checkout has " + str(s["ahead"]) + " commit(s) of its own that "
            + ref + " does not. An update only ever fast-forwards, so it is "
            "stopping here rather than merging them for you.")
        return dict(s, updated=False, error="local commits - nothing was changed")

    if s["blocked"]:
        say("xx  These files have been edited here AND changed by the update, so "
            "it is stopping rather than choosing which version to keep:")
        for p in s["blocked"]:
            say("      " + p)
        say("    Revert them (git checkout -- <file>) or commit them, then update again.")
        return dict(s, updated=False, error="local edits to shipped files")

    say("==> " + str(s["behind"]) + " new commit(s), " + str(len(s["files"]))
        + " file(s) to update.")
    for c in s["commits"][:15]:
        say("      " + c["sha"] + "  " + c["subject"])
    if len(s["commits"]) > 15:
        say("      ... and " + str(len(s["commits"]) - 15) + " more")

    moves = s["moves"]
    if moves:
        say("==> Setting aside " + str(len(moves))
            + " file(s) you had switched on or off, to put back afterwards.")
        _save_state(moves)
        normalise(moves)

    say("==> Merging " + ref + "...")
    code, out, err = git("merge", "--ff-only", ref)
    if code != 0:
        say("xx  The merge did not go through: " + (err or out or "unknown"))
        if moves:
            say("    Putting your switched-off files back.")
            restore(moves)
            _clear_state()
        return dict(s, updated=False, error="merge failed - nothing was changed")

    if moves:
        restore(moves)
        _clear_state()
        say("    Your enabled/disabled choices are back as they were.")

    now = _commit("HEAD")
    say("==> Now on " + now["sha"] + " - " + now["subject"])

    if s["deps_changed"]:
        install_deps()

    new_keys = _env_new_keys()
    if new_keys:
        say("==> The new version documents settings your .env does not have yet. "
            "Nothing has been written to it - add any you want from .env.example:")
        for k in new_keys:
            say("      " + k)

    return dict(s, updated=True, now=now, env_new_keys=new_keys)


# --- entry point -------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Update Uniagent, keeping everything of yours.")
    ap.add_argument("--check", action="store_true",
                    help="say what an update would bring and change nothing")
    ap.add_argument("--json", action="store_true",
                    help="print the result as JSON on the last line")
    ap.add_argument("--no-restart", action="store_true",
                    help="update, but leave the services running the old code")
    ap.add_argument("--ref", default=None,
                    help="what to update to (default: this branch's upstream)")
    args = ap.parse_args(argv)

    if args.check:
        ref = target_ref(args.ref)
        s = survey(ref)
        if not args.json:
            if not s["ok"]:
                say("xx  " + s["error"])
            elif s["up_to_date"]:
                say("Up to date on " + ref + " (" + s["current"]["sha"] + ")."
                    + (" This checkout is " + str(s["ahead"]) + " commit(s) ahead of it."
                       if s["ahead"] else ""))
            else:
                say(str(s["behind"]) + " new commit(s) on " + ref + ":")
                for c in s["commits"]:
                    say("  " + c["sha"] + "  " + c["subject"])
                if s["diverged"]:
                    say("! this checkout also has " + str(s["ahead"])
                        + " commit(s) of its own - an update cannot fast-forward.")
                if s["blocked"]:
                    say("! edited here and changed upstream: " + ", ".join(s["blocked"]))
        else:
            print(json.dumps(s))
        return 0 if s.get("ok") else 1

    result = run(ref=args.ref)
    if args.json:
        print(json.dumps(result))

    # Nothing may be written to the log after this line when a restart is
    # coming: systemd stops the whole cgroup, this process very much included,
    # so the page's only reliable signal that the update got to the end is a
    # marker put down before the restart is ever asked for.
    restarting = result.get("updated") and not args.no_restart
    say(DONE + (" restarting" if restarting else " idle"))

    if restarting:
        restart_services()
    elif result.get("updated"):
        say("==> Not restarting, as asked. The new code runs from the next restart.")
    return 0 if result.get("ok") and not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
