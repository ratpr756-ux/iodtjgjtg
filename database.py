"""SQLite state store for idempotent video and clip processing."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    channel_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    source_path TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status, published_at);
                CREATE TABLE IF NOT EXISTS clips (
                    clip_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
                    clip_index INTEGER NOT NULL,
                    clip_path TEXT NOT NULL,
                    start_seconds REAL NOT NULL,
                    end_seconds REAL NOT NULL,
                    transcript TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    published_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(video_id, clip_index)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
            if "attempts" not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    def add_video(self, video: dict[str, Any]) -> bool:
        now = utc_now()
        with self._connection() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO videos
                (video_id, channel_name, channel_url, title, video_url, published_at,
                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'discovered', ?, ?)""",
                (video["id"], video["channel_name"], video["channel_url"],
                 video["title"], video["url"], video["published_at"], now, now),
            )
            return cur.rowcount == 1

    def get_videos(self, status: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE status = ? ORDER BY published_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_videos(self, limit: int | None = 100, max_attempts: int = 5) -> list[dict[str, Any]]:
        with self._connection() as conn:
            query = """SELECT * FROM videos WHERE status IN ('discovered','downloaded','failed')
                       AND attempts < ? ORDER BY published_at DESC"""
            params: tuple[Any, ...] = (max_attempts,)
            if limit is not None:
                query += " LIMIT ?"
                params += (max(1, int(limit)),)
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_seen_video_ids(self) -> set[str]:
        """IDs already queued or completed; failed rows remain retryable."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT video_id FROM videos WHERE status != 'failed' OR attempts >= 5"
            ).fetchall()
        return {str(row["video_id"]) for row in rows}

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        return dict(row) if row else None

    def get_recent_videos(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM videos ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def increment_attempt(self, video_id: str) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE videos SET attempts=attempts+1, updated_at=? WHERE video_id=?",
                         (utc_now(), video_id))

    def update_video(self, video_id: str, status: str, *, source_path: str | None = None,
                     error: str | None = None) -> None:
        with self._connection() as conn:
            conn.execute(
                """UPDATE videos SET status = ?, source_path = COALESCE(?, source_path),
                   error = ?, updated_at = ? WHERE video_id = ?""",
                (status, source_path, error, utc_now(), video_id),
            )

    def clear_source_path(self, video_id: str) -> None:
        """Forget a temporary source file after it has been processed or removed."""
        with self._connection() as conn:
            conn.execute("UPDATE videos SET source_path=NULL, updated_at=? WHERE video_id=?",
                         (utc_now(), video_id))

    def add_clip(self, video_id: str, clip_index: int, clip_path: str,
                 start_seconds: float, end_seconds: float, transcript: str,
                 metadata: dict[str, Any]) -> int:
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO clips
                (video_id, clip_index, clip_path, start_seconds, end_seconds,
                 transcript, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, clip_index) DO UPDATE SET
                  clip_path=excluded.clip_path, start_seconds=excluded.start_seconds,
                  end_seconds=excluded.end_seconds, transcript=excluded.transcript,
                  metadata_json=excluded.metadata_json""",
                (video_id, clip_index, clip_path, start_seconds, end_seconds,
                 transcript, json.dumps(metadata, ensure_ascii=False), utc_now()),
            )
            row = conn.execute(
                "SELECT clip_id FROM clips WHERE video_id=? AND clip_index=?",
                (video_id, clip_index),
            ).fetchone()
        return int(row["clip_id"])

    def get_clips(self, video_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM clips WHERE video_id=? ORDER BY clip_index", (video_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["published"] = json.loads(item.pop("published_json"))
            result.append(item)
        return result

    def get_all_clips(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM clips ORDER BY created_at, clip_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["published"] = json.loads(item.pop("published_json"))
            result.append(item)
        return result

    def mark_published(self, clip_id: int, platform: str, post_id: str) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT published_json FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown clip_id: {clip_id}")
            published = json.loads(row["published_json"])
            published[platform] = {"id": post_id, "at": utc_now()}
            conn.execute("UPDATE clips SET published_json=? WHERE clip_id=?",
                         (json.dumps(published), clip_id))

