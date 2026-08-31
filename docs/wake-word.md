# Talking to it from across the room

Normally you talk to Uniagent by holding a button. This is the other way: you
say a word out loud, it wakes up, and you just talk.

Everything here happens on your own machine.

---

## How it works, in one picture

There is a tiny program that listens for **one word and nothing else**. It is
not listening to what you say. It cannot understand you. All it does, about
three times a second, is answer one question: *was that the word, yes or no?*

```
   you talking  ->  the little listener  ->  "no"   ->  thrown away
                                         ->  "yes"  ->  NOW it starts recording
```

Almost always the answer is no, and the sound is thrown away immediately.

Only when the answer is **yes** does anything get recorded, and only then does
anything get sent off to be turned into words.

> **Why it is built this way.** The easy way would be to write down everything
> said in the room and check whether the word "computer" is in it. That works,
> but it means every conversation near your desk gets sent to a speech company
> to find out whether it was meant for you. So we don't do that.

---

## What you need

1. Uniagent running (you already have this)
2. One extra Python package, installed once
3. One small model file — this *is* the wake word

---

## Step 1 — install the listener

Open a terminal in the Uniagent folder and paste these two lines:

```bash
python3 -m pip install --user --break-system-packages --no-deps openwakeword
python3 -m pip install --user --break-system-packages onnxruntime numpy scipy scikit-learn tqdm requests
```

Then check it worked:

```bash
python3 -c "import openwakeword; print('listener installed')"
```

You should see `listener installed`.

<details>
<summary>Why those odd flags?</summary>

`--no-deps` is **required**, not a shortcut. The openwakeword package says it
needs something called `tflite-runtime`, which doesn't exist for Python 3.12 or
newer — so without `--no-deps` the install just fails. We don't need it anyway:
Uniagent uses the other engine (onnxruntime), which is in the second line.

`--user --break-system-packages` is what Ubuntu and Debian need to let you
install a Python package at all. It sounds scary. It isn't — it installs into
your own home folder and doesn't touch anything the system depends on.

If you run Uniagent inside a virtual environment instead, drop both flags and
use that environment's pip.
</details>

---

## Step 2 — get a wake word

The wake word **is a file**. A different file means a different wake word.
They go in the `models/wake/` folder.

### The quick way (2 minutes, gives you four ready-made words)

```bash
python3 -c "import openwakeword.utils as u; u.download_models()"
```

Then copy the ones you want into place:

```bash
python3 - <<'PY'
import openwakeword, pathlib, shutil
src = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
dst = pathlib.Path("models/wake"); dst.mkdir(parents=True, exist_ok=True)
for f in src.glob("*_v0.1.onnx"):
    shutil.copy(f, dst / f.name); print("installed:", f.name)
PY
```

That gives you these words to try straight away:

| file | what you say |
|---|---|
| `alexa_v0.1.onnx` | "alexa" |
| `hey_jarvis_v0.1.onnx` | "hey jarvis" |
| `hey_mycroft_v0.1.onnx` | "hey mycroft" |
| `hey_rhasspy_v0.1.onnx` | "hey rhasspy" |

Use one of these first just to check the whole thing works. Then get the word
you actually want.

### Getting "hey computer"

The community models live at <https://openwakeword.com/library>. Downloading one
needs a **free sign-in** (an email address, no payment) — so this bit you have
to do yourself; nobody can do it for you.

**Not all of them work.** Every model is published with two numbers, and they
vary enormously. Get these the right way round:

- **recall** — how often it hears you when you *did* say the word. Higher is better.
- **false activations per hour** — how often it wakes up when nobody said it.
  Lower is better.

Here is every "computer"-ish model in the library today, measured:

| word | model id | recall | false wakes/hr |
|---|---|---|---|
| **hey computer** | **997** | **85.2%** | **1.4** |
| hey computer | 2533 | 64.2% | 1.5 |
| Ok Computer | 2296 | 63.6% | 3.4 |
| Computer | 2333 | 55.6% | 6.4 |
| Computer | 2330 | 43.8% | 20.3 |
| Computer | 372 | 14.0% | 7.5 |
| hey computer | 48 | 11.7% | 4.5 |

**Use model 997:** <https://openwakeword.com/library/997>

1. Sign in (top right)
2. Download the **.onnx** file
3. Put it in the `models/wake/` folder
4. Settings → voice → pick it in the dropdown → save

Model 48 is the one the library's own search tends to surface first, and at
11.7% recall it would ignore you roughly nine times out of ten. Check the two
numbers before trusting a model.

### Why not plain "computer"?

You can see it in the table. The best plain **"Computer"** model manages 55.6%
recall with **6.4 false wakes an hour** — so it misses you nearly half the time
*and* wakes itself up every nine minutes on its own.

That is not bad luck, it is the word. "Computer" is a common English word that
turns up in ordinary conversation and on television, and it is short, so there
is less sound for a model to be sure about. "Hey computer" is two words almost
nobody says by accident.

If you still want plain "computer", you can train your own free at
<https://openwakeword.com/train> — type the word, wait about an hour, download
the `.onnx`. It will still be fighting the same problem.

---

