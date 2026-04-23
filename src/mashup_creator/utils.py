import json
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STREAM_RE = re.compile(r"^\s*Stream #\S+.*?:\s*(Video|Audio|Subtitle|Data):", re.IGNORECASE)


def list_files(folder: Path, exts: set) -> List[Path]:
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts])


def safe_copy_into_library(src: Path, dest_folder: Path) -> Path:
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / src.name
    if dest.exists():
        stem = src.stem
        ext = src.suffix
        dest = dest_folder / f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
    shutil.copy2(src, dest)
    return dest


def make_video_thumbnail(src: Path, cache_folder: Path, width: int = 320, height: int = 180) -> Optional[Path]:
    ffmpeg = ffmpeg_tool("ffmpeg")
    if not ffmpeg:
        return None

    try:
        stat = src.stat()
    except OSError:
        return None

    cache_folder.mkdir(parents=True, exist_ok=True)
    key_src = f"{src.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    key = hashlib.sha1(key_src.encode("utf-8", errors="replace")).hexdigest()[:16]
    out_file = cache_folder / f"{key}.jpg"
    if out_file.exists() and out_file.stat().st_size > 0:
        return out_file

    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "1",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
            "-q:v",
            "4",
            str(out_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not out_file.exists() or out_file.stat().st_size <= 0:
        try:
            if out_file.exists():
                out_file.unlink()
        except OSError:
            pass
        return None
    return out_file


def open_in_file_explorer(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f"open {str(path)!r}")
        else:
            os.system(f"xdg-open {str(path)!r}")
    except Exception:
        pass


def random_start(duration: float, clip_len: float) -> float:
    if duration <= clip_len:
        return 0.0
    return random.uniform(0.0, duration - clip_len)


def fit_to_vertical(clip, target_w: int = 1080, target_h: int = 1920):
    from moviepy.video.fx.Resize import Resize
    from moviepy.video.fx.Crop import Crop

    scale = max(target_w / clip.w, target_h / clip.h)
    resized = clip.with_effects([Resize(scale)])
    return resized.with_effects(
        [Crop(x_center=resized.w / 2, y_center=resized.h / 2, width=target_w, height=target_h)]
    )


def add_epic_motion(clip, target_w: int, target_h: int, segment_len: float = 5.0, max_zoom: float = 0.08):
    from moviepy import concatenate_videoclips
    from moviepy.video.fx.Resize import Resize
    from moviepy.video.fx.Crop import Crop

    if clip.duration is None or clip.duration <= 0:
        return clip
    segments = []
    t = 0.0
    while t < clip.duration - 0.01:
        end = min(t + segment_len, clip.duration)
        seg = clip.subclipped(t, end)
        scale = 1.02 + random.random() * max_zoom
        zoomed = seg.with_effects([Resize(scale)])
        max_dx = max(0.0, (zoomed.w - target_w) / 2)
        max_dy = max(0.0, (zoomed.h - target_h) / 2)
        dx = random.uniform(-max_dx, max_dx) if max_dx > 1 else 0.0
        dy = random.uniform(-max_dy, max_dy) if max_dy > 1 else 0.0
        zoomed = zoomed.with_effects(
            [Crop(x_center=zoomed.w / 2 + dx, y_center=zoomed.h / 2 + dy, width=target_w, height=target_h)]
        )
        segments.append(zoomed)
        t = end
    return concatenate_videoclips(segments, method="compose")


def _tool_names(name: str) -> List[str]:
    if sys.platform.startswith("win") and not name.lower().endswith(".exe"):
        return [f"{name}.exe", name]
    return [name]


def _local_tool_candidates(name: str) -> List[Path]:
    roots = [Path.cwd().resolve(), Path(__file__).resolve().parents[2]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        if hasattr(sys, "_MEIPASS"):
            roots.insert(0, Path(sys._MEIPASS))  # type: ignore[attr-defined]

    subdirs = ["", "bin", "ffmpeg", "ffmpeg/bin", "assets", "assets/bin", "tools", "tools/ffmpeg/bin"]
    return [root / subdir / tool for root in roots for subdir in subdirs for tool in _tool_names(name)]


def _imageio_ffmpeg_tool() -> Optional[str]:
    try:
        import imageio_ffmpeg

        path = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None
    return str(path) if path.is_file() else None


def ffmpeg_tool(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found

    for candidate in _local_tool_candidates(name):
        if candidate.is_file():
            return str(candidate)

    if name == "ffmpeg":
        return _imageio_ffmpeg_tool()

    if name == "ffprobe":
        ffmpeg = ffmpeg_tool("ffmpeg")
        if ffmpeg:
            for tool in _tool_names("ffprobe"):
                candidate = Path(ffmpeg).resolve().parent / tool
                if candidate.is_file():
                    return str(candidate)

    return None


def probe_media(path: Path) -> Dict:
    ffprobe = ffmpeg_tool("ffprobe")
    if not ffprobe:
        return _probe_media_with_ffmpeg(path)

    return _probe_media_with_ffprobe(ffprobe, path)


def _probe_media_with_ffprobe(ffprobe: str, path: Path) -> Dict:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration:stream=codec_type",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "Could not read media metadata.").strip()
        raise RuntimeError(msg)
    return json.loads(result.stdout or "{}")


def _probe_media_with_ffmpeg(path: Path) -> Dict:
    ffmpeg = ffmpeg_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found. Install FFmpeg or install imageio-ffmpeg.")

    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(part for part in [result.stderr, result.stdout] if part)
    data: Dict = {"streams": [], "format": {}}

    duration_match = _DURATION_RE.search(output)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        data["format"]["duration"] = str(int(hours) * 3600 + int(minutes) * 60 + float(seconds))

    for line in output.splitlines():
        stream_match = _STREAM_RE.search(line)
        if stream_match:
            data["streams"].append({"codec_type": stream_match.group(1).lower()})

    if data["format"] or data["streams"]:
        return data

    detail = output.strip() or "Could not read media metadata with ffmpeg."
    raise RuntimeError(detail)


def probe_duration(path: Path) -> Optional[float]:
    data = probe_media(path)
    duration = data.get("format", {}).get("duration")
    if duration is None:
        return None
    try:
        return float(duration)
    except (TypeError, ValueError):
        return None


def probe_has_stream(path: Path, codec_type: str) -> bool:
    data = probe_media(path)
    return any(stream.get("codec_type") == codec_type for stream in data.get("streams", []))


def is_video_readable(path: Path) -> bool:
    try:
        duration = probe_duration(path)
        return duration is not None and duration > 0 and probe_has_stream(path, "video")
    except Exception:
        return False


def validate_output(path: Path, expected_len: float) -> Tuple[bool, str]:
    if not path.exists():
        return False, "Output file was not created."
    if path.stat().st_size < 1024 * 50:
        return False, "Output file is too small."
    try:
        duration = probe_duration(path)
        if duration is None:
            return False, "Output duration is unknown."
        if abs(duration - expected_len) > 0.35:
            return False, f"Output duration {duration:.2f}s is not close to {expected_len:.2f}s."
        if not probe_has_stream(path, "audio"):
            return False, "Output has no audio track."
    except Exception as e:
        return False, f"Output validation failed: {e}"
    return True, "OK"
