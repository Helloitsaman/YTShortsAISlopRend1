"""Authorize this local workflow and show the YouTube channel it can access."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_PATH = WORK / "token.json"


def find_client_secrets() -> Path:
    candidates = sorted(WORK.glob("client_secret*.json"))
    if len(candidates) != 1:
        raise SystemExit(
            "Expected exactly one OAuth client JSON file matching "
            f"{WORK / 'client_secret*.json'}; found {len(candidates)}."
        )
    return candidates[0]


def get_credentials() -> Credentials:
    WORK.mkdir(exist_ok=True)
    credentials = None
    if TOKEN_PATH.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(find_client_secrets()), SCOPES
        )
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
        )

    TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def main() -> None:
    get_credentials()
    print("YouTube upload authorization succeeded.")
    print("Authorization is saved locally in work/token.json.")


if __name__ == "__main__":
    main()
