"""First-run setup: the few questions worth asking once, right after install.

Run by install.sh (and install.ps1) as the last step, and safe to run again by
hand at any time:

    ~/Uniagent/.venv/bin/python3 ~/Uniagent/scripts/setup_wizard.py

Three questions - a password, a port, and one provider to talk to - because
that is the shortest path from "installed" to "actually answers you". Anything
more belongs on the settings page, which can do all of it better and with a
mouse.

NOTHING HERE IS A SECOND SOURCE OF TRUTH. The wire list comes from
provider.WIRES, whether a wire wants a key from provider.wants_key(), what its
URL box is called from provider.base_url_label(), and the provider object is
created by provider.save_custom_provider() - the same call the settings page
makes. Adding a wire (openrouter, say) is an edit to provider.py and nothing
here needs to know about it: it shows up in this menu on its own, with the
right questions attached.
"""

import os
import sys
from pathlib import Path

# Run as a script from anywhere, so scripts/ has to go on the path before the
# imports below - the installer calls this by absolute path from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth
import provider
from cli_md import ACCENT, BOLD, DIM, MUTE, POP, RED, RESET, TEXT

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


# --- talking to the person at the keyboard ---------------------------------

def _open_tty():
    """Somewhere to read answers from, or None if there is nobody there.

    This matters more than it looks. The installer is run as
    `curl ... | bash`, which makes the SCRIPT bash's stdin - so reading
    sys.stdin here would consume installer text or hit EOF instantly rather
    than reaching the person at the keyboard. The controlling terminal is the
    one thing that is still the user in both cases, so we read that directly.

    None means no terminal at all (a cron job, a container build, CI). The
    caller skips the wizard rather than hanging forever on a question nobody
    can answer.
    """
    for name in ("/dev/tty", "CONIN$"):        # POSIX, then the Windows console
        try:
            return open(name, "r")
        except OSError:
            continue
    return sys.stdin if sys.stdin and sys.stdin.isatty() else None


TTY = None


def ask(question, default="", secret=False):
    """One question. Enter alone takes `default`.

    `secret` hides the typing where the terminal allows it - an API key should
    not be left sitting on screen, or in the scrollback of a shared machine.
    """
    hint = DIM + " [" + (("*" * 8) if secret and default else str(default)) + "]" + RESET if default else ""
    sys.stdout.write(TEXT + "  " + question + hint + DIM + " > " + RESET)
    sys.stdout.flush()
    if secret:
        try:
            import termios
            fd = TTY.fileno()
            saved = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] &= ~termios.ECHO                     # index 3 is lflags
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            try:
                answer = TTY.readline()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            print()                                     # the Enter that wasn't echoed
        except Exception:
            answer = TTY.readline()                     # no echo control - carry on visibly
    else:
        answer = TTY.readline()
    if not answer:
        raise EOFError
    answer = answer.strip()
    return answer or str(default)


def ask_yes(question, default=True):
    hint = "Y/n" if default else "y/N"
    answer = ask(question + DIM + " (" + hint + ")" + RESET, "yes" if default else "no")
    return answer.strip().lower() not in ("n", "no")


def heading(text):
    print("\n" + BOLD + ACCENT + "  " + text + RESET)


def note(text):
    print(DIM + "  " + text + RESET)


def good(text):
    print(POP + "  " + text + RESET)


def bad(text):
    print(RED + "  " + text + RESET)


# --- the questions ---------------------------------------------------------

def step_password():
    """Set UNIAGENT_PASSWORD, offering a generated one as the default.

    auth.password() only generates when .env has no non-blank value, so writing
    one here simply means the server never has to invent its own - and the
    person installing gets to know it up front rather than digging it out of a
    log afterwards.
    """
    heading("Password for the web interface")
    current = provider._env_value(auth.ENV_NAME)
    if current:
        note("One is already set. Enter alone keeps it.")
        suggestion = current
    else:
        import secrets
        suggestion = "-".join("".join(secrets.choice(auth.ALPHABET) for _ in range(4))
                              for _ in range(3))
        note("Enter alone accepts the generated one.")
    value = ask("Password", suggestion)
    provider.set_env(auth.ENV_NAME, value)
    good("Password set: " + value)
    return value


