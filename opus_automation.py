"""Local AI-assisted clip extraction, vertical reframing and word-by-word captions."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# These are deliberately signals, not a claim that keyword spotting understands
# the full meaning of a conversation. We combine them with speech density,
# timing, pauses and an optional audio-energy pass below.
CONFLICT_TERMS = {
    "actually", "wrong", "impossible", "never", "secret", "admit", "admitted",
    "confession", "truth", "lie", "lied", "fake", "exposed", "contradiction",
    "mistake", "shocking", "instead", "didn't", "doesn't",
    "مستحيل", "غلط", "الحقيقة", "سر", "اعترف", "اعتراف", "كذب", "كذاب", "محد",
    "ماحد", "كلهم", "عكس", "بالعكس", "لكن", "بس", "مو", "خطأ", "صدمة",
}
EMOTION_TERMS = {
    "wait", "what", "why", "how", "no", "stop", "wow", "crazy", "insane", "unbelievable",
    "wild", "ridiculous", "suddenly", "listen", "look", "seriously", "damn", "oh",
    "يا ساتر", "مستحيل", "شنو", "شلون", "ليش", "كيف", "لا", "وقف", "والله", "تخيل",
    "صدك", "معقولة", "جنوني", "اسمع", "شوف",
}
OPEN_LOOP_TERMS = {
    "wait", "what", "why", "how", "until", "except", "secret",
    "here's", "here is", "that's when", "what happened", "did you know",
    "تدري", "تخيل", "شنو", "ليش", "كيف", "لحد", "إلى أن", "وبعدين", "بس", "السبب",
}
STORY_LINKERS = {
    "then", "because", "but", "after", "before", "so", "when", "until", "finally",
    "meanwhile", "therefore", "وبعدين", "بعدها", "لأن", "لكن", "بس", "لذلك", "وبعد", "من ثم",
}
INTRO_PATTERNS = (
    r"^(?:hey|hi|hello)(?:\s+(?:guys|everyone|chat|everybody))?\b",
    r"^(?:what(?:'s| is) up)(?:\s+(?:guys|everyone|chat))?\b",
    r"^(?:welcome back|thanks for watching|in this video|today we(?:'re| are| will)|today i(?:'m| am| will)|let's talk)\b",
    r"^(?:هلا|اهلا|أهلا|مرحبا)(?:\s+(?:يا شباب|شباب|جماعة|بالجميع))?\b",
    r"^(?:اليوم سنتكلم|اليوم راح نحچي|اليوم راح نتكلم|في هذا الفيديو|خلونا نحچي|خلونا نتكلم)\b",
)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Candidate:
    start: float
    end: float
    words: list[Word]
    score: float
    hook_text: str
    visual_cue: str
    reason: str
    signals: list[str]
    extended_story: bool = False

    @property
    def transcript(self) -> str:
        return " ".join(w.text for w in self.words).strip()


def ensure_ffmpeg_path() -> None:
    """Make an installed FFmpeg discoverable on Windows, including WinGet installs."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        candidates = sorted(
            packages.glob("Gyan.FFmpeg.Shared_*/ffmpeg-*/bin"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for directory in candidates:
            if (directory / "ffmpeg.exe").is_file() and (directory / "ffprobe.exe").is_file():
                os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
                if shutil.which("ffmpeg") and shutil.which("ffprobe"):
                    return
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise FileNotFoundError(
            "FFmpeg tools not found: " + ", ".join(missing)
            + ". Install FFmpeg or add its bin folder to PATH."
        )


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    if command and Path(command[0]).name.lower() in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}:
        ensure_ffmpeg_path()
        resolved = shutil.which(command[0])
        if resolved:
            command[0] = resolved
    result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def probe_duration(path: Path) -> float:
    value = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(value)


def probe_dimensions(path: Path) -> tuple[int, int]:
    value = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)])
    width, height = value.split("x", 1)
    return int(width), int(height)


