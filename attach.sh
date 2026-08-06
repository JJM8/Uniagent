#!/usr/bin/env bash
# Uniagent: make THIS folder a service on THIS machine.
#
#   ./attach.sh                register and start, asking which port
#   ./attach.sh --port 8790    the same without the question
#   ./attach.sh --no-start     write the services but don't start them
#   ./attach.sh --remove       unhook it again
#
# This is the second half of install.sh and nothing else. install.sh downloads
# Uniagent and walks you through a password, a port and a first provider; this
# assumes all of that is long done and does only the part that belongs to the
# machine you are standing at: find a Python that can run the code, write the
# two systemd user services with this folder's path in them, and start them.
#
# It never clones, never pulls, never asks about API keys, and never touches
# your .env beyond the port. Copy the whole folder somewhere else - a second
# machine, a USB stick - run this, and the copy is a running service there too,
# with the same chats, settings, skills and keys it had at home.
#
# Nothing inside the folder records where the folder is, so the same copy can be
# attached on several machines at once, each on its own port. Move the folder
# afterwards and the services will point at where it used to be: run this again
# from the new place and it takes over.

set -euo pipefail

# The folder this script is sitting in, symlinks resolved. Every path below is
# built from it, which is the whole point: there is no baked-in install location.
SELF="${BASH_SOURCE[0]}"
SELF="$(readlink -f "$SELF" 2>/dev/null || echo "$SELF")"
ROOT="$(cd "$(dirname "$SELF")" && pwd)"

UNIT_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"
SERVER_UNIT="uniagent-server.service"
CRON_UNIT="uniagent-cron.service"
ENV_FILE="$ROOT/.env"

if [ -t 1 ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[90m'; N=$'\033[0m'
else
    B=''; G=''; Y=''; R=''; D=''; N=''
fi

step() { printf '%s==>%s %s\n' "$B" "$N" "$1"; }
warn() { printf '%s!!%s  %s\n' "$Y" "$N" "$1" >&2; }
die()  { printf '%sxx%s  %s\n' "$R" "$N" "$1" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- 0. arguments ----------------------------------------------------------

WANT_PORT=""
START=1
REMOVE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --remove|--uninstall|--detach) REMOVE=1 ;;
        --no-start)                    START=0 ;;
        --port)                        shift; WANT_PORT="${1:-}" ;;
        --port=*)                      WANT_PORT="${1#--port=}" ;;
        -h|--help)
            sed -n '2,24p' "$SELF" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "Unknown option: $1  (try --help)" ;;
    esac
    shift
done

# --- 1. detaching ----------------------------------------------------------

if [ "$REMOVE" -eq 1 ]; then
    step "Removing Uniagent's services from this machine"
    systemctl --user disable --now "$SERVER_UNIT" "$CRON_UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVER_UNIT" "$UNIT_DIR/$CRON_UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    # Only our own shim - one written by an install.sh elsewhere on this machine
    # points at a different folder and is not ours to delete.
    if [ -f "$BIN_DIR/uniagentcli" ] && grep -qF "$ROOT/scripts/cli.py" "$BIN_DIR/uniagentcli" 2>/dev/null; then
        rm -f "$BIN_DIR/uniagentcli"
    fi
    printf '%sDone.%s The folder itself is untouched - your chats, settings and .env are all still in %s.\n' \
        "$G" "$N" "$ROOT"
    exit 0
fi

# --- 2. a Python that can actually run this --------------------------------

# Usable means: at least 3.10 (the code uses match statements and modern
# typing), and it can import the two things Uniagent needs. That second half is
# what makes a copied folder work. A .venv carried over from another machine
# looks perfectly fine - the directory is all there - but its pyvenv.cfg names a
# Python that isn't on this machine and its lib/ is pinned to a version that may
# not be either, so it fails at the first import rather than at a path check.
py_ok() {
    have "$1" || return 1
    "$1" -c 'import sys; assert sys.version_info >= (3, 10); import requests, cryptography' >/dev/null 2>&1
}

step "Looking for a Python to run Uniagent with"
PY=""
for candidate in "$ROOT/.venv/bin/python3" "$ROOT/.venv/bin/python" python3 python; do
    if py_ok "$candidate"; then
        PY="$(command -v "$candidate")"
        break
    fi
done

