"""Build a portrait Short from reviewed stills, reference-style captions,
the creator handle, and pre-generated narration. No motion or transitions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1080, 1920
CHANNEL_HANDLE = "@nicheanimeshorts"
CAPTION_CENTER_X = WIDTH // 2
# Keep the text inside the central safe area. The Shorts action rail occupies
# the right edge; centering within this width leaves that rail unobstructed.
CAPTION_MAX_WIDTH = 700
MUSIC_DIR = ROOT / "work" / "media" / "music"
SFX_DIR = ROOT / "work" / "media" / "sfx"
SUBTITLE_WHITE = "#FFFFFF"
SUBTITLE_RED = "#FF3156"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/impact.ttf") if bold else Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else current + " " + word
        if draw.textbbox((0, 0), trial, font=face, stroke_width=2)[2] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


EMPHASIS_SKIP = {
    "THE", "AND", "FOR", "WITH", "FROM", "THAT", "THIS", "WERE", "WAS", "ARE",
    "HAVE", "HAS", "HAD", "HIS", "HER", "ITS", "BUT", "NOT", "JUST", "THEN",
    "WHEN", "WHAT", "WHY", "HOW", "WHO", "YOUR", "THEIR", "INTO", "OVER",
}


def emphasized_word(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", text.upper())
    meaningful = [word for word in words if len(word) > 2 and word not in EMPHASIS_SKIP]
    return meaningful[-1] if meaningful else (words[-1] if words else "")


def draw_centered_subtitle(draw: ImageDraw.ImageDraw, text: str) -> int:
    display = " ".join(text.upper().split())
    size = 104
    face = font(size, True)
    lines = wrap_text(draw, display, face, CAPTION_MAX_WIDTH)
    while len(lines) > 2 and size > 64:
        size -= 6
        face = font(size, True)
        lines = wrap_text(draw, display, face, CAPTION_MAX_WIDTH)
    line_height = int(size * 1.12)
    block_height = len(lines) * line_height
    # Center the caption in the video safe area, above the lower creator/title
    # controls. Its width is capped to stay clear of the right action rail.
    y = int(HEIGHT * 0.62 - block_height / 2)
    emphasis = emphasized_word(display)
    emphasized = False
    stroke_width = max(6, size // 10)
    for line in lines:
        tokens = line.split()
        widths = [draw.textbbox((0, 0), word, font=face, stroke_width=stroke_width)[2] for word in tokens]
        space = draw.textbbox((0, 0), " ", font=face, stroke_width=stroke_width)[2]
        x = CAPTION_CENTER_X - (sum(widths) + space * max(0, len(tokens) - 1)) / 2
        for word, word_width in zip(tokens, widths):
            color = SUBTITLE_RED if word == emphasis and not emphasized else SUBTITLE_WHITE
            draw.text((int(x), y), word, font=face, fill=color,
                      stroke_width=stroke_width, stroke_fill="#000000")
            if color == SUBTITLE_RED:
                emphasized = True
            x += word_width + space
        y += line_height
    return y


def choose_music(topic: str) -> Path | None:
    files = sorted(p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")) if MUSIC_DIR.is_dir() else []
    if not files:
        return None
    digest = hashlib.sha256(topic.encode("utf-8")).digest()
    return files[digest[0] % len(files)]


def choose_sound_effect(text: str) -> Path | None:
    if not SFX_DIR.is_dir():
        return None
    files = sorted(p for p in SFX_DIR.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg"))
    # Add a brief impact only on an actual story turn, rather than on every
    # beat. Kenney's CC0 Impact Sounds pack uses names containing "impact".
    words = set(re.findall(r"[a-z0-9']+", text.lower()))
    event_words = {
        "reveal", "reveals", "revealed", "truth", "secret", "secrets",
        "realize", "realizes", "realized", "betrayal", "betrays", "death",
        "dies", "killed", "attack", "battle", "rumbling", "power", "cost",
        "price", "shocking", "suddenly", "finally", "turns", "twist",
    }
    if not words.intersection(event_words):
        return None
    matches = [p for p in files if "impact" in p.stem.lower() or "hit" in p.stem.lower()]
    if not matches:
        # Backwards-compatible fallback for user-supplied descriptive names.
        matches = [p for p in files if any(token in p.stem.lower() for token in words if len(token) > 3)]
    if not matches:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return matches[digest[0] % len(matches)]


def read_credit(path: Path) -> str:
    sidecar = path.with_suffix(".txt")
    return sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""


def resolve_path(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def build_beat_frame(image_path: Path, beat_text: str, output: Path) -> None:
    with Image.open(image_path) as raw:
        if getattr(raw, "is_animated", False):
            raw.seek(0)
        background = ImageOps.fit(raw.convert("RGB"), (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
    frame = background.convert("RGBA")
    draw = ImageDraw.Draw(frame)

    # One short word group per frame; the handle stays beneath the caption,
    # above the platform's bottom title and subscribe controls.
    caption_bottom = draw_centered_subtitle(draw, beat_text)
    handle_font = font(30, True)
    handle_box = draw.textbbox((0, 0), CHANNEL_HANDLE, font=handle_font, stroke_width=3)
    handle_x = int(CAPTION_CENTER_X - (handle_box[2] - handle_box[0]) / 2)
    handle_y = max(int(HEIGHT * 0.695), caption_bottom + 24)
    draw.text((handle_x, handle_y), CHANNEL_HANDLE, font=handle_font, fill=SUBTITLE_WHITE,
              stroke_width=3, stroke_fill="#000000")
    frame.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)


def audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def _group_words(words: list[dict]) -> list[list[dict]]:
    """Split a visual beat into short, readable captions of up to five words."""
    count = len(words)
    if count <= 5:
        return [words] if words else []
    group_count = max(2, (count + 4) // 5)
    base, extra = divmod(count, group_count)
    groups: list[list[dict]] = []
    cursor = 0
    for index in range(group_count):
        size = base + (1 if index < extra else 0)
        groups.append(words[cursor:cursor + size])
        cursor += size
    return groups


def build_caption_segments(beats: list[dict], aligned_words: list[dict], duration: float) -> list[dict]:
    """Return one timed frame per 3–5-word caption, retaining its beat's still."""
    valid_words = []
    for word in (aligned_words or []):
        text = str(word.get("text", "")).strip()
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            valid_words.append({"text": text, "start": start, "end": end})
    valid_words.sort(key=lambda item: item["start"])

    segments: list[dict] = []
    for index, beat in enumerate(beats):
        beat_start = max(0.0, float(beat.get("start", 0.0)))
        beat_end = (max(beat_start + 0.05, float(beats[index + 1].get("start", duration)))
                    if index + 1 < len(beats) else duration)
        beat_end = min(duration, beat_end)
        beat_end = max(beat_start + 0.05, beat_end)
        beat_words = [
            word for word in valid_words
            if beat_start <= (word["start"] + word["end"]) / 2 < beat_end
        ]
        has_word_timing = bool(beat_words)
        if not beat_words:
            beat_words = [{"text": word} for word in str(beat.get("text", "")).split()]
        groups = _group_words(beat_words)
        if not groups:
            continue

        cursor = beat_start
        total_words = sum(len(group) for group in groups)
        for group_index, group in enumerate(groups):
            if has_word_timing:
                start = beat_start if group_index == 0 else max(cursor, float(group[0]["start"]))
                end = (max(start + 0.05, float(groups[group_index + 1][0]["start"]))
                       if group_index + 1 < len(groups) else beat_end)
            else:
                start = cursor
                group_weight = len(group) / max(1, total_words)
                end = (beat_end if group_index + 1 == len(groups)
                       else start + (beat_end - beat_start) * group_weight)
            end = min(beat_end, max(start + 0.05, end))
            segments.append({
                "image_path": str(beat.get("image_path", "")),
                "text": " ".join(str(word["text"]) for word in group),
                "start": start,
                "end": end,
            })
            cursor = end
    return segments


