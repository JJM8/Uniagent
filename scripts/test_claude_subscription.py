"""Check that the claude-subscription provider actually works on this machine.

Run it by hand - `python3 scripts/test_claude_subscription.py` - after a fresh
`claude login`, or whenever that provider starts misbehaving. It talks to the
real Claude Code CLI and spends real subscription usage, which is the point:
the interesting failures here (expired login, spent usage window, missing CLI)
only exist outside a mock.

    python3 scripts/test_claude_subscription.py [model]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import provider


def main():
    # Found by WIRE, not by name: the provider on this wire is an ordinary
    # editable object and may be called anything at all by now.
    name = next((p["name"] for p in provider.custom_providers()
                 if p["wire"] == "claude-subscription"), None)
    if not name or name not in provider.available():
        print("not available: needs a provider on the claude-subscription wire, "
              "the Claude Code CLI on PATH and the Agent SDK")
        print("  pip install --user claude-agent-sdk && claude login")
        return 1
    model = sys.argv[1] if len(sys.argv) > 1 else provider.default_model(name)
    print("model: " + model + "\nreply: ", end="", flush=True)

    # Streamed rather than collected, because streaming is the part with
    # moving pieces - a thread, a queue and a cancel flag bridging the SDK's
    # async loop to this file's plain generators.
    reply = ""
    try:
        for piece in provider.stream_response(
                "Reply with exactly: OK", provider=name, model=model):
            reply += piece
            print(piece, end="", flush=True)
    except Exception as e:
        print("\nFAILED: " + str(e))
        return 1

    print()
    if reply.strip() == "OK":
        return 0
    # Not a failure. The provider did its job; the model just editorialised.
    print("(expected exactly 'OK' - the provider works, the model was chatty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
