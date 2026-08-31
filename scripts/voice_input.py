"""Hold-to-talk. Hold Scroll Lock, speak, let go - what you said gets typed.

Records the default audio input while the key is held, sends the clip off to be
transcribed when it's released, and hands the text to the callback start() was
given. Nothing here is required for the agent to work: if the key listener or
the microphone isn't available, voice turns itself off and typing still works.

The web page has its own hold-to-talk (a button on the phone, a key on the
desktop) which records in the browser and posts the clip to server.py's
POST /voice - that shares transcribe_audio() below, so both routes transcribe
the same way, on the same provider; only the recording differs.

WHO transcribes is a setting, not a constant: the voice tab picks one of the
providers on the providers tab and a speech model on it, and that choice is
read here on every clip (settings.py re-reads its file each time, so a change
in the web UI is live in this process and in the cron watcher without either
being restarted). An install that has never chosen one falls back to Whisper at
OpenAI on the OPENAI_API_KEY in .env, which is what this did before there was
anything to choose.
"""

import contextlib
import io
import os
import sys
import threading
import wave
from pathlib import Path

try:
    import pyaudio
except ImportError:
    # No audio lib = no local microphone path, but NOT a broken install: the
    # web page records in the browser and never needs PyAudio, and the server
    # has to start without it. _pa() reports the gap when someone actually
    # holds the key, so the note lands at the moment it matters.
    pyaudio = None
import requests

import provider
import settings

try:
    from pynput import keyboard
except ImportError:
    # No display = no pynput available. Create dummy objects to avoid crashes.
    class DummyKey:
        def __eq__(self, other):
            return self is other
        def __repr__(self):
            return "<No keyboard>"
    keyboard = type(sys)('keyboard')
    keyboard.Key = type(sys)('Key')
    keyboard.Key.scroll_lock = DummyKey()

# Which key is hold-to-talk. Scroll Lock is used because it's dead in modern
# software, sits away from the typing keys, and sends a clean press/release
# (unlike the RK580's REC key, which the keyboard handles onboard so the OS
# never sees it). To use a key with no standard name instead, run
#     python3 voice_input.py --detect
# press it, and put the vk it prints into HOLD_KEY_VK - that then wins over the
# named HOLD_KEY below.
HOLD_KEY_VK = None
HOLD_KEY = keyboard.Key.scroll_lock

ENV_FILE = Path(__file__).parent.parent / ".env"

SAMPLE_RATE = 16000  # what Whisper wants
CHANNELS = 1
SAMPLE_WIDTH = 2     # bytes per sample, int16
CHUNK = 1024

# The model the OPENAI_API_KEY fallback asks for. Only reached when the voice
# tab has never been given a provider - anything chosen there names its own.
FALLBACK_MODEL = "whisper-1"

MIN_SECONDS = 0.3  # ignore an accidental tap of the key

DIM = "\033[2m"
RESET = "\033[0m"

_callback = None
_audio = None
_stream = None
_frames = []
_recording = False


def _note(text):
    print(DIM + "[voice] " + text + RESET)


def _matches(key):
    """Is this the hold-to-talk key? Matched by vk if one is configured, else
    by the named key."""
    if HOLD_KEY_VK is not None:
        return getattr(key, "vk", None) == HOLD_KEY_VK
    return key == HOLD_KEY


def _key_label():
    return ("vk " + str(HOLD_KEY_VK)) if HOLD_KEY_VK is not None else str(HOLD_KEY)


@contextlib.contextmanager
def _quiet():
    """Silence the ALSA/JACK probe spam.

    Those warnings come from C libraries writing straight to file descriptor 2,
    so redirecting sys.stderr in Python does nothing - the fd itself has to be
    swapped. Python-level errors are unaffected: they travel as exceptions, not
    as writes to fd 2.

    The spam is a Linux sound stack thing and there is none of it on Windows,
    where fd 2 may not even exist - a process started with no console has no
    standard handles, and os.dup(2) raises there. So the whole dance is skipped
    wherever it cannot be done, which costs nothing: the only thing lost is the
    silencing of warnings that were never going to be printed.
    """
    try:
        saved = os.dup(2)
        null = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        yield
        return
    try:
        os.dup2(null, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(null)
        os.close(saved)


def _pa():
    """The PyAudio instance, opened on first use - starting it is slow and
    noisy, so we don't do it until someone actually holds the key."""
    global _audio
    if pyaudio is None:
        raise RuntimeError(
            "the 'pyaudio' package is not installed - run "
            "pip install pyaudio (or pip install -r requirements-voice.txt) "
            "to enable the local hold-to-talk mic")
    if _audio is None:
        with _quiet():  # enumerating every device is what triggers the spam
            _audio = pyaudio.PyAudio()
    return _audio


def _audio_in(in_data, frame_count, time_info, status):
    """Called by PyAudio on its own thread for each chunk of audio."""
    _frames.append(in_data)
    return (None, pyaudio.paContinue)


def _start_recording():
    global _stream, _recording
    _frames.clear()
    with _quiet():
        _stream = _pa().open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK,
            stream_callback=_audio_in,
        )
        _stream.start_stream()
    _recording = True
    _note("recording...")


def _stop_recording():
    """Stop the stream and return everything recorded as raw PCM."""
    global _stream, _recording
    _recording = False
    _stream.stop_stream()
    _stream.close()
    _stream = None
    return b"".join(_frames)


