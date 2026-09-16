#!/usr/bin/env bash
# Package the Python engine into a standalone binary for the CURRENT platform
# (PyInstaller onefile). Output: dist/dubflow-engine(.exe)
# CI (.github/workflows/release.yml) runs this on all three platforms.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/engine"

.venv/bin/pip install -q pyinstaller

# platform-specific hidden imports / data
EXTRA=""
if [[ "$(uname)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  # mlx ships metal libs that pyinstaller misses
  EXTRA="--collect-all mlx --collect-all mlx_whisper"
fi

.venv/bin/pyinstaller \
  --name dubflow-engine \
  --onefile \
  --collect-all uvicorn \
  --collect-all mlx_whisper $EXTRA \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  run_engine.py

# --- bundle ffmpeg/ffprobe next to the engine (frozen bin dir) ---
PLAT_TAG="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m | sed 's/amd64/x86_64/;s/aarch64/arm64/')"
EXT=""
if [[ "$(uname -s)" == *"MINGW"* || "$(uname -s)" == *"Windows"* ]]; then EXT=".exe"; fi
for b in ffmpeg ffprobe; do
  for cand in "$ROOT/bin/$b-$PLAT_TAG$EXT" "$ROOT/bin/$b$EXT"; do
    if [ -f "$cand" ]; then
      cp "$cand" "dist/$b-$PLAT_TAG$EXT"
      break
    fi
  done
done
# mlx_whisper 等库会用裸名 "ffmpeg" 调用，补通用名（unix 用符号链接）
if [ "$EXT" = "" ]; then
  ln -sf "ffmpeg-$PLAT_TAG"  "dist/ffmpeg"  2>/dev/null || true
  ln -sf "ffprobe-$PLAT_TAG" "dist/ffprobe" 2>/dev/null || true
else
  cp "dist/ffmpeg-$PLAT_TAG$EXT"  "dist/ffmpeg$EXT"  2>/dev/null || true
  cp "dist/ffprobe-$PLAT_TAG$EXT" "dist/ffprobe$EXT" 2>/dev/null || true
fi
ls -la dist/ | grep -E "ffmpeg|ffprobe" || echo "WARN: ffmpeg/ffprobe not bundled"

# --- smoke test: the binary must actually start and answer /health ---
BIN="dist/dubflow-engine"
if [[ "$(uname)" == *"MINGW"* || "$(uname -s)" == *"Windows"* ]]; then BIN="dist/dubflow-engine.exe"; fi
DUBFLOW_PORT=8799 "$BIN" > /tmp/dubflow-engine-smoke.log 2>&1 &
ENGINE_PID=$!
OK=0
for i in $(seq 1 40); do
  if curl -sf http://127.0.0.1:8799/health > /dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
if [ "$OK" != "1" ]; then
  echo "ENGINE SMOKE TEST FAILED:"; tail -30 /tmp/dubflow-engine-smoke.log
  kill $ENGINE_PID 2>/dev/null || true
  exit 1
fi
curl -s http://127.0.0.1:8799/health
kill $ENGINE_PID 2>/dev/null || true
echo ""
echo "engine packaged and smoke-tested: $ROOT/engine/dist/dubflow-engine"
