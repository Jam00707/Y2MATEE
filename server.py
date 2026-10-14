#!/usr/bin/env python3
"""
YT -> MP4 / MP3 downloader  (FastAPI + yt-dlp + ffmpeg)
Personal / offline use only. Respect copyright & YouTube's Terms of Service.
"""
from __future__ import annotations

import base64
import io
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DL_DIR = BASE_DIR / "downloads"
DL_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_BIN = BASE_DIR / "bin"          # bundled ffmpeg (survives sandbox restarts)
LOCAL_BIN = Path.home() / ".local" / "bin"


def _find(name: str) -> str:
    for cand in (PROJECT_BIN / name, LOCAL_BIN / name):
        if cand.exists():
            return str(cand)
    return shutil.which(name) or name


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")

# A JS runtime makes YouTube's signature/N-token solving work reliably (and unlocks all formats).
NODE_BIN = shutil.which("node") or shutil.which("nodejs")
DENO_BIN = shutil.which("deno")

MP3_BITRATES = ["128", "192", "256", "320"]
MP4_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
MAX_DURATION = 3 * 60 * 60          # 3 hours
MAX_CONCURRENT_JOBS = 2
JOB_TTL = 60 * 60                   # keep finished files for 1 hour
CLEAN_EVERY = 10 * 60

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com", "music.youtube.com")

