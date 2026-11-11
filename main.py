"""Daily YouTube-to-Shorts automation pipeline."""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import schedule
import yaml
from dotenv import load_dotenv

from auto_uploader import YouTubeUploader
from caption_generator import generate_metadata
from database import Database
from opus_automation import create_clips, ensure_ffmpeg_path
from youtube_monitor import YouTubeMonitor

PUBLISH_LOCK = threading.Lock()


def project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


ROOT = Path(__file__).resolve().parent


def load_config(path: str | Path = ROOT / "config.yaml") -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    clips = config.setdefault("clips", {})
    clips["whisper_model"] = os.getenv("WHISPER_MODEL", clips.get("whisper_model", "small"))
    clips["whisper_device"] = os.getenv("WHISPER_DEVICE", clips.get("whisper_device", "auto"))
    clips["whisper_compute_type"] = os.getenv("WHISPER_COMPUTE_TYPE", clips.get("whisper_compute_type", "auto"))
    return config


def configure_logging(config: dict[str, Any]) -> None:
    log_dir = project_path(config.get("paths", {}).get("logs", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, str(config.get("logging", {}).get("level", "INFO")).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8"), logging.StreamHandler()],
    )
    # Keep one-time OAuth authorization codes and callback query strings out of logs.
    logging.getLogger("waitress").setLevel(logging.WARNING)
    logging.getLogger("google_auth_oauthlib.flow").setLevel(logging.WARNING)


def _publish_clip(clip_id: int, clip_path: Path, metadata: dict[str, Any], config: dict[str, Any],
                  db: Database, already_published: dict[str, Any]) -> bool:
    settings = config.get("publishing", {})
    if not settings.get("enabled", False):
        return False
    youtube_cfg = settings.get("youtube", {})
    if (youtube_cfg.get("enabled", False) and "youtube" not in already_published
            and os.environ.get("YOUTUBE_PUBLISH", "1") == "1"):
        token_file = youtube_cfg.get("token_file", "data/youtube_token.json")
        client_file = youtube_cfg.get("client_secrets", "data/youtube_client_secrets.json")
        uploader = YouTubeUploader(str(project_path(client_file)), str(project_path(token_file)))
        try:
            video_id = uploader.upload(clip_path, metadata,
                                       privacy=str(youtube_cfg.get("privacy", "public")),
                                       category_id=str(youtube_cfg.get("category_id", "22")))
            db.mark_published(clip_id, "youtube", video_id)
            logging.getLogger(__name__).info("Uploaded YouTube video %s", video_id)
            return True
        except Exception:
            logging.getLogger(__name__).exception("YouTube upload failed for clip %s", clip_id)
    return False


def _channel_key(url: str) -> str:
    value = str(url or "").strip().rstrip("/").lower()
    return value[:-7] if value.endswith("/videos") else value


def _rotation_state_file(config: dict[str, Any]) -> Path:
    return project_path(config.get("publishing", {}).get(
        "rotation_state_file", "data/publishing_rotation.json"))


def _save_rotation_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _active_publish_channel(config: dict[str, Any], now=None) -> tuple[dict[str, Any] | None, dict[str, Any], Path]:
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    publication = config.get("publishing", {})
    channels = [channel for channel in config.get("channels", [])
                if channel.get("authorized_for_reuse") and channel.get("url")]
    path = _rotation_state_file(config)
    if not channels:
        return None, {}, path
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    active_key = _channel_key(state.get("active_channel_url", ""))
    active_index = next((i for i, channel in enumerate(channels)
                         if _channel_key(channel["url"]) == active_key), None)
    if active_index is not None:
        def parse_time(value):
            if not value:
                return None
            try:
                result = datetime.fromisoformat(value)
                return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
            except (TypeError, ValueError):
                return None

        started_at = parse_time(state.get("started_at"))
        deferred_until = parse_time(state.get("defer_rotation_until"))
        # If a prior day's global YouTube cap delayed this channel's first post,
        # keep its turn until it can publish instead of skipping its day.
        if deferred_until and (not started_at or now < deferred_until):
            return channels[active_index], state, path
        base_time = started_at or parse_time(state.get("selected_at"))
        if base_time:
            expires_at = base_time + timedelta(
                hours=max(1, int(publication.get("channel_rotation_hours", 24))), minutes=1)
            if deferred_until:
                expires_at = max(expires_at, deferred_until)
            if now < expires_at:
                return channels[active_index], state, path

    if active_index is None:
        initial_url = _channel_key(publication.get("rotation_start_channel_url", ""))
        next_index = next((i for i, channel in enumerate(channels)
                           if _channel_key(channel["url"]) == initial_url), 0)
    else:
        next_index = (active_index + 1) % len(channels)
    channel = channels[next_index]
    state = {"active_channel_url": channel["url"], "active_channel_name": channel.get("name", "YouTube"),
             "selected_at": now.isoformat(timespec="seconds"), "started_at": None,
             "defer_rotation_until": None}
    _save_rotation_state(path, state)
    return channel, state, path


def _publish_pending_clips(config: dict[str, Any], db: Database,
                           stop_event: threading.Event | None = None,
                           on_phase: Callable[[str], None] | None = None) -> None:
    if not PUBLISH_LOCK.acquire(blocking=False):
        return
    try:
        from datetime import datetime, timedelta, timezone
        log = logging.getLogger(__name__)
        settings = config.get("publishing", {})
        youtube_cfg = settings.get("youtube", {})
        if (not settings.get("enabled", False) or not youtube_cfg.get("enabled", False)
                or os.environ.get("YOUTUBE_PUBLISH", "1") != "1"):
            return
        if stop_event is not None and stop_event.is_set():
            return

        now = datetime.now(timezone.utc)
        channel, rotation, state_path = _active_publish_channel(config, now)
        if channel is None:
            log.info("Publishing is waiting for a monitored channel")
            return
        clips = db.get_all_clips()
        cutoff = now - timedelta(hours=24)
        daily_count = 0
        posted_times = []
        for clip in clips:
            details = clip["published"].get("youtube")
            if not details:
                continue
            try:
                timestamp = datetime.fromisoformat(details["at"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                posted_times.append(timestamp)
                if timestamp >= cutoff:
                    daily_count += 1
            except (KeyError, TypeError, ValueError):
                continue

        daily_cap = int(settings.get("max_posts_per_platform_per_day", 3))
        if daily_count >= daily_cap:
            next_slot = min(timestamp for timestamp in posted_times if timestamp >= cutoff)
            rotation["defer_rotation_until"] = (next_slot + timedelta(hours=24, minutes=6)).isoformat(timespec="seconds")
            _save_rotation_state(state_path, rotation)
            log.info("YouTube limit reached; %s keeps today's source-channel slot until it can publish",
                     channel.get("name", channel["url"]))
            return

        minimum_gap = timedelta(hours=max(0.0, float(settings.get("minimum_hours_between_posts", 2))))
        if posted_times and now - max(posted_times) < minimum_gap:
            log.info("Waiting for the %s-hour YouTube post interval", minimum_gap.total_seconds() / 3600)
            return

        for clip in clips:
            if stop_event is not None and stop_event.is_set():
                log.info("Publishing paused by user")
                return
            if "youtube" in clip["published"]:
                continue
            source_video = db.get_video(str(clip["video_id"]))
            if not source_video or _channel_key(source_video.get("channel_url", "")) != _channel_key(channel["url"]):
                continue
            clip_path = Path(clip["clip_path"])
            if not clip_path.is_file():
                log.warning("Clip file is missing: %s", clip_path)
                continue
            if on_phase:
                on_phase(f"جاري نشر مقطع {clip['clip_id']} من قناة {channel.get('name', '')}")
            # One upload per scheduler pass enforces the two-hour spacing.
            if _publish_clip(int(clip["clip_id"]), clip_path, clip["metadata"], config,
                             db, clip["published"]):
                started = rotation.get("started_at")
                if not started or now >= datetime.fromisoformat(started) + timedelta(hours=24):
                    rotation["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                rotation["defer_rotation_until"] = None
                _save_rotation_state(state_path, rotation)
            return
        if rotation.get("defer_rotation_until") and not any(
                "youtube" not in clip["published"]
                and (source_video := db.get_video(str(clip["video_id"])))
                and _channel_key(source_video.get("channel_url", "")) == _channel_key(channel["url"])
                for clip in clips):
            rotation["defer_rotation_until"] = None
            _save_rotation_state(state_path, rotation)
        log.info("No queued clips are available for today's source channel %s",
                 channel.get("name", channel["url"]))
    finally:
        PUBLISH_LOCK.release()


def run_pipeline(config: dict[str, Any], db: Database,
                 stop_event: threading.Event | None = None,
                 on_phase: Callable[[str], None] | None = None) -> None:
    stop_event = stop_event or threading.Event()
    log = logging.getLogger(__name__)
    app = config.get("app", {})
    paths = config.get("paths", {})
    source_dir = project_path(paths.get("source_downloads", "downloads/sources"))
    clip_dir = project_path(paths.get("clips", "downloads/clips"))
    ensure_ffmpeg_path()
    monitor = YouTubeMonitor(config.get("channels", []), source_dir,
                             int(app.get("lookback_hours", 24)),
                             int(app.get("max_videos_per_channel_per_run", 3)),
                             bool(app.get("include_older_unseen_channel_videos", True)))
    # A prior crash must not leave full-length sources consuming the user's disk.
    monitor.clear_source_cache()
    if stop_event.is_set():
        return
    try:
        if on_phase:
            on_phase("جاري فحص القنوات بحثاً عن فيديوهات جديدة")
        found = monitor.discover_recent(stop_event, db.get_seen_video_ids())
        inserted = sum(1 for video in found if db.add_video(video))
        log.info("Found %s unseen videos across monitored channels; %s were newly queued", len(found), inserted)
    except Exception:
        log.exception("Discovery failed; previously queued videos will still be processed")

    if stop_event.is_set():
        return

    max_videos = int(app.get("max_videos_per_run", 0))
    pending = db.get_pending_videos(None if max_videos <= 0 else max_videos)
    for video in pending:
        if stop_event.is_set():
            log.info("Pipeline stopped by user")
            break
        video_id = video["video_id"]
        db.increment_attempt(video_id)
        source: Path | None = None
        try:
            source = Path(video["source_path"]) if video.get("source_path") else None
            if not source or not source.is_file():
                if on_phase:
                    on_phase(f"جاري تنزيل الفيديو: {video['title']}")
                log.info("Downloading %s: %s", video_id, video["title"])
                source = monitor.download(video)
                db.update_video(video_id, "downloaded", source_path=str(source))
            if stop_event.is_set():
                log.info("Download for %s finished; stopping before clip processing", video_id)
                break
            if on_phase:
                on_phase(f"جاري تحليل الصوت واختيار المقاطع: {video['title']}")
            log.info("Selecting hook-led clips from %s", video_id)
            clips = create_clips(source, clip_dir, video_id, video["title"],
                                 config.get("clips", {}), int(app.get("max_clips_per_video", 5)))
            if stop_event.is_set():
                log.info("Clip rendering for %s finished; saving clips and stopping before publishing", video_id)
            if not clips:
                if stop_event.is_set():
                    break
                db.update_video(video_id, "no_clips")
                continue
            for clip in clips:
                metadata = generate_metadata(
                    clip["transcript"], video["title"],
                    hook=str(clip.get("hook", "")),
                    visual_cue=str(clip.get("visual_cue", "")),
                    moment_reason=str(clip.get("moment_reason", "")),
                    start_seconds=float(clip["start_seconds"]),
                    end_seconds=float(clip["end_seconds"]),
                    source_channel=str(video.get("channel_name", "")),
                )
                sidecar = clip["path"].with_suffix(".json")
                import json
                sidecar.write_text(json.dumps({**metadata, **{k: v for k, v in clip.items()
                                                               if k not in {"path"}}},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
                clip_id = db.add_clip(video_id, int(clip["clip_index"]), str(clip["path"]),
                                      float(clip["start_seconds"]), float(clip["end_seconds"]),
                                      str(clip["transcript"]), metadata)
            db.update_video(video_id, "processed")
        except Exception as exc:
            log.exception("Processing failed for %s", video_id)
            db.update_video(video_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if source is not None:
                try:
                    resolved_source = source.resolve()
                    resolved_source.relative_to(source_dir.resolve())
                    if resolved_source.is_file():
                        size = resolved_source.stat().st_size
                        resolved_source.unlink()
                        log.info("Deleted temporary source for %s (%s bytes)", video_id, size)
                except ValueError:
                    # Never delete a path outside the configured source cache.
                    log.warning("Leaving source outside the temporary cache untouched: %s", source)
                except OSError:
                    log.exception("Could not delete temporary source for %s: %s", video_id, source)
            db.clear_source_path(video_id)
    _publish_pending_clips(config, db, stop_event, on_phase)


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover recent videos, cut Shorts and publish them.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--once", action="store_true", help="Run now and exit instead of scheduling daily.")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    config = load_config(args.config)
    configure_logging(config)
    db = Database(project_path(config.get("paths", {}).get("database", "data/pipeline.sqlite3")))
    run_pipeline(config, db)
    if args.once:
        return
    interval = max(1, int(config.get("app", {}).get("poll_hours", 24)))
    schedule.every(interval).hours.do(run_pipeline, config, db)
    schedule.every(5).minutes.do(_publish_pending_clips, config, db)
    logging.getLogger(__name__).info("Scheduled pipeline every %s hours", interval)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()

