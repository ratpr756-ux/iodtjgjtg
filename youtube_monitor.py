"""Discover and download recent, explicitly authorized YouTube sources."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import yt_dlp

log = logging.getLogger(__name__)


class YouTubeMonitor:
    def __init__(self, channels: list[dict[str, Any]], download_dir: str | Path,
                 lookback_hours: int = 24, max_videos_per_channel: int = 3,
                 include_older_unseen: bool = False):
        self.channels = channels
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.lookback = timedelta(hours=lookback_hours)
        self.max_videos_per_channel = max(1, int(max_videos_per_channel))
        self.include_older_unseen = bool(include_older_unseen)

    @staticmethod
    def _js_runtime_options() -> dict[str, Any]:
        configured = os.getenv("YTDLP_NODE_PATH", "").strip()
        node = configured if configured and Path(configured).is_file() else shutil.which("node")
        if not node and os.name == "nt":
            candidates = [
                Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" /
                "dependencies" / "node" / "bin" / "node.exe",
                Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
            ]
            node = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
        return {"js_runtimes": {"node": {"path": str(Path(node).resolve())}}} if node else {}

    @staticmethod
    def _published_timestamp(info: dict[str, Any]) -> int | None:
        value = info.get("release_timestamp") or info.get("timestamp")
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        # Do not treat upload_date (date only) as a precise 24-hour timestamp.
        return None

    @staticmethod
    def _videos_tab_url(channel_url: str) -> str:
        parsed = urlparse(channel_url)
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/videos"):
            return urlunparse(parsed._replace(path=path))
        return urlunparse(parsed._replace(path=path + "/videos"))

    def clear_source_cache(self) -> int:
        """Remove leftover original media and partial downloads from prior runs."""
        removed = 0
        for path in self.download_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.exception("Could not remove temporary source file %s", path)
        if removed:
            log.info("Cleared %s leftover temporary source file(s)", removed)
        return removed

    def discover_recent(self, stop_event: threading.Event | None = None,
                        known_video_ids: set[str] | None = None) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - self.lookback
        known_video_ids = known_video_ids or set()
        discovered: list[dict[str, Any]] = []
        for channel in self.channels:
            if stop_event is not None and stop_event.is_set():
                break
            if not channel.get("authorized_for_reuse", False):
                log.warning("Skipping %s: authorized_for_reuse is not enabled", channel.get("name", channel.get("url")))
                continue
            channel_url = str(channel["url"]).rstrip("/")
            listing_url = self._videos_tab_url(channel_url)
            options = {"quiet": True, "no_warnings": True, "skip_download": True,
                       "extract_flat": "in_playlist", "ignoreerrors": True,
                       "playlistend": 1000, "socket_timeout": 15,
                       "retries": 1, "extractor_retries": 1,
                       **self._js_runtime_options()}
            try:
                log.info("Scanning channel videos %s", channel.get("name", listing_url))
                with yt_dlp.YoutubeDL(options) as ydl:
                    listing = ydl.extract_info(listing_url, download=False)
                entries = (listing or {}).get("entries") or []
                log.info("Channel scan returned %s entries", len(entries))
                metadata_lookups = 0
                recent_from_channel = 0
                for entry in entries:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if not entry or not entry.get("id"):
                        continue
                    if str(entry["id"]) in known_video_ids:
                        continue
                    video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
                    if not video_url.startswith("http"):
                        video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                    stamp = self._published_timestamp(entry)
                    # The channel's video list is newest first. Stop at the 24-hour
                    # boundary instead of resolving metadata for older videos.
                    upload_date = str(entry.get("upload_date") or "")
                    if (not self.include_older_unseen and not stamp and upload_date
                            and upload_date < cutoff.strftime("%Y%m%d")):
                        break
                    if not stamp:
                        if metadata_lookups >= 30:
                            log.info("Reached metadata lookup limit for %s; moving to the next channel", channel_url)
                            break
                        metadata_lookups += 1
                        try:
                            with yt_dlp.YoutubeDL({**options, "extract_flat": False}) as ydl:
                                full = ydl.extract_info(video_url, download=False)
                        except Exception:
                            log.exception("Could not read video metadata for %s from %s; continuing this channel",
                                          entry["id"], channel_url)
                            continue
                        if not full:
                            continue
                        stamp = self._published_timestamp(full)
                        entry = full
                        if not stamp and self.include_older_unseen:
                            upload_date = str(entry.get("upload_date") or "")
                            if re.fullmatch(r"\d{8}", upload_date):
                                try:
                                    stamp = int(datetime.strptime(upload_date, "%Y%m%d")
                                                .replace(tzinfo=timezone.utc).timestamp())
                                except ValueError:
                                    pass
                    if not stamp:
                        continue
                    if not self.include_older_unseen and datetime.fromtimestamp(stamp, timezone.utc) < cutoff:
                        break
                    discovered.append({
                        "id": str(entry.get("id")),
                        "channel_name": str(channel.get("name") or listing.get("title") or "YouTube"),
                        "channel_url": channel_url,
                        "title": str(entry.get("title") or "Untitled video"),
                        "url": video_url,
                        "published_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"),
                    })
                    recent_from_channel += 1
                    if recent_from_channel >= self.max_videos_per_channel:
                        log.info("Selected the latest %s recent videos from %s",
                                 recent_from_channel, channel.get("name", channel_url))
                        break
            except Exception:
                log.exception("Could not scan channel %s", channel_url)
        # Stable de-duplication in case multiple monitored URLs overlap.
        unique: dict[str, dict[str, Any]] = {item["id"]: item for item in discovered}
        return sorted(unique.values(), key=lambda item: item["published_at"], reverse=True)

    def download(self, video: dict[str, Any]) -> Path:
        # Discovery records use id/url; queued SQLite rows use video_id/video_url.
        # Accept both shapes so a discovered video can be downloaded after it is
        # persisted and read back from the database.
        video_id = str(video.get("id") or video.get("video_id") or "").strip()
        video_url = str(video.get("url") or video.get("video_url") or "").strip()
        if not video_id:
            raise ValueError("Video record is missing its YouTube video ID")
        if not video_url:
            raise ValueError(f"Video {video_id} is missing its YouTube URL")

        video_dir = self.download_dir / video_id
        video_dir.mkdir(parents=True, exist_ok=True)
        # Never resume a possibly huge partial from a prior attempt.
        for old_file in video_dir.iterdir():
            if old_file.is_file():
                try:
                    old_file.unlink()
                except OSError:
                    log.exception("Could not clear stale source file %s", old_file)
        template = str(video_dir / "source.%(ext)s")
        options = {
            # A single <=720p MP4 is sufficient for vertical reframing and avoids
            # very large 4K video-only streams that can stall on slow connections.
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "overwrites": False,
            "socket_timeout": 15,
            "retries": 1,
            "fragment_retries": 1,
            "file_access_retries": 1,
            **self._js_runtime_options(),
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([video_url])
        except Exception:
            for partial in video_dir.iterdir():
                if partial.is_file():
                    try:
                        partial.unlink()
                    except OSError:
                        log.exception("Could not remove failed download file %s", partial)
            raise
        files = sorted(video_dir.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        files = [p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
        if not files:
            raise FileNotFoundError(f"yt-dlp did not produce a video for {video_id}")
        return files[0]