def _transcribe(path: Path, model_name: str, device: str, compute_type: str) -> list[Word]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Install faster-whisper from requirements.txt") from exc
    selected_device = str(device or "auto").lower()
    selected_compute = str(compute_type or "auto").lower()
    if selected_device == "auto":
        try:
            import ctranslate2
            selected_device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            selected_device = "cpu"
    if selected_compute == "auto":
        selected_compute = "float16" if selected_device == "cuda" else "int8"
    try:
        model = WhisperModel(model_name, device=selected_device, compute_type=selected_compute)
    except Exception:
        if selected_device != "cuda":
            raise
        log.exception("Whisper GPU initialization failed; falling back to CPU")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        selected_device, selected_compute = "cpu", "int8"
    log.info("Whisper model %s selected %s with %s", model_name, selected_device, selected_compute)

    def consume(selected_model) -> list[Word]:
        segments, _ = selected_model.transcribe(str(path), task="transcribe", word_timestamps=True,
                                                vad_filter=True, beam_size=3,
                                                condition_on_previous_text=False)
        result: list[Word] = []
        for segment in segments:
            if segment.words:
                for word in segment.words:
                    value = str(word.word).strip()
                    if value:
                        result.append(Word(float(word.start), float(word.end), value))
            elif str(segment.text).strip():
                # Fallback for models/media that do not expose word timestamps.
                text_words = str(segment.text).split()
                span = max(0.05, float(segment.end) - float(segment.start))
                for i, value in enumerate(text_words):
                    start = float(segment.start) + span * i / max(1, len(text_words))
                    end = float(segment.start) + span * (i + 1) / max(1, len(text_words))
                    result.append(Word(start, end, value))
        return result

    try:
        # faster-whisper yields segments lazily, so CUDA failures can happen here,
        # after model initialization. Guard the full iteration and retry the CPU.
        return consume(model)
    except Exception:
        if selected_device != "cuda":
            raise
        log.exception("Whisper GPU inference failed; retrying this video on CPU")
        cpu_model = WhisperModel(model_name, device="cpu", compute_type="int8")
        log.info("Whisper model %s selected CPU with int8 fallback", model_name)
        return consume(cpu_model)


def _normalise_text(text: str) -> str:
    value = text.lower().replace("’", "'")
    value = re.sub(r"[\u064b-\u065f\u0670]", "", value)
    return value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))


def _matches_terms(text: str, terms: set[str]) -> list[str]:
    normalized = _normalise_text(text)
    found = []
    for term in terms:
        normalized_term = _normalise_text(term)
        if re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", normalized):
            found.append(term)
    return sorted(found, key=len, reverse=True)


def _is_intro(text: str) -> bool:
    normalized = _normalise_text(text).strip(" .,!؟?:;\"'")
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in INTRO_PATTERNS)


def _audio_energy_profile(path: Path) -> list[float]:
    """Return half-second RMS levels at 4 kHz; failure leaves transcript-only scoring."""
    try:
        import numpy as np

        ensure_ffmpeg_path()
        command = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-i", str(path), "-vn", "-ac", "1", "-ar", "4000", "-f", "f32le", "pipe:1"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert process.stdout is not None
        frame_bytes = 2000 * 4  # half a second at 4 kHz
        levels: list[float] = []
        while True:
            chunk = process.stdout.read(frame_bytes)
            if not chunk:
                break
            samples = np.frombuffer(chunk[:len(chunk) - len(chunk) % 4], dtype="<f4")
            levels.append(float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0)
        return_code = process.wait(timeout=30)
        if return_code:
            raise RuntimeError(f"ffmpeg audio analysis exited with {return_code}")
        return levels
    except Exception:
        log.warning("Audio-energy analysis unavailable; using transcript cues only", exc_info=True)
        try:
            if "process" in locals() and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        except Exception:
            pass
        return []


