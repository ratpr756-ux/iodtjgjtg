from pathlib import Path
import sys, subprocess
sys.path.insert(0, r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
from opus_automation import ensure_ffmpeg_path, _transcribe
import shutil
ensure_ffmpeg_path()
root = Path(r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\work")
source = root / "whisper_sine.wav"
subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=5", "-c:a", "pcm_s16le", str(source)], check=True)
words = _transcribe(source, "small", "cpu", "int8")
print(f"WHISPER_OK words={len(words)} av_decode=passed")
