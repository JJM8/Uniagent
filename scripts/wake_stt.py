"""The wake word, done by asking the local transcriber instead of a wake model.

wake_word.py explains why "transcribe everything, look for the word in it" is
normally the wrong shape: it means sending every sentence spoken near the
microphone to a speech model to find out whether it was meant for you, which
costs money on a paid transcriber and privacy on any of them. Neither cost
applies when the transcriber IS a local one already running on this machine
for ordinary voice input - see voice_provider in settings.py, which for the
"local" wire is something like a whisper.cpp/faster-whisper server on
localhost. The audio already isn't leaving the machine, and there's nothing to
bill.

What this buys over openWakeWord: a phrase you can type instead of one of a
few dozen pre-trained ones, and - the point of it - one fewer model resident
in memory. This never imports openwakeword or onnxruntime; it calls
voice_input.transcribe_audio, the exact function POST /voice already uses, so
an install running this listens with the SAME transcriber loaded for the SAME
reason, not a second one loaded alongside it.

The trade is cost per chunk: a real transcription pass instead of one
openWakeWord frame. So this doesn't transcribe on every chunk that arrives -
it keeps a rolling window of recent audio per session and only spends a
transcription pass on it once every RETRY_SECONDS.

    the room  ->  [buffer, here]  ->  nothing, most passes
                                  ->  [local transcriber]  ->  "computer, ..."
                                  ->  word found  ->  record  ->  transcribe (again, for real)
"""

import io
import re
import threading
import time
import wave

import voice_input

# How much trailing audio to keep and run past the transcriber. Long enough to
# hold the whole phrase however it happens to fall, short enough that a pass
# isn't chewing on a minute of room noise every time.
#
# Longer than it looks like it needs to be, deliberately. The obvious move is
# to shrink this until a pass gets cheap, and it does nothing: Whisper pads
# every input to a fixed 30-second window before the encoder sees it, so the
# encoder - which is nearly all of the cost - does identical work either way.
# Measured on this machine's GTX 1660, one clip cut to different lengths:
#
#     0.5s window -> 2.30s        2.0s window -> 2.35s
#     1.0s window -> 2.31s        3.0s window -> 2.36s
#     1.5s window -> 2.34s
#
# Six times the audio for sixty milliseconds. So the window is chosen for
# whether it can HOLD the phrase, never for what it costs: a two-word wake word
# takes the best part of a second to say, and one that straddles the boundary
# between two windows is in neither of them whole. 1.5s leaves room for it
# wherever it lands.
WINDOW_SECONDS = 1.5

# The floor on how often a pass may run. Zero: passes go back to back, the next
# starting as soon as the last one finished, so consecutive windows overlap by
# however much of the window the pass didn't take. That is as fast as this can
# notice you, and on small.en (see stt_server.py, which is the model this
# expects) it is what the GPU can afford - roughly 0.35s a pass, so a 1.5s
# window overlaps the one before it by about 1.15s.
#
# It was 1.0 while large-v3-turbo was the transcriber, where it never actually
# bound: a pass took 1.15s, already slower than the throttle. On a model with
# 0.35s passes it would have thrown away two thirds of the speed.
RETRY_SECONDS = 0.0

# Same idle/debounce shape as wake_word.py, and the same reasons: a page left
# open on an unwatched tab shouldn't hold a session open forever, and one
# phrase should mean one wake even though the window still contains it on the
# next pass.
IDLE_SECONDS = 90
MAX_SESSIONS = 4
DEBOUNCE_SECONDS = 2.0

SAMPLE_RATE = 16000   # what the browser sends, same as wake_word.py
SAMPLE_WIDTH = 2       # bytes per sample, int16
CHANNELS = 1

# What the page sends in one POST, mirroring WAKE_CHUNK_MS in web/index.html.
# Only used to account for the audio in flight when working out how far back a
# hit reaches - see _backdate_ms - so it being a little out costs a little
# accuracy there and nothing else.
CHUNK_MS = 320

_WORD_RE = re.compile(r"[^a-z0-9]+")

_lock = threading.Lock()
_sessions = {}   # id -> {"buf": bytearray, "used", "checked", "fired"}


class WakeError(Exception):
    """Same job as wake_word.WakeError: a sentence the ear button can show,
    for when the local transcriber can't be reached or hasn't been chosen."""


def _reap(now):
    """Drop the sessions nobody is feeding. Called under the lock, same as
    wake_word._reap and for the same reason."""
    for key in [k for k, s in _sessions.items() if now - s["used"] > IDLE_SECONDS]:
        del _sessions[key]


