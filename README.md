# Uniagent(WIP)
WARNING: PROJECT IS NOT FULLY COMPLETE OR READY FOR INSTALL. 
An AI agent framework in Python. It gives an LLM tools to read and write files, run terminal commands, search the web, send emails, manage scheduled jobs, and more. You can use it from a browser on any device on your network, or from the terminal.

![Uniagent UI](images/uniagent_ui.png)

## Features

### Tool system

Every tool is a standalone `.py` file in `tools/`. The loader (`scripts/tool_processor.py`) scans the folder on startup, imports each file, and checks it has four things:

- `NAME` -- identifier
- `DESCRIPTION` -- what it does
- `INSTRUCTIONS` -- how to call it
- `run(**kwargs)` -- the function

No config, no registration, no schema files. Broken tools are logged in a `BROKEN` list while the rest still load. The agent can create its own tools at runtime by writing new `.py` files into `tools/`, which are loaded immediately.

Built-in tools include: read/write files, run terminal commands, search/fetch the web, send and manage email, take screenshots, add scheduled jobs, read skills, launch subagents, and inspect images.

All of them work on Linux and on Windows. Where a platform genuinely differs the tool changes with it rather than being switched off: the terminal keeps a real PowerShell open behind a ConPTY on Windows and a real bash behind a pty on Linux (and the agent is told which shell it has, so it writes commands for the right one), and the screenshot tool captures through .NET on Windows and gnome-screenshot on Linux. A tool that cannot do its job on the machine it is running on says so plainly instead of reporting success.

### Web UI vs CLI

Uniagent has both a web interface and a CLI (`uniagentcli`, from `scripts/cli.py`).

The **web UI** is the main way to use it. It has a three-panel layout:

- **Left sidebar** -- chat list with history management, and a search box over it
- **Center** -- conversation with streaming messages and a real-time token counter
- **Right panel** -- context files in collapsible sections, tool list with status indicators and search, pinned tools/skills
- **Settings** -- 13 tabs: Models, Providers, Email, Workspaces, Appearance, Voice, Context, Tools/skills, Compaction, Cron, Usage, Safety, System
- **Subagent status** -- shows how many subagents are currently working
- **Model switcher** -- change provider or model mid-conversation
- **Attachments** -- the paperclip beside the message box picks files, which are uploaded into `chats/<chat>/attachments/` when the message is sent and named in it by full path
- **Continue** -- a turn that was stopped, or that died on the provider, offers a `continue` button under its last line

**Chat search** is the box under "+ new chat". It searches what was actually *said*, not just chat titles: type a word or two and the list becomes the chats those words appear in -- in messages, in the model's reasoning, in tool calls and in their output -- best match first. Several words all have to appear, though not next to each other. A title match ranks above a text match, and within each group the chat the words are densest in comes first; the second line of each row shows the piece of the conversation that matched. Emptying the box (or pressing Escape in it) puts the full list back. Cron runs are searched too. Searching reads every transcript on disk, so it answers with the best 60 matches and says how many more there were.

Attachments go up one at a time, and the message only goes once every one of them is on disk -- so the paths it names are paths the agent can already read with `read_file`. Nothing is uploaded until a message is actually sent, a name that clashes with one already there becomes `report-2.pdf` rather than overwriting it, and a transfer cut off half way leaves nothing behind.

Continuing does not send the word "continue". A stopped turn leaves `[stopped by the user]` in the history and a failed one leaves `Error: the turn failed - ...`; both are markers rather than anything the agent said, so `/continue` takes them back off and re-runs the turn with **no new message**. The model picks up from the last tool result as if it had never been interrupted -- which works on every provider, because a tool result is already sent as a user turn. A call that was cut off mid-flight keeps its "unknown whether it ran, check rather than assume" result, and only a turn stopped mid-sentence has anything added to it. The button and typing `/continue` are the same thing, in the browser and in the terminal.

The **CLI** is a terminal-based interface with markdown rendering and keyboard shortcuts. It is lighter and faster for quick interactions or scripting, but the web UI offers more information at a glance and a more streamlined experience for regular use.

### Voice control from any device

The web frontend has a hold-to-talk microphone button. Press it, speak, release. Audio is recorded in the browser using the Web Audio API and sent to the server's `POST /voice` endpoint, which transcribes it and returns the text, which then enters the chat as if you typed it.

