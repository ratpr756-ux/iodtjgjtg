from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from database import Database
from youtube_monitor import YouTubeMonitor
import opus_automation
from opus_automation import Word, create_clips, ensure_ffmpeg_path
import main


def test_persisted_video_download_shape():
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        db = Database(temp_path / "db.sqlite3")
        db.add_video({
            "id": "fixture-id", "channel_name": "Fixture", "channel_url": "https://www.youtube.com/@fixture",
            "title": "Fixture title", "url": "https://www.youtube.com/watch?v=fixture-id",
            "published_at": "2026-11-11T00:00:00+00:00",
        })
        queued = db.get_pending_videos()[0]
        seen_urls = []
        seen_options = []
        class FakeYDL:
            def __init__(self, options):
                self.options = options
                seen_options.append(options)
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def download(self, urls):
                seen_urls.extend(urls)
                output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"fixture")
        monitor = YouTubeMonitor([], temp_path / "sources")
        with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
            result = monitor.download(queued)
        assert result.name == "source.mp4"
        assert seen_urls == ["https://www.youtube.com/watch?v=fixture-id"]
        assert "height<=720" in seen_options[0]["format"]


def test_failed_download_removes_partial_source_file():
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        class FakeYDL:
            def __init__(self, options): self.options = options
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def download(self, _urls):
                partial = Path(self.options["outtmpl"].replace("%(ext)s", "mp4.part"))
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"partial")
                raise RuntimeError("fixture download failure")
        monitor = YouTubeMonitor([], temp_path / "sources")
        video = {"video_id": "failed-id", "video_url": "https://www.youtube.com/watch?v=failed-id"}
        with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
            try:
                monitor.download(video)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected fixture download failure")
        assert not list((temp_path / "sources").rglob("*mp4.part"))


def test_channel_scan_stops_after_24_hours():
    from datetime import datetime, timedelta, timezone
    import youtube_monitor
    now = datetime.now(timezone.utc)
    entries = [
        {"id": "recent", "title": "Recent", "timestamp": int((now - timedelta(minutes=5)).timestamp())},
        {"id": "old", "title": "Old", "timestamp": int((now - timedelta(hours=25)).timestamp())},
        {"id": "later-recent", "title": "Should not be reached", "timestamp": int((now - timedelta(hours=1)).timestamp())},
    ]
    seen_options = []
    class FakeYDL:
        def __init__(self, options): seen_options.append(options)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download=False): return {"title": "Fixture", "entries": entries}
    monitor = YouTubeMonitor([{"name": "Fixture", "url": "https://www.youtube.com/@fixture", "authorized_for_reuse": True}], "unused")
    with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
        found = monitor.discover_recent()
    assert [item["id"] for item in found] == ["recent"]
    assert seen_options[0]["playlistend"] == 1000
    assert seen_options[0]["socket_timeout"] == 15


def test_discovery_limits_each_channel_to_its_latest_three_recent_uploads():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    entries = [{"id": f"video-{i}", "title": f"Video {i}",
                "timestamp": int((now - timedelta(days=3, minutes=i * 3)).timestamp())}
               for i in range(5)]
    class FakeYDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download=False): return {"title": "Channel", "entries": entries}
    monitor = YouTubeMonitor([{"name": "Channel", "url": "https://www.youtube.com/@channel/videos",
                               "authorized_for_reuse": True}], "unused", max_videos_per_channel=3,
                           include_older_unseen=True)
    with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
        found = monitor.discover_recent()
    assert [item["id"] for item in found] == ["video-0", "video-1", "video-2"]
    with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
        older = monitor.discover_recent(known_video_ids={item["id"] for item in found})
    assert [item["id"] for item in older] == ["video-3", "video-4"]