app = FastAPI(title="Y2MATEE — YouTube to MP4 / MP3", docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_cache(request, call_next):
    """Never let a browser (or proxy) cache API/download responses."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS + 2)
sem = threading.Semaphore(MAX_CONCURRENT_JOBS)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def base_opts() -> dict:
    o = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": FFMPEG,
        "socket_timeout": 25,
        "retries": 6,
        "fragment_retries": 6,
        "extractor_retries": 3,
        "file_access_retries": 3,
        # YouTube often answers 403 when fragments are fetched in parallel from a datacenter IP.
        "concurrent_fragment_downloads": 1,
        "http_chunk_size": None,
        "noplaylist": True,
    }
    runtimes = {}
    if NODE_BIN:
        runtimes["node"] = {"path": NODE_BIN}
    if DENO_BIN:
        runtimes["deno"] = {"path": DENO_BIN}
    if runtimes:
        o["js_runtimes"] = runtimes
    return o


def friendly_error(msg: str) -> str:
    m = msg.lower()
    if "private video" in m:
        return "This video is private."
    if "members-only" in m or "join this channel" in m:
        return "This video is members-only."
    if "age" in m and ("restrict" in m or "confirm" in m):
        return "This video is age-restricted — it can't be fetched without signing in."
    if "video unavailable" in m or "not available" in m or "invalid video id" in m or "does not exist" in m:
        return "Video unavailable — please check the link (the ID may be wrong or the video deleted)."
    if "playlist" in m and "no video id" in m:
        return "That looks like a playlist/channel link — please use a single video link."
    if "403" in m:
        return "YouTube throttled this request (HTTP 403). Please try again in a minute."
    if "429" in m or "too many requests" in m:
        return "YouTube is rate-limiting this server right now. Try again later."
    if "sign in" in m or "login" in m:
        return "YouTube is asking this server to sign in. Try again later."
    if "unsupported url" in m or "no video" in m:
        return "That link isn't a YouTube video."
    return "Could not process that video. Check the link and try again."


def is_youtube(url: str) -> bool:
    return any(h in url.lower() for h in YOUTUBE_HOSTS)


def human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "—"


def thumb_data_uri(url: str | None, max_w: int = 480) -> str | None:
    """Fetch the thumbnail server-side -> small base64 data URI (works in offline previews)."""
    if not url:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=12).read()
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if img.width > max_w:
                img = img.resize((max_w, int(img.height * max_w / img.width)))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=72, optimize=True)
            raw = buf.getvalue()
        except Exception:
            pass
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    except Exception:
        return None


def cleanup() -> None:
    now = time.time()
    try:
        for f in DL_DIR.iterdir():
            try:
                if f.is_file() and now - f.stat().st_mtime > JOB_TTL:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
        with jobs_lock:
            for jid in [k for k, v in jobs.items() if now - v.get("created", now) > JOB_TTL]:
                jobs.pop(jid, None)
    except Exception:
        pass


def cleanup_loop() -> None:
    while True:
        time.sleep(CLEAN_EVERY)
        cleanup()


threading.Thread(target=cleanup_loop, daemon=True).start()


def safe_name(s: str, limit: int = 70) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "video").strip().rstrip(".")
    return (s[:limit] or "video").strip()


# --------------------------------------------------------------------------- #
#  Models
# --------------------------------------------------------------------------- #
class InfoReq(BaseModel):
    url: str


class ConvertReq(BaseModel):
    url: str
    fmt: str = "mp4"          # mp4 | mp3
    quality: str = "720"      # mp4: height (e.g. "1080") | mp3: bitrate (e.g. "192")


# --------------------------------------------------------------------------- #
#  API
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health():
    return {"ok": True, "ffmpeg": Path(FFMPEG).exists(), "yt_dlp": yt_dlp.version.__version__}


@app.post("/api/info")
async def info(req: InfoReq):
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "Please paste a YouTube link first.")
    if not is_youtube(url):
        raise HTTPException(400, "That doesn't look like a YouTube link.")

    opts = base_opts() | {"skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400 if "403" not in str(e) else 502, friendly_error(str(e)))
    except Exception as e:
        raise HTTPException(502, friendly_error(str(e)))

    if d.get("_type") == "playlist":
        entries = [e for e in (d.get("entries") or []) if e]
        if not entries:
            raise HTTPException(400, "That playlist is empty.")
        d = entries[0]

    duration = int(d.get("duration") or 0)
    if duration > MAX_DURATION:
        raise HTTPException(400, f"Video too long (max {MAX_DURATION // 3600} h).")

    heights = sorted({
        int(f.get("height")) for f in (d.get("formats") or [])
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }, reverse=True)
    max_h = heights[0] if heights else 360

    thumbnails = d.get("thumbnails") or []
    thumb_url = thumbnails[-1].get("url") if thumbnails else d.get("thumbnail")

    return {
        "id": d.get("id"),
        "title": d.get("title") or "Untitled",
        "channel": d.get("channel") or d.get("uploader") or "Unknown",
        "duration": duration,
        "duration_text": f"{duration // 60}:{duration % 60:02d}",
        "views": d.get("view_count") or 0,
        "upload_date": d.get("upload_date") or "",
        "thumb_url": thumb_url,
        "thumb": thumb_data_uri(thumb_url),
        "max_height": max_h,
        "mp4_options": [h for h in MP4_HEIGHTS if h <= max_h] or [max_h],
        "mp3_options": MP3_BITRATES,
    }


def run_job(jid: str, url: str, fmt: str, quality: str) -> None:
    job = jobs[jid]
    tmpl = str(DL_DIR / f"{jid}.%(ext)s")

    def hook(d):
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job.update({
                "state": "downloading",
                "percent": round(done / total * 100, 1) if total else 0.0,
                "speed": d.get("speed") or 0,
                "eta": d.get("eta"),
                "frag": f"{d.get('fragment_index') or ''}/{d.get('fragment_count') or ''}".strip("/"),
            })
        elif st == "finished":
            job.update({"state": "converting", "percent": 99.0, "speed": 0, "eta": None, "frag": ""})

    opts = base_opts() | {
        "outtmpl": tmpl,
        "noplaylist": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [],
        "windowsfilenames": True,
        "restrictfilenames": False,
        "overwrites": True,
    }

    if fmt == "mp3":
        br = quality if quality in MP3_BITRATES else "192"
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": br,
        }, {
            "key": "FFmpegMetadata",
            "add_metadata": True,
        }]
    else:
        h = int(quality) if str(quality).isdigit() else 720
        opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}]/best"
        )
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{
            "key": "FFmpegMetadata",
            "add_metadata": True,
        }]

    def attempt(tries: int = 3) -> dict:
        """Run yt-dlp; on a 403/network hiccup, wipe partial files and retry with a fresh extraction."""
        last = None
        for i in range(tries):
            if i:
                job.update({"state": "starting", "percent": 0.0, "retry": i + 1})
                time.sleep(2.5 * i)
                for stale in DL_DIR.glob(f"{jid}.*"):
                    stale.unlink(missing_ok=True)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    d = ydl.extract_info(url, download=True)
                    if d and d.get("_type") == "playlist":
                        d = next((e for e in (d.get("entries") or []) if e), d)
                return d or {}
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).lower()
                retryable = any(k in msg for k in ("403", "429", "timed out", "timeout", "temporar",
                                                   "unable to download", "incomplete read", "connection"))
                if not retryable or i == tries - 1:
                    raise
        raise last  # pragma: no cover

    with sem:
        job.update({"state": "starting", "started": time.time()})
        try:
            d = attempt()

            files = sorted(DL_DIR.glob(f"{jid}.*"), key=lambda p: p.stat().st_size, reverse=True)
            files = [f for f in files if f.suffix.lower() not in (".part", ".ytdl", ".tmp")]
            if not files:
                raise RuntimeError("Conversion produced no file.")
            f = files[0]
            ext = f.suffix.lower().lstrip(".")
            if fmt == "mp3" and ext != "mp3":
                new = f.with_suffix(".mp3")
                shutil.move(str(f), str(new))
                f = new

            job.update({
                "state": "done",
                "percent": 100.0,
                "file": f.name,
                "ext": f.suffix.lower().lstrip("."),
                "size": f.stat().st_size,
                "size_text": human(f.stat().st_size),
                "title": (d or {}).get("title") or job.get("title") or "video",
                "finished": time.time(),
            })
        except Exception as e:  # noqa: BLE001
            job.update({"state": "error", "error": friendly_error(str(e))})
        finally:
            for stale in DL_DIR.glob(f"{jid}.*"):
                if stale.name != job.get("file"):
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError:
                        pass


@app.post("/api/convert")
async def convert(req: ConvertReq):
    url = (req.url or "").strip()
    fmt = (req.fmt or "mp4").lower().strip()
    quality = str(req.quality or "").strip()

    if not url or not is_youtube(url):
        raise HTTPException(400, "Paste a valid YouTube link.")
    if fmt not in ("mp3", "mp4"):
        raise HTTPException(400, "Format must be mp3 or mp4.")
    if fmt == "mp3" and quality not in MP3_BITRATES:
        quality = "192"
    if fmt == "mp4":
        quality = quality if quality.isdigit() else "720"

    with jobs_lock:
        active = sum(1 for j in jobs.values() if j.get("state") in ("queued", "starting", "downloading", "converting"))
        if active >= MAX_CONCURRENT_JOBS + 2:
            raise HTTPException(429, "Server busy — wait for the current downloads to finish.")

    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {
        "id": jid, "url": url, "fmt": fmt, "quality": quality,
        "state": "queued", "percent": 0.0, "speed": 0, "eta": None,
        "error": None, "file": None, "size": 0, "size_text": "—", "frag": "",
        "created": time.time(),
    }
    executor.submit(run_job, jid, url, fmt, quality)
    return {"job": jid}


@app.get("/api/progress/{jid}")
async def progress(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found (it may have expired).")
    out = {k: job.get(k) for k in
           ("id", "state", "percent", "speed", "eta", "error", "file", "ext",
            "size", "size_text", "title", "fmt", "quality", "frag")}
    out["speed_text"] = human(job.get("speed")) + "/s" if job.get("speed") else ""
    out["download_url"] = f"/api/download/{jid}" if job.get("state") == "done" else None
    return JSONResponse(out)


@app.get("/api/download/{jid}")
async def download(jid: str):
    job = jobs.get(jid)
    if not job or job.get("state") != "done" or not job.get("file"):
        raise HTTPException(404, "Nothing to download (yet).")
    f = DL_DIR / job["file"]
    if not f.exists():
        raise HTTPException(410, "File expired — please convert again.")
    name = f"{safe_name(job.get('title') or 'video')}.{job.get('ext') or f.suffix.lstrip('.')}"
    job["last_dl"] = time.time()
    return FileResponse(
        f,
        filename=name,
        media_type="application/octet-stream",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{name.encode("ascii", "ignore").decode() or "download"}"; '
                                   f"filename*=UTF-8''{name}",
            "X-File-Size": str(f.stat().st_size),
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
