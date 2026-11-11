"""Local Arabic dashboard for configuring and running the Shorts pipeline."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import schedule
import yaml
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_wtf.csrf import CSRFProtect
from waitress import serve

from auto_uploader import YouTubeUploader
from database import Database
from main import (ROOT, _publish_pending_clips, configure_logging,
                  load_config as load_pipeline_config, project_path, run_pipeline)

load_dotenv(ROOT / ".env")

secret_key = os.getenv("FLASK_SECRET_KEY", "").strip()
if len(secret_key) < 24 or secret_key.lower().startswith("replace-"):
    secret_key = secrets.token_urlsafe(48)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key,
    WTF_CSRF_ENABLED=True,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)
CSRFProtect(app)

STATE_LOCK = threading.RLock()
CONFIG_LOCK = threading.RLock()
DB_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
DB_CACHE: dict[str, Database] = {}
INSTANCE_LOCK_HANDLE = None
STATE = {
    "running": False,
    "paused": False,
    "stop_requested": False,
    "phase": "جاهز",
    "last_run": None,
    "last_error": None,
}


def read_config() -> dict:
    with CONFIG_LOCK:
        with (ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}


def write_config(config: dict) -> None:
    temp_path = ROOT / "config.yaml.tmp"
    with CONFIG_LOCK:
        temp_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temp_path.replace(ROOT / "config.yaml")


def get_db(config: dict | None = None) -> Database:
    current = config or read_config()
    path = project_path(current.get("paths", {}).get("database", "data/pipeline.sqlite3"))
    key = str(path.resolve())
    with DB_LOCK:
        if key not in DB_CACHE:
            DB_CACHE[key] = Database(path)
        return DB_CACHE[key]


def youtube_token_exists(config: dict) -> bool:
    youtube = config.get("publishing", {}).get("youtube", {})
    token_path = project_path(youtube.get("token_file", "data/youtube_token.json"))
    if not token_path.is_file():
        return False
    try:
        from google.oauth2.credentials import Credentials
        credentials = Credentials.from_authorized_user_file(
            str(token_path), ["https://www.googleapis.com/auth/youtube.upload"])
        return bool(credentials.valid or (credentials.expired and credentials.refresh_token))
    except Exception:
        return False


def youtube_client_exists(config: dict) -> bool:
    youtube = config.get("publishing", {}).get("youtube", {})
    return project_path(youtube.get("client_secrets", "data/youtube_client_secrets.json")).is_file()


def _start_pipeline(trigger: str) -> bool:
    config = read_config()
    if config.get("app", {}).get("automation_paused", False):
        return False
    with STATE_LOCK:
        if STATE["running"]:
            return False
        STOP_EVENT.clear()
        STATE.update(running=True, paused=False, stop_requested=False,
                     phase="جاري تشغيل خط المعالجة", last_error=None)
    thread = threading.Thread(target=_pipeline_worker, args=(trigger,), daemon=True,
                              name="shorts-pipeline")
    thread.start()
    return True


def _pipeline_worker(trigger: str) -> None:
    try:
        config = load_pipeline_config(ROOT / "config.yaml")
        configure_logging(config)
        logging.getLogger(__name__).info("Pipeline started from %s", trigger)
        def update_phase(value: str) -> None:
            with STATE_LOCK:
                STATE["phase"] = value
        run_pipeline(config, get_db(config), STOP_EVENT, update_phase)
        with STATE_LOCK:
            if STOP_EVENT.is_set():
                STATE.update(running=False, paused=True, stop_requested=False,
                             phase="تم إيقاف العمل",
                             last_run=datetime.now().astimezone().isoformat(timespec="minutes"),
                             last_error=None)
            else:
                STATE.update(running=False, phase="اكتمل آخر تشغيل",
                             last_run=datetime.now().astimezone().isoformat(timespec="minutes"),
                             last_error=None)
    except Exception as exc:
        logging.getLogger(__name__).exception("Pipeline run failed")
        with STATE_LOCK:
            if STOP_EVENT.is_set():
                STATE.update(running=False, paused=True, stop_requested=False,
                             phase="تم إيقاف العمل", last_error=None,
                             last_run=datetime.now().astimezone().isoformat(timespec="minutes"))
            else:
                STATE.update(running=False, phase="تعذّر إكمال التشغيل", last_error=str(exc),
                             last_run=datetime.now().astimezone().isoformat(timespec="minutes"))


def _scheduled_run() -> None:
    config = read_config()
    app_config = config.get("app", {})
    if app_config.get("auto_schedule_enabled", True) and not app_config.get("automation_paused", False):
        _start_pipeline("daily schedule")


def _scheduled_publish() -> None:
    config = read_config()
    if (config.get("app", {}).get("automation_paused", False)
            or not config.get("publishing", {}).get("enabled", False)):
        return

    def publish_worker() -> None:
        try:
            pipeline_config = load_pipeline_config(ROOT / "config.yaml")
            _publish_pending_clips(pipeline_config, get_db(pipeline_config), STOP_EVENT)
        except Exception:
            logging.getLogger(__name__).exception("Scheduled YouTube publication check failed")

    threading.Thread(target=publish_worker, daemon=True, name="youtube-publisher").start()


def _scheduler_loop() -> None:
    schedule.clear("daily-pipeline")
    schedule.clear("youtube-publish")
    schedule.every(max(1, int(read_config().get("app", {}).get("poll_hours", 24)))) \
        .hours.do(_scheduled_run).tag("daily-pipeline")
    schedule.every(5).minutes.do(_scheduled_publish).tag("youtube-publish")
    while True:
        schedule.run_pending()
        time.sleep(20)


def _dashboard_context() -> dict:
    config = read_config()
    db = get_db(config)
    all_clips = db.get_all_clips()
    clips = list(reversed(all_clips))[:8]
    videos = db.get_recent_videos(8)
    with STATE_LOCK:
        run_state = dict(STATE)
    channels = config.get("channels", [])
    return {
        "config": config,
        "channels": channels,
        "clips": clips,
        "total_clips": len(all_clips),
        "videos": videos,
        "run_state": run_state,
        "youtube_connected": youtube_token_exists(config),
        "youtube_client_uploaded": youtube_client_exists(config),
        "authorized_channels": sum(bool(item.get("authorized_for_reuse")) for item in channels),
    }


@app.get("/")
def dashboard():
    return render_template("index.html", **_dashboard_context())


@app.get("/api/status")
def api_status():
    with STATE_LOCK:
        return jsonify(dict(STATE))


@app.post("/pipeline/run")
def run_now():
    if _start_pipeline("dashboard button"):
        flash("بدأ التشغيل. يمكنك متابعة الحالة من هذه الصفحة.", "success")
    elif read_config().get("app", {}).get("automation_paused", False):
        flash("العمل متوقف حالياً. اضغط استئناف العمل أولاً.", "warning")
    else:
        flash("خط المعالجة يعمل حالياً.", "warning")
    return redirect(url_for("dashboard") + "#overview")


@app.post("/pipeline/stop")
def stop_work():
    config = read_config()
    app_settings = config.setdefault("app", {})
    app_settings["automation_paused"] = True
    app_settings["auto_schedule_enabled"] = False
    app_settings["run_on_startup"] = False
    write_config(config)
    STOP_EVENT.set()
    with STATE_LOCK:
        if STATE["running"]:
            STATE.update(stop_requested=True, phase="جاري إيقاف العمل بعد انتهاء المهمة الحالية")
        else:
            STATE.update(paused=True, stop_requested=False, phase="تم إيقاف العمل")
    flash("تم طلب إيقاف العمل وتعطيل الجدولة اليومية. ستكتمل المهمة الجارية بأمان أولاً.", "warning")
    return redirect(url_for("dashboard") + "#overview")


@app.post("/pipeline/resume")
def resume_work():
    with STATE_LOCK:
        if STATE["running"]:
            flash("انتظر حتى تنتهي عملية الإيقاف الحالية ثم استأنف العمل.", "warning")
            return redirect(url_for("dashboard") + "#overview")
    config = read_config()
    app_settings = config.setdefault("app", {})
    app_settings["automation_paused"] = False
    app_settings["auto_schedule_enabled"] = True
    app_settings["run_on_startup"] = True
    write_config(config)
    STOP_EVENT.clear()
    with STATE_LOCK:
        STATE.update(paused=False, stop_requested=False, phase="تم استئناف العمل")
    _start_pipeline("resume button")
    flash("استؤنف العمل وبدأ فحص القنوات الآن.", "success")
    return redirect(url_for("dashboard") + "#overview")


@app.post("/channels/add")
def add_channel():
    name = request.form.get("name", "").strip()
    channel_url = request.form.get("url", "").strip()
    parsed = urlparse(channel_url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com"}
    if not name or host not in allowed_hosts or parsed.scheme != "https":
        flash("أدخل اسم القناة ورابط قناة HTTPS من youtube.com.", "error")
        return redirect(url_for("dashboard") + "#channels")
    if request.form.get("rights_confirm") != "on":
        flash("أكد أولاً أنك تملك الفيديوهات أو لديك إذن بإعادة استخدامها.", "error")
        return redirect(url_for("dashboard") + "#channels")
    config = read_config()
    channels = config.setdefault("channels", [])
    if any(item.get("url", "").rstrip("/").lower() == channel_url.rstrip("/").lower()
           for item in channels):
        flash("هذه القناة موجودة مسبقاً.", "warning")
        return redirect(url_for("dashboard") + "#channels")
    channels.append({"name": name, "url": channel_url, "authorized_for_reuse": True})
    write_config(config)
    flash(f"أُضيفت قناة {name} إلى المراقبة.", "success")
    return redirect(url_for("dashboard") + "#channels")


@app.post("/channels/<int:index>/delete")
def delete_channel(index: int):
    config = read_config()
    channels = config.get("channels", [])
    if 0 <= index < len(channels):
        name = channels[index].get("name", "القناة")
        del channels[index]
        write_config(config)
        flash(f"أُزيلت قناة {name} من المراقبة.", "success")
    else:
        flash("تعذر العثور على القناة المطلوبة.", "error")
    return redirect(url_for("dashboard") + "#channels")


@app.post("/settings/save")
def save_settings():
    config = read_config()
    app_settings = config.setdefault("app", {})
    publishing = config.setdefault("publishing", {})
    youtube = publishing.setdefault("youtube", {})
    publishing["enabled"] = request.form.get("publishing_enabled") == "on"
    app_settings["auto_schedule_enabled"] = request.form.get("auto_schedule_enabled") == "on"
    app_settings["run_on_startup"] = app_settings["auto_schedule_enabled"]
    youtube["enabled"] = request.form.get("youtube_enabled") == "on"
    privacy = request.form.get("privacy", "public")
    youtube["privacy"] = privacy if privacy in {"public", "private", "unlisted"} else "public"
    try:
        app_settings["max_clips_per_video"] = 5
        clips = config.setdefault("clips", {})
        clips["min_seconds"] = 8
        clips["max_seconds"] = 30
        clips["extended_max_seconds"] = min(45, max(30, int(clips.get("extended_max_seconds", 45))))
        clips["target_seconds"] = min(30, max(8, int(clips.get("target_seconds", 20))))
    except ValueError:
        flash("تحقق من أرقام إعدادات المقاطع.", "error")
        return redirect(url_for("dashboard") + "#settings")
    write_config(config)
    flash("حُفظت الإعدادات.", "success")
    return redirect(url_for("dashboard") + "#settings")


@app.post("/settings/youtube-client")
def upload_youtube_client():
    upload = request.files.get("client_json")
    if not upload or not upload.filename:
        flash("اختر ملف OAuth JSON من Google Cloud.", "error")
        return redirect(url_for("dashboard") + "#settings")
    try:
        payload = json.loads(upload.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        flash("الملف ليس JSON صالحاً.", "error")
        return redirect(url_for("dashboard") + "#settings")
    if not isinstance(payload, dict) or "installed" not in payload:
        flash("استخدم OAuth Client من نوع Desktop app.", "error")
        return redirect(url_for("dashboard") + "#settings")
    secret_path = project_path("data/youtube_client_secrets.json")
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    flash("حُفظ ملف الربط محلياً. اضغط زر ربط YouTube لإتمام التفويض مرة واحدة.", "success")
    return redirect(url_for("dashboard") + "#youtube")


@app.post("/youtube/connect")
def connect_youtube():
    config = read_config()
    settings = config.get("publishing", {}).get("youtube", {})
    client = project_path(settings.get("client_secrets", "data/youtube_client_secrets.json"))
    token = project_path(settings.get("token_file", "data/youtube_token.json"))
    if not client.is_file():
        flash("ارفع ملف Google OAuth من قسم بيانات Google OAuth أولاً، ثم اضغط زر الربط مجدداً.", "warning")
        return redirect(url_for("dashboard") + "#oauth")
    try:
        YouTubeUploader(client, token).connect()
        flash("تم ربط قناة YouTube بنجاح.", "success")
    except Exception as exc:
        logging.getLogger(__name__).exception("YouTube OAuth connection failed")
        flash(f"تعذر الربط: {exc}", "error")
    return redirect(url_for("dashboard") + "#youtube")


@app.get("/clips/<int:clip_id>/preview")
def preview_clip(clip_id: int):
    for clip in get_db().get_all_clips():
        if int(clip["clip_id"]) == clip_id:
            path = Path(clip["clip_path"])
            if path.is_file():
                return send_file(path, mimetype="video/mp4", as_attachment=False,
                                 download_name=path.name)
            break
    flash("ملف المقطع غير موجود على الجهاز.", "error")
    return redirect(url_for("dashboard") + "#clips")


@app.errorhandler(413)
def request_too_large(_error):
    flash("حجم الملف أكبر من الحد المسموح.", "error")
    return redirect(url_for("dashboard") + "#settings")


def _acquire_single_instance() -> bool:
    """Prevent duplicate schedulers and pipeline runs if the launcher is opened twice."""
    global INSTANCE_LOCK_HANDLE
    lock_path = project_path("data/clipflow.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"1")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        return False
    INSTANCE_LOCK_HANDLE = handle
    return True


def start_server() -> None:
    config = read_config()
    configure_logging(config)
    if not _acquire_single_instance():
        logging.getLogger(__name__).warning("ClipFlow is already running; this copy will exit.")
        return
    # OAuth callback URLs contain one-time authorization codes; keep access logs out of the app log.
    logging.getLogger("waitress").setLevel(logging.WARNING)
    threading.Thread(target=_scheduler_loop, daemon=True, name="pipeline-scheduler").start()
    app_config = config.get("app", {})
    is_paused = app_config.get("automation_paused", False)
    with STATE_LOCK:
        STATE.update(paused=is_paused, phase="تم إيقاف العمل" if is_paused else "جاهز")
    if app_config.get("run_on_startup", True) and not is_paused:
        _start_pipeline("application startup")
    port = int(os.getenv("PORT", "5000"))
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    start_server()

