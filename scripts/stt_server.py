"""A local speech-to-text server: faster-whisper kept loaded on the GPU
between requests, speaking the same shape OpenAI's own transcriber does -
POST {base}/audio/transcriptions, a multipart "file" and "model", back comes
{"text": ...}. That shape is the one thing provider.py's _transcribe_openai
already knows how to call (see scripts/provider.py), so this plugs into the
existing "local"/"openai"-wire provider machinery with no change on the
Uniagent side at all: add a custom provider whose base_url points here, pick
it on the voice tab, done.

Why this exists rather than paying OpenAI or Groq per minute: once a model is
loaded here it never reloads between clips - the load is the slow part
(twenty-odd seconds), and every transcription after that is just the decode.
Nothing leaves this machine, either, and there is no per-minute bill.

Model choice, benchmarked on the machine this actually runs on (a GTX 1660,
Turing, 6GB VRAM, no tensor cores - so plain float16 buys nothing here; the
speed comes from int8 running on the DP4A path Turing does have):

    model            compute      RTF on an 11s clip   realtime factor
    large-v3-turbo   float16      0.91                 1.1x
    large-v3-turbo   int8_float16 0.24                 4.2x
    medium           int8_float16 0.35                 2.8x
    small            int8_float16 0.076                13.2x
    small.en         int8_float16 0.052                19.1x

Encoder size dominates the cost, not beam width - large-v3-turbo kept the
large encoder and only trimmed decoder layers, so it never got fast on this
card. small.en is the largest model that clears "0.1x of real time" with
headroom to spare, and the English-only variant is both a little faster and a
little more accurate than plain "small" for an install that only ever hears
English. Swap MODEL below (or pass a different one as argv[1]) if that
changes.

Read that table with care, though, because RTF is a flattering measure and it
is measured on an 11s clip. Whisper pads every input to a fixed 30-second
window before the encoder runs, so what a pass costs barely depends on how
much audio went into it - the number that matters for a spoken command is the
FLOOR, not the ratio. Measured here, same clips through this server:

    clip length     large-v3-turbo      small.en
    1.3s            2.45s               0.33s
    4.6s            3.71s               0.38s
    9.7s            2.82s               0.51s
    15.3s           2.99s               0.60s

Turbo costs about two and a half seconds to hear "open the browser", and so
would have cost that on every pass of the wake word too - which is what
scripts/wake_stt.py runs back to back while anybody is talking. That floor,
not the RTF, is why this serves small.en.
"""

import email
import io
import json
import sys
import tempfile
import time
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from faster_whisper import WhisperModel

MODEL = sys.argv[1] if len(sys.argv) > 1 else "small.en"
DEVICE = "cuda"
COMPUTE_TYPE = "int8_float16"
BEAM_SIZE = 5
PORT = 8321

# The id this server advertises over /models and accepts on a request - not
# necessarily MODEL itself. provider.py's stt_models() only offers an id up
# on the voice tab's dropdown if it looks speech-shaped (contains "whisper",
# "transcribe", etc - see _STT_WORDS there), and a bare Hugging Face repo name
# like "small.en" doesn't. The model actually loaded is still MODEL; this is
# just the name the rest of Uniagent gets to recognise it by. There is only
# ever one model loaded per process, so whatever "model" a request names is
# ignored rather than checked against this.
SERVED_MODEL_ID = "whisper-" + MODEL

# Model weights land here rather than the default ~/.cache - the account's
# home directory is on the machine's nearly-full system disk (1.9GB free at
# the time this was set up), while the drive this repo actually lives on has
# room to spare.
DOWNLOAD_ROOT = "/media/joshy/WD_Blue/.stt-models"


def _load():
    print("[stt] loading " + MODEL + " onto " + DEVICE + " (" + COMPUTE_TYPE
          + ")...", flush=True)
    t0 = time.time()
    m = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE,
                      download_root=DOWNLOAD_ROOT)
    print("[stt] ready in %.1fs - listening on :%d" % (time.time() - t0, PORT),
          flush=True)
    return m


def _parse_multipart(content_type, body):
    """The "file" and "model" fields out of a multipart/form-data body -
    everything requests.post(files=..., data=...) sends. Built on the stdlib
    email package rather than the deprecated cgi module or a hand-rolled
    boundary splitter: multipart IS a MIME format, and email already parses
    MIME correctly, headers and all."""
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    msg = email.message_from_bytes(raw, policy=policy.HTTP)
    file_bytes, filename = None, "audio.wav"
    for part in msg.iter_parts():
        if part.get_filename():
            file_bytes = part.get_payload(decode=True)
            filename = part.get_filename()
    return file_bytes, filename


class Handler(BaseHTTPRequestHandler):
    # The base class logs every request to stderr by name and status, which
    # is a fine default for a page server and noise for one that is asked
    # something a few times a minute - see do_POST for what's printed instead.
    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body_dict):
        out = json.dumps(body_dict).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        # provider.py's stt_models() asks this to fill the voice tab's
        # dropdown - {"data": [...]} is the shape it and every OpenAI-wire
        # endpoint answer with.
        if self.path.rstrip("/").endswith("/models"):
            self._reply(200, {"data": [{"id": SERVED_MODEL_ID}]})
        else:
            self._reply(404, {"error": "no such route"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/audio/transcriptions"):
            self._reply(404, {"error": "no such route"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            file_bytes, filename = _parse_multipart(
                self.headers.get("Content-Type", ""), body)
            if not file_bytes:
                raise ValueError("no audio file in the request")
        except Exception as e:
            self._reply(400, {"error": "could not read the upload - " + str(e)})
            return

        # A real file on disk rather than a BytesIO: the decoder underneath
        # (PyAV) wants something it can seek and probe for its container, and
        # the clip is a few hundred KB at most - this costs nothing.
        suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".wav"
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix) as f:
                f.write(file_bytes)
                f.flush()
                t0 = time.time()
                segments, info = model.transcribe(
                    f.name, beam_size=BEAM_SIZE, vad_filter=True)
                text = "".join(s.text for s in segments).strip()
                dt = time.time() - t0
            print("[stt] %.2fs of audio in %.2fs (RTF %.2f): %r"
                  % (info.duration, dt, dt / max(info.duration, 0.001), text),
                  flush=True)
        except Exception as e:
            self._reply(500, {"error": type(e).__name__ + ": " + str(e)[:300]})
            return
        self._reply(200, {"text": text})


if __name__ == "__main__":
    model = _load()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