Transcription goes through the same providers chat does. The voice tab picks one of them and a speech model on it -- Whisper or gpt-4o-transcribe at OpenAI, whatever a local server is serving, or a Gemini model handed the audio directly -- and that choice is global: the browser button, the desktop hold-to-talk key and the terminal all use it. With nothing chosen it falls back to Whisper on `OPENAI_API_KEY`.

There is also a **wake word**, for when a hand on a button is no use -- the other side of the room, both hands full. It works the way a smart speaker does, and the order of operations is the whole design: a small model that knows exactly one phrase listens to the room and answers yes or no about each third of a second, and **nothing is recorded, kept or transcribed until it says yes**. The obvious way to build this -- transcribe everything, look for the word in the text -- would mean sending every sentence spoken near the microphone to a speech model to find out whether it was meant for you, which costs money on a paid transcriber and privacy on any of them. So the room's audio goes to `POST /wake`, into the model, and nowhere else.

The model is [openWakeWord](https://github.com/dscripka/openWakeWord): open, free, no account and no key, running on this machine's CPU through onnxruntime. It costs about 11ms of work per 320ms of audio -- some 3% of one core -- and a quiet room costs nothing at all, because the browser holds a gate shut through silence and sends nothing. A wake word is a **file** in `models/wake/`, not a word typed into a box; the voice tab's dropdown is whatever is actually on disk. `docs/wake-word.md` is a step-by-step guide to getting one, including the two flags a current Python needs (openWakeWord declares `tflite-runtime`, which has no wheel for 3.12+, so it must be installed with `--no-deps`; nothing here uses it).

The browser keeps a second of audio behind the phrase and sends it in front: the model does not judge a frame on its own, and given no run-up it scores a perfectly clear wake word at zero. With 200ms it reaches 0.96 and from 800ms a flat 1.00, so a second is comfortably inside. The voice tab shows the model's live score while you talk, which is the only way to pick a threshold that isn't guesswork.

Once it fires, everything said is captured whole rather than cut into pieces on every pause. When you stop talking is judged locally, for free, by loudness alone -- `wake_silence_ms` is a pause that long after your last word, and it costs nothing to watch for. Once it fires, one clip goes back through the same `POST /voice` the hold-to-talk button uses, and that's the one message the session sends.

**Being slow to talk is handled rather than punished.** A think in the middle of a sentence doesn't split it into two messages the way cutting on every pause would -- there's only one message a session ever sends, at the very end, so a pause is just a pause. What it costs is `wake_silence_ms` worth of waiting after you actually stop, and that wait is free: nothing is transcribed until it's over.

There's also an optional `wake_captions` setting, off by default, that re-transcribes the whole clip so far roughly once a second while you talk so a caption can follow along above the message box -- whatever it says REPLACES what's shown, not an addition to it. It's a nicety, not a requirement: it isn't what decides you've stopped talking, and because each poll re-sends everything said so far rather than just what's new, it multiplies what a session bills -- ten seconds of talking bills roughly what fifty-five would normally cost. Leave it off unless you want the live feedback badly enough to pay for it.

The microphone is deliberately deaf while a reply is being read aloud, on top of the browser's echo cancellation: without that, an agent reading out its own wake word wakes itself up.

Replies can be read back out. The voice tab has a second provider/model pair for it, and picking one is the on switch -- leave it blank and the page stays silent. When a turn ends on the model actually saying something (not a tool call, a stop or an error), the finished reply is synthesised and played in the window watching that chat. The audio is made only when a page asks for it, once per reply no matter how many windows are open.

Which voice reads it is picked on the same tab, from the list that provider and model actually serve -- OpenAI's thirteen (nine of them on the older `tts-1` pair), Gemini's thirty prebuilt names -- each listed with a few words on how it sounds, since the names alone tell you nothing. Gemini's descriptions are Google's own; OpenAI publishes none, so those are written from how each one reads. Underneath it, a box for how it should read: tone, accent, pace, in your own words. `gpt-4o-mini-tts` takes that as its own instructions field and Gemini is told it in the prompt; `tts-1` and `tts-1-hd` ignore it entirely -- they accept the field and read the same way regardless -- so the tab says so rather than leaving a box that quietly does nothing. Both are optional -- left alone, the voice is whatever that endpoint defaults to, which is what installs from before the picker existed keep hearing.

A **hear it** button under the two reads a test phrase out in whatever is on the form at that moment, saved or not -- so a voice can be auditioned before you commit to it. The phrase is the `speak_test_phrase` setting, editable on the tab or in `settings.json`, and blank falls back to the shipped one. Each press is one real synthesis and costs what one costs.

The server binds to `0.0.0.0`, so the page is accessible from any device on the local network (phone, tablet, another laptop). It serves over HTTPS on port 8764 using a self-signed certificate it generates itself, which is what lets browsers hand the page a microphone. Accept the certificate warning once per device and the mic works.

On desktop, there is also a local voice path: hold Scroll Lock, speak, release. PyAudio records from the default microphone without needing the browser.

### Scheduled jobs (cron)

The cron system (`scripts/cron.py`) runs as a separate watcher process. It reads `cron.json` every 30 seconds and fires jobs when they are due.

A schedule is two unix timestamps in seconds, and nothing else: `start` is the moment of the first run, `interval` is the gap between runs. Runs land on `start`, `start + interval`, `start + 2 x interval`, and so on. Leave `interval` out (or set it to 0) for a job that happens once and never again. A job is a JSON object in the file's `jobs` list:

```json
{
  "name": "ai-brief",
  "start": 1785567600,
  "interval": 86400,
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "safety": 7,
  "safety_extra": "Searching the web and writing this job's own briefing file are its normal work.",
  "prompt": "Write me a briefing and save it to ..."
}
```

The settings page's **cron** tab lists the jobs as rows, one per job, each showing its name and when it next runs. A row folds open (they all start shut) into the job's settings: the schedule as a date picker and a repeat box, the provider, model, temperature and safety, and the prompt itself. The file itself is still editable directly at the bottom of that tab, for the fields the form does not cover. `date -d @1785567600` reads a timestamp back in local time, and `date -d "tomorrow 07:00" +%s` writes one. The `cron` tool the agent uses will also take `"07:00"`, `"now"` or `"6h"` and convert them, so asking for something daily at 7am does not need any arithmetic. Two things follow from the timing being plain seconds: a schedule that has already gone past is never caught up (a daily job written at 9am first fires at its time tomorrow), and an interval is a fixed number of seconds, so a daily job lands an hour out after the clocks change until its `start` is nudged back.

Each cron job gets its own **cron chat** -- a persistent conversation at `chats/cron/<name>/` that records every run, every tool call, and every result. These chats appear in the web UI sidebar alongside normal chats, so you can open them and see exactly what the job did. They are created from the moment the job is written to `cron.json`, not just when it first fires, so you can see a job exists before it has ever run.

Cron jobs run through the same tool loop and safety checks as regular chat. Each job can specify its own provider, model, temperature, and safety number in `cron.json`.

### Safety system

Every tool call from the model is validated before it runs. A separate, smaller LLM checks the call against a configurable safety prompt. The check is layered:

1. **Whitelist** -- trusted tools like `screenshot_tool` skip the check entirely
2. **Blacklist** -- scans for core system paths and flags anything dangerous
3. **LLM verification** -- everything else is sent to a verification model that rates the danger level (0-10); a rating at or under the chat's safety number runs, anything above it is flagged
4. **User approval** -- flagged calls pause and ask you to approve or deny before running

The safety number is one scale end to end: 0 asks about every call, 10 checks nothing, and the slider in the corner of a chat sets that chat's own. The number, the checking prompt, the verification model and provider, the two lists, and the two cron settings below are all on the settings page's safety tab.

**Cron jobs and safety:** a scheduled job's tool calls go through the same check, on the same 0-10 scale, with one difference that changes everything about how the number should be picked: there is no human present, so a call rated above the number is **denied outright** rather than held for approval. The job simply carries on without that step. Unattended work therefore fails safe, but a number set too low is a job that quietly never finishes.

What makes that workable is that the check is told what the job is. Alongside the shared checking prompt, a cron run carries an addendum -- the **added for scheduled jobs** box on the safety tab -- holding the job's own `prompt` (its `{task}`), the job's own `safety_extra` rules if it has any (its `{rules}`), and the call restated at the end (`{call}`, because a checking model handed two thousand words of task between the call and the question starts answering about the task). Ordinary work for the job then reads as ordinary work, and a call the task never asked for stands out and is rated 8 or more.

So the pieces per job, in `cron.json`: `safety`, a number 0-10, or absent to follow the safety tab's **default for scheduled jobs** (7 as shipped -- higher than a chat's, because a chat can ask and a job cannot); and `safety_extra`, a sentence or two that is true about this job and no other. `/cronsafety` lists every job's number and where it came from, and `/cronsafety <job> 0-10` sets one. A job can still replace the whole checking prompt with a `safety_prompt` of its own containing `{call}`, which wins over all of the above -- the escape hatch, not the normal route. Jobs written before the numbers existed (`"safety": true|false`) are still read: `false` means 10, and the watcher logs a line asking for a number instead.

