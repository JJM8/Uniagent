"""The password on the front door, and the session cookie behind it.

server.py checks every request against this before it does anything else. There
is exactly one password, it lives in .env as UNIAGENT_PASSWORD, and it is stored
in plain text alongside the API keys that are already there - a password sitting
next to your OpenAI key is not a new secret to protect, it's the same one.

FIRST RUN generates a password rather than starting without one. There is never
a moment where the server is up and unprotected: no "set a password" screen,
because that screen would itself have to be reachable without a password, and on
a 0.0.0.0 bind that hands the first person to load it a shell on this machine.
The generated password is printed once, and the alphabet it's drawn from has no
l/1/O/0 in it because the whole point is typing it into a phone.

SESSIONS ARE SIGNED, NOT STORED. The cookie is <nonce>.<expiry>.<hmac>, where
the hmac is over the first two fields keyed by the password itself. Nothing is
kept in memory, so:
  - restarting the server (POST /restart re-execs it) does not log anyone out,
  - changing the password in .env invalidates every existing cookie for free,
    because the key that signed them is gone.

None of this helps over plain http, where the password and the cookie both
cross the network in the clear. server.py serves the app on https only and
redirects http to it; that is not decoration, it is the other half of this file.
"""

import hmac
import os
import secrets
import time
from hashlib import sha256
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env"
ENV_NAME = "UNIAGENT_PASSWORD"

# 30 days, refreshed on every request that carries a valid cookie - so a phone
# you use weekly stays logged in and one you lend out doesn't, forever.
SESSION_DAYS = 30

# No l/1/I/O/0: this gets typed on a phone keyboard, and a password you can't
# read off the screen is one you'll replace with something weak. 12 characters
# from 31 is about 59 bits, which is far past what the lockout below allows
# anyone to work through.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# Wrong-password lockout, per IP. Five free attempts (fat fingers on a phone),
# then a doubling wait capped at an hour. Against the generated password this is
# irrelevant; it matters the moment someone sets the password to their dog's
# name, which is exactly when it should.
FREE_TRIES = 5
BASE_LOCKOUT = 60
MAX_LOCKOUT = 3600

_fails = {}  # ip -> [consecutive failures, locked-until timestamp]


def _read_env():
    try:
        return ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _generate():
    """A new password, written into .env and returned. Printed here rather than
    by the caller because this happens exactly once in the life of an install
    and the line must not get lost in whatever the caller decides to log."""
    value = "-".join("".join(secrets.choice(ALPHABET) for _ in range(4))
                     for _ in range(3))
    lines = _read_env()
    lines.append(ENV_NAME + "=" + value)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The file already held every API key; now it holds the door key too.
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass
    print("\n" + "=" * 58)
    print("  Uniagent has generated a password for the web interface:")
    print("\n      " + value + "\n")
    print("  It is saved in " + str(ENV_FILE) + " as " + ENV_NAME + ".")
    print("  Change it there to anything you like, then restart.")
    print("=" * 58 + "\n")
    return value


def password():
    """The current password, generating and saving one if .env hasn't got a
    non-blank UNIAGENT_PASSWORD. Read fresh from the file every time rather
    than cached, so editing .env and restarting is the whole update story - and
    so a password changed by hand takes effect without a code path that could
    still be holding the old one."""
    for line in _read_env():
        line = line.strip()
        if line.startswith(ENV_NAME + "="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    return _generate()


def check_password(candidate):
    """Whether `candidate` is the password. compare_digest, not ==, so the
    comparison doesn't return faster on a wrong first character."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate, password())


# --- Session cookies ---

def _sign(nonce, expiry):
    msg = (nonce + "." + str(expiry)).encode()
    return hmac.new(password().encode(), msg, sha256).hexdigest()


def new_session():
    """A fresh signed cookie value for someone who just gave the right
    password."""
    nonce = secrets.token_hex(8)
    expiry = int(time.time()) + SESSION_DAYS * 86400
    return nonce + "." + str(expiry) + "." + _sign(nonce, expiry)


def valid_session(token):
    """Whether this cookie is one we signed and hasn't expired. Any malformed
    token is simply invalid - a caller never learns which part was wrong."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, expiry, sig = parts
    try:
        if int(expiry) < time.time():
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sig, _sign(nonce, expiry))


# --- Lockout ---

def locked_for(ip):
    """Seconds this IP still has to wait before another guess is accepted, or 0
    if it can try now."""
    entry = _fails.get(ip)
    if not entry:
        return 0
    remaining = entry[1] - time.time()
    return int(remaining) + 1 if remaining > 0 else 0


def note_failure(ip):
    """Count a wrong password and start (or extend) this IP's lockout."""
    entry = _fails.setdefault(ip, [0, 0])
    entry[0] += 1
    if entry[0] >= FREE_TRIES:
        wait = min(BASE_LOCKOUT * 2 ** (entry[0] - FREE_TRIES), MAX_LOCKOUT)
        entry[1] = time.time() + wait


def note_success(ip):
    """Clear an IP's failures - it proved it knows the password."""
    _fails.pop(ip, None)
