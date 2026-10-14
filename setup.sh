#!/usr/bin/env bash
# Y2MATEE — one-command setup.
# Creates the virtualenv, installs Python deps, fetches a static ffmpeg/ffprobe
# into ./bin, sanity-checks everything, then starts the server on :8000.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PY="${PYTHON:-python3}"
FF_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

info()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
command -v "$PY" >/dev/null 2>&1 || die "python3 not found. Install Python 3.10+ first."
PYV=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
info "Python $PYV"

if command -v node >/dev/null 2>&1; then
  info "Node.js $(node --version)  (yt-dlp JS runtime — good)"
else
  warn "Node.js not found. YouTube extraction will be unreliable and some"
  warn "qualities will be missing. Install it:  https://nodejs.org  /  apt install nodejs"
fi

# ---------------------------------------------------------------- venv
if [ ! -x .venv/bin/python ]; then
  info "Creating virtualenv (.venv)"
  "$PY" -m venv .venv
fi
info "Installing Python dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# ---------------------------------------------------------------- ffmpeg
mkdir -p bin downloads
touch bin/.gitkeep downloads/.gitkeep

have_ffmpeg() { [ -x bin/ffmpeg ] || command -v ffmpeg >/dev/null 2>&1; }

if ! have_ffmpeg; then
  info "Downloading static ffmpeg + ffprobe into ./bin"
  case "$(uname -sm)" in
    "Linux x86_64")
      tmp=$(mktemp -d)
      curl -fL --retry 3 -o "$tmp/ff.tar.xz" "$FF_URL"
      tar -xf "$tmp/ff.tar.xz" -C "$tmp"
      d=$(find "$tmp" -maxdepth 1 -type d -name 'ffmpeg-*-static' | head -1)
      [ -n "$d" ] || die "Could not unpack ffmpeg."
      cp "$d/ffmpeg" "$d/ffprobe" bin/
      chmod +x bin/ffmpeg bin/ffprobe
      rm -rf "$tmp"
      ;;
    *)
      warn "No static build for $(uname -sm). Install ffmpeg yourself:"
      warn "  Debian/Ubuntu : sudo apt install ffmpeg"
      warn "  macOS         : brew install ffmpeg"
      warn "  Windows       : winget install Gyan.FFmpeg"
      ;;
  esac
fi

if [ -x bin/ffmpeg ]; then info "$(bin/ffmpeg -version | head -1)"; else info "ffmpeg: $(ffmpeg -version | head -1)"; fi

# ---------------------------------------------------------------- smoke test
info "Import check"
.venv/bin/python -c "import server; print('   ffmpeg ->', server.FFMPEG); print('   node   ->', server.NODE_BIN)"

cat <<EOF

$(printf '\033[1;32mReady.\033[0m')  Starting Y2MATEE on port $PORT …

    →  http://localhost:$PORT

Press Ctrl+C to stop.

EOF

exec .venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port "$PORT"