### Usage tracking

Every request the app makes is written down as it happens: one line appended to `usage/YYYY-MM.jsonl` holding the timestamp, chat, provider, model, token counts and how long it took. That covers chat turns, the safety check on each tool call, compactions and spoken summaries, from every front-end -- the web UI, the CLI, cron jobs and subagents all go through the same loop. Nothing is sent anywhere; the ledger is a local file, and reading it costs no API calls.

Totals are computed at read time rather than kept as a running sum. That is what makes it safe for the web server, the cron watcher and the terminal to all be writing at once: an append of one short line has no race, where three processes incrementing a shared counter would silently lose some of them.

The settings page's **usage** tab shows the window you pick (today, 7 days, 30 days, all time) as four headline figures, a bar per day, and the same numbers broken down by kind, by model and by chat. `/usage` reports the same thing as text, and takes the same windows -- `/usage 7d`, `/usage all`, `/usage chat` for the chat you are in.

**Counts say where they came from.** Providers differ about what they report: Anthropic gives the input count before the first output token and updates the output as it goes, the OpenAI wire only reports if asked and only at the end, and plenty of local servers report nothing at all. On top of that, a turn that calls a tool stops reading the stream the moment the call is complete, so the provider's final usage event often never arrives -- on exactly the turns that did the most work. So each number is labelled: **reported** by the provider, **estimated** here with a local tokenizer when it wasn't, or **unknown** when neither was possible. A `~` in the tab means part of that figure was measured locally, `—` means it could not be measured at all, and nothing is ever quietly counted as zero.