def test_plain_channel_url_scans_the_videos_tab_not_channel_tabs():
    from datetime import datetime, timezone
    stamp = int(datetime.now(timezone.utc).timestamp())
    requested = []
    class FakeYDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download=False):
            requested.append(url)
            if url.endswith("/@speed/videos"):
                return {"title": "Speed", "entries": [
                    {"id": "speed-upload-1", "title": "Recent upload", "timestamp": stamp}]}
            return {"title": "Speed", "entries": [
                {"id": "UCchannelID123456", "title": "Speed - Videos"}]}
    monitor = YouTubeMonitor([{"name": "Speed", "url": "https://www.youtube.com/@speed",
                               "authorized_for_reuse": True}], "unused")
    with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
        found = monitor.discover_recent()
    assert requested == ["https://www.youtube.com/@speed/videos"]
    assert [item["id"] for item in found] == ["speed-upload-1"]


def test_one_metadata_error_does_not_skip_other_channel_videos():
    from datetime import datetime, timezone
    now = int(datetime.now(timezone.utc).timestamp())
    scanned = []
    class FakeYDL:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download=False):
            scanned.append(url)
            if url == "https://www.youtube.com/@first/videos":
                return {"title": "First", "entries": [{"id": "broken"}, {"id": "second"}]}
            if url == "https://www.youtube.com/watch?v=broken":
                raise RuntimeError("fixture metadata error")
            if url == "https://www.youtube.com/watch?v=second":
                return {"id": "second", "title": "Second upload", "timestamp": now}
            if url == "https://www.youtube.com/@second/videos":
                return {"title": "Second channel", "entries": [
                    {"id": "third", "title": "Third upload", "timestamp": now}]}
            raise AssertionError(url)
    monitor = YouTubeMonitor([
        {"name": "First", "url": "https://www.youtube.com/@first/videos", "authorized_for_reuse": True},
        {"name": "Second", "url": "https://www.youtube.com/@second/videos", "authorized_for_reuse": True},
    ], "unused")
    with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
        found = monitor.discover_recent()
    assert {item["id"] for item in found} == {"second", "third"}
    assert "https://www.youtube.com/@second/videos" in scanned


def test_one_video_metadata_error_does_not_skip_other_channels():
    from datetime import datetime, timezone
    now = int(datetime.now(timezone.utc).timestamp())
    scanned = []
    class FakeYDL:
        def __init__(self, options): self.options = options
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download=False):
            scanned.append(url)
            if url == "https://www.youtube.com/@first/videos":
                return {"title": "First", "entries": [{"id": "broken"}, {"id": "second"}]}
            if url == "https://www.youtube.com/watch?v=broken":
                raise RuntimeError("fixture metadata error")
            if url == "https://www.youtube.com/watch?v=second":
                return {"id": "second", "title": "Second upload", "timestamp": now}
            if url == "https://www.youtube.com/@second/videos":
                return {"title": "Second channel", "entries": [
                    {"id": "third", "title": "Third upload", "timestamp": now}]}
            raise AssertionError(url)
    monitor = YouTubeMonitor([
        {"name": "First", "url": "https://www.youtube.com/@first/videos", "authorized_for_reuse": True},
        {"name": "Second", "url": "https://www.youtube.com/@second/videos", "authorized_for_reuse": True},
    ], "unused")
    with patch("youtube_monitor.yt_dlp.YoutubeDL", FakeYDL):
        found = monitor.discover_recent()
    assert {item["id"] for item in found} == {"second", "third"}
    assert "https://www.youtube.com/@second/videos" in scanned