def _port_free(number):
    """Whether we could actually bind that port right now. Cheap, and it turns
    'it silently failed to start' into a question asked before it happens."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", number))
            return True
        except OSError:
            return False


def step_port():
    """Set UNIAGENT_HTTPS_PORT - the one you actually browse to.

    Only the https port is asked about. The plain one is a redirect and nothing
    else, so a second question about it would be a question about plumbing.
    It is moved out of the way automatically if the chosen port lands on it.
    """
    heading("Port for the web interface")
    note("This is the address you open in a browser. Enter alone keeps the default.")
    while True:
        raw = ask("Port", provider.port("UNIAGENT_HTTPS_PORT", 8764))
        try:
            number = int(str(raw).strip())
        except ValueError:
            bad("That is not a number.")
            continue
        if not 1 <= number <= 65535:
            bad("A port has to be between 1 and 65535.")
            continue
        if number < 1024 and os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
            bad("Ports below 1024 need root, and Uniagent does not run as root.")
            continue
        if not _port_free(number):
            note("Something is already listening on " + str(number) + ".")
            if not ask_yes("Use it anyway?", default=False):
                continue
        break

    provider.set_env("UNIAGENT_HTTPS_PORT", str(number))
    # The redirect port only matters when it would collide with the real one.
    plain = provider.port("UNIAGENT_PORT", 8763)
    if plain == number:
        plain = number - 1 if number > 1024 else number + 1
        provider.set_env("UNIAGENT_PORT", str(plain))
        note("Moved the plain-http redirect to " + str(plain) + " to keep them apart.")
    good("Web interface on https://localhost:" + str(number))
    return number


def step_provider():
    """Create one provider object, picked from whatever wires exist today."""
    heading("Model provider")
    existing = provider.custom_providers()
    if existing:
        note("Already configured: " + ", ".join(p["name"] for p in existing))
        if not ask_yes("Add another?", default=False):
            return
    else:
        note("Uniagent ships with no providers - it needs one to answer you at all.")

    # Straight from provider.WIRES, so a wire added later is simply here.
    wires = sorted(provider.WIRES)
    print()
    for i, wire in enumerate(wires, 1):
        label = provider.wire_label(wire)
        extra = "" if label == wire else DIM + "  " + label + RESET
        print("    " + ACCENT + str(i) + RESET + ". " + TEXT + wire + RESET + extra)
    print("    " + DIM + "0. skip - set one up on the settings page later" + RESET)
    print()

    while True:
        choice = ask("Which wire?", "1")
        if choice.strip() == "0":
            note("Skipped. Add one on the settings page whenever you like.")
            return
        # A name is accepted as readily as a number - typing "openai" is the
        # obvious thing to do and refusing it would be pedantry.
        if choice.strip().lower() in wires:
            wire = choice.strip().lower()
            break
        try:
            wire = wires[int(choice) - 1]
            break
        except (ValueError, IndexError):
            bad("Pick a number from the list, or type the wire's name.")

    taken = {p["name"] for p in provider.custom_providers()}
    default_name = wire if wire not in taken else wire + "-2"
    while True:
        name = ask("Name for it", default_name).strip().lower()
        if name in taken:
            bad("There is already a provider called " + name + ".")
            continue
        break

    key = ""
    if provider.wants_key(wire):
        key = ask("API key", "", secret=True)
        if not key:
            note("No key - the provider is saved but will not work until one is added.")

    base_url = ""
    label = provider.base_url_label(wire)
    if label:
        default_url = provider.WIRE_DEFAULT_URL.get(wire, "")
        base_url = ask(label.capitalize(), default_url)
        if base_url == default_url:
            base_url = ""      # saved blank means "follow the wire's default"

    try:
        provider.save_custom_provider(name, wire=wire, base_url=base_url, key=key)
    except ValueError as e:
        bad(str(e))
        return
    good("Created provider '" + name + "' on the " + wire + " wire.")


# --- the whole thing -------------------------------------------------------

def main():
    global TTY
    TTY = _open_tty()
    if TTY is None:
        # No terminal: an unattended install. Silence rather than a hang.
        print("No terminal available - skipping setup. "
              "Run scripts/setup_wizard.py by hand to configure Uniagent.")
        return 0

    print()
    print(BOLD + ACCENT + "  UNIAGENT" + RESET + DIM + "   first-run setup" + RESET)
    print(DIM + "  Three questions. Everything here can be changed later on the "
          "settings page." + RESET)

    try:
        step_password()
        port = step_port()
        step_provider()
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C or a closed pipe half way through. Whatever was answered is
        # already written, so say so rather than implying it was all lost.
        print()
        note("Setup stopped. Anything answered so far has been saved; "
             "run scripts/setup_wizard.py again to finish.")
        return 1

    heading("Done")
    print(TEXT + "  Open " + POP + "https://localhost:" + str(port) + RESET)
    note("Your browser will warn about the certificate the first time - it is "
         "self-signed. Advanced, then Proceed.")
    note("Everything lives in " + str(ENV_FILE) + ".")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
