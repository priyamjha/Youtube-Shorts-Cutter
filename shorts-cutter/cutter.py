"""
Engine for the Shorts Cutter: download, analyze, plan, cut.

Every long step takes a `progress(fraction, **extra)` callback and a
`cancel` threading.Event, so the web UI can show live progress and stop a job.
All tools used here are free and run locally: yt-dlp, FFmpeg, PySceneDetect.
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}


class Cancelled(Exception):
    """Raised inside a step when the user cancels the job."""


def check_tools():
    """Return a list of missing command-line tools (empty when all present)."""
    return [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]


# --------------------------------------------------------------------------
# 1. Video info + download (yt-dlp)
# --------------------------------------------------------------------------

def _ydl_base_opts():
    return {"quiet": True, "no_warnings": True, "noplaylist": True}


def fetch_info(url):
    """Metadata only, no download. Fast (a couple of seconds)."""
    import yt_dlp

    with yt_dlp.YoutubeDL(_ydl_base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":  # URL was a playlist; take the first entry
        info = info["entries"][0]
    return {
        "id": info["id"],
        "title": info.get("title") or info["id"],
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        "live_status": info.get("live_status") or "not_live",
        "is_live": bool(info.get("is_live")),
        "upload_date": info.get("upload_date") or "",
        "webpage_url": info.get("webpage_url") or url,
    }


def download(url, dest_dir, quality, progress, cancel):
    """
    Download the video as MP4 into dest_dir. Returns the file path.
    quality: max height (720 or 1080). Livestreams are recorded from the start.
    """
    import yt_dlp

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    state = {"part": 1}

    def hook(d):
        if cancel.is_set():
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else 0.0
            progress(frac, eta=d.get("eta"), speed=d.get("speed"),
                     downloaded=done, total=total, part=state["part"])
        elif d["status"] == "finished":
            state["part"] += 1
            progress(1.0, eta=0, speed=None, part=state["part"] - 1)

    opts = dict(_ydl_base_opts())
    opts.update({
        "format": (f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]"
                   f"/bv*[height<={quality}]+ba/b[height<={quality}]/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "live_from_start": True,
        "retries": 10,
        "fragment_retries": 10,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("_type") == "playlist":
        info = info["entries"][0]
    vid = info["id"]
    files = sorted(p for p in dest_dir.glob(f"{vid}.*") if p.suffix in VIDEO_EXTS)
    if not files:
        raise RuntimeError("Download finished but no video file was found.")
    return files[0]


# --------------------------------------------------------------------------
# 2. Analysis (FFmpeg silence detection + PySceneDetect scene changes)
# --------------------------------------------------------------------------

def probe_duration(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def _run_ffmpeg_with_progress(cmd, total_seconds, progress, cancel, line_cb=None):
    """
    Run an ffmpeg command built with `-progress pipe:2 -nostats`.
    Parses out_time from stderr for progress; passes every stderr line to line_cb.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    tail = []
    try:
        for line in proc.stderr:
            line = line.strip()
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)
            if cancel.is_set():
                proc.kill()
                raise Cancelled()
            m = re.match(r"out_time_us=(\d+)", line)
            if m and total_seconds > 0:
                progress(min(1.0, int(m.group(1)) / 1e6 / total_seconds))
            if line_cb:
                line_cb(line)
    finally:
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail[-8:]))


def find_silences(path, noise_db, min_dur, total_seconds, progress, cancel):
    """
    Silent stretches as (start, end) seconds. Audio only, so it is fast.
    noise_db: anything quieter than this counts as silence (e.g. -30).
    """
    starts, ends = [], []

    def on_line(line):
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            starts.append(float(m.group(1)))
        m = re.search(r"silence_end: (-?[\d.]+)", line)
        if m:
            ends.append(float(m.group(1)))

    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:2",
           "-i", str(path), "-vn",
           "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
           "-f", "null", "-"]
    _run_ffmpeg_with_progress(cmd, total_seconds, progress, cancel, on_line)
    return list(zip(starts, ends))


