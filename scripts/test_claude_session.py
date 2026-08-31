"""Check that a persistent Claude Code session actually works end to end.

Run it by hand - `python3 scripts/test_claude_session.py` - after `claude login`.
It spends real subscription usage and really runs the tool it is approved to
run, which is the point: the things worth testing here (a gate that fires, a
session that remembers, a model swapped under a live conversation) have no
meaning against a mock.

Three turns, checking the three claims claude_session.py makes:

  1  a tool call comes back to Uniagent to be approved, and only runs when
     Uniagent says yes
  2  the session REMEMBERS turn 1 without turn 2 resending it
  3  a denial stops the call happening at all
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import claude_session
import settings
import tool_processor

CHAT = "test-claude-session"
MARKER = "uniagent-session-probe-42"


def main():
    tool_processor.load_tools()
    chosen = settings.load()
    system = ("You are Uniagent, running on this machine. Be terse. "
              "When asked to run a shell command, run it with a tool.")

    turns, gated, answer = [], [], {"allow": True}

    def sync():
        pass

    def approve(question):
        print("\n  [approval] " + question[:90])
        print("  [approval] Uniagent answers: " + ("yes" if answer["allow"] else "NO"))
        return answer["allow"]

    def on_text(piece):
        print(piece, end="", flush=True)

    def on_tool_call(shown, name=None, id=None):
        gated.append(shown)
        print("\n  [tool call] " + shown[:120])

    def on_tool_result(text, name=None, spent=None, id=None):
        print("  [tool result] " + text.strip()[:120].replace("\n", " | "))

    def on_safety(safe, reason, checked=True):
        print("\n  [safety] safe=" + str(safe) + " checked=" + str(checked)
              + " - " + str(reason)[:80])

    def turn(text, threshold=0):
        print("\n\n=== USER: " + text + "\n")
        claude_session.run_turn(
            turns, sync, text, CHAT, "claude-sub", "sonnet", system,
            None, chosen, approve, on_text=on_text, on_tool_call=on_tool_call,
            on_tool_result=on_tool_result, on_safety=on_safety,
            safety_threshold=threshold)

    try:
        # 1 - a real tool call, approved through Uniagent.
        turn("Run this exact shell command and tell me what it printed: echo " + MARKER)
        first = claude_session.active().get(CHAT, {})
        assert gated, "no tool call was ever put to Uniagent"
        ran = json.dumps(turns)
        assert MARKER in ran, "the command's output never reached the transcript"
        print("\n\n  OK 1: tool call gated by Uniagent, approved, and it ran")

        # 2 - the session remembers, with nothing resent.
        before = len(turns)
        turn("Without running anything, what did that command print?")
        said = " ".join(t.get("content") or "" for t in turns[before:]
                        if t.get("role") == "assistant")
        assert MARKER in said, "the session did not remember turn 1: " + said[:200]
        assert claude_session.active().get(CHAT, {}).get("since") == first.get("since"), \
            "the session was rebuilt between turns instead of reused"
        print("\n\n  OK 2: same session, and it remembered without resending history")

        # 3 - a denial actually stops it.
        answer["allow"] = False
        before = len(turns)
        turn("Run this exact shell command: echo denied-" + MARKER)
        ran = json.dumps(turns[before:])
        assert "denied-" + MARKER not in ran or "DENIED" in ran, \
            "a denied call appears to have run anyway"
        print("\n\n  OK 3: denial stopped the call")
    finally:
        claude_session.close(CHAT)

    print("\nall three passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
