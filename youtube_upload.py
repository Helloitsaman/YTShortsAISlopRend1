"""Upload a rendered video to YouTube with explicit metadata and visibility."""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
TOKEN_PATH = WORK / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        raise SystemExit(
            "YouTube authorization is missing. Run youtube_auth.py first."
        )

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise SystemExit(
                "YouTube authorization expired. Run youtube_auth.py again."
            ) from exc
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise SystemExit(
            "YouTube authorization is invalid. Run youtube_auth.py again."
        )
    return creds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload a rendered short to your authenticated YouTube channel."
    )
    parser.add_argument("--file", required=True, type=Path, help="Rendered video file")
    parser.add_argument("--title", required=True, help="YouTube title (max 100 characters)")
    parser.add_argument("--description", required=True, help="YouTube description")
    parser.add_argument(
        "--privacy",
        required=True,
        choices=("private", "unlisted", "public"),
        help="Video visibility; required to avoid accidental public uploads",
    )
    parser.add_argument(
        "--made-for-kids",
        required=True,
        choices=("yes", "no"),
        help="Declare whether this video is made for kids",
    )
    parser.add_argument("--tags", default="", help="Comma-separated video tags")
    parser.add_argument("--category-id", default="24", help="YouTube category ID")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    video_path = args.file.expanduser().resolve()
    if not video_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")
    if len(args.title) > 100:
        raise SystemExit("YouTube titles must be 100 characters or fewer.")
    if not args.description.strip():
        raise SystemExit("Description cannot be empty.")

    mime_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"
    body = {
        "snippet": {
            "title": args.title,
            "description": args.description,
            "categoryId": args.category_id,
        },
        "status": {
            "privacyStatus": args.privacy,
            "selfDeclaredMadeForKids": args.made_for_kids == "yes",
        },
    }
    if args.tags.strip():
        body["snippet"]["tags"] = [tag.strip() for tag in args.tags.split(",") if tag.strip()]

    youtube = build("youtube", "v3", credentials=credentials())
    media = MediaFileUpload(
        str(video_path), mimetype=mime_type, chunksize=8 * 1024 * 1024, resumable=True
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        progress, response = request.next_chunk(num_retries=5)
        if progress is not None:
            print(f"Upload progress: {progress.progress() * 100:.0f}%")

    video_id = response["id"]
    print(f"Upload complete: https://www.youtube.com/watch?v={video_id}")
    print(f"Visibility: {args.privacy}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Upload cancelled.", file=sys.stderr)
        raise SystemExit(130)