def find_scene_cuts(path, total_seconds, progress, cancel, frame_skip=2, threshold=27.0):
    """
    Scene-change times in seconds, via PySceneDetect running in a subprocess
    (so it can be killed on cancel and report progress without blocking).
    """
    cmd = [sys.executable, str(Path(__file__).resolve()), "--scene-worker",
           str(path), str(frame_skip), str(threshold)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    result = None
    try:
        for line in proc.stdout:
            if cancel.is_set():
                proc.kill()
                raise Cancelled()
            if line.startswith("PROGRESS "):
                progress(float(line.split()[1]))
            elif line.startswith("RESULT "):
                result = json.loads(line[7:])
    finally:
        proc.wait()
    if proc.returncode != 0 or result is None:
        err = proc.stderr.read()[-800:]
        raise RuntimeError("scene detection failed:\n" + err)
    return result


def _scene_worker(path, frame_skip, threshold):
    """Child-process entry point for find_scene_cuts."""
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(path)
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold))
    total = video.duration.get_seconds() if video.duration else 0
    done = threading.Event()

    def report():
        while not done.wait(0.5):
            if total > 0:
                print(f"PROGRESS {min(1.0, video.position.get_seconds() / total):.4f}", flush=True)

    threading.Thread(target=report, daemon=True).start()
    manager.detect_scenes(video, frame_skip=frame_skip, show_progress=False)
    done.set()
    scenes = manager.get_scene_list()
    # Each scene's start time is a cut point, except the very first scene (t=0).
    cuts = [start.get_seconds() for start, _ in scenes[1:]]
    print("PROGRESS 1.0", flush=True)
    print("RESULT " + json.dumps(cuts), flush=True)


# --------------------------------------------------------------------------
# 3. Planning: choose where each short starts and ends
# --------------------------------------------------------------------------

def _pick_end(lo, hi, silence_cuts, scene_cuts):
    """Latest good cut point in [lo, hi]. Prefer silence, then scene change."""
    for label, pool in (("silence", silence_cuts), ("scene change", scene_cuts)):
        window = [c for c in pool if lo <= c <= hi]
        if window:
            return window[-1], label
    return hi, "hard cut"


def plan_segments(total, silence_cuts, scene_cuts, min_len, max_len):
    """Split [0, total] into clips of min_len..max_len seconds at natural points."""
    segments, start = [], 0.0
    while total - start > max_len:
        end, reason = _pick_end(start + min_len, start + max_len, silence_cuts, scene_cuts)
        segments.append((start, end, reason))
        start = end
    segments.append((start, total, "end of video"))
    segments = [s for s in segments if s[1] - s[0] >= min(min_len, 5)]
    return [{"n": i, "start": round(s, 2), "end": round(e, 2),
             "dur": round(e - s, 2), "reason": r}
            for i, (s, e, r) in enumerate(segments, 1)]


# --------------------------------------------------------------------------
# 4. Cutting (FFmpeg), with optional vertical 9:16 conversion for Shorts
# --------------------------------------------------------------------------

_VERTICAL_BLUR = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
    "boxblur=10:2,scale=1080:1920,setsar=1[bg2];"
    "[fg]scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1[fg2];"
    "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
)
_VERTICAL_CROP = (
    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,setsar=1,format=yuv420p[v]"
)

_encoder_cache = {}


def _video_encoder():
    """Hardware H.264 on Apple Silicon when available, else libx264."""
    if "enc" not in _encoder_cache:
        res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True)
        _encoder_cache["enc"] = "h264_videotoolbox" if "h264_videotoolbox" in res.stdout else "libx264"
    return _encoder_cache["enc"]


def _encode_args(encoder):
    if encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]


def cut_clip(source, start, end, out_path, fmt, progress, cancel):
    """
    Re-encode one segment to out_path. fmt: 'blur' (9:16 with blurred
    background), 'crop' (9:16 center crop) or 'original'.
    """
    duration = end - start
    filt = {"blur": _VERTICAL_BLUR, "crop": _VERTICAL_CROP}.get(fmt)
    tmp = out_path.with_suffix(".part.mp4")

    def build(encoder):
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:2", "-y",
               "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
        if filt:
            cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "0:a?"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a?", "-pix_fmt", "yuv420p"]
        cmd += _encode_args(encoder)
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-movflags", "+faststart", "-r", "30", str(tmp)]
        return cmd

    encoder = _video_encoder()
    try:
        _run_ffmpeg_with_progress(build(encoder), duration, progress, cancel)
    except RuntimeError:
        if encoder == "libx264":
            raise
        _encoder_cache["enc"] = "libx264"  # hardware encoder failed; fall back for good
        _run_ffmpeg_with_progress(build("libx264"), duration, progress, cancel)
    tmp.replace(out_path)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--scene-worker":
        _scene_worker(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
    else:
        print("This module is used by app.py. Run: ./run.sh")
