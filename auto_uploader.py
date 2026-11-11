"""YouTube Data API OAuth and resumable video uploads."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
YOUTUBE_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploader:
    def __init__(self, client_secrets: str | Path, token_file: str | Path):
        self.client_secrets = Path(client_secrets)
        self.token_file = Path(token_file)

    def _service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), YOUTUBE_SCOPE)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.client_secrets.exists():
                    raise FileNotFoundError(f"Upload a Google OAuth Desktop client JSON first: {self.client_secrets}")
                flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), YOUTUBE_SCOPE)
                creds = flow.run_local_server(port=0)
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(creds.to_json(), encoding="utf-8")
        return build("youtube", "v3", credentials=creds)

    def connect(self) -> None:
        """Complete first-time OAuth authorization or refresh an existing token."""
        self._service()

    def upload(self, video_path: str | Path, metadata: dict[str, Any],
               privacy: str = "public", category_id: str = "22") -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        youtube = self._service()
        body = {
            "snippet": {
                "title": str(metadata["title"])[:100],
                "description": str(metadata["description"])[:5000],
                "tags": [str(tag).lstrip("#") for tag in metadata.get("hashtags", [])],
                "categoryId": str(category_id),
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        request = youtube.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True),
        )
        response = None
        attempts = 0
        while response is None:
            try:
                _, response = request.next_chunk()
            except HttpError as exc:
                if getattr(exc.resp, "status", 0) not in {500, 502, 503, 504}:
                    raise
                attempts += 1
                if attempts > 5:
                    raise
                wait = min(60, 2 ** attempts)
                log.warning("Temporary YouTube upload error; retrying in %ss", wait)
                time.sleep(wait)
        return str(response["id"])
