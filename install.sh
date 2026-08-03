#!/usr/bin/env bash
# Uniagent one-line installer for Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/JJM8/Uniagent/main/install.sh | bash
#
# It clones the repo, builds a .venv, writes .env, puts `uniagentcli` on your
# PATH, and installs two systemd *user* services - the web server and the cron
# watcher - so both come up at boot and restart if they die.
#
# systemd user services, not system ones, so the whole install needs no root
# except for installing git/python when they are genuinely missing. Everything
# lives under $HOME and runs as you, which is what the agent wants anyway: it
# reads your .env, writes your chats, and opens your files.
#
# Re-running is safe: an existing checkout is pulled rather than re-cloned, an
# existing .env is left alone, and the services are restarted onto the new code.
#
#   install.sh --remove   stops and removes the services and the CLI shim
#                         (the checkout and your .env are left alone)
#
# Environment:
#   UNIAGENT_HOME   where to install (default ~/Uniagent)
#   UNIAGENT_REF    branch or tag to check out (default main)

set -euo pipefail

REPO_URL="${UNIAGENT_REPO:-https://github.com/JJM8/Uniagent.git}"
ROOT="${UNIAGENT_HOME:-$HOME/Uniagent}"
REF="${UNIAGENT_REF:-main}"
HTTPS_PORT=8764   # only the fallback; re-read from .env after the setup wizard
UNIT_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"
SERVER_UNIT="uniagent-server.service"
CRON_UNIT="uniagent-cron.service"