Cached input is recorded separately where a provider reports it (DeepSeek and OpenAI do so automatically, without being asked). Anthropic reports its input count *excluding* cached tokens where every other wire includes them, so that one is put back together on the way in -- otherwise the same conversation would read as far cheaper on Anthropic than on anything else.

The ledger starts empty on the day it is installed: chats only ever stored their most recent token count, so there is no history to backfill from. Requests that failed are recorded too, since a request that died part-way still sent its prompt and is charged for it.

### Model support

Swap between Anthropic (Claude), OpenAI (GPT), DeepSeek, and Google (Gemini) from the settings panel or the quick selector. Each chat remembers its own provider, model, and temperature. Subagents and cron jobs can use different models too, set per-job in `cron.json`.

### Wires: adding an API Uniagent has never heard of

A provider is *who* you talk to. A **wire** is the *shape* of the API it
serves — where the request goes, what carries the key, and which keys the body
has in which order — and almost every wire is data rather than code. They live
in `defaults/wires.json`, one entry each:

```json
"openrouter": {
  "label": "OpenRouter",
  "dialect": "openai",
  "default_base_url": "https://openrouter.ai/api/v1",
  "endpoint": "/chat/completions",
  "auth": {"header": "Authorization", "template": "Bearer $key"},
  "body": {
    "model": "$model",
    "messages": "$messages",
    "temperature": "$temperature",
    "stream": true
  }
}
```

That is a complete, working backend. Nothing in Python has to learn the word
"openrouter".

**The body is sent in the order you write it.** What is in the file is what
goes on the wire, key for key — so an endpoint that insists on `model` first
gets `model` first, and you can see that it will by reading it. Values are
literals or placeholders: `$model`, `$messages`, `$system`, `$temperature`,
`$tools`, `$stop`, `$max_tokens`, `$key`, and `$setting:NAME` for a box the
wire adds to its own settings form. A placeholder with nothing to fill it
deletes its key, and anything that empties, outward — which is how Gemini's
`systemInstruction` disappears whole on a turn with no system prompt while
`generationConfig` keeps its temperature.

What stays as code is the **dialect**, of which there are three: `openai`,
`anthropic` and `gemini`. A dialect is the part that is structure rather than
arrangement — how the history nests, and how the streamed reply is read back.
Between them they cover essentially everything anyone ships; the openai dialect
alone is spoken by OpenRouter, Groq, Together, xAI, Mistral, Fireworks,
Cerebras, Ollama, vLLM and LM Studio. So adding a provider is a JSON entry,
and only a genuinely new *response format* needs Python.

Settings → **providers** has a wires list under the provider grid. Adding or
editing one gives you a live preview of the exact request it would send — URL,
headers and body, rendered against a real turn with the key as a placeholder —
which turns "it returns 401" into "look, the key is going in the wrong header".
A wire that could not work is refused at the moment you write it rather than
at 7am inside a cron job.