if [ -z "$PY" ]; then
    # Nothing here can run it yet, so build a virtualenv inside the folder. This
    # is the one step that wants a network, and it is dependencies only - no
    # clone, no config, nothing of yours touched.
    have python3 || die "No python3 on this machine. Install Python 3.10 or newer and run this again."
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "This machine's python3 is older than 3.10. Install a newer one and run this again."

    step "None of them had the dependencies - building $ROOT/.venv"
    # --copies rather than the default symlink: a FAT or exFAT stick cannot hold
    # a symlink at all, and that is exactly where a folder like this ends up.
    rm -rf "$ROOT/.venv"
    python3 -m venv --copies "$ROOT/.venv" \
        || die "Could not create the virtualenv. On Debian or Ubuntu: sudo apt install python3-venv"
    PY="$ROOT/.venv/bin/python3"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r "$ROOT/requirements.txt" \
        || die "Installing the dependencies failed - check the network and run this again."
    "$PY" -m pip install --quiet -r "$ROOT/requirements-voice.txt" 2>/dev/null \
        || printf '%s    (skipped the optional voice extras - browser hold-to-talk works regardless)%s\n' "$D" "$N"
    py_ok "$PY" || die "The virtualenv was built but still cannot import requests and cryptography."
fi

printf '%s    using %s%s\n' "$D" "$PY" "$N"

# --- 3. .env ---------------------------------------------------------------

# Normally this exists, because the folder was copied from a working install and
# brought its keys with it. It only doesn't when someone deliberately left it
# behind, and then a blank one is the right thing: the server generates a
# password into it on first start and the settings page fills in the rest.
if [ ! -f "$ENV_FILE" ]; then
    warn "No .env in this folder - starting a blank one. Add your API keys on the settings page."
    cp "$ROOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

# --- 4. which port ---------------------------------------------------------

if [ -n "$WANT_PORT" ]; then
    case "$WANT_PORT" in
        ''|*[!0-9]*) die "--port wants a number, not '$WANT_PORT'." ;;
    esac
    [ "$WANT_PORT" -ge 1 ] && [ "$WANT_PORT" -le 65535 ] || die "A port has to be between 1 and 65535."
    "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import provider; provider.set_env("UNIAGENT_HTTPS_PORT", sys.argv[2])' \
        "$ROOT/scripts" "$WANT_PORT"
elif [ -t 0 ] || [ -e /dev/tty ]; then
    # The wizard's port question, and only that one - the password is already in
    # the .env that came with the folder, and so are the providers. Asking again
    # would be asking you to re-enter things you have.
    "$PY" "$ROOT/scripts/setup_wizard.py" --port-only \
        || warn "Port question skipped - keeping whatever .env already says."
fi

# Read back rather than remembered, so this is right whether the question was
# asked, answered with Enter, skipped, or never reached.
HTTPS_PORT="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import provider; print(provider.port("UNIAGENT_HTTPS_PORT", 8764))' "$ROOT/scripts" 2>/dev/null || echo 8764)"

# --- 5. the uniagentcli command --------------------------------------------

step "Pointing the uniagentcli command at this folder"
mkdir -p "$BIN_DIR"
# Deleted rather than overwritten: on a machine set up the old way this path is
# a symlink into a checkout, and "cat >" writes straight through a symlink to
# its target - which would overwrite scripts/uniagentcli with a copy carrying
# this machine's absolute paths.
rm -f "$BIN_DIR/uniagentcli"
cat > "$BIN_DIR/uniagentcli" <<EOF
#!/bin/sh
# Generated by Uniagent's attach.sh. Re-run it to regenerate.
exec "$PY" "$ROOT/scripts/cli.py" "\$@"
EOF
chmod +x "$BIN_DIR/uniagentcli"

PATH_OK=1
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        PATH_OK=0
        touched=0
        for rc in "$HOME/.profile" "$HOME/.bashrc"; do
            [ -f "$rc" ] || continue
            touched=1
            grep -qs '\.local/bin' "$rc" && continue
            printf '\n# Added by Uniagent\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
        done
        if [ "$touched" -eq 0 ]; then
            printf '# Added by Uniagent\nexport PATH="$HOME/.local/bin:$PATH"\n' > "$HOME/.profile"
        fi
        ;;
esac

# --- 6. the services -------------------------------------------------------

if ! have systemctl || [ -z "${XDG_RUNTIME_DIR:-}" ] || ! systemctl --user show-environment >/dev/null 2>&1; then
    warn "No systemd user session here, so autostart was not installed."
    warn "Start Uniagent by hand with:  $PY $ROOT/scripts/server.py"
    exit 0
fi

# Where the services pointed before, if anywhere. Worth saying out loud: running
# this from a second copy silently moves your autostart onto that copy, and
# finding out by wondering why your edits do nothing is a bad afternoon.
PREVIOUS=""
if [ -f "$UNIT_DIR/$SERVER_UNIT" ]; then
    PREVIOUS="$(sed -n 's|^WorkingDirectory=\(.*\)/scripts$|\1|p' "$UNIT_DIR/$SERVER_UNIT" | head -1)"
fi

step "Writing the systemd user services"
mkdir -p "$UNIT_DIR"

