"""Responsive approval dashboard and daily draft scheduler for Shorts Studio."""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import image_finder  # the licensed still-image agent (beat division + per-beat ops)


ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
WEB = ROOT / "web"
DRAFT_PATH = WORK / "current_draft.json"
STATUS_PATH = WORK / "pipeline_status.json"
PREFERENCES_PATH = WORK / "preferences.json"
PORT = int(os.environ.get("SHORTS_DASHBOARD_PORT", "8765"))
DEFAULT_SECRET_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ShortsStudio" / "secrets.env"

state_lock = threading.Lock()
json_write_lock = threading.RLock()
job_running = False
active_process: subprocess.Popen[str] | None = None
cancel_event = threading.Event()

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topic", "angle", "hook", "script", "loop_line", "title", "description", "tags", "scenes", "visual_beats", "sources"],
    "properties": {
        "topic": {"type": "string"},
        "angle": {"type": "string"},
        "hook": {"type": "string"},
        "script": {"type": "string"},
        "loop_line": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "visual_beats": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["phrase", "image_query"],
                "properties": {
                    "phrase": {"type": "string"},
                    "image_query": {"type": "string"},
                },
            },
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["caption", "visual_notes", "visual_query"],
                "properties": {
                    "caption": {"type": "string"},
                    "visual_notes": {"type": "string"},
                    "visual_query": {"type": "string"},
                    "image_path": {"type": "string"},
                },
            },
        },
    },
}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # OneDrive/antivirus may briefly hold the destination during sync. Use a
    # per-write temporary name so concurrent requests never collide, then retry
    # the atomic replacement for transient Windows sharing/permission locks.
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    with json_write_lock:
        try:
            temp.write_text(payload, encoding="utf-8")
            for attempt in range(6):
                try:
                    temp.replace(path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.15 * (attempt + 1))
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def preferences() -> dict:
    value = read_json(PREFERENCES_PATH, {})
    return value if isinstance(value, dict) else {}


def secret_file_candidates() -> list[Path]:
    """Secrets files to load, most-specific first. A key already set wins, so an
    explicit file or LOCALAPPDATA takes precedence over the project folder."""
    candidates: list[Path] = []
    explicit = os.environ.get("SHORTS_SECRET_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(DEFAULT_SECRET_PATH)
    candidates.append(ROOT / "secrets.env")  # convenient project-local location
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_optional_secrets() -> None:
    for secret_path in secret_file_candidates():
        if not secret_path.is_file():
            continue
        # utf-8-sig tolerates a BOM that Notepad adds when saving the file.
        for raw in secret_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def current_assets_dir() -> Path | None:
    """Return the assets folder of the render currently staged for review, if any."""
    status = read_json(STATUS_PATH, {})
    if not isinstance(status, dict):
        return None
    file = str(status.get("file", "")).strip()
    if not file:
        return None
    rendered = Path(file)
    return rendered.parent / (rendered.stem + "-assets")


def set_status(stage: str, message: str, **extra: object) -> None:
    current = read_json(STATUS_PATH, {})
    if not isinstance(current, dict):
        current = {}
    now = time.time()
    current.update({"stage": stage, "message": message, "updated_at": now, **extra})
    if "phase_started_at" not in extra or extra.get("reset_phase_clock"):
        current["phase_started_at"] = now
    current.pop("reset_phase_clock", None)
    write_json(STATUS_PATH, current)


def find_claude() -> str | None:
    configured = os.environ.get("CLAUDE_CLI")
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude.exe"
    return str(fallback) if fallback.is_file() else None


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ValueError("Claude did not return a JSON draft.")
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("Claude returned an unexpected draft format.")
    return data


def validate_draft(draft: dict) -> None:
    required = ("topic", "angle", "hook", "script", "loop_line", "title", "description", "tags", "scenes", "sources")
    missing = [key for key in required if key not in draft]
    if missing:
        raise ValueError("Draft is missing fields: " + ", ".join(missing))
    if not str(draft["script"]).strip():
        raise ValueError("The spoken script is empty.")
    if len(str(draft["title"])) > 100:
        raise ValueError("Draft title exceeds YouTube's 100-character limit.")
    if not isinstance(draft["scenes"], list) or not 4 <= len(draft["scenes"]) <= 6:
        raise ValueError("Draft must contain four to six visual scenes.")


def normalize_draft(draft: dict) -> dict:
    """Fill fields added by newer versions so an older saved draft remains editable."""
    result = dict(draft)
    script = str(result.get("script", "")).strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script) if s.strip()]
    result.setdefault("hook", sentences[0] if sentences else "")
    result.setdefault("loop_line", sentences[-1] if sentences else "")
    normalized_scenes = []
    for scene in result.get("scenes", []) if isinstance(result.get("scenes"), list) else []:
        if not isinstance(scene, dict):
            continue
        item = dict(scene)
        item.setdefault("visual_query", str(item.get("visual_notes", item.get("caption", "")))[:120])
        item.setdefault("image_path", "")
        normalized_scenes.append(item)
    result["scenes"] = normalized_scenes
    normalized_beats = []
    for beat in result.get("visual_beats", []) if isinstance(result.get("visual_beats"), list) else []:
        if isinstance(beat, dict) and str(beat.get("phrase", "")).strip():
            normalized_beats.append({"phrase": str(beat.get("phrase", "")).strip(),
                                     "image_query": str(beat.get("image_query", "")).strip()})
    result["visual_beats"] = normalized_beats
    result.setdefault("sources", [])
    result.setdefault("tags", [])
    return result


