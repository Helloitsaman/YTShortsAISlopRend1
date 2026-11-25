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


def draw_centered_subtitle(draw: ImageDraw.ImageDraw, text: str) -> None:
    display = " ".join(text.upper().split())
    size = 82
    face = font(size, True)
    lines = wrap_text(draw, display, face, WIDTH - 140)
    while len(lines) > 2 and size > 54:
        size -= 4
        face = font(size, True)
        lines = wrap_text(draw, display, face, WIDTH - 140)
    line_height = int(size * 1.22)
    block_height = len(lines) * line_height
    # Anchor the caption block in the lower fifth. The creator handle sits
    # directly below it while remaining above the Shorts bottom controls.
    y = int(HEIGHT * 0.80 - block_height)
    emphasis = emphasized_word(display)
    emphasized = False
    stroke_width = max(5, size // 10)
    for line in lines:
        tokens = line.split()
        widths = [draw.textbbox((0, 0), word, font=face, stroke_width=stroke_width)[2] for word in tokens]
        space = draw.textbbox((0, 0), " ", font=face, stroke_width=stroke_width)[2]
        x = (WIDTH - (sum(widths) + space * max(0, len(tokens) - 1))) / 2
        for word, word_width in zip(tokens, widths):
            color = SUBTITLE_RED if word == emphasis and not emphasized else SUBTITLE_WHITE
            draw.text((int(x), y), word, font=face, fill=color,
                      stroke_width=stroke_width, stroke_fill="#000000")
            if color == SUBTITLE_RED:
                emphasized = True
            x += word_width + space
        y += line_height


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
    keywords = {w.lower() for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}
    matches = [p for p in files if any(word in p.stem.lower() for word in keywords)]
    return matches[0] if matches else None


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

    # Reference style: only the original still, centered uppercase captions,
    # one red emphasis word, and the creator's small handle at the bottom.
    draw_centered_subtitle(draw, beat_text)
    handle_font = font(34, True)
    handle_box = draw.textbbox((0, 0), CHANNEL_HANDLE, font=handle_font, stroke_width=3)
    handle_x = (WIDTH - (handle_box[2] - handle_box[0])) // 2
    handle_y = int(HEIGHT * 0.855)
    draw.text((handle_x, handle_y), CHANNEL_HANDLE, font=handle_font, fill=SUBTITLE_WHITE,
              stroke_width=3, stroke_fill="#000000")
    frame.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)


def audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


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
    images: list[Path] = []
    for beat in beats:
        image = resolve_path(str(beat["image_path"]))
        if not image.is_file():
            raise FileNotFoundError(f"Beat {beat['beat']} image is missing: {image}")
        images.append(image)

    # Cut points are the beat start times; each still shows until the next beat
    # begins, so images change in sync with the narration and cover all audio.
    starts = [max(0.0, float(b.get("start", 0.0))) for b in beats]
    starts[0] = 0.0
    cuts = starts + [duration]
    durations = [max(0.1, cuts[i + 1] - cuts[i]) for i in range(len(beats))]

    print("[2/4] Building bold phone-safe subtitle frames…", flush=True)
    frames: list[Path] = []
    for i, beat in enumerate(beats):
        frame = assets / f"frame-{i + 1:02d}.jpg"
        build_beat_frame(images[i], str(beat.get("text", "")), frame)
        frames.append(frame)

    music = choose_music(topic)
    sound_effects = [choose_sound_effect(str(b.get("text", "")) + " " + str(b.get("search_query", ""))) for b in beats]
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
            sfx_inputs.append((next_audio_index, int(max(0.0, cuts[i]) * 1000)))
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
        audio_filters.append(f"[{input_index}:a]adelay={delay}|{delay},volume=0.22[{label}]")
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