## Step 3 — turn it on

1. Restart Uniagent (Settings → System → restart, or restart the server).
2. Open the web page.
3. Go to **Settings → voice**.
4. At **wake word**, pick your file from the dropdown.

   *Dropdown empty?* Then no file is in `models/wake/`. Go back to Step 2.
5. Press **save settings**.
6. Close settings. You will see a new **ear button** 👂 next to the microphone
   button by the message box. **Press it.**
7. Your browser will ask to use the microphone. Say yes.

The ear turns **green** — it is listening for your word.

**Now say your wake word.** The ear turns **red** and a line appears above the
message box showing what it heard. Keep talking. When you stop, it sends.

### Talking to it while it is already working

You don't have to wait for it to finish. Speaking to a busy agent does exactly
what typing into the box and pressing enter does: what you said joins the job
already running, at its next step, instead of queuing up as a separate job
behind the whole of it. So "no, the front room" reaches it while there is still
something to change.

Your words go on screen the moment they are sent, greyed out and marked *sends
at the next tool call*, and turn solid when the agent actually takes them.

---

## Step 4 — get it right for your room

Back in **Settings → voice** (leave the ear turned on), there are four sliders.

**how sure it has to be** — the important one. Underneath it is a live bar that
moves while you talk:

```
████·····················  0.08
```

- Talk normally, *without* saying the wake word. Watch the highest number.
- Now say the wake word. Watch it jump — usually near 1.00.
- Set the slider **between those two numbers**.

Too low and the TV wakes it up. Too high and it ignores you.

**a pause this long ends a sentence** — how long a gap means you stopped
talking. Default 0.9s.

**and a pause this long sends the message** — how long a gap means you're
finished and it should send. **This is the one to turn up if it keeps
interrupting you.** Default 1.4s.

Turning it up is cheaper than it sounds — see the next section.

**how long it keeps listening afterwards** — after it answers, you can keep
talking for this long without saying the wake word again. Default 45s.

---

## Being slow to talk is fine

Nobody, and no program, can tell the difference between *pausing in the middle
of a sentence* and *finishing a sentence*. So this doesn't pretend to.

It waits a moment, sends — and if you turn out not to have finished, it
**takes it back**: it stops what the agent was doing and sends the whole thing
again as one message, with your late words marked so the agent knows it was one
slow sentence and not two separate questions.

So this works fine:

> "Computer, turn the heating on…"
> *(agent starts thinking)*
> "…in the front room only."

The agent gets **one** message: turn the heating on in the front room only.

There is one exception. If the agent already **did** something — ran a command,
sent an email — it can't be taken back, so that part is kept and your extra
words are added on top. The page tells you when this happens.

Don't want it to interrupt at all? Turn off *"talking again stops the turn"* at
the bottom of the voice settings. Your late words then just get added to what
the agent is already working on.

---

## When it goes wrong

**The ear button isn't there.**
No wake word is chosen. Settings → voice → pick one → save.

**The dropdown is empty.**
No `.onnx` file in `models/wake/`. See Step 2.

**"the wake word needs the 'openwakeword' package"**
Step 1 didn't work. Run it again and read the error.

**The ear turns green, then stops on its own.**
Something is missing on the machine and the message on screen says what. It
stops rather than repeating the error three times a second.

**It never wakes up.**
Turn the *how sure it has to be* slider down and watch the live bar (Step 4).
If the bar never moves at all, the microphone isn't reaching it — check the
browser is showing a recording indicator.

**It wakes up at the television.**
Turn the slider up, or use a longer, stranger wake word.

**It wakes itself up when reading a reply out loud.**
It shouldn't — the microphone is deliberately deaf while the agent is talking.
If it still happens, your speakers are very loud; turn them down.

**Nothing is transcribed / "no transcriber".**
The wake word and the transcriber are two different settings. The wake word
found you; now something has to turn your words into text. Pick a provider and
a speech model at the top of Settings → voice. Point it at a local speech
server and nothing you say ever leaves the machine.

**"another tab of this browser is already listening".**
Only one window listens at a time, on purpose: a sentence spoken near two of
them would be heard twice and sent twice. Close or stop the other tab and this
one takes over — it does so on its own the moment the listening tab closes. A
tab that was killed rather than closed holds the lease for about nine seconds
before it goes stale and anybody else can claim it.

**It still arrived twice, from two devices.**
A phone and a desktop are two browsers and cannot share the lease above, so the
server catches this instead: the same spoken message arriving twice within five
seconds is taken once. If they were more than five seconds apart, turn one of
the two listeners off.

---

## What this costs

**The listener:** about 11 milliseconds of work per third of a second of audio —
roughly 3% of one CPU core. A silent room costs nothing at all, because silence
is never even sent to it.

**Your privacy:** the wake word listener never sends anything anywhere. After it
wakes up, your sentence goes to whichever transcriber you chose in Settings →
voice. If that is a local speech server, nothing leaves your machine, full stop.
If it is OpenAI or Google, then what you say *after the wake word* goes to them —
the same as the hold-to-talk button already does.

**Money:** the wake word is free. Only what you say after it costs anything, and
only if your transcriber is a paid one.