def render_from_beats(beats_path: Path, output_path: Path) -> None:
    data = json.loads(beats_path.read_text(encoding="utf-8"))
    beats = data.get("beats", [])
    if not beats:
        raise ValueError("The beat plan is empty.")
    missing = [b["beat"] for b in beats if not str(b.get("image_path", "")).strip()]
    if missing:
        raise ValueError("These beats still need a licensed image before rendering: "
                         + ", ".join(str(m) for m in missing))

    assets = output_path.parent / (output_path.stem + "-assets")
    assets.mkdir(parents=True, exist_ok=True)

    wav_path = resolve_path(str(data.get("audio_path", "")))
    if not wav_path.is_file():
        # Fall back to the narration stored beside the beat plan.
        wav_path = beats_path.parent / "narration.wav"
    if not wav_path.is_file():
        raise FileNotFoundError("The narration audio was not found; regenerate narration before rendering.")
    duration = max(float(data.get("duration") or 0.0), audio_duration(wav_path))

    topic = str(data.get("topic", "Short"))
    print(f"[1/4] Loading {len(beats)} reviewed stills…", flush=True)
    for beat in beats:
        image = resolve_path(str(beat["image_path"]))
        if not image.is_file():
            raise FileNotFoundError(f"Beat {beat['beat']} image is missing: {image}")

    # Still changes follow the reviewed beat timestamps. Captions within each
    # still are split into short, timed groups using ElevenLabs word alignment.
    starts = [max(0.0, float(b.get("start", 0.0))) for b in beats]
    starts[0] = 0.0
    beat_cuts = starts + [duration]
    alignment_path = beats_path.parent / "alignment.json"
    alignment = json.loads(alignment_path.read_text(encoding="utf-8")) if alignment_path.is_file() else {}
    segments = build_caption_segments(beats, alignment.get("words", []), duration)
    if not segments:
        raise ValueError("No subtitle segments could be built from the narration.")
    durations = [max(0.05, float(segment["end"]) - float(segment["start"])) for segment in segments]

    print(f"[2/4] Building {len(segments)} word-synced caption frames (4–5 words each)…", flush=True)
    frames: list[Path] = []
    for i, segment in enumerate(segments):
        frame = assets / f"frame-{i + 1:03d}.jpg"
        image = resolve_path(segment["image_path"])
        build_beat_frame(image, str(segment["text"]), frame)
        frames.append(frame)

    music = choose_music(topic)
    # Keep effects sparse (at most three per Short) so they punctuate key
    # moments without competing with the ElevenLabs narration.
    sound_effects: list[Path | None] = []
    last_sfx_at = -999.0
    for i, beat in enumerate(beats):
        effect = choose_sound_effect(str(beat.get("text", "")))
        beat_time = beat_cuts[i]
        if effect and (len([item for item in sound_effects if item]) >= 3 or beat_time - last_sfx_at < 4.0):
            effect = None
        if effect:
            last_sfx_at = beat_time
        sound_effects.append(effect)
    credits = [c for c in (read_credit(p) for p in [music, *sound_effects] if p) if c]
    attribution_path = assets / "attribution.txt"
    if credits:
        existing = attribution_path.read_text(encoding="utf-8").strip() if attribution_path.is_file() else ""
        attribution_path.write_text("\n".join(x for x in (existing, "Audio credits: " + "; ".join(dict.fromkeys(credits))) if x) + "\n", encoding="utf-8")

    print("[3/4] Encoding the vertical video with hard cuts and narration…", flush=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoder_list = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", check=False).stdout
    video_codec = "libx264" if "libx264" in encoder_list else "mpeg4"
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for frame, scene_duration in zip(frames, durations):
        command.extend(["-loop", "1", "-framerate", "30", "-t", f"{scene_duration:.4f}", "-i", str(frame)])
    voice_index = len(frames)
    command.extend(["-i", str(wav_path)])
    music_index = None
    if music:
        music_index = voice_index + 1
        command.extend(["-stream_loop", "-1", "-i", str(music)])
    sfx_inputs: list[tuple[int, int]] = []
    next_audio_index = voice_index + 1 + (1 if music else 0)
    for i, effect in enumerate(sound_effects):
        if effect:
            command.extend(["-i", str(effect)])
            sfx_inputs.append((next_audio_index, int(max(0.0, beat_cuts[i]) * 1000)))
            next_audio_index += 1

    filters = []
    for i, scene_duration in enumerate(durations):
        # Static still scaled to fill the frame: no zoom, pan, or parallax.
        filters.append(f"[{i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
                       f"trim=duration={scene_duration:.4f},setpts=PTS-STARTPTS,fps=30,format=yuv420p,settb=AVTB[v{i}]")
    concat_inputs = "".join(f"[v{i}]" for i in range(len(frames)))
    filters.append(f"{concat_inputs}concat=n={len(frames)}:v=1:a=0,format=yuv420p[vout]")

    audio_filters = [f"[{voice_index}:a]anull[voice]"]
    mix_labels = ["[voice]"]
    if music_index is not None:
        audio_filters.append("[voice]asplit=2[voicemix][voiceside]")
        audio_filters.append(f"[{music_index}:a]volume=0.18,atrim=duration={duration + 0.1:.3f},asetpts=PTS-STARTPTS[music]")
        audio_filters.append("[music][voiceside]sidechaincompress=threshold=0.025:ratio=8:attack=15:release=260[ducked]")
        mix_labels = ["[voicemix]", "[ducked]"]
    for i, (input_index, delay) in enumerate(sfx_inputs):
        label = f"sfx{i}"
        audio_filters.append(f"[{input_index}:a]adelay={delay}|{delay},volume=0.12[{label}]")
        mix_labels.append(f"[{label}]")
    if len(mix_labels) == 1:
        audio_filters.append("[voice]anull[aout]")
    else:
        audio_filters.append("".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=2,alimiter=limit=0.95[aout]")
    filters.extend(audio_filters)
    command.extend(["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                    "-c:v", video_codec, *(["-preset", "veryfast", "-crf", "21"] if video_codec == "libx264" else ["-q:v", "3"]),
                    "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(output_path)])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise RuntimeError("FFmpeg could not render the short: " + (result.stderr.strip()[-1800:] or f"exit {result.returncode}"))
    print("[4/4] Render finished. Preview is ready for your review.", flush=True)
    print(f"Rendered short: {output_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--beats", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    render_from_beats(args.beats, args.output)


if __name__ == "__main__":
    main()