Your edits go to `wires.json` in the project root, which is merged *over* the
shipped file key by key. Overriding one thing does not mean owning all of it:
point Gemini at a proxy and you still get every later fix to its body template,
and **revert** is deleting your key rather than remembering what it used to
say. Two wires cannot be described this way and say so instead of offering an
editor that could not work — `bedrock` signs with AWS SigV4 through boto3, and
`claude-subscription` drives the Claude Code CLI.

### Subagents

Uniagent supports background subagents. These are separate agents with their own full tool access that work in parallel while the main conversation continues. Each subagent has its own chat history and can be assigned a different provider or model. The web UI shows how many subagents are currently working.

## Project structure

```
Uniagent/
├── context/          System prompts, safety rules, memory, skill index
├── defaults/         What Uniagent ships: starting context, and wires.json -
│                     every API shape it can speak, as data
├── scripts/          Core loop (main.py), web server (server.py), CLI (cli.py),
│                     cron watcher (cron.py), tool loader, voice input,
│                     settings, compaction, tool validation
├── tools/            One .py file per tool -- drop a file, it is a tool
├── skills/           Knowledge files loaded on demand (not callable tools, just read)
├── memories/         Topic-specific files indexed but not auto-injected
├── chats/            Per-chat JSON history and settings, including cron/ subfolder
├── usage/            One appended line per API request, sharded by month
├── web/              HTML/CSS/JS frontend (single-page app)
└── images/           Screenshots and assets
```

## Getting started

**Windows 10/11** — one line, pasted into Command Prompt or PowerShell:

```
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/JJM8/Uniagent/main/install.ps1 | iex"
```

It installs git and Python if needed, clones the repo, installs dependencies,
asks the same three first-run questions the Linux installer asks (a password, a
port, one provider), puts `uniagentcli` on your PATH, registers the server +
cron watcher to start at every logon, waits for the server to answer, and opens
the web UI with the password on screen. No administrator rights required. Full
walkthrough in `setup.md`.