# Colour only when we're on a terminal - piped output stays plain.
if [ -t 1 ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[90m'; N=$'\033[0m'
else
    B=''; G=''; Y=''; R=''; D=''; N=''
fi

step() { printf '%s==>%s %s\n' "$B" "$N" "$1"; }
warn() { printf '%s!!%s  %s\n' "$Y" "$N" "$1" >&2; }
die()  { printf '%sxx%s  %s\n' "$R" "$N" "$1" >&2; exit 1; }

# --- 0. uninstall ----------------------------------------------------------

if [ "${1:-}" = "--remove" ] || [ "${1:-}" = "--uninstall" ]; then
    step "Removing Uniagent's services"
    systemctl --user disable --now "$SERVER_UNIT" "$CRON_UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVER_UNIT" "$UNIT_DIR/$CRON_UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    rm -f "$BIN_DIR/uniagentcli"
    printf '%sDone.%s The checkout at %s was left in place - delete it by hand if you want it gone.\n' \
        "$G" "$N" "$ROOT"
    exit 0
fi

# --- 1. prerequisites ------------------------------------------------------

# Only ask for root when something is actually missing. On a machine that
# already has git and python3 this whole section is a no-op and the install
# never prompts for a password.
have() { command -v "$1" >/dev/null 2>&1; }

# python3 -m venv is a separate package on Debian/Ubuntu, and its absence only
# shows up when venv creation fails - so test for it up front like a binary.
have_venv() { command -v python3 >/dev/null 2>&1 && python3 -c 'import venv, ensurepip' 2>/dev/null; }

missing=()
have git || missing+=(git)
have python3 || missing+=(python3)

if [ ${#missing[@]} -gt 0 ] || ! have_venv; then
    step "Installing prerequisites (git, python3, python3-venv)"
    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then
        have sudo || die "git or python3 is missing and there is no sudo here. Install them by hand, then re-run."
        SUDO="sudo"
    fi
    if   have apt-get; then $SUDO apt-get update -qq && $SUDO apt-get install -y git python3 python3-venv python3-pip
    elif have dnf;     then $SUDO dnf install -y git python3 python3-pip
    elif have pacman;  then $SUDO pacman -Sy --noconfirm git python python-pip
    elif have zypper;  then $SUDO zypper --non-interactive install git python3 python3-pip python3-virtualenv
    elif have apk;     then $SUDO apk add --no-cache git python3 py3-pip
    else
        die "Unknown package manager. Install git and python3 (3.10+, with venv) by hand, then re-run."
    fi
fi

have git || die "git still isn't available."
have python3 || die "python3 still isn't available."

# Python 3.10+ - the codebase uses match statements and modern typing.
python3 - <<'PY' || die "Uniagent needs Python 3.10 or newer. Upgrade python3 and re-run."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# --- 2. clone or update ----------------------------------------------------

if [ -d "$ROOT/.git" ]; then
    step "Updating the existing checkout in $ROOT"
    git -C "$ROOT" fetch --quiet origin "$REF"
    # Merge rather than reset: a local .env or edited settings is the normal
    # state of a working install and must survive an update.
    git -C "$ROOT" checkout --quiet "$REF" 2>/dev/null || true
    git -C "$ROOT" merge --ff-only "origin/$REF" --quiet \
        || warn "Local commits or changes here - skipping the fast-forward. Merge by hand if you wanted the new code."
elif [ -e "$ROOT" ]; then
    die "$ROOT already exists and is not a git checkout. Move it, or set UNIAGENT_HOME to somewhere else."
else
    step "Cloning Uniagent into $ROOT"
    git clone --quiet --branch "$REF" "$REPO_URL" "$ROOT"
fi

# --- 3. virtualenv and dependencies ----------------------------------------

VENV="$ROOT/.venv"
PY="$VENV/bin/python3"

step "Installing dependencies into $VENV"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT/requirements.txt"

# Voice extras are optional and need system headers (portaudio, X11) that a
# server install won't have. Try quietly; a failure here is not a failure.
if ! "$PY" -m pip install --quiet -r "$ROOT/requirements-voice.txt" 2>/dev/null; then
    printf '%s    (skipped the optional voice extras - browser hold-to-talk works regardless;%s\n' "$D" "$N"
    printf '%s     for the desktop hotkey: sudo apt install portaudio19-dev && %s -m pip install -r %s)%s\n' \
        "$D" "$PY" "$ROOT/requirements-voice.txt" "$N"
fi

# --- 4. .env ---------------------------------------------------------------

ENV_FILE="$ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    step "Writing $ENV_FILE from the example"
    cp "$ROOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"   # it holds API keys and mail passwords
else
    step "Keeping your existing .env"
fi

# --- 5. the uniagentcli command --------------------------------------------

step "Installing the uniagentcli command into $BIN_DIR"
mkdir -p "$BIN_DIR"
# Written here rather than symlinked to scripts/uniagentcli because that shim
# calls a bare `python3`, which would miss the venv's requests/cryptography.
#
# Deleted first, and this is not tidiness: on a machine set up the old way this
# path is a SYMLINK into the repo, and "cat >" writes through a symlink to its
# target - which would overwrite scripts/uniagentcli in the checkout with a
# copy carrying this machine's absolute paths baked in. It really did; that is
# how this line came to exist.
rm -f "$BIN_DIR/uniagentcli"
cat > "$BIN_DIR/uniagentcli" <<EOF
#!/bin/sh
# Generated by Uniagent's install.sh. Re-run the installer to regenerate.
exec "$PY" "$ROOT/scripts/cli.py" "\$@"
EOF
chmod +x "$BIN_DIR/uniagentcli"

case ":$PATH:" in
    *":$BIN_DIR:"*) PATH_OK=1 ;;
    *)
        PATH_OK=0
        # ~/.profile is read by login shells, ~/.bashrc by interactive ones -
        # a new terminal needs whichever the user's setup actually sources, so
        # write to both, and create ~/.profile if this home has neither.
        touched=0
        for rc in "$HOME/.profile" "$HOME/.bashrc"; do
            [ -f "$rc" ] || continue
            touched=1
            grep -qs '\.local/bin' "$rc" && continue
            printf '\n# Added by Uniagent installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
        done
        if [ "$touched" -eq 0 ]; then
            printf '# Added by Uniagent installer\nexport PATH="$HOME/.local/bin:$PATH"\n' > "$HOME/.profile"
        fi
        ;;
esac

# --- 6. first-run setup ----------------------------------------------------

# Before the services start, not after: the wizard sets the password and the
# port, and a server already running on the old ones would have to be restarted
# to pick them up. It reads /dev/tty rather than stdin - stdin here is the
# installer script itself, coming down the curl pipe - and skips itself
# entirely when there is no terminal, so an unattended install still works.
step "First-run setup"
"$PY" "$ROOT/scripts/setup_wizard.py" || warn "Setup did not finish - run $ROOT/scripts/setup_wizard.py again whenever you like."

# Whatever the wizard settled on. Read back from .env rather than remembered,
# so this is right whether the wizard ran, was skipped, or the file was already
# there from a previous install.
HTTPS_PORT="$("$PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts'); import provider; print(provider.port('UNIAGENT_HTTPS_PORT', 8764))" 2>/dev/null || echo 8764)"

# --- 7. systemd user services ----------------------------------------------

if ! have systemctl || [ -z "${XDG_RUNTIME_DIR:-}" ] || ! systemctl --user show-environment >/dev/null 2>&1; then
    warn "No systemd user session here, so autostart was not installed."
    warn "Start Uniagent by hand with:  $PY $ROOT/scripts/server.py"
    exit 0
fi

# A machine that was set up before this installer may still be running the old
# hand-written unit; two servers would fight over port 8764.
if systemctl --user list-unit-files 2>/dev/null | grep -q '^uniagent-web\.service'; then
    warn "An older uniagent-web.service is installed. Retiring it in favour of $SERVER_UNIT."
    systemctl --user disable --now uniagent-web.service 2>/dev/null || true
fi

step "Installing the systemd user services"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$SERVER_UNIT" <<EOF
[Unit]
Description=Uniagent web server - the chat UI on https://localhost:$HTTPS_PORT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Unbuffered, so the agent's output reaches the journal live instead of sitting
# in a block buffer until the process exits.
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$ROOT/scripts
ExecStart=$PY $ROOT/scripts/server.py
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

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$ROOT/scripts
ExecStart=$PY $ROOT/scripts/cron.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --quiet "$SERVER_UNIT" "$CRON_UNIT"
systemctl --user restart "$SERVER_UNIT" "$CRON_UNIT"

# Without lingering, user services only run while you are logged in - so a
# rebooted headless box would come up with no Uniagent. This is the one step
# that may ask for a password, and the install is still fine if it is refused.
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

# --- 8. wait for it, then print the password -------------------------------

step "Waiting for the server to come up..."
# The password is read from .env rather than scraped out of the journal: the
# server writes the one it generates straight into .env, so this covers a fresh
# install and a re-install over an .env that already had one.
password=""
up=0
for _ in $(seq 1 45); do
    if [ "$up" -eq 0 ] && : <"/dev/tcp/127.0.0.1/$HTTPS_PORT" 2>/dev/null; then
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
    printf '%s  Uniagent is running.%s\n' "$G" "$N"
    printf '%s  Password:  %s%s\n' "$G" "$password" "$N"
    printf '%s  (it lives in %s as UNIAGENT_PASSWORD)%s\n' "$D" "$ENV_FILE" "$N"
    printf '%s==========================================================%s\n' "$G" "$N"
elif [ "$up" -eq 1 ]; then
    warn "Running, but no password turned up in $ENV_FILE. Look for it in: journalctl --user -u $SERVER_UNIT"
else
    warn "The server has not answered on port $HTTPS_PORT yet. It may still be starting."
    warn "Check it with: systemctl --user status $SERVER_UNIT"
fi

echo
printf '%sInstalled.%s\n' "$G" "$N"
printf '  Web UI:     https://localhost:%s   (other devices: https://<this-machine-ip>:%s)\n' "$HTTPS_PORT" "$HTTPS_PORT"
printf '              First visit warns about the self-signed certificate - Advanced, then Proceed.\n'
if [ "${PATH_OK:-1}" -eq 1 ]; then
    printf '  CLI:        uniagentcli "a question"\n'
else
    printf '  CLI:        uniagentcli "a question"   (open a new terminal first - PATH changed)\n'
fi
printf '  API keys:   %s   (or the settings page in the web UI)\n' "$ENV_FILE"
printf '  Logs:       journalctl --user -u %s -f\n' "$SERVER_UNIT"
printf '  Restart:    systemctl --user restart %s %s\n' "$SERVER_UNIT" "$CRON_UNIT"
printf '  Update:     curl -fsSL https://raw.githubusercontent.com/JJM8/Uniagent/main/install.sh | bash\n'
printf '  Uninstall:  %s/install.sh --remove\n' "$ROOT"

if [ "$up" -eq 1 ] && have xdg-open && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    xdg-open "https://localhost:$HTTPS_PORT" >/dev/null 2>&1 || true
fi
