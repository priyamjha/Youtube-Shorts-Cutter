"""
Shorts Cutter: local web UI + API.

Run with ./run.sh (or .venv/bin/python app.py) and open http://127.0.0.1:5055.
Jobs run in background threads; state is saved to data/jobs.json so planned
shorts survive a restart and can be exported in batches later.
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import cutter

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
JOBS_FILE = DATA_DIR / "jobs.json"
OUTPUT_DIR = Path.home() / "shorts-output"   # where finished shorts go
PORT = 5055

DEFAULT_SETTINGS = {
    "max_len": 60,       # seconds; YouTube Shorts allow up to 180
    "min_len": 15,
    "scenes": True,      # run PySceneDetect (slow on long videos, better cuts)
    "noise_db": -30,     # quieter than this = silence
    "quality": 1080,     # download height cap: 720 or 1080
    "format": "blur",    # blur | crop | original
}

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
jobs = {}                 # id -> job dict
lock = threading.RLock()
cancels = {}              # id -> threading.Event
workers = {}              # id -> Thread


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def save_jobs():
    with lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(jobs.values()), indent=1))
        tmp.replace(JOBS_FILE)


def load_jobs():
    if not JOBS_FILE.exists():
        return
    for job in json.loads(JOBS_FILE.read_text()):
        # Anything that was mid-run when the app last stopped is no longer running.
        if job["status"] in ("exporting",):
            job["status"] = "ready"
            job["export"] = None
            job["message"] = "Export was interrupted; export again to continue."
        elif job["status"] not in ("ready", "done", "error", "cancelled"):
            job["status"] = "error"
            job["error"] = "Interrupted by app restart. Click Retry."
        job["progress"] = 0
        job["eta"] = None
        jobs[job["id"]] = job
        refresh_exported(job)


def safe_name(title):
    name = re.sub(r"[^\w\s\-\.\(\)\[\]]", "", title).strip()
    name = re.sub(r"\s+", " ", name)
    return (name or "video")[:80]


# --------------------------------------------------------------------------
# Job helpers
# --------------------------------------------------------------------------

def new_job(url, settings):
    return {
        "id": uuid.uuid4().hex[:10],
        "url": url,
        "created": time.time(),
        "status": "queued",
        "phase": "Queued",
        "progress": 0.0,
        "eta": None,
        "detail": "",
        "message": "",
        "error": "",
        "settings": settings,
        "video": None,
        "file": None,
        "analysis": None,
        "segments": [],
        "exported": [],
        "export": None,
        "out_dir": None,
        "timings": {},
        "log": [],
    }


def log(job, text):
    job["log"].append(f"{time.strftime('%H:%M:%S')}  {text}")
    job["log"] = job["log"][-200:]


def set_state(job, **fields):
    with lock:
        job.update(fields)


def refresh_exported(job):
    """Which short numbers already exist on disk, with sizes."""
    out = job.get("out_dir")
    found = {}
    if out and Path(out).is_dir():
        for p in Path(out).glob("short_*.mp4"):
            m = re.match(r"short_(\d+)\.mp4$", p.name)
            if m:
                found[int(m.group(1))] = p.stat().st_size
    job["exported"] = sorted(found)
    job["sizes"] = {str(k): v for k, v in found.items()}
    if job["segments"] and len(job["exported"]) >= len(job["segments"]) and job["status"] == "ready":
        job["status"] = "done"


def fmt_time(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Worker: analyze (download + silence + scenes + plan)
# --------------------------------------------------------------------------

def analyze_worker(job_id):
    job = jobs[job_id]
    cancel = cancels[job_id]
    st = job["settings"]

    def phase(name, status):
        set_state(job, status=status, phase=name, progress=0.0, eta=None, detail="")
        log(job, name)
        save_jobs()

    def make_progress(t0):
        last = {"save": 0}

        def progress(frac, **extra):
            elapsed = time.time() - t0
            eta = extra.get("eta")
            if eta is None and frac > 0.02:
                eta = elapsed / frac - elapsed
            detail = ""
            if extra.get("speed"):
                detail = f"{extra['speed'] / 1e6:.1f} MB/s"
            if extra.get("total"):
                detail += f"  {extra['downloaded'] / 1e6:.0f} / {extra['total'] / 1e6:.0f} MB"
            if extra.get("part") and extra.get("part") <= 2:
                detail += f"  (part {extra['part']}/2: {'video' if extra['part'] == 1 else 'audio'})"
            set_state(job, progress=frac, eta=eta, detail=detail.strip())
            if time.time() - last["save"] > 2:
                last["save"] = time.time()
                save_jobs()
        return progress

    try:
        missing = cutter.check_tools()
        if missing:
            raise RuntimeError(f"Missing tools: {', '.join(missing)}. Run: brew install ffmpeg")

        # 1. Info
        phase("Fetching video info", "fetching")
        info = cutter.fetch_info(job["url"])
        set_state(job, video=info)
        if info["live_status"] == "is_upcoming":
            raise RuntimeError("This stream hasn't started yet. Try again once it is live or finished.")
        if info["is_live"]:
            log(job, "Live stream: recording from the start; download finishes when the stream ends.")
        log(job, f"{info['title']}  ({fmt_time(info['duration'])})")
        save_jobs()

        # 2. Download (skip if we already have the file)
        existing = sorted(p for p in DOWNLOAD_DIR.glob(f"{info['id']}.*")
                          if p.suffix in cutter.VIDEO_EXTS) if DOWNLOAD_DIR.exists() else []
        if existing and not info["is_live"]:
            path = existing[0]
            log(job, f"Already downloaded: {path.name}")
        else:
            phase("Downloading video", "downloading")
            t0 = time.time()
            path = cutter.download(job["url"], DOWNLOAD_DIR, st["quality"], make_progress(t0), cancel)
            job["timings"]["download"] = time.time() - t0
            log(job, f"Downloaded in {fmt_time(time.time() - t0)}")
        set_state(job, file={"path": str(path), "size": path.stat().st_size})

        total = cutter.probe_duration(path)
        job["video"]["duration"] = total

        # 3. Silence detection
        phase("Finding pauses (silence detection)", "silence")
        t0 = time.time()
        sil = cutter.find_silences(path, st["noise_db"], 0.4, total, make_progress(t0), cancel)
        silence_cuts = [(s + e) / 2 for s, e in sil]
        job["timings"]["silence"] = time.time() - t0
        log(job, f"{len(silence_cuts)} pauses found in {fmt_time(time.time() - t0)}")

        # 4. Scene detection
        scene_cuts = []
        if st["scenes"]:
            phase("Finding scene changes (PySceneDetect)", "scenes")
            t0 = time.time()
            scene_cuts = cutter.find_scene_cuts(path, total, make_progress(t0), cancel)
            job["timings"]["scenes"] = time.time() - t0
            log(job, f"{len(scene_cuts)} scene changes found in {fmt_time(time.time() - t0)}")

        # 5. Plan
        phase("Planning shorts", "planning")
        segs = cutter.plan_segments(total, silence_cuts, scene_cuts, st["min_len"], st["max_len"])
        reasons = {}
        for s in segs:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        out_dir = OUTPUT_DIR / f"{safe_name(info['title'])} [{info['id']}]"
        out_dir.mkdir(parents=True, exist_ok=True)
        set_state(job, segments=segs, out_dir=str(out_dir),
                  analysis={"silences": len(silence_cuts), "scenes": len(scene_cuts),
                            "reasons": reasons, "total": total},
                  status="ready", phase="Ready to export", progress=1.0, eta=None, detail="",
                  message=f"{len(segs)} shorts planned. Choose which ones to export.")
        refresh_exported(job)
        log(job, f"{len(segs)} shorts planned → {out_dir}")
    except cutter.Cancelled:
        set_state(job, status="cancelled", phase="Cancelled", eta=None, detail="")
        log(job, "Cancelled")
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI
        set_state(job, status="error", phase="Error", error=str(e)[-600:], eta=None, detail="")
        log(job, f"ERROR: {e}")
    finally:
        save_jobs()
        workers.pop(job_id, None)


# --------------------------------------------------------------------------
# Worker: export a range of shorts
# --------------------------------------------------------------------------

def export_worker(job_id, numbers):
    job = jobs[job_id]
    cancel = cancels[job_id]
    source = Path(job["file"]["path"])
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = job["settings"]["format"]
    seg_by_n = {s["n"]: s for s in job["segments"]}
    t0 = time.time()
    total_secs = sum(seg_by_n[n]["dur"] for n in numbers)
    done_secs = 0.0

    export = {"numbers": numbers, "total": len(numbers), "done": 0, "current": None,
              "progress": 0.0, "eta": None, "started": t0, "failed": []}
    set_state(job, status="exporting", phase="Exporting shorts", export=export, error="")
    log(job, f"Exporting {len(numbers)} shorts ({numbers[0]}–{numbers[-1]}), format: {fmt}")
    save_jobs()

    try:
        for n in numbers:
            seg = seg_by_n[n]
            out_path = out_dir / f"short_{n:03d}.mp4"
            export["current"] = n

            def progress(frac, _seg=seg):
                secs = done_secs + frac * _seg["dur"]
                overall = secs / total_secs if total_secs else 0
                elapsed = time.time() - t0
                eta = elapsed / overall - elapsed if overall > 0.02 else None
                export.update(progress=overall, eta=eta, clip_progress=frac)
                set_state(job, progress=overall, eta=eta,
                          detail=f"short {n} of {len(seg_by_n)}  ({export['done'] + 1}/{len(numbers)} in this batch)")

            try:
                cutter.cut_clip(source, seg["start"], seg["end"], out_path, fmt, progress, cancel)
            except cutter.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                export["failed"].append(n)
                log(job, f"short_{n:03d} failed: {str(e)[-200:]}")
            done_secs += seg["dur"]
            export["done"] += 1
            refresh_exported(job)
            if export["done"] % 3 == 0:
                save_jobs()
        elapsed = time.time() - t0
        msg = f"Exported {export['done'] - len(export['failed'])} shorts in {fmt_time(elapsed)}"
        if export["failed"]:
            msg += f" ({len(export['failed'])} failed)"
        log(job, msg)
        set_state(job, status="ready", phase="Ready to export", message=msg, progress=1.0,
                  eta=None, detail="", export=None)
        refresh_exported(job)
    except cutter.Cancelled:
        for p in out_dir.glob("*.part.mp4"):
            p.unlink(missing_ok=True)
        refresh_exported(job)
        set_state(job, status="ready", phase="Ready to export", export=None, eta=None, detail="",
                  message=f"Export stopped. {len(job['exported'])} shorts exported so far.")
        log(job, "Export cancelled")
    except Exception as e:  # noqa: BLE001
        set_state(job, status="error", phase="Export error", error=str(e)[-600:], export=None)
        log(job, f"ERROR: {e}")
    finally:
        save_jobs()
        workers.pop(job_id, None)


def start_thread(job_id, target, *args):
    cancels[job_id] = threading.Event()
    t = threading.Thread(target=target, args=(job_id, *args), daemon=True)
    workers[job_id] = t
    t.start()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    return jsonify({
        "tools": {"ffmpeg": shutil.which("ffmpeg") is not None,
                  "encoder": cutter._video_encoder() if shutil.which("ffmpeg") else None},
        "output_dir": str(OUTPUT_DIR),
        "defaults": DEFAULT_SETTINGS,
        "free_gb": round(shutil.disk_usage(Path.home()).free / 1e9, 1),
    })


@app.get("/api/jobs")
def api_jobs():
    with lock:
        return jsonify(sorted(jobs.values(), key=lambda j: -j["created"]))


@app.post("/api/jobs")
def api_create():
    body = request.get_json(force=True)
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify({"error": "Paste a YouTube link (starting with http)."}), 400
    st = dict(DEFAULT_SETTINGS)
    for k in st:
        if k in (body.get("settings") or {}):
            st[k] = (body["settings"][k])
    st["max_len"] = max(5, min(180, int(st["max_len"])))
    st["min_len"] = max(3, min(st["max_len"] - 1, int(st["min_len"])))
    st["noise_db"] = max(-60, min(-10, int(st["noise_db"])))
    st["quality"] = 720 if int(st["quality"]) <= 720 else 1080
    st["scenes"] = bool(st["scenes"])
    st["format"] = st["format"] if st["format"] in ("blur", "crop", "original") else "blur"

    job = new_job(url, st)
    with lock:
        jobs[job["id"]] = job
    save_jobs()
    start_thread(job["id"], analyze_worker)
    return jsonify(job)


def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    return jsonify(get_job(job_id))


@app.post("/api/jobs/<job_id>/export")
def api_export(job_id):
    job = get_job(job_id)
    if job["status"] not in ("ready", "done"):
        return jsonify({"error": f"Job is {job['status']}, not ready to export."}), 400
    if job_id in workers:
        return jsonify({"error": "This job is already running."}), 400
    body = request.get_json(force=True)
    n_total = len(job["segments"])
    if body.get("all"):
        lo, hi = 1, n_total
    else:
        try:
            lo, hi = int(body["from"]), int(body["to"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "Give a range like from 1 to 10."}), 400
    lo, hi = max(1, min(lo, hi)), min(n_total, max(lo, hi))
    if lo > n_total:
        return jsonify({"error": f"Only {n_total} shorts exist."}), 400
    numbers = [n for n in range(lo, hi + 1)]
    if not body.get("overwrite"):
        numbers = [n for n in numbers if n not in job["exported"]]
    if not numbers:
        return jsonify({"error": "Those shorts are already exported. Tick 'overwrite' to redo them."}), 400
    start_thread(job_id, export_worker, numbers)
    return jsonify({"ok": True, "count": len(numbers)})


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id):
    get_job(job_id)
    if job_id in cancels:
        cancels[job_id].set()
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/retry")
def api_retry(job_id):
    job = get_job(job_id)
    if job_id in workers:
        return jsonify({"error": "Already running."}), 400
    set_state(job, status="queued", error="", message="", segments=[], export=None)
    save_jobs()
    start_thread(job_id, analyze_worker)
    return jsonify({"ok": True})


@app.delete("/api/jobs/<job_id>")
def api_delete(job_id):
    job = get_job(job_id)
    if job_id in cancels:
        cancels[job_id].set()
    t = workers.get(job_id)
    if t:
        t.join(timeout=10)
    remove_files = request.args.get("files") == "1"
    with lock:
        jobs.pop(job_id, None)
    if remove_files:
        if job.get("file"):
            Path(job["file"]["path"]).unlink(missing_ok=True)
        if job.get("out_dir") and Path(job["out_dir"]).is_dir():
            shutil.rmtree(job["out_dir"], ignore_errors=True)
    save_jobs()
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/open")
def api_open(job_id):
    job = get_job(job_id)
    target = job.get("out_dir") or str(OUTPUT_DIR)
    Path(target).mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", target])
    else:
        subprocess.Popen(["explorer", target])
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/clip/<int:n>")
def api_clip(job_id, n):
    job = get_job(job_id)
    path = Path(job["out_dir"]) / f"short_{n:03d}.mp4"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.post("/api/open-output")
def api_open_output():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["open", str(OUTPUT_DIR)] if sys.platform == "darwin" else ["xdg-open", str(OUTPUT_DIR)])
    return jsonify({"ok": True})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    load_jobs()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n🎬 Shorts Cutter running at {url}\n   Shorts are saved to: {OUTPUT_DIR}\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