def test_real_ffmpeg_vertical_caption_render():
    ensure_ffmpeg_path()
    ffmpeg = opus_automation.shutil.which("ffmpeg")
    ffprobe = opus_automation.shutil.which("ffprobe")
    assert ffmpeg and ffprobe
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "fixture.mp4"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=28",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=28",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ], check=True, capture_output=True, text=True)
        words = [Word(0.5 + i * 0.4, 0.8 + i * 0.4,
                      ["What?", "just", "happened?", "Nobody", "expected", "this!"][i]
                      if i < 6 else ("wait" if i % 2 == 0 else "world")) for i in range(55)]
        with patch("opus_automation._transcribe", return_value=words):
            clips = create_clips(source, root / "clips", "fixture-id", "Fixture", {
                "min_seconds": 12, "max_seconds": 30, "target_seconds": 24,
                "output_width": 1080, "output_height": 1920, "subtitle_font": "Arial",
            }, max_clips=1)
        assert len(clips) == 1
        output = Path(clips[0]["path"])
        info = json.loads(subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(output),
        ], check=True, capture_output=True, text=True).stdout)
        video_stream = next(stream for stream in info["streams"] if "width" in stream)
        assert (video_stream["width"], video_stream["height"]) == (1080, 1920)
        assert 8 <= float(info["format"]["duration"]) <= 45
        assert "hook" in clips[0] and "moment_reason" in clips[0] and "visual_cue" in clips[0]
        assert output.with_suffix(".ass").is_file()
        subtitle = output.with_suffix(".ass").read_text(encoding="utf-8-sig")
        assert "Style: Kinetic,Arial,56" in subtitle
        assert "fscx112" not in subtitle and "fscy112" not in subtitle


def test_hook_selection_rejects_greetings_and_finds_mid_thought_peak():
    greeting_words = ["Hello", "everyone,", "today", "we", "are", "going", "to", "talk", "about"]
    weak = [Word(i * 0.4, i * 0.4 + 0.25, word) for i, word in enumerate(greeting_words)]
    weak += [Word(4.0 + i * 0.4, 4.25 + i * 0.4, "ordinary") for i in range(50)]
    assert opus_automation._candidate_windows(weak, 30, 12, 30, 24) == []

    words = [Word(i * 0.25, i * 0.25 + 0.2, word) for i, word in enumerate(
        ["we", "were", "talking", "normally", "and", "then", "he", "said", "Wait!",
         "That", "is", "completely", "impossible."])]
    base_end = words[-1].end
    words.extend(Word(base_end + 0.05 + i * 0.25, base_end + 0.25 + i * 0.25,
                      "normal") for i in range(120))
    selected = opus_automation._candidate_windows(words, 60, 12, 30, 24)
    assert selected
    assert selected[0].start >= 1.8
    assert "Wait" in selected[0].hook_text
    assert 8 <= selected[0].end - selected[0].start <= 30
    assert "منتصف الحوار" in selected[0].reason


def test_extended_duration_requires_fast_connected_story():
    words = []
    for i in range(240):
        if i == 0:
            text = "Wait!"
        elif i % 9 == 0:
            text = "because"
        elif i % 13 == 0:
            text = "then"
        else:
            text = "story"
        words.append(Word(i * 0.25, i * 0.25 + 0.2, text))
    story = opus_automation._candidate_windows(words, 60, 12, 30, 24, extended_maximum=45)
    assert story and max(item.end - item.start for item in story) <= 45
    assert any(item.extended_story and item.end - item.start > 30 for item in story)


def test_variable_length_ends_at_a_natural_point():
    words = []
    for i in range(240):
        time_offset = 2.5 if i >= 56 else 0.0
        text = "Wait!" if i == 0 else ("impossible." if i == 55 else "story")
        start = i * 0.25 + time_offset
        words.append(Word(start, start + 0.2, text))
    selected = opus_automation._candidate_windows(words, 70, 8, 30, 20)
    assert selected
    chosen = selected[0]
    assert chosen.start == 0
    assert 8 <= chosen.end - chosen.start < 30
    assert "نهاية جملة" in chosen.reason


def test_clip_selection_spreads_across_the_whole_video():
    moments = [opus_automation.Candidate(start, start + 12, [], float(10 - i),
                                         "hook", "zoom", "reason", [])
               for i, start in enumerate([10, 210, 410, 610, 810])]
    selected = opus_automation._spread_candidates(moments, 1000, 5)
    assert [item.start for item in selected] == [10, 210, 410, 610, 810]


