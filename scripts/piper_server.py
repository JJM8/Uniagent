"""Piper's voices kept loaded in memory, so a reply doesn't pay to load one.

The same idea as stt_server.py beside it, and for the same reason. Piper's own
CLI is a one-shot: every call starts a Python process, reads an .onnx voice off
disk, builds an onnxruntime session, says the words, and throws all of it away.
Measured on this machine, that fixed cost is about 1.7 seconds, and the words
themselves are 7.5ms per character on top:

    "Paris."                          6 chars    1868ms
    "Paris is the capital of France." 31 chars   1817ms
    a 135-character sentence                     2695ms

So saying one word costs very nearly what saying a paragraph does, and in a
spoken conversation - where most replies ARE a word or two - almost the whole
wait is loading a model that was already loaded a minute ago.

This holds the voices open instead. First use of a voice pays the load; every
call after it is just the synthesis. Nothing else about the audio changes: the
same piper, the same .onnx files, the same 16-bit mono WAV out.

    POST /speak  {"model": "/abs/path/en_US-amy-medium.onnx", "text": "..."}
                 -> audio/wav

`model` is an absolute path rather than a voice name on purpose: provider.py
already resolves the voice tab's choice against PIPER_VOICE_DIR, and having it
resolved in one place means this server needs no configuration of its own and
cannot disagree with the app about which file a name means.

It binds to 127.0.0.1 and has no password, exactly like stt_server.py: it is a
helper for the process on this machine, not a network service, and the only
thing it will do for a caller is read a file the caller already named.

RUN IT with the python that has piper installed, which is usually NOT the one
running Uniagent - piper is typically a pipx install with a venv of its own:

    /home/you/.local/share/pipx/venvs/piper-tts/bin/python3 scripts/piper_server.py

provider.py finds this by trying it and falling back to the one-shot CLI when
it isn't there, so nothing breaks on an install that never starts it.
"""

import io
import json
import os
import sys
import threading
import time
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from piper import PiperVoice

HOST = "127.0.0.1"
PORT = int(os.environ.get("UNIAGENT_PIPER_PORT", "8322"))

# How many voices to hold at once. Each is its own onnxruntime session and a few
# tens of megabytes, and an install has a handful of voices at most - one for
# replies, maybe another being auditioned on the voice tab. Past this the least
# recently used is dropped, which costs its next caller one load rather than
# growing the process without limit.
MAX_VOICES = 4

# Longest text to accept. Piper is fast but not instant, and a caller that sends
# a novel by accident should get an error rather than a minute of silence.
MAX_CHARS = 20000

_lock = threading.Lock()
_voices = OrderedDict()      # path -> PiperVoice, most recently used last


def _voice(path):
    """The PiperVoice for `path`, loaded if this is the first time it's asked
    for. Held under the lock for the whole load: two requests for the same cold
    voice arriving together should mean one load and one wait, not two of each
    - and a load is by far the most expensive thing this process does."""
    key = str(path)
    with _lock:
        if key in _voices:
            _voices.move_to_end(key)
            return _voices[key]
        began = time.time()
        voice = PiperVoice.load(key)
        _voices[key] = voice
        while len(_voices) > MAX_VOICES:
            dropped, _ = _voices.popitem(last=False)
            print("dropped %s to stay under %d voices" % (dropped, MAX_VOICES))
        print("loaded %s in %dms" % (Path(key).stem, (time.time() - began) * 1000))
        return voice


def _say(voice, text):
    """`text` as WAV bytes. synthesize_wav writes the header itself from the
    model's own sample rate, which is the same thing provider.py's _wav() does
    by hand for the CLI path, so both produce the same FORMAT - 16-bit mono at
    the voice's rate.

    Not the same bytes, though, and not because of anything here: piper's
    voices are VITS models with a stochastic duration predictor, so the same
    sentence synthesised twice differs either way. Two calls through the CLI
    disagree with each other by as much as a CLI call and a warm one do. There
    is nothing to reconcile - it is the same model doing the same job, and only
    the loading is skipped."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        voice.synthesize_wav(text, out)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    # The default logs a line per request to stderr, which under systemd means
    # a journal entry for every sentence the agent speaks. The loads and the
    # failures are worth a line; the successes are not.
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="text/plain; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The caller gave up - a stopped turn, a closed tab. Not an error
            # worth a stack trace in the journal.
            pass

    def do_GET(self):
        """Whether this is up, and what it currently holds. provider.py doesn't
        use this - it just tries /speak - but it makes "is the warm server
        actually running?" answerable with curl instead of guesswork."""
        if self.path != "/health":
            self._send("not found", code=404)
            return
        with _lock:
            loaded = [Path(k).stem for k in _voices]
        self._send(json.dumps({"ok": True, "loaded": loaded}),
                   "application/json")

    def do_POST(self):
        if self.path != "/speak":
            self._send("not found", code=404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._send("no body", code=400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send("expected a JSON object: " + str(e), code=400)
            return

        text = (body.get("text") or "").strip()
        model = (body.get("model") or "").strip()
        if not text:
            self._send("no text", code=400)
            return
        if len(text) > MAX_CHARS:
            self._send("that text is longer than the %d character limit"
                       % MAX_CHARS, code=413)
            return
        if not model:
            self._send("no model - send the absolute path of a .onnx voice",
                       code=400)
            return
        # The caller names a file and this opens it, so it is worth being
        # specific that it must be a piper voice that exists. Anything else is
        # a bug in the caller, and a clear 400 says so where a stack trace in
        # the journal would not.
        path = Path(model)
        if path.suffix.lower() != ".onnx" or not path.is_file():
            self._send("no piper voice at " + str(path), code=400)
            return

        try:
            audio = _say(_voice(path), text)
        except Exception as e:
            # Said as a sentence, like the rest of the speech path: provider.py
            # puts whatever comes back in front of whoever pressed the button.
            self._send("piper failed to read that out - "
                       + type(e).__name__ + ": " + str(e)[:200], code=500)
            return
        self._send(audio, "audio/wav")


def main():
    print("piper voices warm on http://%s:%d" % (HOST, PORT))
    print("  POST /speak  {\"model\": \"<abs path>.onnx\", \"text\": \"...\"}")
    # Voices load on first use rather than here: which ones this install
    # actually wants is the voice tab's business, and loading a guess at
    # startup would spend seconds on a voice nobody asked for.
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except OSError as e:
        print("could not listen on %s:%d - %s" % (HOST, PORT, e), file=sys.stderr)
        print("  (already running? another process may hold the port)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
