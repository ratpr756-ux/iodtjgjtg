import sys
from pathlib import Path
sys.path.insert(0, r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
from database import Database
from auto_uploader import YouTubeUploader
root = Path(r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
db = Database(root / "data" / "pipeline.sqlite3")
clips = db.get_all_clips()
published = [(clip, clip["published"].get("youtube", {}).get("id")) for clip in clips]
published = [(clip, video_id) for clip, video_id in published if video_id]
assert published, "No published clips found in the local database"
service = YouTubeUploader(root / "data" / "youtube_client_secrets.json", root / "data" / "youtube_token.json")._service()
ids = ",".join(video_id for _, video_id in published)
result = service.videos().list(part="snippet,status,processingDetails", id=ids, maxResults=50).execute()
returned = {item["id"]: item for item in result.get("items", [])}
for clip, video_id in published:
    item = returned.get(video_id)
    if not item:
        print("NOT_VISIBLE_YET", video_id)
        continue
    status = item.get("status", {})
    processing = item.get("processingDetails", {})
    print("VERIFIED", video_id, status.get("privacyStatus"), processing.get("processingStatus"), item.get("snippet", {}).get("title", ""))