def test_variable_length_ends_at_a_natural_point():
    words = []
    for i in range(240):
        time_offset = 2.5 if i >= 56 else 0.0
        text = "Wait!" if i == 0 else ("impossible." if i == 55 else "story")
        start = i * 0.25 + time_offset
        words.append(Word(start, start + 0.2, text))
    selected = opus_automation._candidate_windows(words, 70, 8, 30, 20)
    assert selected
    chosen = selected[0]
    assert chosen.start == 0
    assert 8 <= chosen.end - chosen.start < 30
    assert "نهاية جملة" in chosen.reason


def test_clip_selection_spreads_across_the_whole_video():
    moments = [opus_automation.Candidate(start, start + 12, [], float(10 - i),
                                         "hook", "zoom", "reason", [])
               for i, start in enumerate([10, 210, 410, 610, 810])]
    selected = opus_automation._spread_candidates(moments, 1000, 5)
    assert [item.start for item in selected] == [10, 210, 410, 610, 810]


def test_caption_metadata_is_grounded_bilingual_and_limited():
    from caption_generator import generate_metadata
    metadata = generate_metadata("What? GTA lowriders are not what you think.", "GTA 5 stream",
                                 hook="What? GTA lowriders", visual_cue="Quick zoom.",
                                 moment_reason="Opening contrast", start_seconds=12.5, end_seconds=36.0)
    assert metadata["hook"] == "What? GTA lowriders"
    assert len(metadata["hashtags"]) <= 4
    assert "GTA5" in " ".join(metadata["hashtags"])
    assert "#Shorts" in metadata["hashtags"]
    assert len(metadata["title"]) <= 72
    assert "Tell me" in metadata["cta"]
    arabic = generate_metadata("هذا الكلام مستحيل، شنو رأيكم؟")
    assert "comments" not in arabic["caption"] and "التعليقات" in arabic["cta"]


def test_caption_metadata_shortens_hook_and_uses_channel_topics():
    from caption_generator import generate_metadata
    metadata = generate_metadata(
        "Why did he leave so fast? And then he says something about purple.",
        "I Went Undercover as Spiderman For Halloween!",
        hook="Why did he leave so fast? And then he says something about purple. The",
        source_channel="سبيد",
    )
    assert metadata["title"] == "Why did he leave so fast?"
    assert metadata["hashtags"] == ["#Entertainment", "#IShowSpeed", "#SpiderMan", "#Shorts"]
    assert len(metadata["caption"]) < 45


def test_transcription_keeps_spoken_language():
    from types import SimpleNamespace
    fake_word = SimpleNamespace(word="hello", start=0.0, end=0.5)
    fake_segment = SimpleNamespace(words=[fake_word], text="hello", start=0.0, end=0.5)
    fake_model = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    fake_model.transcribe.return_value = (iter([fake_segment]), None)
    with patch("faster_whisper.WhisperModel", return_value=fake_model):
        words = opus_automation._transcribe(Path("unused.wav"), "small", "cpu", "int8")
    assert words[0].text == "hello"
    assert fake_model.transcribe.call_args.kwargs["task"] == "transcribe"


def test_gpu_transcription_failure_falls_back_to_cpu():
    from types import SimpleNamespace
    from unittest.mock import Mock
    devices = []
    class FakeModel:
        def __init__(self, name, device, compute_type):
            devices.append((device, compute_type))
            self.device = device
        def transcribe(self, *args, **kwargs):
            if self.device == "cuda":
                def broken_segments():
                    raise RuntimeError("missing CUDA library")
                    yield
                return broken_segments(), None
            word = SimpleNamespace(word="Hello", start=0.0, end=0.5)
            segment = SimpleNamespace(words=[word], text="Hello", start=0.0, end=0.5)
            return iter([segment]), None
    with patch("faster_whisper.WhisperModel", FakeModel):
        words = opus_automation._transcribe(Path("unused.wav"), "small", "cuda", "float16")
    assert [(item.text, item.start, item.end) for item in words] == [("Hello", 0.0, 0.5)]
    assert devices == [("cuda", "float16"), ("cpu", "int8")]


