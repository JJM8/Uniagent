"""The wake word: a model that listens for one phrase and nothing else.

This is deliberately NOT "transcribe everything and look for the word in it".
That arrangement works, and it is the wrong shape: it sends every sentence
spoken near the microphone to a speech model to find out whether it was meant
for you, which costs money on a paid transcriber and privacy on any of them.

What runs here instead is openWakeWord - a small open model, trained on one
phrase, that answers a single question about each 80ms of audio: was that the
phrase, yes or no. It runs on the CPU of this machine, needs no account and no
key, and nothing it hears goes anywhere. Only once it says yes does anything
get recorded, and only then does a clip reach a transcriber.

    the room  ->  [wake model, here]  ->  nothing, almost always
                                      ->  "yes"  ->  record  ->  transcribe

The browser does the listening (see index.html's wake section) and posts the
audio here in short chunks. The model is stateful - it hears a phrase across
frames, not within one - so there is one loaded per browser session, keyed by
an id that session invents, and they are dropped again when a session goes
quiet. Nothing here starts until somebody actually presses the ear button:
importing openwakeword pulls in onnxruntime, which is slow and heavy and has no
business being loaded by an install that never uses this.

Models live in models/wake/ as .onnx files. There is no model shipped with this
and there cannot usefully be: a wake word is a choice, the ready-made ones are
a few dozen phrases, and which one you want is not something an install can
guess. docs/wake-word.md says where to get one.
"""

import re
import threading
import time
from pathlib import Path

MODELS = Path(__file__).parent.parent / "models" / "wake"

# What a model file can be. ONNX is what the ready-made ones are published as
# and what runs on every platform this does; tflite is accepted because
# openWakeWord's own downloads come as both and there is no reason to refuse
# one somebody already has.
SUFFIXES = (".onnx", ".tflite")

# How long a session may go without a chunk before its model is dropped. A page
# left open on a tab nobody is looking at should not hold an ONNX runtime open
# forever, and re-loading one costs about a second - which is only ever paid by
# somebody who has just started talking again after a minute of silence.
IDLE_SECONDS = 90

# How many models may be loaded at once. Each one is its own runtime and its
# own few tens of megabytes, and there is no sane reason for more than a couple
# of windows to be listening at the same time.
MAX_SESSIONS = 4

# After a detection, ignore this long. One phrase is one wake: the model sees a
# rolling window, so the frames just after a hit still contain the phrase and
# would fire again on the next chunk without this.
DEBOUNCE_SECONDS = 2.0

_lock = threading.Lock()
_sessions = {}          # id -> {"model", "used", "fired", "name"}
_oww = None             # the openwakeword module, imported on first use
_import_error = None    # why it couldn't be, said once and remembered
_features_ready = False  # the shared feature models are known to be on disk


class WakeError(Exception):
    """The wake word can't be listened for, and this is the sentence saying
    why. Written to be read by a person: it is what the page shows when the ear
    button doesn't work, and usually names the thing left to install."""


def available():
    """Every wake model in models/wake/, by file name. The page turns this into
    a dropdown, so what a person picks from is what is actually on disk."""
    try:
        found = [p.name for p in MODELS.iterdir()
                 if p.is_file() and p.suffix.lower() in SUFFIXES]
    except OSError:
        return []
    return sorted(found)


def label(name):
    """A model file name as the phrase it listens for: "hey_computer_v0.1.onnx"
    -> "hey computer". A guess, and only ever used for reading - to title the
    dropdown, and to take the phrase back off the front of the first thing
    transcribed after a wake. The file is the truth about what it detects; this
    is the truth about what it is called."""
    stem = Path(name).stem
    # Version tails are how these are published (..._v0.1, ..._v1) and are not
    # part of anybody's wake word.
    stem = re.sub(r"[_-]v[0-9.]+$", "", stem)
    return re.sub(r"[_-]+", " ", stem).strip().lower()


def _module():
    """The openwakeword module, imported on first use. The import is the
    expensive part of this whole file - onnxruntime is tens of megabytes of
    shared library - so an install that never presses the ear button never pays
    it. A failure is remembered rather than retried: it is always the same
    missing package, and retrying it per chunk would mean a stack trace several
    times a second."""
    global _oww, _import_error
    if _oww is not None:
        return _oww
    if _import_error is not None:
        raise WakeError(_import_error)
    try:
        import openwakeword
        import openwakeword.model
        _oww = openwakeword
        return _oww
    except Exception as e:
        _import_error = (
            "the wake word needs the 'openwakeword' package, which isn't "
            "installed - run: pip install -r requirements-voice.txt   ("
            + type(e).__name__ + ": " + str(e)[:120] + ")")
        raise WakeError(_import_error)


