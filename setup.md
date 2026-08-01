# Setup

How to get Uniagent running on a fresh machine.

## Windows 10 / 11 (one line)

Open PowerShell (any folder) and paste:

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/JJM8/Uniagent/main/install.ps1 | iex"
```

The installer:

1. installs **git** and **Python 3.12** via winget if they're missing
2. clones Uniagent into `%USERPROFILE%\Uniagent` (or `$env:UNIAGENT_HOME`)
3. makes a `.venv` and installs the dependencies
4. writes `.env` from the example if you don't have one yet
5. puts a `uniagentcli` command on your PATH (new terminal needed)
6. installs a **scheduled task** so the server and cron watcher start at every
   logon, and starts them immediately
7. opens `https://localhost:8764` and prints the first-run password (it's also
   in `%USERPROFILE%\Uniagent\logs\server.out.log`)

Afterwards: `update.ps1` pulls new code and restarts the server;
`install-autostart.ps1 -Remove` stops it starting at logon. `schtasks /End /TN
Uniagent` stops it right now. The first time the server listens, Windows asks
to allow it through the firewall — click **Allow** (or run the installer as
admin to have the rule added for you).

Notes specific to Windows:

- The **web UI is fully supported**. The terminal CLI's live-keyboard chat
  mode is Unix-only for now; on Windows use `uniagentcli "a question"` for
  one-shot turns or `echo text | uniagentcli` for piped input.
- Voice: the **browser hold-to-talk works out of the box** (it records in the
  page). The local desktop hold-to-talk key needs `requirements-voice.txt`
  (pyaudio/pynput) — the installer tries, and the app runs fine without it.
- "Always on before anyone logs in" (a true Windows service) needs admin
  rights — see the README's NSSM note. The scheduled task covers the normal
  case: it comes up at every logon and restarts if the server crashes.

## Linux

### Prerequisites

```bash
sudo apt update
sudo apt install python3 python3-pip git -y
```

## Clone and install

```bash
git clone https://github.com/JJM8/Uniagent.git
cd Uniagent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Voice extras (the local hold-to-talk key) are optional:

```bash
.venv/bin/pip install -r requirements-voice.txt   # pyaudio, pynput
```

## Configure API keys

Copy the example env file and add your keys:

```bash
cp .env.example .env
nano .env
```

Required keys depend on which providers you use:

- `OPENAI_API_KEY` — image reading, token counting, and voice transcription when
  the settings page's voice tab has not been pointed at a provider of its own
- `DEEPSEEK_API_KEY` — DeepSeek models
- `ANTHROPIC_API_KEY` — Anthropic / Claude models
- `EMAIL_ACCOUNTS` — every mailbox the agent can read and send from, as one JSON list

Email accounts are easiest to add on the settings page (gear icon, email tab), which
writes this line for you and tests the sign-in. By hand it looks like:

```
EMAIL_ACCOUNTS=[{"name":"personal","address":"you@example.com","password":"app password","default":true}]
```

Each account needs a `name` (what a tool call passes as `"account"`), an `address` and a
`password` — an app password where the provider insists on one, which most now do. The
IMAP and SMTP servers are worked out from the address: from a built-in table for common
domains, otherwise by asking the provider (autoconfig, then Thunderbird's public database,
then the usual hostnames). Add `"imap"`, `"smtp"`, `"imap_port"`, `"smtp_port"`,
`"imap_security"`/`"smtp_security"` (`"ssl"` or `"starttls"`) and `"tls_verify": false` only
when a provider needs something unusual — a local Proton Bridge, say. `"default": true`
marks the account a tool call gets when it doesn't name one.

Personal Outlook, Hotmail and Live accounts can't be used: Microsoft turned off password
sign-in for IMAP and SMTP, and OAuth2 isn't supported here yet. Tuta offers no IMAP at all.

## Run the server

```bash
python3 scripts/server.py
```

The first time it starts it generates a password and prints it:

```
==========================================================
  Uniagent has generated a password for the web interface:

      rh7z-g3r3-32km

  It is saved in /path/to/Uniagent/.env as UNIAGENT_PASSWORD.
  Change it there to anything you like, then restart.
==========================================================
```

Write it down. If you lose it, read it back with `grep UNIAGENT_PASSWORD .env`,
or change it in `.env` and restart. Changing it logs out every device.

Open `https://localhost:8764` in a browser and enter the password.

## Access from other devices

Find your machine's IP address:

```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

Then open `https://<your-ip>:8764` on any device on the same network. The web
interface works as a voice controlled terminal from phones, tablets, and other
laptops.

**The first visit on each device shows a certificate warning.** Uniagent serves
itself over HTTPS using a certificate it generates for itself, and your browser
does not recognise the signer. This is expected. Click **Advanced**, then
**Proceed** — once per device, then the browser remembers. On Chrome for Android
the wording is "Your connection isn't private" → Advanced → "Proceed to
(unsafe)".

HTTPS is not optional here: the password and its session cookie would otherwise
cross your network in plain text, and browsers only allow microphone access on
an HTTPS page, so hold-to-talk needs it too. Port 8763 still answers, but only
to redirect you to 8764.

The certificate covers `localhost`, your hostname, and your machine's current
network address. If your router later gives the machine a different address,
Uniagent notices at startup and generates a new certificate — you will get the
warning once more on each device.

## Run on startup (optional)

The server ships with a systemd service file. Install it with:

```bash
sudo cp scripts/uniagent-server.service /etc/systemd/system/
sudo systemctl enable uniagent-server
sudo systemctl start uniagent-server
```

## Run local models (optional)

Set `"provider": "local"` in `settings.json` and point it at your local model server (Ollama, LM Studio, or similar). You can then pick exactly which memory files, tools, and system prompts get injected so a small model on your GPU is not wasting VRAM on stuff you do not need.

## Update

```bash
cd uniagent
git pull
# Restart the server
```