# RequiresMountsFor and ConditionPathExists are the removable-drive half of
# this. Without them, booting with the stick unplugged gives you a service that
# fails, restarts, fails, restarts, forever, filling the journal. With them,
# systemd waits for the mount if it is coming and quietly skips the service if
# it isn't - and `systemctl --user start uniagent-server` once you plug the
# stick back in picks it straight up.
cat > "$UNIT_DIR/$SERVER_UNIT" <<EOF
[Unit]
Description=Uniagent web server - the chat UI on https://localhost:$HTTPS_PORT
After=network-online.target
Wants=network-online.target
RequiresMountsFor="$ROOT"
ConditionPathExists=$ROOT/scripts/server.py

[Service]
Type=simple
# Unbuffered, so the agent's output reaches the journal live instead of sitting
# in a block buffer until the process exits.
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$ROOT/scripts
ExecStart="$PY" "$ROOT/scripts/server.py"
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/$CRON_UNIT" <<EOF
[Unit]
Description=Uniagent cron watcher - fires scheduled prompts from cron.json
After=network-online.target
Wants=network-online.target
RequiresMountsFor="$ROOT"
ConditionPathExists=$ROOT/scripts/cron.py

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$ROOT/scripts
ExecStart="$PY" "$ROOT/scripts/cron.py"
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --quiet "$SERVER_UNIT" "$CRON_UNIT"

if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$ROOT" ]; then
    warn "These services used to run $PREVIOUS. They now run this folder instead."
fi

# Without lingering, user services only run while you are logged in, so a
# rebooted machine comes up with no Uniagent until someone signs in. This is the
# one step that may ask for a password, and the rest is fine if it is refused.
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    step "Enabling lingering so the services survive logout and start at boot"
    if ! loginctl enable-linger "$USER" 2>/dev/null; then
        if have sudo && sudo -n true 2>/dev/null; then
            sudo loginctl enable-linger "$USER" 2>/dev/null || true
        fi
    fi
    loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes' \
        || warn "Could not enable lingering. Uniagent will start when you log in, but not before. Fix with: sudo loginctl enable-linger $USER"
fi

if [ "$START" -eq 0 ]; then
    printf '\n%sRegistered.%s Not started, as asked. Start it with: systemctl --user start %s %s\n' \
        "$G" "$N" "$SERVER_UNIT" "$CRON_UNIT"
    exit 0
fi

systemctl --user restart "$SERVER_UNIT" "$CRON_UNIT"

# --- 7. wait for it, then say where it is ----------------------------------

step "Waiting for the server to come up..."
password=""
up=0
for _ in $(seq 1 45); do
    # stderr silenced BEFORE the /dev/tcp redirect, not after: bash applies
    # redirections left to right, so with 2>/dev/null written last the failed
    # connect has already printed "Connection refused" to a still-open stderr.
    if [ "$up" -eq 0 ] && : 2>/dev/null <"/dev/tcp/127.0.0.1/$HTTPS_PORT"; then
        up=1
    fi
    if [ -z "$password" ]; then
        # \042 and \047 are " and ' - .env strips surrounding quotes when it is
        # read, so a quoted password is not the password that gets checked.
        password="$(sed -n 's/^UNIAGENT_PASSWORD=//p' "$ENV_FILE" | tr -d '\042\047' | head -1)"
    fi
    [ "$up" -eq 1 ] && [ -n "$password" ] && break
    sleep 2
done

echo
if [ "$up" -eq 1 ] && [ -n "$password" ]; then
    printf '%s==========================================================%s\n' "$G" "$N"
    printf '%s  Uniagent is running from %s%s\n' "$G" "$ROOT" "$N"
    printf '%s  Password:  %s%s\n' "$G" "$password" "$N"
    printf '%s==========================================================%s\n' "$G" "$N"
elif [ "$up" -eq 1 ]; then
    warn "Running, but no password turned up in $ENV_FILE. Look for it in: journalctl --user -u $SERVER_UNIT"
else
    warn "The server has not answered on port $HTTPS_PORT yet. It may still be starting."
    warn "Check it with: systemctl --user status $SERVER_UNIT"
fi

echo
printf '  Web UI:     https://localhost:%s   (other devices: https://<this-machine-ip>:%s)\n' "$HTTPS_PORT" "$HTTPS_PORT"
printf '              First visit warns about the self-signed certificate - Advanced, then Proceed.\n'
if [ "$PATH_OK" -eq 1 ]; then
    printf '  CLI:        uniagentcli "a question"\n'
else
    printf '  CLI:        uniagentcli "a question"   (open a new terminal first - PATH changed)\n'
fi
printf '  Logs:       journalctl --user -u %s -f\n' "$SERVER_UNIT"
printf '  Restart:    systemctl --user restart %s %s\n' "$SERVER_UNIT" "$CRON_UNIT"
printf '  Detach:     %s/attach.sh --remove\n' "$ROOT"