def ask_claude(prompt: str, current: dict | None = None) -> dict:
    cli = find_claude()
    if not cli:
        raise RuntimeError("Claude Code CLI was not found. Install it or set CLAUDE_CLI to its path.")

    if current is None:
        task = (
            "Create one original YouTube Shorts package about anime/TV storytelling commentary. "
            "Return topic, analytical angle, a compelling first-line hook, a 90-120 word spoken script, "
            "a final loop_line, title, short description, up to 12 tags, 4-6 scenes, and sources when reliable sources are known. "
            "Make it sound conversational, surprising and specific, not like a recap or generic AI narration. "
            "Do not claim a topic is trending unless the supplied sources substantiate that. Do not invent sources or facts. "
            "For anime fan reception, interpretations, debates, and community context, prioritize relevant Reddit discussions. "
            "Use exact Reddit post permalinks in sources when you actually accessed them, and distinguish fan opinions from canon. "
            "Verify concrete plot, episode, character, and production facts with reliable primary sources where possible. "
            "If Reddit or web research tools are unavailable, do not pretend to have searched Reddit or fabricate citations; "
            "leave unsupported claims out and make that limitation clear in the sources. "
            "Avoid dialogue and lyrics. The spoken script must start with the exact hook and end with the exact loop_line. "
            "The loop_line closes the argument and makes the final words flow naturally into the opening hook on replay. "
            "Each scene needs a short on-screen caption and a concise visual_query. "
            "Keep captions to 2-7 words, readable on a phone, and do not duplicate the complete spoken script on screen. "
            "Also return visual_beats: an ordered list of 10 to 15 short, consecutive, non-overlapping phrases whose words, "
            "in order, are exactly the spoken script (concatenating the phrases with single spaces reproduces the script word for word, "
            "including the hook first and the loop_line last). For each beat include image_query: a concrete, specific description of the "
            "single real image to show while that exact phrase is spoken. When the phrase is about a character, name that character and the "
            "show (for example 'Mikasa Ackerman Attack on Titan'); for a place, event, object, or concept, name it concretely "
            "(for example 'Wall Maria ruins' or 'medieval city gate at dusk'). The image is pulled from public-domain and Creative Commons "
            "libraries, so describe the subject itself and never request a screenshot, poster, logo, or copyrighted frame. Every image_query "
            "must be specific to its own beat and match what is being said there — never generic filler and never the same query twice. "
        )
        prompt_text = task + "\nCreator preferences and show list: " + prompt.strip()
    else:
        prompt_text = (
            "Revise this Shorts package from the creator's notes. Return the complete package in the same schema. "
            "Preserve unchanged facts and structure. Keep the strong hook and the replay loop unless the notes ask otherwise. "
            "Keep visual_beats consistent: their phrases must still concatenate to exactly the (possibly revised) script in order, "
            "stay at 10-15 beats, and each image_query must name the specific subject discussed in its phrase (character + show, place, "
            "object, or concept) for a Creative Commons image search — never a copyrighted screenshot, poster, or logo. "
            "Prioritize Reddit discussions for fan interpretations when research access is available; include exact post permalinks, "
            "label opinions as opinions, and verify concrete canon facts with primary sources. Never claim Reddit research that was not done. "
            "Do not invent facts or sources.\nCreator notes: " + prompt.strip() +
            "\nCurrent package:\n" + json.dumps(current, ensure_ascii=False)
        )

    set_status("drafting", "Claude is choosing a fresh angle, then writing the hook, script and visual plan…",
               eta_min_seconds=20, eta_max_seconds=120)
    command = [
        cli, "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config",
        "--permission-prompts", "none", "--no-session-persistence", "--max-turns", "1",
        "--json-schema", json.dumps(SCHEMA, separators=(",", ":")), prompt_text,
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=240, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError("Claude Code could not generate the draft: " + detail[-1800:])
    draft = extract_json(result.stdout)
    validate_draft(draft)
    return draft


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        proc.terminate()


def run_step(stage: str, message: str, command: list[str], timeout: int,
             eta_min: int, eta_max: int) -> str:
    global active_process
    proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    with state_lock:
        active_process = proc
    lines: list[str] = []
    current_stage = stage
    current_message = message
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_queue.put(line.rstrip())
        output_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    started = time.time()
    ended = False
    while proc.poll() is None:
        if cancel_event.is_set():
            _kill_process_tree(proc)
            proc.wait(timeout=10)
            raise InterruptedError("Cancellation requested.")
        if time.time() - started > timeout:
            _kill_process_tree(proc)
            proc.wait(timeout=10)
            raise TimeoutError(f"{message} timed out after {timeout // 60} minutes.")
        try:
            line = output_queue.get(timeout=2)
            if line is None:
                ended = True
            elif line:
                lines.append(line)
                if len(lines) > 100:
                    del lines[:20]
                detail = line.replace("\r", " ").strip()
                if len(detail) > 240:
                    detail = detail[-240:]
                current_message = detail or message
                if stage == "rendering" and detail.startswith("[1/5]"):
                    current_stage = "assets"
                elif stage == "rendering" and detail.startswith(("[2/5]", "[3/5]", "[4/5]", "[5/5]")):
                    current_stage = "rendering"
                extras: dict[str, object] = {"eta_min_seconds": eta_min, "eta_max_seconds": eta_max,
                                             "phase_started_at": started, "progress_line": detail}
                if stage == "uploading":
                    progress_match = re.search(r"(\d{1,3})%", detail)
                    if progress_match:
                        extras["progress_percent"] = min(100, int(progress_match.group(1)))
                set_status(current_stage, current_message, **extras)
        except queue.Empty:
            pass
        elapsed = int(time.time() - started)
        set_status(current_stage, current_message, eta_min_seconds=max(0, eta_min - elapsed),
                   eta_max_seconds=max(0, eta_max - elapsed), phase_started_at=started)
    if not ended:
        reader.join(timeout=2)
        while True:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                break
            if line:
                lines.append(line)
    with state_lock:
        active_process = None
    if proc.returncode:
        raise RuntimeError("\n".join(lines[-35:])[-2400:] or f"{message} failed (exit {proc.returncode}).")
    return "\n".join(lines)


def run_planning(draft: dict) -> None:
    """Phase 1: generate narration + alignment, then divide into beats and find a
    licensed still for each. Stops at the 'review_stills' checkpoint — no render
    and no upload happen here."""
    global job_running, active_process
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = WORK / "renders" / f"short-{stamp}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        assets_dir = output.parent / (output.stem + "-assets")
        assets_dir.mkdir(parents=True, exist_ok=True)
        write_json(DRAFT_PATH, draft)
        cancel_event.clear()

        set_status("aligning", "Generating ElevenLabs narration and word-level timing…",
                   file=str(output), eta_min_seconds=15, eta_max_seconds=180)
        run_step(
            "aligning", "Generating narration and alignment…",
            [sys.executable, "-u", str(ROOT / "narration.py"), "--draft", str(DRAFT_PATH),
             "--assets-dir", str(assets_dir)],
            600, 15, 180,
        )
        if cancel_event.is_set():
            raise InterruptedError("Cancellation requested.")

        set_status("assets", "Dividing the narration into beats and finding a licensed still for each…",
                   file=str(output), eta_min_seconds=20, eta_max_seconds=180)
        run_step(
            "assets", "Finding licensed stills per beat and checking reuse terms…",
            [sys.executable, "-u", str(ROOT / "image_finder.py"), "--mode", "plan",
             "--draft", str(DRAFT_PATH), "--assets-dir", str(assets_dir)],
            600, 20, 180,
        )
        if cancel_event.is_set():
            raise InterruptedError("Cancellation requested.")

        plan = read_json(assets_dir / "beats.json", {})
        beats = plan.get("beats", []) if isinstance(plan, dict) else []
        filled = sum(1 for b in beats if b.get("image_path"))
        flagged = len(beats) - filled
        message = (f"Review the {len(beats)} stills below. {filled} found, "
                   + (f"{flagged} need your attention. " if flagged else "all matched. ")
                   + "Replace, remove, or retry any beat, then approve the stills to build the preview.")
        set_status("review_stills", message, file=str(output),
                   beats_total=len(beats), beats_filled=filled, eta_min_seconds=0, eta_max_seconds=0)
        with state_lock:
            job_running = False
        return
    except InterruptedError:
        set_status("cancelled", "Build cancelled before rendering. No upload was started.")
    except Exception as exc:
        set_status("failed", str(exc))
    finally:
        with state_lock:
            job_running = False
            active_process = None


def run_render() -> None:
    """Phase 2: render the preview from the approved beat plan. Stops at the
    'ready_to_upload' preview checkpoint — upload still needs explicit approval."""
    global job_running, active_process
    try:
        status = read_json(STATUS_PATH, {})
        output = Path(str(status.get("file", "")))
        assets_dir = output.parent / (output.stem + "-assets")
        beats_path = assets_dir / "beats.json"
        if not beats_path.is_file():
            raise ValueError("The approved beat plan is missing. Rebuild from the script.")
        draft = read_json(DRAFT_PATH, {})
        cancel_event.clear()

        set_status("rendering", "Encoding the preview from your approved stills…",
                   file=str(output), eta_min_seconds=35, eta_max_seconds=150)
        render_output = run_step(
            "rendering", "Building bold subtitles and encoding the video…",
            [sys.executable, "-u", str(ROOT / "render_short.py"), "--beats", str(beats_path), "--output", str(output)],
            900, 35, 150,
        )
        if cancel_event.is_set():
            raise InterruptedError("Cancellation requested.")

        # Append required credits (image + audio) to the YouTube description.
        attribution_path = assets_dir / "attribution.txt"
        if attribution_path.is_file() and isinstance(draft, dict):
            credits = attribution_path.read_text(encoding="utf-8").strip()
            if credits:
                draft["description"] = (str(draft.get("description", "")).rstrip() + "\n\n" + credits).strip()
                write_json(DRAFT_PATH, draft)

        set_status("ready_to_upload", "Build complete. Preview the video, then choose Upload when you are happy with it.",
                   eta_min_seconds=0, eta_max_seconds=0, file=str(output), render_details=render_output[-800:])
        with state_lock:
            job_running = False
        return
    except InterruptedError:
        set_status("cancelled", "Render cancelled. No upload was started.")
    except Exception as exc:
        set_status("failed", str(exc))
    finally:
        with state_lock:
            job_running = False
            active_process = None


def run_upload(privacy: str, made_for_kids: str) -> None:
    global job_running, active_process
    try:
        status = read_json(STATUS_PATH, {})
        if not isinstance(status, dict) or status.get("stage") != "ready_to_upload":
            raise ValueError("Build a preview first, then upload from the preview checkpoint.")
        output = Path(str(status.get("file", "")))
        draft = read_json(DRAFT_PATH, {})
        if not output.is_file() or not isinstance(draft, dict):
            raise ValueError("The rendered video or approved draft is missing. Build it again.")
        set_status("uploading", "Sending the rendered video to YouTube; upload time depends on your connection…",
                   eta_min_seconds=15, eta_max_seconds=max(60, int(output.stat().st_size / 125_000) + 90), file=str(output))
        command = [sys.executable, "-u", str(ROOT / "youtube_upload.py"), "--file", str(output),
                   "--title", str(draft["title"]), "--description", str(draft["description"]),
                   "--privacy", privacy, "--made-for-kids", made_for_kids,
                   "--tags", ",".join(draft.get("tags", []))]
        upload_output = run_step("uploading", "Uploading video and metadata to YouTube…", command, 1800, 15,
                                 max(60, int(output.stat().st_size / 125_000) + 90))
        match = re.search(r"https://www\.youtube\.com/watch\?v=[\w-]+", upload_output)
        set_status("uploaded", "Short uploaded successfully.", video_url=match.group(0) if match else "",
                   privacy=privacy, file=str(output), details=upload_output[-1200:])
    except InterruptedError:
        set_status("cancelled", "Upload stopped. Check YouTube Studio in case YouTube completed it just before cancellation.")
    except Exception as exc:
        set_status("failed", str(exc))
    finally:
        with state_lock:
            job_running = False
            active_process = None


def generate_draft(direction: str, automated: bool = False) -> None:
    global job_running
    try:
        draft = ask_claude(direction)
        write_json(DRAFT_PATH, draft)
        prefs = preferences()
        prefs["pending_review"] = True
        recent = prefs.get("recent_topics", [])
        if not isinstance(recent, list):
            recent = []
        topic = str(draft.get("topic", "")).strip()
        if topic:
            prefs["recent_topics"] = (recent + [topic])[-30:]
        if automated:
            prefs["last_auto_draft_date"] = datetime.now().astimezone().date().isoformat()
        write_json(PREFERENCES_PATH, prefs)
        set_status("draft", "Daily idea and script ready for your review." if automated else "Draft ready. Review and edit it before building.")
    except Exception as exc:
        set_status("failed", str(exc))
    finally:
        with state_lock:
            job_running = False


def start_generation(direction: str, automated: bool = False) -> None:
    global job_running
    with state_lock:
        if job_running:
            raise ValueError("Another draft, build, or upload is already running.")
        job_running = True
    if automated:
        prefs = preferences()
        prefs["last_auto_draft_date"] = datetime.now().astimezone().date().isoformat()
        write_json(PREFERENCES_PATH, prefs)
    label = "Generating today's idea from your show list…" if automated else "Claude is choosing a fresh topic and writing the script…"
    set_status("drafting", label, eta_min_seconds=20, eta_max_seconds=120)
    threading.Thread(target=generate_draft, args=(direction, automated), daemon=True).start()


def daily_scheduler() -> None:
    while True:
        try:
            prefs = preferences()
            now = datetime.now().astimezone()
            show_list = str(prefs.get("show_list", "")).strip()
            schedule = str(prefs.get("daily_time", "09:00"))
            try:
                hour, minute = map(int, schedule.split(":"))
            except (ValueError, TypeError):
                hour, minute = 9, 0
            date_key = now.date().isoformat()
            if (prefs.get("auto_daily", True) and show_list and not prefs.get("pending_review")
                    and prefs.get("last_auto_draft_date") != date_key
                    and (now.hour, now.minute) >= (hour, minute)):
                try:
                    recent = prefs.get("recent_topics", [])
                    recent_text = "\nRecent topics already used; choose a distinct angle: " + "; ".join(recent[-12:]) if isinstance(recent, list) and recent else ""
                    start_generation("Pick one fresh topic from this show list." + recent_text + "\n" + show_list, automated=True)
                except ValueError:
                    pass
        except Exception as exc:
            print(f"[scheduler] {exc}", flush=True)
        time.sleep(30)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ShortsDashboard/2.0"

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _body(self, max_size: int = 1_000_000) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > max_size:
            raise ValueError("Request body is empty or too large.")
        data = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _handle_beat_action(self, path: str, data: dict) -> None:
        """Replace, remove, or retry the search for a single beat at the review step."""
        global job_running
        status = read_json(STATUS_PATH, {})
        if not isinstance(status, dict) or status.get("stage") != "review_stills":
            raise ValueError("Per-beat edits are only available at the Review stills step.")
        assets = current_assets_dir()
        if not assets or not (assets / "beats.json").is_file():
            raise ValueError("No beat plan was found.")
        try:
            number = int(data.get("beat", 0))
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            raise ValueError("Choose a valid beat to edit.")
        with state_lock:
            if job_running:
                raise ValueError("A build is already running; try again in a moment.")
            job_running = True
        try:
            if path == "/api/beat/retry":
                query = str(data.get("query", "")).strip() or None
                image_finder.retry_beat(assets, number, query)
            elif path == "/api/beat/replace":
                raw = str(data.get("data", ""))
                if raw.strip().startswith("data:") and "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    image_bytes = base64.b64decode(raw, validate=False)
                except Exception:
                    raise ValueError("The uploaded image could not be decoded.")
                if not image_bytes:
                    raise ValueError("No image data was received.")
                image_finder.replace_beat_image(assets, number, image_bytes, str(data.get("filename", "upload")))
            elif path == "/api/beat/remove":
                image_finder.remove_beat(assets, number)
        finally:
            with state_lock:
                job_running = False
        plan = read_json(assets / "beats.json", {})
        beats = plan.get("beats", []) if isinstance(plan, dict) else []
        filled = sum(1 for b in beats if b.get("image_path"))
        flagged = len(beats) - filled
        message = (f"Review the {len(beats)} stills. {filled} ready"
                   + (f", {flagged} need attention." if flagged else "; all matched.")
                   + " Approve the stills to build the preview.")
        set_status("review_stills", message, file=str(status.get("file", "")),
                   beats_total=len(beats), beats_filled=filled, eta_min_seconds=0, eta_max_seconds=0)
        self._json(200, {"plan": plan})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/status":
            status = read_json(STATUS_PATH, {"stage": "idle", "message": "Ready for your next short."})
            if isinstance(status, dict) and status.get("phase_started_at"):
                status["elapsed_seconds"] = max(0, int(time.time() - float(status["phase_started_at"])))
            self._json(200, {"status": status, "claude_found": bool(find_claude()),
                             "authorized": (WORK / "token.json").exists(),
                             "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
                             "preferences": preferences()})
        elif path == "/api/draft":
            draft = read_json(DRAFT_PATH, None)
            self._json(200, {"draft": normalize_draft(draft) if isinstance(draft, dict) else None})
        elif path == "/api/preview":
            status = read_json(STATUS_PATH, {})
            if not isinstance(status, dict) or status.get("stage") != "ready_to_upload":
                self._json(404, {"error": "No completed preview is ready."})
                return
            file = Path(str(status.get("file", "")))
            if not file.is_file():
                self._json(404, {"error": "The rendered preview file was not found."})
                return
            self._send(200, file.read_bytes(), "video/mp4")
        elif path == "/api/manifest":
            assets = current_assets_dir()
            manifest = read_json(assets / "media_manifest.json", None) if assets else None
            if not isinstance(manifest, dict):
                self._json(200, {"manifest": None})
                return
            self._json(200, {"manifest": manifest})
        elif path == "/api/beats":
            assets = current_assets_dir()
            plan = read_json(assets / "beats.json", None) if assets else None
            manifest = read_json(assets / "media_manifest.json", None) if assets else None
            self._json(200, {"plan": plan if isinstance(plan, dict) else None,
                             "disclaimer": manifest.get("disclaimer") if isinstance(manifest, dict) else ""})
        elif path == "/api/beat-image":
            assets = current_assets_dir()
            try:
                number = int((parse_qs(urlparse(self.path).query).get("beat", ["0"])[0] or "0"))
            except ValueError:
                number = 0
            plan = read_json(assets / "beats.json", {}) if assets else {}
            beats = plan.get("beats", []) if isinstance(plan, dict) else []
            beat = next((b for b in beats if b.get("beat") == number), None)
            rel = str(beat.get("image_path", "")).strip() if beat else ""
            if not assets or not rel:
                self._json(404, {"error": "No such image."})
                return
            image = (ROOT / rel).resolve()
            images_root = (assets / "images").resolve()
            if not str(image).startswith(str(images_root)) or not image.is_file():
                self._json(404, {"error": "No such image."})
                return
            content_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                             ".png": "image/png", ".webp": "image/webp"}
            self._send(200, image.read_bytes(), content_types.get(image.suffix.lower(), "image/jpeg"))
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        global job_running
        path = urlparse(self.path).path
        approval_claimed = False
        try:
            data = self._body(40_000_000 if path == "/api/beat/replace" else 1_000_000)
            if path == "/api/preferences":
                prefs = preferences()
                show_list = str(data.get("show_list", "")).strip()[:6000]
                daily_time = str(data.get("daily_time", "09:00"))
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
                    raise ValueError("Choose a valid local time in 24-hour format.")
                prefs.update({"show_list": show_list, "daily_time": daily_time,
                              "auto_daily": bool(data.get("auto_daily", True))})
                write_json(PREFERENCES_PATH, prefs)
                self._json(200, {"ok": True, "preferences": prefs})
            elif path in ("/api/generate", "/api/daily"):
                prefs = preferences()
                direction = str(data.get("direction", "")).strip()
                if path == "/api/daily" or not direction:
                    show_list = str(prefs.get("show_list", "")).strip()
                    if not show_list:
                        raise ValueError("Add your anime and show list in Weekly preferences first.")
                    recent = prefs.get("recent_topics", [])
                    recent_text = "\nDo not repeat these recent topics; choose a different specific angle: " + "; ".join(recent[-12:]) if isinstance(recent, list) and recent else ""
                    direction = "Choose a fresh, specific topic from this list." + recent_text + "\n" + show_list
                start_generation(direction, automated=(path == "/api/daily"))
                self._json(202, {"ok": True})
            elif path == "/api/revise":
                notes, current = str(data.get("notes", "")).strip(), data.get("draft")
                if not notes or not isinstance(current, dict):
                    raise ValueError("Enter revision notes and keep the current draft loaded.")
                current = normalize_draft(current)
                with state_lock:
                    if job_running:
                        raise ValueError("Another draft, build, or upload is already running.")
                    job_running = True
                try:
                    revised = ask_claude(notes, current)
                    write_json(DRAFT_PATH, revised)
                    set_status("draft", "Revised draft ready. Review it before building.")
                    self._json(200, {"draft": revised})
                except Exception as exc:
                    set_status("failed", str(exc))
                    raise
                finally:
                    with state_lock:
                        job_running = False
            elif path == "/api/save":
                draft = data.get("draft")
                if not isinstance(draft, dict):
                    raise ValueError("No draft was supplied.")
                draft = normalize_draft(draft)
                validate_draft(draft)
                write_json(DRAFT_PATH, draft)
                set_status("draft", "Your edits are saved.")
                self._json(200, {"ok": True})
            elif path == "/api/reject":
                prefs = preferences()
                prefs["pending_review"] = False
                write_json(PREFERENCES_PATH, prefs)
                set_status("rejected", "Draft rejected. The next daily idea will arrive on schedule.")
                self._json(200, {"ok": True})
            elif path == "/api/approve":
                draft = data.get("draft")
                if not isinstance(draft, dict):
                    raise ValueError("No draft was supplied.")
                draft = normalize_draft(draft)
                validate_draft(draft)
                with state_lock:
                    if job_running:
                        raise ValueError("A build or upload is already running.")
                    prior = read_json(STATUS_PATH, {})
                    if isinstance(prior, dict) and prior.get("stage") in ("review_stills", "ready_to_upload", "uploading", "uploaded"):
                        raise ValueError("This draft already has a build in progress. Cancel or reject it before starting another.")
                    job_running = True
                    approval_claimed = True
                cancel_event.clear()
                write_json(DRAFT_PATH, draft)
                prefs = preferences()
                prefs["pending_review"] = False
                write_json(PREFERENCES_PATH, prefs)
                set_status("queued", "Script approved. Generating narration, then matching licensed stills to the speech…",
                           eta_min_seconds=45, eta_max_seconds=240)
                threading.Thread(target=run_planning, args=(draft,), daemon=True).start()
                approval_claimed = False
                self._json(202, {"ok": True})
            elif path == "/api/approve-stills":
                with state_lock:
                    status = read_json(STATUS_PATH, {})
                    if not isinstance(status, dict) or status.get("stage") != "review_stills":
                        raise ValueError("Review and approve the stills from the Review stills step first.")
                    if job_running:
                        raise ValueError("A build is already running.")
                    assets = current_assets_dir()
                    plan = read_json(assets / "beats.json", {}) if assets else {}
                    beats = plan.get("beats", []) if isinstance(plan, dict) else []
                    if not beats:
                        raise ValueError("No beat plan was found. Rebuild from the script.")
                    missing = [str(b["beat"]) for b in beats if not str(b.get("image_path", "")).strip()]
                    if missing:
                        raise ValueError("These beats still need a licensed image (replace, retry, or remove them): "
                                         + ", ".join(missing))
                    job_running = True
                plan["approved"] = True
                write_json(assets / "beats.json", plan)
                cancel_event.clear()
                set_status("rendering", "Stills approved. Encoding the preview…",
                           file=str(status.get("file", "")), eta_min_seconds=35, eta_max_seconds=150)
                threading.Thread(target=run_render, daemon=True).start()
                self._json(202, {"ok": True})
            elif path in ("/api/beat/retry", "/api/beat/replace", "/api/beat/remove"):
                self._handle_beat_action(path, data)
            elif path == "/api/upload":
                privacy = str(data.get("privacy", "private"))
                made_for_kids = str(data.get("made_for_kids", "no"))
                if privacy not in ("private", "unlisted", "public"):
                    raise ValueError("Choose Private, Unlisted or Public.")
                if made_for_kids not in ("yes", "no"):
                    raise ValueError("Choose the made-for-kids setting.")
                with state_lock:
                    if job_running:
                        raise ValueError("A build or upload is already running.")
                    job_running = True
                cancel_event.clear()
                threading.Thread(target=run_upload, args=(privacy, made_for_kids), daemon=True).start()
                self._json(202, {"ok": True})
            elif path == "/api/reset":
                # Hard reset: wipe draft + status so the UI starts fresh on page reload.
                # Refused if a job is actively running to avoid orphaning a render/upload.
                with state_lock:
                    if job_running:
                        raise ValueError("A build or upload is running; cancel it before resetting.")
                    cancel_event.clear()
                DRAFT_PATH.unlink(missing_ok=True)
                write_json(STATUS_PATH, {"stage": "idle", "message": "Ready for your next short."})
                self._json(200, {"ok": True})
            elif path == "/api/cancel":
                with state_lock:
                    status = read_json(STATUS_PATH, {})
                    stage = status.get("stage") if isinstance(status, dict) else None
                    if stage == "review_stills" and not job_running:
                        # Discard the unapproved beat plan and return to the draft.
                        set_status("draft", "Build discarded before rendering. Edit the script or approve again to rebuild.")
                        self._json(202, {"ok": True})
                        return
                    if not job_running or stage not in ("queued", "aligning", "assets", "rendering", "uploading"):
                        raise ValueError("There is no active build or upload to cancel.")
                    cancel_event.set()
                    proc = active_process
                set_status("cancelling", "Stopping the active step; cleaning up…")
                if proc is not None:
                    _kill_process_tree(proc)
                self._json(202, {"ok": True})
            else:
                self._json(404, {"error": "Not found"})
        except Exception as exc:
            if approval_claimed:
                with state_lock:
                    job_running = False
            print(f"[dashboard] {path} failed: {exc}", flush=True)
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        print("[dashboard] " + format % args)


def main() -> None:
    load_optional_secrets()
    WORK.mkdir(parents=True, exist_ok=True)
    for folder in ("images", "music", "sfx"):
        (WORK / "media" / folder).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=daily_scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Shorts Studio dashboard: {url}")
    print("Daily drafts are generated only while this app is running. Press Ctrl+C to stop it.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
