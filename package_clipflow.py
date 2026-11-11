from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
source = Path(r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
target = Path(r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline.zip")
files = [
    ".env.example", "README.md", "requirements.txt", "config.yaml",
    "auto_uploader.py", "caption_generator.py", "database.py", "install_windows.bat",
    "main.py", "opus_automation.py", "Start ClipFlow.bat", "web_app.py", "youtube_monitor.py",
    "templates/base.html", "templates/index.html", "static/app.js", "static/style.css",
]
with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=7) as archive:
    for relative in files:
        path = source / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        archive.write(path, arcname="shorts_pipeline/" + relative)
with ZipFile(target, "r") as archive:
    names = archive.namelist()
    assert "shorts_pipeline/templates/index.html" in names
    assert "shorts_pipeline/static/style.css" in names
    assert "shorts_pipeline/.env" not in names
    assert not any("/data/" in name or "/.venv/" in name for name in names)
    print("Packaged", len(names), "files; stop button and OAuth instructions included; local secrets excluded.")
    print("Archive size:", target.stat().st_size, "bytes")
