"""Voice messages: Telegram voice notes are transcribed and handled like typed messages.

Uses OpenAI's transcription API (VOICE_MODEL, default gpt-4o-transcribe) with VOICE_OPENAI_API_KEY,
or the lab's OpenAI key if that's the only one. Telegram sends OGG/Opus; it's converted to MP3 with
ffmpeg first. VOICE_LANGUAGE (e.g. ro or en) improves accuracy; empty = detect.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

import sandbox

API = "https://api.openai.com/v1/audio/transcriptions"


def key() -> str | None:
    return sandbox.secret("VOICE_OPENAI_API_KEY") or sandbox.secret("LAB_OPENAI_API_KEY")


def available() -> bool:
    return bool(key())


def _to_mp3(src: Path) -> Path:
    if not shutil.which("ffmpeg"):
        return src
    dst = src.with_suffix(".mp3")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
                        "-b:a", "48k", str(dst)], capture_output=True)
    return dst if r.returncode == 0 and dst.exists() else src


def transcribe(audio: bytes, suffix: str = ".ogg") -> str:
    """Blocking (run it in a thread). Raises RuntimeError with a readable message on failure."""
    k = key()
    if not k:
        raise RuntimeError("Voice messages need VOICE_OPENAI_API_KEY in .env.")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"voice{suffix}"
        src.write_bytes(audio)
        f = _to_mp3(src)
        boundary = uuid.uuid4().hex
        fields = {"model": os.environ.get("VOICE_MODEL", "gpt-4o-transcribe"), "response_format": "json"}
        if os.environ.get("VOICE_LANGUAGE"):
            fields["language"] = os.environ["VOICE_LANGUAGE"]
        body = b""
        for name, value in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
        mime = "audio/mpeg" if f.suffix == ".mp3" else "audio/ogg"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{f.name}\"\r\n"
                 f"Content-Type: {mime}\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {k}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode()).get("text", "").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300].replace(k, "***")
        raise RuntimeError(f"Transcription failed ({e.code}): {detail}") from None
    except OSError as e:
        raise RuntimeError(f"Transcription failed: {e}") from None