def _normalize(text):
    # Punctuation becomes a space, not nothing: "co-worker" collapsing to
    # "coworker" would let it falsely match a wake word split across the
    # hyphen, which a space between the two halves can't do.
    return _WORD_RE.sub(" ", text.lower()).strip()


def _matches(text, words):
    """Any of `words` present as a substring of the (normalized) transcript.
    Substring rather than exact-match-the-whole-thing: a transcript is
    whatever came out of the window right now, mid-sentence as often as not,
    and "computer" said in the middle of "okay computer, what's" should still
    count."""
    norm = _normalize(text)
    if not norm:
        return False
    for w in words:
        w = _normalize(w)
        if w and w in norm:
            return True
    return False


def _backdate_ms(clip, started):
    """How far back from the page's "now" the window we just matched began.

    The page keeps a few seconds of the room in a ring buffer and throws
    nothing away, so on a hit it can start the message where the phrase STARTED
    rather than where we finished noticing it. That is the whole difference
    between "console... do this" and "console do this", and this number is what
    tells it how far to reach back.

    Two pieces, both measured rather than assumed:

        the window   the audio we just transcribed, however long it really was
        the pass     how long the transcription itself took

    The pass counts because the page went on recording throughout it. The
    newest sample in the window was current when the buffer was snapshotted;
    by the time this answer lands the page is a whole pass further on. One
    chunk is added for the audio that was in flight on its way here.

    Overshooting is cheap and undershooting is not - anything before the phrase
    is cut off along WITH the phrase at the other end (see withoutPhrase in
    web/index.html), while a word lost off the front is simply lost - but it
    isn't free either, because that cutting relies on the phrase being
    transcribed recognisably. So this stays an honest estimate rather than a
    generous one."""
    window_ms = len(clip) / float(SAMPLE_RATE * SAMPLE_WIDTH) * 1000.0
    return int(window_ms + (time.time() - started) * 1000.0 + CHUNK_MS)


def _wav(pcm):
    """Raw PCM wrapped in a WAV container, in memory - transcribe_audio wants
    a real file, same as voice_input._wav."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    buf.seek(0)
    return buf


def listen(session, pcm, words):
    """Feed one chunk of audio to `session`'s rolling buffer, and say whether
    any of `words` was heard in it lately.

    `pcm` is raw signed 16-bit mono at 16kHz - the same chunks wake_word.listen
    takes, so the browser's side of this (see index.html's wake section) needs
    no changes to use either engine. `words` is the list of phrases to listen
    for, matched as substrings of the lowercased transcript.

    Unlike wake_word.listen there is no model to load per session - just a
    buffer of recent audio and a clock for when to next spend a transcription
    pass on it. Answers {"wake": bool, "score": float} to match the shape the
    page already reads; there's no confidence number a transcript can honestly
    give, so score is 1.0 on a hit and 0.0 otherwise.

    Raises WakeError when the configured transcriber can't be reached or none
    is chosen, which the caller turns into a message on the page - the same
    error a stuck POST /voice would give, because it's the same call."""
    now = time.time()
    with _lock:
        _reap(now)
        s = _sessions.get(session)
        if s is None:
            if len(_sessions) >= MAX_SESSIONS:
                del _sessions[min(_sessions, key=lambda k: _sessions[k]["used"])]
            s = {"buf": bytearray(), "used": now, "checked": 0.0, "fired": 0.0}
            _sessions[session] = s
        s["used"] = now
        s["buf"].extend(pcm)
        max_bytes = int(WINDOW_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH)
        if len(s["buf"]) > max_bytes:
            del s["buf"][:len(s["buf"]) - max_bytes]

        # Neither a debounce-cooldown chunk nor an off-schedule one spends a
        # transcription pass - they just top up the buffer for whenever the
        # next real pass comes.
        due = now - s["fired"] >= DEBOUNCE_SECONDS and now - s["checked"] >= RETRY_SECONDS
        if not due:
            return {"wake": False, "score": 0.0}
        s["checked"] = now
        clip = bytes(s["buf"])

    if not words or not clip:
        return {"wake": False, "score": 0.0}

    try:
        text = voice_input.transcribe_audio(_wav(clip), "wake.wav")
    except voice_input.VoiceError as e:
        raise WakeError(str(e))

    if not _matches(text, words):
        return {"wake": False, "score": 0.0}

    with _lock:
        s["fired"] = time.time()
        # Cleared so the phrase that just fired cannot contribute to the next
        # pass - same reason wake_word.listen resets its model after firing.
        s["buf"] = bytearray()
    return {"wake": True, "score": 1.0}


def forget(session):
    """Drop a session's buffer - the page saying it has stopped listening."""
    with _lock:
        _sessions.pop(session, None)
