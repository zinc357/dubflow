#!/usr/bin/env bash
# Package the Python engine into a standalone binary for the CURRENT platform
# (PyInstaller onefile). Output: dist/dubflow-engine(.exe)
# Cross-platform: macOS / Linux / Windows-GitBash. CI runs this too.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/engine"

.venv/bin/pip install -q pyinstaller

# platform-specific hidden imports / data
EXTRA=""
OS_TAG="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS_TAG" in
  mingw*|msys*|windows*)
    OS_TAG="win32"
    ;;
  darwin)
    if [ "$(uname -m)" = "arm64" ]; then
      EXTRA="--collect-all mlx --collect-all mlx_whisper"
    fi
    ;;
esac
ARCH_TAG="$(uname -m | tr '[:upper:]' '[:lower:]')"
case "$ARCH_TAG" in
  amd64|x64) ARCH_TAG="x86_64" ;;
  aarch64)   ARCH_TAG="arm64" ;;
esac
PLAT_TAG="${OS_TAG}-${ARCH_TAG}"
EXT=""
if [ "$OS_TAG" = "win32" ]; then EXT=".exe"; fi

.venv/bin/pyinstaller \
  --name dubflow-engine \
  --onefile \
  --collect-all uvicorn \
  $EXTRA \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \n  --hidden-import python_multipart \n  --hidden-import multipart \
  run_engine.py

# --- bundle ffmpeg/ffprobe next to the engine (frozen bin dir) ---
for b in ffmpeg ffprobe; do
  for cand in "$ROOT/bin/$b-$PLAT_TAG$EXT" "$ROOT/bin/$b$EXT"; do
    if [ -f "$cand" ]; then
      cp "$cand" "dist/$b-$PLAT_TAG$EXT"
      break
    fi
  done
done
# 通用名：mlx_whisper / faster-whisper 内部用裸名 "ffmpeg" 调用
if [ "$EXT" = "" ]; then
  ln -sf "ffmpeg-$PLAT_TAG"  "dist/ffmpeg"  2>/dev/null || true
  ln -sf "ffprobe-$PLAT_TAG" "dist/ffprobe" 2>/dev/null || true
else
  cp "dist/ffmpeg-$PLAT_TAG$EXT"  "dist/ffmpeg$EXT"  2>/dev/null || true
  cp "dist/ffprobe-$PLAT_TAG$EXT" "dist/ffprobe$EXT" 2>/dev/null || true
fi

# --- smoke test: the binary must actually start and answer /health ---
BIN="dist/dubflow-engine$EXT"
DUBFLOW_PORT=8799 "$BIN" > /tmp/dubflow-engine-smoke.log 2>&1 &
ENGINE_PID=$!
OK=0
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8799/health > /dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
if [ "$OK" != "1" ]; then
  echo "ENGINE SMOKE TEST FAILED:"
  tail -30 /tmp/dubflow-engine-smoke.log
  kill $ENGINE_PID 2>/dev/null || true
  exit 1
fi
curl -s http://127.0.0.1:8799/health
echo ""
kill $ENGINE_PID 2>/dev/null || true
echo "engine packaged and smoke-tested: $ROOT/engine/dist/dubflow-engine$EXT"