def _wav(pcm):
    """Wrap raw PCM in a WAV container, in memory - Whisper wants a real file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    buf.seek(0)
    return buf


def _api_key():
    """The OpenAI key. Prefers the environment, falls back to the .env file.

    Anything launched from the desktop (VS Code included) inherits the
    environment from login, so a key exported later in the session never
    reaches it. The file always does.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key

    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


class VoiceError(Exception):
    """Whisper couldn't be asked, or didn't answer. The message is meant to be
    read by a human - it's what the web page shows when a clip fails."""


def _chosen():
    """(provider, model) off the voice tab, or (None, "") when nothing has been
    chosen. Read fresh every clip: settings.py re-reads its file on every call,
    so changing this in the web UI is live on the next thing you say.

    settings.load() has already blanked a provider that no longer exists, so a
    name that comes back here is one that can actually be called."""
    try:
        s = settings.load()
    except Exception:
        return None, ""
    return (s.get("voice_provider") or None), (s.get("voice_model") or "")


def transcribe_audio(clip, filename):
    """The spoken text in `clip`, or raise VoiceError saying why not.

    Any format the model takes (wav, webm, ogg, mp4, mp3...) - it goes by the
    extension of `filename`, so that has to match the bytes. The browser sends
    WAV today (it cuts clips out of a continuous stream and writes the header
    itself - see wakeWav in web/index.html), but this stays format-agnostic
    rather than assuming that: the extension is already the contract, and the
    key-held path below is the only caller that can promise WAV.

    Goes to whichever provider the voice tab names, and only falls back to
    Whisper on the OPENAI_API_KEY in .env when it names nobody.
    """
    # Read once, here: `clip` can be a file object, and a stream that has been
    # consumed by one attempt has nothing left for the next.
    data = clip.read() if hasattr(clip, "read") else clip

    name, model = _chosen()
    if name:
        try:
            return provider.transcribe(name, model, data, filename)
        except requests.RequestException as e:
            raise VoiceError("could not reach " + name + " - " + type(e).__name__
                             + ": " + str(e))
        except Exception as e:
            # provider.transcribe explains itself in a sentence; anything else
            # is at least named rather than swallowed.
            raise VoiceError(str(e)[:300])

    return _whisper_fallback(data, filename)


def _whisper_fallback(data, filename):
    """What this did before the voice tab existed: Whisper at OpenAI, on the
    OPENAI_API_KEY in .env. Kept so an install that has never chosen a provider
    still has a working microphone."""
    key = _api_key()
    if not key:
        raise VoiceError("no transcriber - pick a provider and a speech model on "
                         "the settings page's voice tab, or set OPENAI_API_KEY in "
                         + str(ENV_FILE))

    try:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + key},
            files={"file": (filename, data)},
            data={"model": FALLBACK_MODEL},
            timeout=60,
        )
    except requests.RequestException as e:
        raise VoiceError("could not reach Whisper - " + type(e).__name__ + ": " + str(e))

    if r.status_code != 200:
        raise VoiceError("Whisper returned HTTP " + str(r.status_code) + " - " + r.text[:200])

    return r.json().get("text", "").strip()


def _transcribe(pcm):
    """The spoken text, or None if it couldn't be transcribed. The key-held
    path never raises - a failed clip is a note in the terminal and nothing
    more, because there's no one waiting on an answer here."""
    try:
        return transcribe_audio(_wav(pcm), "speech.wav")
    except VoiceError as e:
        _note(str(e))
        return None


def _handle(pcm):
    """Transcribe a finished clip and run it as a turn. Own thread."""
    text = _transcribe(pcm)
    if not text:
        return
    print("> " + text)  # so you can see what it actually heard
    _callback(text)


def _on_press(key):
    # Holding a key repeats the press event, so only the first one counts.
    if _matches(key) and not _recording:
        try:
            _start_recording()
        except Exception as e:
            _note("no microphone - " + type(e).__name__ + ": " + str(e))


def _on_release(key):
    if not _matches(key) or not _recording:
        return

    pcm = _stop_recording()
    seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    if seconds < MIN_SECONDS:
        _note("too short, ignored")
        return

    # Transcribing takes a second or two - don't block the key listener with it.
    threading.Thread(target=_handle, args=(pcm,), daemon=True).start()


def start(callback):
    """Start listening for the hold-to-talk key. Returns straight away."""
    global _callback
    _callback = callback

    try:
        listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
        listener.daemon = True
        listener.start()
    except Exception as e:
        # A missing display or an unreadable input device must not stop the
        # agent - you can still type.
        _note("disabled - " + type(e).__name__ + ": " + str(e))
        return None

    _note("hold " + _key_label() + " to talk")
    return listener


def _detect():
    """Print the identity of every key pressed, so you can find the vk of the
    REC key. Run: python3 voice_input.py --detect"""
    print("Press the key you want for hold-to-talk. It will print below.")
    print("If pressing REC prints nothing, the keyboard is handling it onboard")
    print("and the OS never sees it - it can't be used until remapped.")
    print("Press Esc to quit.\n")

    def on_press(key):
        if key == keyboard.Key.esc:
            return False
        vk = getattr(key, "vk", None)
        char = getattr(key, "char", None)
        print("  key=%-16r vk=%s  char=%r" % (key, vk, char))

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    import sys
    if "--detect" in sys.argv:
        _detect()
    else:
        print("This module is imported by main.py. To find a key's vk, run:")
        print("    python3 voice_input.py --detect")