def test_pipeline_records_clip_without_uploading():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "sources" / "source.mp4"
        source.parent.mkdir()
        source.write_bytes(b"local fixture")
        clip = root / "fixture_short.mp4"
        clip.write_bytes(b"local clip")
        now = "2026-11-11T00:00:00+00:00"
        class FakeMonitor:
            def __init__(self, *args, **kwargs): pass
            def clear_source_cache(self): return 0
            def discover_recent(self, stop_event=None, known_video_ids=None):
                return [{"id": "pipeline-fixture", "channel_name": "Fixture", "channel_url": "https://www.youtube.com/@fixture",
                         "title": "Fixture title", "url": "https://www.youtube.com/watch?v=pipeline-fixture", "published_at": now}]
            def download(self, video):
                assert video["video_id"] == "pipeline-fixture"
                return source
        def fake_clips(*args, **kwargs):
            return [{"clip_index": 1, "path": clip, "start_seconds": 0.0, "end_seconds": 25.0,
                     "transcript": "A fixture sentence.", "score": 1.0, "source_title": "Fixture title"}]
        db = Database(root / "db.sqlite3")
        config = {
            "app": {"lookback_hours": 24, "max_videos_per_run": 1, "max_clips_per_video": 1},
            "channels": [{"name": "Fixture", "url": "https://www.youtube.com/@fixture", "authorized_for_reuse": True}],
            "paths": {"database": str(root / "db.sqlite3"), "source_downloads": str(root / "sources"), "clips": str(root / "clips")},
            "clips": {"target_seconds": 25},
            "publishing": {"enabled": False, "youtube": {"enabled": False}},
        }
        with patch("main.YouTubeMonitor", FakeMonitor), patch("main.create_clips", fake_clips):
            main.run_pipeline(config, db)
        videos = db.get_recent_videos()
        clips = db.get_all_clips()
        assert len(videos) == 1 and videos[0]["status"] == "processed"
        assert not source.exists() and videos[0]["source_path"] is None
        assert len(clips) == 1 and clips[0]["metadata"]["title"] == "A fixture sentence"
        assert clip.with_suffix(".json").is_file()


def test_pipeline_processes_every_pending_channel_video():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "sources" / "source.mp4"
        source.parent.mkdir()
        now = "2026-11-11T00:00:00+00:00"
        records = [{"id": f"channel-video-{i}", "channel_name": f"Channel {i}",
                    "channel_url": f"https://www.youtube.com/@channel{i}",
                    "title": f"Fixture {i}", "url": f"https://www.youtube.com/watch?v=channel-video-{i}",
                    "published_at": now} for i in range(3)]
        class FakeMonitor:
            def __init__(self, *args, **kwargs): pass
            def clear_source_cache(self): return 0
            def discover_recent(self, stop_event=None, known_video_ids=None): return records
            def download(self, video):
                source.write_bytes(b"local fixture")
                return source
        def fake_clips(_source, _out, video_id, title, _settings, _max):
            clip = root / f"{video_id}.mp4"
            clip.write_bytes(b"clip")
            return [{"clip_index": 1, "path": clip, "start_seconds": 2.0, "end_seconds": 14.0,
                     "transcript": "What happened here?", "hook": "What happened?",
                     "visual_cue": "Quick zoom", "moment_reason": "Strong question",
                     "score": 3.0, "source_title": title}]
        db = Database(root / "all-channels.sqlite3")
        config = {
            "app": {"lookback_hours": 24, "max_videos_per_run": 0,
                    "max_videos_per_channel_per_run": 3, "max_clips_per_video": 5},
            "channels": [],
            "paths": {"database": str(root / "all-channels.sqlite3"),
                      "source_downloads": str(root / "sources"), "clips": str(root / "clips")},
            "clips": {"target_seconds": 20},
            "publishing": {"enabled": False, "youtube": {"enabled": False}},
        }
        with patch("main.YouTubeMonitor", FakeMonitor), patch("main.create_clips", fake_clips):
            main.run_pipeline(config, db)
        videos = db.get_recent_videos(10)
        assert len(videos) == 3
        assert all(video["status"] == "processed" for video in videos)
        assert all(video["source_path"] is None for video in videos)
        assert not source.exists()
        assert len(db.get_all_clips()) == 3