def _audio_spike(profile: list[float], start: float, end: float) -> float:
    if not profile:
        return 0.0
    first = max(0, int(start / 0.5))
    last = min(len(profile), max(first + 1, int(end / 0.5)))
    opening = profile[first:last]
    if not opening:
        return 0.0
    context = profile[max(0, first - 12):first] + profile[last:min(len(profile), last + 12)]
    if not context:
        return 0.0
    baseline = sorted(context)[len(context) // 2]
    peak = max(opening)
    ratio = peak / max(baseline, 0.0005)
    return min(2.0, max(0.0, (ratio - 1.35) * 0.55))


def _candidate_windows(words: list[Word], duration: float, minimum: int, maximum: int,
                       target: int, audio_profile: list[float] | None = None,
                       extended_maximum: int = 45) -> list[Candidate]:
    if not words:
        return []
    # Candidate cuts include mid-thought locations, clear pauses, sentence
    # boundaries, and strong lexical cues. Sampling by time keeps long streams
    # affordable while still finding in-media-res openings.
    starts = {0}
    next_sample = 0.0
    for i, word in enumerate(words):
        if word.start >= next_sample:
            starts.add(i)
            next_sample = word.start + 1.0
        token_text = word.text.lower()
        if _matches_terms(token_text, CONFLICT_TERMS | EMOTION_TERMS | OPEN_LOOP_TERMS):
            starts.add(i)
        if i:
            prior = words[i - 1]
            if word.start - prior.end >= 0.42 or re.search(r"[.!?؟]$", prior.text):
                starts.add(i)

    word_starts = [word.start for word in words]
    candidates: list[Candidate] = []
    base_lengths = sorted({minimum, min(maximum, max(minimum, round(target * 0.55))),
                           min(maximum, max(minimum, round(target * 0.78))),
                           min(maximum, target), maximum})
    story_lengths = sorted({length for length in (36, extended_maximum)
                            if maximum < length <= extended_maximum})
    for index in sorted(starts):
        start = max(0.0, words[index].start)
        if duration - start < minimum:
            continue
        variants = [(length, False) for length in base_lengths]
        variants.extend((length, True) for length in story_lengths)
        for requested_length, is_extended in variants:
            limit = min(duration, start + requested_length)
            right = bisect_left(word_starts, limit)
            window = [word for word in words[index:right] if word.end > start]
            if not window:
                continue
            # End on the last spoken word before the requested boundary to trim
            # trailing silence. Do not let that shorten below the hard minimum.
            end = min(limit, window[-1].end)
            if end - start < minimum or len(window) < max(7, int((end - start) * 0.65)):
                continue
            natural_ends = [word.end for position, word in enumerate(window[:-1])
                            if (re.search(r"[.!?؟]$", word.text)
                                or window[position + 1].start - word.end >= 1.2)
                            and start + minimum <= word.end <= limit]
            if natural_ends:
                end = natural_ends[-1]
                window = [word for word in window if word.start < end]
                closed_at_natural_end = True
            else:
                closed_at_natural_end = False
            trailing_silence = max(0.0, limit - end)
            span = end - start
            first_three = [w for w in window if w.start < start + 3.0]
            if not first_three:
                continue
            opening_text = " ".join(w.text for w in first_three)
            if _is_intro(opening_text):
                continue

            conflict_hits = _matches_terms(opening_text, CONFLICT_TERMS)
            emotion_hits = _matches_terms(opening_text, EMOTION_TERMS)
            open_loop_hits = _matches_terms(opening_text, OPEN_LOOP_TERMS)
            question_start = bool(re.search(r"[?؟]", opening_text)) or bool(
                _matches_terms(opening_text, {"what", "why", "how", "did you", "شنو", "ليش", "كيف"}))
            punctuation_spike = bool(re.search(r"[!?؟]{1,}", opening_text))
            acoustic_spike = _audio_spike(audio_profile or [], start, min(start + 3.0, end))
            cue_terms = CONFLICT_TERMS | EMOTION_TERMS | OPEN_LOOP_TERMS
            cue_words = [word for word in first_three if _matches_terms(word.text, cue_terms)]
            hook_delay = max(0.0, cue_words[0].start - start) if cue_words else 0.0
            density = len(window) / max(1.0, span)
            opening_density = len(first_three) / max(0.5, min(3.0, end - start))
            prior_gap = max(0.0, start - words[index - 1].end) if index else 0.0
            prior_rate = sum(1 for word in words[max(0, index - 12):index]
                             if word.start >= start - 2.5) / 2.5
            next_rate = sum(1 for word in window if word.start < start + 2.5) / 2.5
            pace_spike = next_rate - prior_rate >= 0.8
            gaps = [max(0.0, right_word.start - left_word.end)
                    for left_word, right_word in zip(window, window[1:])]
            max_gap = max(gaps, default=0.0)
            dead_air = sum(max(0.0, gap - 0.65) for gap in gaps)
            lexical_hook = bool(conflict_hits or emotion_hits or open_loop_hits)
            acoustic_hook = acoustic_spike >= 0.9 and (question_start or punctuation_spike or emotion_hits)
            if not lexical_hook and not acoustic_hook:
                continue
            if (opening_density < 0.65 or density < 0.85 or max_gap > 1.6
                    or dead_air > max(1.5, span * 0.10)):
                continue

            story_hits = _matches_terms(" ".join(w.text for w in window), STORY_LINKERS)
            continuous_story = (is_extended and len(story_hits) >= 2 and density >= 1.3
                                and max_gap <= 1.15 and dead_air <= max(2.0, span * 0.06))
            if is_extended and not continuous_story:
                continue

            signals: list[str] = []
            if conflict_hits:
                signals.append("تضاد/معلومة لافتة: " + "، ".join(conflict_hits[:2]))
            if emotion_hits:
                signals.append("إشارة انفعالية في الكلمات: " + "، ".join(emotion_hits[:2]))
            if open_loop_hits or question_start:
                signals.append("سؤال أو فراغ معلوماتي في أول 3 ثوانٍ")
            if acoustic_spike >= 0.9:
                signals.append("قفزة واضحة في شدة الصوت مقارنة بما حولها")
            if prior_gap >= 0.42 and (conflict_hits or emotion_hits or open_loop_hits):
                signals.append(f"صمت قصير {prior_gap:.1f}ث يسبق كلمة الخطاف")
            if pace_spike:
                signals.append("تسارع واضح في إيقاع الكلام عند بداية اللحظة")
            if index > 0:
                signals.append("دخول مباشر في منتصف الحوار" + (f" بعد توقف {prior_gap:.1f}ث" if prior_gap >= 0.42 else ""))
            signals.append(f"كثافة الكلام {density:.1f} كلمة/ث")
            if dead_air <= 0.5:
                signals.append("لا توجد فجوات صمت طويلة داخل النافذة")
            else:
                signals.append(f"خُصم من التقييم لوجود {dead_air:.1f}ث هواء ميت")
            if continuous_story:
                signals.append("قصة متصلة وسريعة تبرر امتداد المقطع فوق 30 ثانية")
            if trailing_silence >= 0.35:
                signals.append(f"قُصّ الصمت من نهاية المقطع ({trailing_silence:.1f}ث)")
            if closed_at_natural_end:
                signals.append("توقف المقطع عند نهاية جملة أو وقفة طبيعية بدل تمديده")

            hook = " ".join(word.text for word in first_three).strip()
            hook = re.sub(r"\s+", " ", hook)
            if acoustic_spike >= 0.9:
                visual_cue = "قصّ مباشر مع قفزة النبرة، ثم زوم سريع وخفيف (5–8%) على أول كلمة." 
            elif conflict_hits:
                visual_cue = "ابدأ على كلمة التناقض نفسها، مع punch-in خفيف وإبرازها في الكابتشن المتحرك." 
            else:
                visual_cue = "ابدأ من أول كلمة بلا تمهيد، مع قطع سريع أو زوم خفيف عند السؤال." 
            reason = "؛ ".join(signals)
            score = (2.5 * bool(conflict_hits) + 2.0 * bool(emotion_hits)
                     + 1.8 * bool(open_loop_hits) + 1.1 * question_start
                     + 1.0 * punctuation_spike + 1.2 * acoustic_spike
                     + 0.6 * pace_spike + (0.8 if prior_gap >= 0.42 and lexical_hook else 0.0)
                     - min(6.0, hook_delay * 4.0)
                     + min(2.0, opening_density * 0.45) + min(1.5, density * 0.5)
                     - min(3.0, dead_air * 0.8) + (0.7 if index else 0.0)
                     + (1.0 if closed_at_natural_end else 0.0)
                     + (2.0 if continuous_story else 0.0))
            candidates.append(Candidate(start, end, window, score, hook, visual_cue,
                                       reason, signals, continuous_story))
    candidates.sort(key=lambda c: c.score, reverse=True)
    selected: list[Candidate] = []
    for candidate in candidates:
        if all(candidate.end <= chosen.start + 3 or candidate.start >= chosen.end - 3 for chosen in selected):
            selected.append(candidate)
    return sorted(selected, key=lambda c: c.start)


def _spread_candidates(candidates: list[Candidate], source_duration: float,
                       maximum: int) -> list[Candidate]:
    """Prefer the strongest moment in each time slice, then fill any empty slices."""
    if not candidates or maximum <= 0:
        return []
    ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
    chosen: list[Candidate] = []
    bucket_count = min(maximum, max(1, int(source_duration // 8)))
    bucket_width = source_duration / bucket_count

    def fits(candidate: Candidate) -> bool:
        return all(candidate.end <= item.start + 3 or candidate.start >= item.end - 3
                   for item in chosen)

    for bucket in range(bucket_count):
        left = bucket * bucket_width
        right = source_duration if bucket == bucket_count - 1 else (bucket + 1) * bucket_width
        options = [item for item in ranked if left <= item.start < right and fits(item)]
        if options:
            chosen.append(options[0])
    for candidate in ranked:
        if len(chosen) >= maximum:
            break
        if candidate not in chosen and fits(candidate):
            chosen.append(candidate)
    return sorted(chosen, key=lambda item: item.start)


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{cs:02d}"


def _ass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _write_kinetic_ass(words: list[Word], path: Path, clip_start: float, clip_end: float,
                       font: str) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Kinetic,{font},56,&H00FFFFFF,&H0000D7FF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,72,72,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    local_words = [w for w in words if w.end > clip_start and w.start < clip_end]
    groups: list[list[Word]] = []
    group: list[Word] = []
    for word in local_words:
        phrase_length = len(" ".join(item.text for item in group + [word]))
        boundary = bool(group and (
            len(group) >= 4 or word.start - group[-1].end > 0.65
            or word.start - group[0].start >= 2.2 or phrase_length > 38
            or re.search(r"[.!?؟]$", group[-1].text)))
        if boundary:
            groups.append(group)
            group = []
        group.append(word)
        if re.search(r"[.!?؟]$", word.text):
            groups.append(group)
            group = []
    if group:
        groups.append(group)

    for phrase_words in groups:
        phrase_parts = [_ass_escape(word.text) for word in phrase_words]
        for index, word in enumerate(phrase_words):
            start = max(clip_start, word.start) - clip_start
            next_start = phrase_words[index + 1].start if index + 1 < len(phrase_words) else word.end + 0.18
            end = min(clip_end, next_start) - clip_start
            if end <= start:
                continue
            phrase = " ".join(
                (r"{\c&H0000D7FF&\b1}" + part + r"{\c&H00FFFFFF&\b0}")
                if item_index == index else part
                for item_index, part in enumerate(phrase_parts)
            )
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Kinetic,,0,0,0,,{phrase}\n")
    path.write_text("".join(lines), encoding="utf-8-sig")


def _render_clip(source: Path, candidate: Candidate, output: Path, index: int,
                 width: int, height: int, font: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path = output.with_suffix(".ass")
    _write_kinetic_ass(candidate.words, subtitle_path, candidate.start, candidate.end, font)
    source_width, source_height = probe_dimensions(source)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{candidate.start:.3f}",
        "-i", str(source), "-t", f"{candidate.end - candidate.start:.3f}",
    ]
    # Preserve the full horizontal frame over a dim, blurred fill. Portrait
    # sources already fit the Shorts canvas and only need a clean scale/crop.
    if source_width / max(1, source_height) < 0.85:
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
              f"subtitles='{subtitle_path.name}'")
        command.extend(["-vf", vf])
    else:
        graph = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=24:2,eq=brightness=-0.22:saturation=0.78[bg];"
            f"[fgsrc]scale={width}:{int(height * 0.62)}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,subtitles='{subtitle_path.name}'[vout]"
        )
        command.extend(["-filter_complex", graph, "-map", "[vout]", "-map", "0:a?"])
    command.extend([
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
    ])
    _run(command, cwd=output.parent)


def create_clips(source_path: str | Path, output_dir: str | Path, video_id: str,
                 source_title: str, settings: dict[str, Any], max_clips: int = 5) -> list[dict[str, Any]]:
    source = Path(source_path)
    duration = probe_duration(source)
    min_s = max(8, int(settings.get("min_seconds", 8)))
    max_s = min(30, max(min_s, int(settings.get("max_seconds", 30))))
    target_s = min(max_s, max(min_s, int(settings.get("target_seconds", 24))))
    extended_s = min(45, max(max_s, int(settings.get("extended_max_seconds", 45))))
    words = _transcribe(source, str(settings.get("whisper_model", "small")),
                        str(settings.get("whisper_device", "cpu")),
                        str(settings.get("whisper_compute_type", "int8")))
    audio_profile = _audio_energy_profile(source) if settings.get("analyze_audio_peaks", True) else []
    ranked_candidates = _candidate_windows(words, duration, min_s, max_s, target_s,
                                           audio_profile, extended_s)
    candidates = _spread_candidates(ranked_candidates, duration, max_clips)
    if not candidates:
        log.warning("No speech-rich, hook-led clips found for %s", video_id)
        return []
    root = Path(output_dir) / video_id
    root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        clip_path = root / f"{video_id}_{index:02d}.mp4"
        _render_clip(source, candidate, clip_path, index,
                     int(settings.get("output_width", 1080)),
                     int(settings.get("output_height", 1920)),
                     str(settings.get("subtitle_font", "Arial")))
        results.append({"clip_index": index, "path": clip_path,
                        "start_seconds": candidate.start, "end_seconds": candidate.end,
                        "transcript": candidate.transcript, "score": candidate.score,
                        "source_title": source_title, "hook": candidate.hook_text,
                        "visual_cue": candidate.visual_cue, "moment_reason": candidate.reason,
                        "selection_signals": candidate.signals,
                        "extended_story": candidate.extended_story})
    return results


