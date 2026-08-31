"""Cut oversized tool results out of a chat that already has them.

    python3 scripts/trim_history.py                 # look at every chat, change nothing
    python3 scripts/trim_history.py chat-0084c45f   # look at one chat
    python3 scripts/trim_history.py --apply chat-0084c45f

tool_results.clamp() bounds every result from the moment it is installed, but
it can do nothing about the ones already sitting in a history - and those are
the expensive ones, because a result in a history is not paid for once. It is
re-sent on every turn for the rest of that chat's life. chat-0084c45f was
carrying 2,037,177 characters in a single turn: ~435k tokens on a conversation
that was otherwise 20k, forever, and /compact is no escape because compacting
a chat means sending that history to the model first.

So this runs the same clamp over what is already on disk. The full text is
written into the chat's own results/ folder exactly as a clipped result would
have written it, so nothing is destroyed - it just stops being carried.

It is deliberately a command you run and not a sweep that happens at startup.
Rewriting somebody's transcript is not a thing to do quietly in the
background, and it prints what it would do until you add --apply.

STOP THE SERVER FIRST when applying. A running server holds open chats in
memory and writes them back on the next turn, which would put the 2MB
straight back.
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import main
import tool_results

# What counts as worth cutting. Above the budget clamp() would have trimmed it
# anyway; the margin keeps this from rewriting a history to save a few hundred
# characters, which is churn rather than a fix.
FLOOR = tool_results.BUDGET * 2


def oversized(turns):
    """The (index, size) of every tool result in `turns` too big to carry."""
    out = []
    for i, t in enumerate(turns):
        if not isinstance(t, dict) or t.get("role") != "tool":
            continue
        content = t.get("content")
        if isinstance(content, str) and len(content) > FLOOR:
            out.append((i, len(content)))
    return out


def chats():
    """Every chat folder that has a history, deepest routes included - a cron
    run's transcript is a history like any other and can carry the same 2MB."""
    if not main.CHATS.exists():
        return []
    found = sorted(main.CHATS.rglob(main.HISTORY_FILE))
    # Neither an archived copy nor a deleted one is ever sent to a model
    # again, so trimming them would cost work and save nothing - and the
    # archive is specifically the untouched copy this script promises to
    # leave behind, which rewriting would rather defeat.
    skip = ("chat_archive", "deleted_chats")
    return [p for p in found if not any(s in p.parts for s in skip)]


def trim(path, apply):
    """Report what `path` is carrying, and rewrite it when apply is set.
    Returns the characters removed."""
    cid = "/".join(path.parent.relative_to(main.CHATS).parts)
    try:
        turns = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("  ! " + cid + ": unreadable (" + type(e).__name__ + ")")
        return 0
    if not isinstance(turns, list):
        return 0
    big = oversized(turns)
    if not big:
        return 0

    # Measured the way context_segments() measures it - the content of each
    # turn and nothing else. The file on disk is bigger (tool_calls, timing,
    # a thinking model's reasoning_content, JSON escaping), but that is not
    # what the context panel counts and not what the window fills with, so
    # reporting it here would quote a number nothing else in the app agrees
    # with.
    was = sum(len(str(t.get("content"))) for t in turns
              if isinstance(t, dict) and t.get("content"))
    saved = 0
    for i, size in big:
        clipped = tool_results.clamp(turns[i]["content"], _tool_of(turns, i), cid)
        saved += size - len(clipped)
        print("  " + cid + " turn " + str(i) + ": " + f"{size:,}"
              + " -> " + f"{len(clipped):,}" + " chars"
              + ("" if apply else "   (dry run)"))
        if apply:
            turns[i]["content"] = clipped

    if apply:
        # The whole folder first, the same way compaction does it - a rewrite
        # you cannot undo is not one worth having.
        destination = _archive(path.parent)
        path.write_text(json.dumps(turns, indent=2), encoding="utf-8")
        print("    archived to " + str(destination))
    print("    chat was ~" + f"{was:,}" + " chars, "
          + ("now" if apply else "would be") + " ~" + f"{was - saved:,}")
    return saved


def _tool_of(turns, i):
    """Which tool produced the result at `i`, for the saved file's name. Found
    by matching the result's tool_call_id back to the call that made it; a
    history old enough not to have one just gets a generic name."""
    want = turns[i].get("tool_call_id")
    if want:
        for t in reversed(turns[:i]):
            for c in (t.get("tool_calls") or []) if isinstance(t, dict) else []:
                if c.get("id") == want:
                    return (c.get("function") or {}).get("name") or "tool"
    return "tool"


def _archive(folder):
    """A copy of the chat's folder in chats/chat_archive/, never overwriting
    an existing one (compaction.archive's rule, and for the same reason)."""
    root = main.CHATS / "chat_archive"
    root.mkdir(parents=True, exist_ok=True)
    base = folder.name + "-untrimmed"
    destination, n = root / base, 2
    while destination.exists():
        destination = root / (base + "-" + str(n))
        n += 1
    shutil.copytree(folder, destination)
    return destination


def run(names, apply):
    paths = chats()
    if names:
        wanted = set(names)
        paths = [p for p in paths
                 if p.parent.name in wanted
                 or "/".join(p.parent.relative_to(main.CHATS).parts) in wanted]
        if not paths:
            print("no chat matched: " + ", ".join(names))
            return 1
    total = sum(trim(p, apply) for p in paths)
    if not total:
        print("nothing oversized - no tool result above "
              + f"{FLOOR:,}" + " characters.")
        return 0
    print("\n" + ("removed " if apply else "would remove ") + f"{total:,}"
          + " characters (~" + f"{total // 4:,}" + " tokens) from every future"
          " turn of those chats.")
    if not apply:
        print("nothing was changed. Re-run with --apply to do it.")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    sys.exit(run(args, "--apply" in sys.argv[1:]))
