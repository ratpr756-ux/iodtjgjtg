import sys
sys.path.insert(0, r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
from database import Database
from pathlib import Path
root = Path(r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
db = Database(root / "data" / "pipeline.sqlite3")
for clip in db.get_all_clips():
    if clip["video_id"] == "1PKa4MZrHfU":
        published = clip["published"].get("youtube", {}).get("id")
        print(f"clip={clip['clip_index']} published={published or 'queued'} file_exists={Path(clip['clip_path']).is_file()}")