def test_publishing_uses_one_source_channel_and_two_hour_spacing():
    from datetime import datetime, timedelta, timezone
    from threading import Event
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        db = Database(root / "publish.sqlite3")
        channels = [
            {"name": "Kai", "url": "https://www.youtube.com/@Kai", "authorized_for_reuse": True},
            {"name": "Speed", "url": "https://www.youtube.com/@IShowSpeed", "authorized_for_reuse": True},
            {"name": "Ronaldo", "url": "https://www.youtube.com/@Ronaldo", "authorized_for_reuse": True},
        ]
        for index, channel in enumerate(channels):
            video_id = f"publish-source-{index}"
            db.add_video({"id": video_id, "channel_name": channel["name"], "channel_url": channel["url"],
                          "title": channel["name"], "url": f"https://www.youtube.com/watch?v={video_id}",
                          "published_at": "2026-11-11T00:00:00+00:00"})
            clip = root / f"{video_id}.mp4"
            clip.write_bytes(b"clip")
            db.add_clip(video_id, 1, str(clip), 0, 12, "short transcript", {"title": channel["name"]})

        config = {"channels": channels, "publishing": {
            "enabled": True, "youtube": {"enabled": True}, "max_posts_per_platform_per_day": 3,
            "minimum_hours_between_posts": 2, "rotation_start_channel_url": channels[1]["url"],
            "rotation_state_file": str(root / "rotation.json"),
        }}
        uploaded = []
        def fake_upload(clip_id, clip_path, metadata, cfg, database, already_published):
            uploaded.append(clip_id)
            database.mark_published(clip_id, "youtube", f"upload-{clip_id}")
            return True
        with patch("main._publish_clip", side_effect=fake_upload):
            main._publish_pending_clips(config, db, Event())
            main._publish_pending_clips(config, db, Event())
        assert len(uploaded) == 1
        published = [clip for clip in db.get_all_clips() if "youtube" in clip["published"]]
        assert len(published) == 1
        assert db.get_video(published[0]["video_id"])["channel_url"] == channels[1]["url"]
        state = json.loads((root / "rotation.json").read_text(encoding="utf-8"))
        assert state["active_channel_url"] == channels[1]["url"]

        now = datetime.fromisoformat(state["started_at"])
        next_channel, _, _ = main._active_publish_channel(config, now + timedelta(hours=25))
        assert next_channel["name"] == "Ronaldo"


def test_daily_scheduler_is_continuous_until_paused():
    import web_app
    with patch.object(web_app, "read_config", return_value={"app": {"auto_schedule_enabled": True, "automation_paused": False}}), patch.object(web_app, "_start_pipeline") as start:
        web_app._scheduled_run()
        start.assert_called_once_with("daily schedule")
    with patch.object(web_app, "read_config", return_value={"app": {"auto_schedule_enabled": True, "automation_paused": True}}), patch.object(web_app, "_start_pipeline") as start:
        web_app._scheduled_run()
        start.assert_not_called()
    response = web_app.app.test_client().get("/")
    assert response.status_code == 200 and "إيقاف العمل" in response.get_data(as_text=True)


