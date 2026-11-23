"""Narration + word-level timing for Shorts Studio.

Generates the spoken narration and the word timing the image-finder agent and
the renderer use to align still images and subtitles to the speech.

Timing sources, in order of preference:
  1. ElevenLabs text-to-speech "with-timestamps": one call returns the audio
     plus per-character alignment, which we fold into word timings. (This is the
     supported alignment method for the TTS endpoint.)
  2. ElevenLabs forced-alignment: if the TTS response has no usable alignment,
     the produced audio and the script are sent to /v1/forced-alignment.
  3. Offline estimate: with no ElevenLabs key, Windows speech synthesis produces
     the audio and timings are estimated proportionally. These are clearly
     flagged as estimated so downstream steps and the creator know.

Writes `narration.wav` and `alignment.json` into the render's assets folder.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

import imageio_ffmpeg

DEFAULT_SECRET_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ShortsStudio" / "secrets.env"
HTTP_TIMEOUT = 180


def load_env_file() -> None:
    candidates = []
    explicit = os.environ.get("SHORTS_SECRET_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(DEFAULT_SECRET_PATH)
    candidates.append(Path(__file__).resolve().parent / "secrets.env")  # project-local
    seen: set[str] = set()
    for env_path in candidates:
        marker = str(env_path).lower()
        if marker in seen or not env_path.is_file():
            continue
        seen.add(marker)
        # utf-8-sig tolerates a BOM that Notepad adds when saving the file.
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def log(message: str) -> None:
    print(message, flush=True)


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    result = subprocess.run([ffmpeg_exe(), "-y", "-i", str(mp3_path), "-ac", "1", "-ar", "44100", str(wav_path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise RuntimeError("Could not convert narration audio: " + result.stderr[-1200:])


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


# --------------------------------------------------------------------------- #
# Alignment parsing
# --------------------------------------------------------------------------- #
def words_from_characters(characters: list[str], starts: list[float], ends: list[float]) -> list[dict]:
    """Fold per-character alignment into per-word {text, start, end}."""
    words: list[dict] = []
    current: list[str] = []
    word_start: float | None = None
    word_end: float | None = None
    for ch, start, end in zip(characters, starts, ends):
        if ch.isspace():
            if current:
                words.append({"text": "".join(current), "start": float(word_start), "end": float(word_end)})
                current, word_start, word_end = [], None, None
            continue
        if word_start is None:
            word_start = start
        word_end = end
        current.append(ch)
    if current:
        words.append({"text": "".join(current), "start": float(word_start), "end": float(word_end)})
    return words


# --------------------------------------------------------------------------- #
# ElevenLabs endpoints
# --------------------------------------------------------------------------- #
def elevenlabs_tts_with_timestamps(script: str, mp3_path: Path) -> list[dict]:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb").strip()
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(voice_id)}"
           "/with-timestamps?output_format=mp3_44100_128")
    payload = json.dumps({
        "text": script, "model_id": model_id,
        "voice_settings": {"stability": 0.42, "similarity_boost": 0.78, "style": 0.25,
                           "use_speaker_boost": True},
    }).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST", headers={
        "xi-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))

    audio_b64 = data.get("audio_base64") or data.get("audio")
    if not audio_b64:
        raise RuntimeError("ElevenLabs did not return audio.")
    mp3_path.write_bytes(base64.b64decode(audio_b64))

    alignment = data.get("alignment") or {}
    characters = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if characters and starts and ends and len(characters) == len(starts) == len(ends):
        return words_from_characters(characters, starts, ends)
    return []  # caller will fall back to forced alignment


def elevenlabs_forced_alignment(wav_path: Path, script: str) -> list[dict]:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    boundary = "----ShortsStudio" + uuid.uuid4().hex
    audio_bytes = wav_path.read_bytes()
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="file"; filename="narration.wav"\r\n')
    parts.append(b"Content-Type: audio/wav\r\n\r\n")
    parts.append(audio_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="text"\r\n\r\n')
    parts.append(script.encode("utf-8"))
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    request = urllib.request.Request("https://api.elevenlabs.io/v1/forced-alignment", data=body, method="POST",
                                     headers={"xi-api-key": api_key, "Accept": "application/json",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    words = []
    for word in data.get("words", []) or []:
        text = str(word.get("text", "")).strip()
        if text:
            words.append({"text": text, "start": float(word.get("start", 0.0)), "end": float(word.get("end", 0.0))})
    return words


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def synthesize(script: str, assets_dir: Path) -> dict:
    assets_dir.mkdir(parents=True, exist_ok=True)
    wav_path = assets_dir / "narration.wav"
    mp3_path = assets_dir / "narration.mp3"
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()

    words: list[dict] = []
    timing_source = "estimated"

    if api_key:
        log("[1/2] Requesting ElevenLabs narration with word timing…")
        try:
            words = elevenlabs_tts_with_timestamps(script, mp3_path)
            mp3_to_wav(mp3_path, wav_path)
            if words:
                timing_source = "elevenlabs_alignment"
                log(f"[1/2] Received narration with character alignment ({len(words)} words).")
            else:
                log("[1/2] TTS returned no alignment; running ElevenLabs forced alignment…")
                words = elevenlabs_forced_alignment(wav_path, script)
                if words:
                    timing_source = "elevenlabs_forced_alignment"
                    log(f"[1/2] Forced alignment produced {len(words)} word timings.")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            detail = exc.read().decode("utf-8", "replace")[-600:] if isinstance(exc, urllib.error.HTTPError) and exc.fp else str(exc)
            raise RuntimeError(f"ElevenLabs narration failed: {detail}")
    else:
        log("[1/2] ELEVENLABS_API_KEY is not set; using Windows speech synthesis (timings will be estimated).")
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 176)
        engine.save_to_file(script, str(wav_path))
        engine.runAndWait()
        if not wav_path.is_file() or wav_path.stat().st_size < 1000:
            raise RuntimeError("Windows speech synthesis did not create a narration file.")

    duration = wav_duration(wav_path)
    if words:
        duration = max(duration, words[-1]["end"])

    alignment = {
        "audio_path": str(wav_path.resolve()),
        "duration": duration,
        "timing_source": timing_source,
        "words": words,  # empty list means timings must be estimated downstream
        "script": script,
    }
    (assets_dir / "alignment.json").write_text(json.dumps(alignment, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[2/2] Narration ready: {duration:.1f}s, timing source: {timing_source}.")
    return alignment


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate narration audio and word timing.")
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--assets-dir", required=True, type=Path)
    args = parser.parse_args()
    load_env_file()
    draft = json.loads(args.draft.read_text(encoding="utf-8"))
    script = str(draft.get("script", "")).strip()
    loop_line = str(draft.get("loop_line", "")).strip()
    if loop_line and not script.casefold().rstrip(" .!?…").endswith(loop_line.casefold().rstrip(" .!?…")):
        script = (script.rstrip() + " " + loop_line).strip()
    if not script:
        log("Narration failed: the approved draft has no script.")
        return 1
    try:
        synthesize(script, args.assets_dir.resolve())
        return 0
    except Exception as exc:
        log(f"Narration failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
