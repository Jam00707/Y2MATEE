# Y2MATEE — YouTube → MP4 / MP3 downloader

A small, self-hosted web app in the spirit of the classic "paste a link → pick MP3/MP4 → Convert"
converter sites, but running **entirely on your own machine**: FastAPI backend + `yt-dlp` + `ffmpeg`,
with a single-page frontend (no frameworks, no CDN, works offline).

```
ytdownloader/
├── server.py                  # FastAPI app: /api/info, /api/convert, /api/progress/<id>, /api/download/<id>
├── static/index.html          # the whole UI (inline CSS + JS, EN/UR language toggle)
├── setup.sh                   # one-command install & run
├── requirements.txt
├── .github/workflows/ci.yml   # smoke test on push
├── bin/                       # ffmpeg + ffprobe (downloaded by setup.sh, git-ignored)
└── downloads/                 # generated files, auto-deleted after 1 hour (git-ignored)
```

---

## 🚀 Quick start (one command)

```bash
./setup.sh          # creates .venv, installs deps, fetches ffmpeg, starts :8000
```

Then open <http://localhost:8000>. Manual install is in section 1 below.

---

## 1. Install

You need **Python 3.10+**, **Node.js** (used by yt-dlp to solve YouTube's JS challenges — without it
some formats are missing or extraction fails) and **ffmpeg**.

```bash
# python deps
pip install -r requirements.txt

# ffmpeg (pick one)
#   Linux (static build, no root needed):
mkdir -p ~/.local/bin && cd /tmp
curl -LO https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg-release-amd64-static.tar.xz
cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe ~/.local/bin/
#   Debian/Ubuntu:  sudo apt install ffmpeg
#   macOS:          brew install ffmpeg
#   Windows:        winget install Gyan.FFmpeg

# verify
ffmpeg -version | head -1
node --version
```

`server.py` finds ffmpeg automatically in `~/.local/bin` or on `PATH`.

## 2. Run

This repo ships with a virtualenv and a bundled ffmpeg, so on this machine you can just do:

```bash
cd ytdownloader
.venv/bin/python server.py       # http://0.0.0.0:8000
```

Fresh machine (no `.venv`, no `bin/`):

```bash
cd ytdownloader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Or with auto-reload while developing:

```bash
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Open <http://localhost:8000>.

`server.py` looks for ffmpeg in `ytdownloader/bin/` first, then `~/.local/bin`, then `PATH`.
It also uses `node` (if installed) as yt-dlp's JS runtime — without it YouTube extraction is
unreliable and some qualities go missing.

> **Note on the Arena preview:** the in-app file/preview pane is a sandboxed iframe with no network
> access, so `fetch()` calls to the API are blocked there. Open the app in a real browser tab
> (the live-preview URL, or `localhost:8000` when running locally) and everything works.

## 3. How it works

| Step | Endpoint | What happens |
|---|---|---|
| 1 | `POST /api/info` | yt-dlp extracts metadata only → title, channel, duration, views, thumbnail (embedded as a base64 data URI so it shows even offline) and the list of available qualities. |
| 2 | `POST /api/convert` | Creates a job and runs yt-dlp in a background thread pool (max 2 concurrent). |
| 3 | `GET /api/progress/<job>` | Polled by the UI every 0.9 s → percent, speed, ETA, state. |
| 4 | `GET /api/download/<job>` | Streams the finished file with a clean `Content-Disposition` filename (`<video title>.mp4/.mp3`). |

**MP4:** `bestvideo[height<=H][ext=mp4]+bestaudio[ext=m4a]/…` merged by ffmpeg into a single `.mp4`,
with metadata tags written in.

**MP3:** `bestaudio` → `FFmpegExtractAudio` at 128 / 192 / 256 / 320 kbps + `FFmpegMetadata`.

**Reliability details**

* `js_runtimes: {"node": {...}}` — enables yt-dlp's JS challenge solving (otherwise YouTube returns
  403 or hides formats).
* `concurrent_fragment_downloads: 1` — parallel fragment fetching from a datacenter IP reliably
  triggers `HTTP 403: Forbidden`; sequential is slower but works.
* Every job retries up to 3× on 403 / 429 / timeouts, wiping `.part` files and re-extracting fresh
  stream URLs between attempts.
* Friendly error messages for private, members-only, age-restricted, deleted and non-YouTube links.
* A background cleaner deletes files and job records older than 1 hour; the sandbox has limited disk.

## 4. Config knobs (top of `server.py`)

| Constant | Default | Meaning |
|---|---|---|
| `MP3_BITRATES` | 128–320 | qualities offered for MP3 |
| `MP4_HEIGHTS` | 144–2160 | qualities offered for MP4 (filtered by what the video actually has) |
| `MAX_DURATION` | 3 h | longer videos are rejected |
| `MAX_CONCURRENT_JOBS` | 2 | parallel conversions |
| `JOB_TTL` | 1 h | how long files stay on disk |

## 5. Troubleshooting

### "The conversion finishes but nothing downloads"

Almost always an **iframe sandbox** issue, not a server issue. Check the server log:

```
GET /api/convert …      200    ← job created
GET /api/progress/xxx … 200    ← finished, file written to downloads/
GET /api/download/xxx … ???    ← if this line is MISSING, the browser never asked for the file
```

If the `GET /api/download/...` line never appears, the page is running inside a restricted frame
(e.g. Arena's in-app preview uses `sandbox="allow-scripts"` without `allow-downloads` or
`allow-same-origin`), so the browser silently blocks the download. Fixes, in order of preference:

1. **Open the app in a real browser tab** — the live-preview URL, or `http://localhost:8000`.
2. Use the **“Open in new tab”** button next to Download (it points straight at `api/download/<job>`).
3. Right-click / long-press the download link → *Save link as…*

The UI detects this situation automatically (`SANDBOXED` flag in `index.html`) and shows a blue
banner plus a fallback direct link.

To confirm the backend is healthy independent of the browser:

```bash
curl -i -o /tmp/test.bin "http://localhost:8000/api/download/<job-id>"
```

### Other errors

| Symptom | Cause / fix |
|---|---|
| `HTTP Error 403: Forbidden` | YouTube throttling. The server already retries 3× with fresh URLs; if it persists, wait a minute or lower the quality. |
| `No supported JavaScript runtime could be found` | Install Node.js — without it yt-dlp can't solve YouTube's JS challenges and formats go missing. |
| `ffmpeg not found` | Put it in `ytdownloader/bin/`, `~/.local/bin`, or on `PATH`. |
| Job vanishes after a while | Files and job records are deleted after `JOB_TTL` (1 h) — convert again. |
| Age-restricted / members-only video | Needs cookies: run yt-dlp with `--cookies-from-browser` or export a `cookies.txt` and add `"cookiefile"` to `base_opts()`. |

## 6. Ideas to extend it

- Playlist support (currently only the first entry of a playlist link is used).
- `--cookies-from-browser` / a cookies file to unlock age-restricted or membership videos.
- WebM/Opus or WAV output, thumbnail embedding into the MP3 (`EmbedThumbnail` post-processor).
- Subtitle download (`.srt` / `.vtt`).
- SQLite job history instead of the in-memory dict, and a queue page for bulk links.

---

## ⚠️ Legal / responsible use

Downloading from YouTube generally **violates YouTube's Terms of Service** and may infringe the
copyright of the uploader. Keep this tool for **personal, offline use** of content you own, that is
openly licensed (e.g. Creative Commons), or that you have explicit permission to save. Do not run it
as a public service, do not monetise it, and never re-upload or sell what you download. This project
is not affiliated with YouTube or Google.