def _load(name):
    """One loaded model for `name`, or WakeError saying what is missing."""
    path = MODELS / name
    # Checked rather than trusted: `name` comes off a request, and a file name
    # from outside has no business being joined onto a path unexamined.
    if Path(name).name != name or path.suffix.lower() not in SUFFIXES:
        raise WakeError("'" + str(name)[:60] + "' is not a wake model file name")
    if not path.is_file():
        raise WakeError("there is no wake model called " + name + " in "
                        + str(MODELS) + " - see docs/wake-word.md for where to "
                        "get one")

    oww = _module()
    _features()

    framework = "onnx" if path.suffix.lower() == ".onnx" else "tflite"
    try:
        return oww.model.Model(wakeword_models=[str(path)],
                               inference_framework=framework)
    except Exception as e:
        raise WakeError("could not load " + name + " - " + type(e).__name__
                        + ": " + str(e)[:160])


def _features():
    """Make sure the two shared feature models are on disk - a melspectrogram
    and Google's speech embedding, which every wake model sits on top of and
    which are downloaded once rather than shipped.

    The odd argument is doing real work. download_models() takes a list of
    official wake words to fetch and ALWAYS tops up the feature models first;
    passing the empty list means "and also every official wake word", which is
    15MB of models for phrases nobody here asked for. A name that matches
    nothing gets the features and none of the rest.

    This is the only moment anything in this file touches the network, and it
    happens once per install."""
    global _features_ready
    if _features_ready:
        return
    try:
        _module().utils.download_models(model_names=["-features-only-"])
    except Exception as e:
        raise WakeError("could not fetch openWakeWord's shared feature models "
                        "(this happens once, and needs the internet) - "
                        + type(e).__name__ + ": " + str(e)[:120])
    _features_ready = True


def _reap(now):
    """Drop the sessions nobody is feeding. Called under the lock, on the way
    in, so a page that was closed without saying so costs one idle window
    rather than a runtime held open until the server restarts."""
    for key in [k for k, s in _sessions.items() if now - s["used"] > IDLE_SECONDS]:
        del _sessions[key]


def listen(session, pcm, name, threshold):
    """Feed one chunk of audio to `session`'s model and say whether the wake
    word was in it.

    `pcm` is raw signed 16-bit mono at 16kHz - what the model wants, and what
    the browser sends, so nothing here resamples or converts. Answers
    {"wake": bool, "score": float}, where the score is the highest the model
    reached anywhere in this chunk: the page shows it while you are setting the
    threshold, which is the only way to pick one that isn't guesswork.

    Raises WakeError when there is nothing to listen with - no package, no
    model file - which the caller turns into a message on the page."""
    import numpy as np

    now = time.time()
    with _lock:
        _reap(now)
        s = _sessions.get(session)
        if s is None or s["name"] != name:
            if len(_sessions) >= MAX_SESSIONS:
                # The oldest is the one least likely to still have somebody in
                # front of it.
                del _sessions[min(_sessions, key=lambda k: _sessions[k]["used"])]
            # Loading happens under the lock on purpose. It takes about a
            # second, and two chunks arriving during it would otherwise each
            # load their own copy of the same model and throw one away.
            s = {"model": _load(name), "name": name, "used": now, "fired": 0}
            _sessions[session] = s
        s["used"] = now

    audio = np.frombuffer(pcm, dtype=np.int16)
    if audio.size == 0:
        return {"wake": False, "score": 0.0}

    scores = s["model"].predict(audio)
    score = max(scores.values()) if scores else 0.0

    # One phrase is one wake. The model sees a rolling window, so the frames
    # just after a hit still have the phrase in them and would fire again on
    # the next chunk - which, at the other end of this, means a second session
    # opening on top of the one that just did.
    if score < threshold or now - s["fired"] < DEBOUNCE_SECONDS:
        return {"wake": False, "score": float(score)}

    s["fired"] = now
    # Cleared so the phrase that just fired cannot contribute to the next one.
    s["model"].reset()
    return {"wake": True, "score": float(score)}


def forget(session):
    """Drop a session's model - the page saying it has stopped listening."""
    with _lock:
        _sessions.pop(session, None)