def test_youtube_upload_request_without_network():
    from auto_uploader import YouTubeUploader
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "clip.mp4"
        path.write_bytes(b"fixture")
        class FakeRequest:
            def next_chunk(self): return None, {"id": "fixture-upload-id"}
        service = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        service.videos.return_value.insert.return_value = FakeRequest()
        uploader = YouTubeUploader("unused-client.json", "unused-token.json")
        metadata = {"title": "Fixture title", "description": "Fixture description", "hashtags": ["#Shorts"]}
        with patch.object(uploader, "_service", return_value=service), \
             patch("googleapiclient.http.MediaFileUpload", return_value="fixture-media"):
            result = uploader.upload(path, metadata, privacy="private")
        assert result == "fixture-upload-id"
        body = service.videos.return_value.insert.call_args.kwargs["body"]
        assert body["status"]["privacyStatus"] == "private"
        assert body["snippet"]["tags"] == ["Shorts"]


def test_stop_and_resume_controls():
    import copy
    import web_app
    settings = {"app": {"automation_paused": False, "auto_schedule_enabled": True, "run_on_startup": True}}
    def read_settings(): return copy.deepcopy(settings)
    def write_settings(value):
        settings.clear()
        settings.update(copy.deepcopy(value))
    old_csrf = web_app.app.config["WTF_CSRF_ENABLED"]
    web_app.app.config["WTF_CSRF_ENABLED"] = False
    web_app.STOP_EVENT.clear()
    with web_app.STATE_LOCK:
        web_app.STATE.update(running=False, paused=False, stop_requested=False, phase="جاهز")
    try:
        with patch.object(web_app, "read_config", side_effect=read_settings), \
             patch.object(web_app, "write_config", side_effect=write_settings), \
             patch.object(web_app, "_start_pipeline", return_value=True) as start:
            client = web_app.app.test_client()
            stop_response = client.post("/pipeline/stop")
            assert stop_response.status_code == 302
            assert settings["app"]["automation_paused"] is True
            assert settings["app"]["auto_schedule_enabled"] is False
            assert web_app.STOP_EVENT.is_set()
            resume_response = client.post("/pipeline/resume")
            assert resume_response.status_code == 302
            assert settings["app"]["automation_paused"] is False
            assert settings["app"]["auto_schedule_enabled"] is True
            assert not web_app.STOP_EVENT.is_set()
            start.assert_called_once_with("resume button")
    finally:
        web_app.app.config["WTF_CSRF_ENABLED"] = old_csrf
        web_app.STOP_EVENT.clear()
        with web_app.STATE_LOCK:
            web_app.STATE.update(running=False, paused=False, stop_requested=False, phase="جاهز")


for test in [test_persisted_video_download_shape, test_failed_download_removes_partial_source_file,
             test_real_ffmpeg_vertical_caption_render,
             test_hook_selection_rejects_greetings_and_finds_mid_thought_peak,
             test_extended_duration_requires_fast_connected_story,
             test_variable_length_ends_at_a_natural_point,
             test_clip_selection_spreads_across_the_whole_video,
             test_caption_metadata_is_grounded_bilingual_and_limited,
             test_channel_scan_stops_after_24_hours,
             test_discovery_limits_each_channel_to_its_latest_three_recent_uploads,
             test_plain_channel_url_scans_the_videos_tab_not_channel_tabs,
             test_one_video_metadata_error_does_not_skip_other_channels,
             test_transcription_keeps_spoken_language, test_gpu_transcription_failure_falls_back_to_cpu,
             test_pipeline_records_clip_without_uploading, test_pipeline_processes_every_pending_channel_video,
             test_publishing_uses_one_source_channel_and_two_hour_spacing,
             test_daily_scheduler_is_continuous_until_paused,
             test_youtube_upload_request_without_network,
             test_stop_and_resume_controls]:
    test()
    print("PASS", test.__name__)