**Linux** — one line, pasted into any terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/JJM8/Uniagent/main/install.sh | bash
```

Same deal: it installs git and Python if they're missing, clones into
`~/Uniagent`, builds a `.venv`, writes `.env`, puts `uniagentcli` on your PATH,
and installs the server and the cron watcher as **systemd user services** so
both start at boot and restart if they crash. It finishes by printing the web
password. No root needed unless git or Python have to be installed. Full
walkthrough in `setup.md`.

**Linux/macOS, by hand:**

```bash
git clone https://github.com/JJM8/Uniagent.git
cd Uniagent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
# Add your API keys
python3 scripts/server.py
```

### Moving an install somewhere else

An installed Uniagent is just its folder. Nothing inside it records where it
lives, so you can copy the whole thing to another machine, another disk or a USB
stick, and run one script there to make it a service again:

```bash
./attach.sh                 # Linux: asks which port, then starts it
```

```
attach.cmd                  Windows: the same thing. Double-clicking works.
```

That does the second half of the installer and nothing else — finds a Python
that can run the code (building a `.venv` if the one that travelled with the
folder can't run on this machine), puts `uniagentcli` on this machine's PATH,
writes the autostart with this folder's path in it, starts it, and prints your
password. No clone, no pull, no questions about API keys: your password,
providers, chats, skills and settings all travelled with the folder. Run it
again after moving the folder, and it points at the new location. `./attach.sh
--remove` (or `attach.cmd -Remove`) unhooks it, leaving the folder alone.

It is one script per platform doing one job: systemd user services on Linux, a
logon task on Windows. `attach.sh` run under Git Bash on Windows hands over to
`attach.ps1` rather than half-installing something systemd isn't there to run.

Two things worth knowing when the folder lives on a stick: everything it writes
(chats, logs) is written to the stick, which is slow on cheap flash;
and `.env` holds your API keys in plain text, so a lost stick is lost keys. If
the stick isn't plugged in at boot, the services skip themselves quietly rather
than restart-looping — plug it in and `systemctl --user start uniagent-server`
(on Windows, `schtasks /Run /TN Uniagent`).

On first run the server generates a password and prints it. Open https://localhost:8764 in a browser, or https://your-machine-ip:8764 from any device on your network, and enter it. The password lives in `.env` as `UNIAGENT_PASSWORD`; change it there and restart to log every device out.

Your browser will warn about the certificate the first time on each device, because Uniagent signs its own. Click Advanced, then proceed. Port 8763 only redirects to 8764 — the app itself is HTTPS-only, since the password would otherwise cross the network in plain text.

### Updating

Uniagent checks for new code by itself every few hours, and puts a dot on the
**settings** button when there is some. Settings → **system** then says what is
waiting — how many commits, what they were, how many files — and **update now**
is the whole of doing it: the server fetches the code, restarts onto it, and the
page reconnects on its own. **check for updates** asks the remote there and then
rather than waiting for the next automatic check. From a terminal it is the same
thing:

```bash
./scripts/update.sh --check     # what would change, without changing it
./scripts/update.sh             # do it, then restart the services
```

```
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
```

An update that cannot fast-forward stops rather than merging: local commits of
your own, or an edit to a file the update also changes, are both reported by
name and nothing is written. Commit them or revert them, then update again.

**Nothing of yours is touched.** Your `.env` and API keys, chats, system prompt,
memories, settings, model lists, cron jobs, MCP servers and workspaces all stay
as they are — an update only ever fast-forwards, and git only writes the files
Uniagent ships. Nothing is reset and `git clean` is never run, so a tool or
skill you wrote yourself is simply invisible to it.

Tools and skills that *did* ship are updated wherever you keep them, switched on
or off. That last part needs doing on purpose: `tools/`, `skills/` and
`disabled/` are all tracked, and the switch in the tools tab moves a bundle
between them, so a plain `git pull` would update the copy at its shipped path
and leave your moved one stale and duplicated — turning a skill you had
switched off back on. The updater takes those moves out of the way first and
puts them back after.

If you have edited a file that ships with Uniagent and the update changes that
same file, it stops and names the file rather than picking a winner. Same for
local commits: it fast-forwards or it does nothing. And it never writes to
`.env` — if a new version documents keys you don't have, it lists them and
leaves them to you.

### CLI mode

```bash
python3 scripts/cli.py
```

To get it as a command from anywhere, link the launcher onto your PATH:

```bash
ln -sf "$PWD/scripts/uniagentcli" ~/.local/bin/uniagentcli
```

On Windows the installer does this for you; `attach.ps1` does it again on a machine you have moved the folder to.

Then `uniagentcli` opens a chat, and `uniagentcli "some question"` runs a single turn and exits. It reads the terminal's colours from the same theme the web UI uses; set `UNIAGENT_THEME=light` if your terminal has a light background.

The full-screen chat works the same on Windows as it does on Linux — the keys below included. It needs a console that understands ANSI escape codes, which is every Windows 10 and 11 console; an older one is told so and pointed at `uniagentcli "a question"` and the web UI instead.

The prompt stays live while the agent works, and what you press decides when your message lands:

| Key | While a reply is running |
|:----|:-------------------------|
| `enter` | fold it into the turn already running, as soon as the tool in flight comes back |
| `tab` | hold it until the reply is completely finished, then send it as its own turn |
| `esc` | stop the running turn, same as `/stop` |
| `/command` | runs immediately, queued behind nothing |

Queued messages are listed above the prompt until they go. On an idle prompt `enter` and `tab` both just send, and `tab` completes a half-typed `/command`.

A status bar sits under the prompt the whole time, showing the chat's model and how full its context is (`deepseek/deepseek-v4-flash ███░░░░ 12.4k/128k 10%`), the same two numbers the web UI keeps on screen. It is recounted in the background after every turn, never on the keystroke path, and a `~` marks a count the model's own tokenizer could not do exactly.

Bare `/chats` and `/model` open a list you arrow through instead of printing one: `↑↓` to move, typing filters, `enter` selects, `esc` cancels. Give either an argument (`/model openai gpt-5.2`) and it behaves as the plain command. Loading a chat replays its transcript so you can see what is in it, capped at the last 30 turns with `/history` for the raw thing.

### Cron watcher (for scheduled jobs)

```bash
python3 scripts/cron.py
```

Or run it as a systemd service (service files included in `scripts/`). On
Windows the installer's scheduled task runs the server and the cron watcher
together (see `scripts/run-server.ps1`). If you want Uniagent as a true
Windows service that runs before anyone logs in, wrap `scripts/run-server.ps1`
with NSSM (`nssm install Uniagent "powershell.exe" "-NoProfile -ExecutionPolicy
Bypass -File C:\path\to\scripts\run-server.ps1"`) — needs an admin shell.

## Requirements

- Python 3.10+
- API keys for whatever models you want to use (set in `.env`)
- Ports 8763 and 8764 available
