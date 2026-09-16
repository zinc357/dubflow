"""Download manager: whisper models + bundled ffmpeg, with progress state.

The GUI polls GET /downloads and triggers POSTs; downloads run in daemon
threads so the engine stays responsive.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict

import httpx

from .config import settings
from .ffmpeg_tools import bundled, plat_tag

# ---------------------------------------------------------------------------
# Model catalog. Each entry has a SOURCE CHAIN, tried in order:
#   modelscope -> 国内直连，最稳（mlx-community / Systran 官方命名空间原生同步）
#   hf-mirror  -> HF_ENDPOINT 指向的镜像（默认 hf-mirror.com）
# backend "mlx"         -> Apple Silicon (macOS)
# backend "ctranslate2" -> faster-whisper (NVIDIA CUDA / CPU), TODO(platform): 真机验证
# ---------------------------------------------------------------------------
_MLX_FILES = ["config.json", "weights.npz"]
_CT2_FILES = ["config.json", "model.bin", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"]

# whisper.cpp ggml models live in the huge ggerganov/whisper.cpp repo;
# skip tree listing and download the single model file directly.
_WCPP_FILES = {
    "ggml-tiny": ["ggml-tiny.bin"],
    "ggml-base": ["ggml-base.bin"],
    "ggml-small": ["ggml-small.bin"],
    "ggml-large-v3-turbo-q5_0": ["ggml-large-v3-turbo-q5_0.bin"],
}
_WCPP_SOURCES = [("hf-mirror", "ggerganov/whisper.cpp")]

def _ms(repo: str) -> tuple:
    return ("modelscope", repo)

def _hfm(repo: str) -> tuple:
    return ("hf-mirror", repo)

CATALOG: Dict[str, Dict[str, Any]] = {
    "tiny":               {"backend": "mlx", "files": _MLX_FILES,
                           "sources": [_ms("mlx-community/whisper-tiny-mlx"), _hfm("mlx-community/whisper-tiny")]},
    "medium":             {"backend": "mlx", "files": _MLX_FILES,
                           "sources": [_ms("mlx-community/whisper-medium-4bit"), _hfm("mlx-community/whisper-medium-4bit")]},
    "large-v3":           {"backend": "mlx", "files": _MLX_FILES,
                           "sources": [_ms("mlx-community/whisper-large-v3-4bit"), _hfm("mlx-community/whisper-large-v3-4bit")]},
    "large-v3-turbo":     {"backend": "mlx", "files": ["config.json", "weights.safetensors"],
                           "sources": [_ms("mlx-community/whisper-large-v3-turbo"), _hfm("mlx-community/whisper-large-v3-turbo")]},
    "large-v3-turbo-q4":  {"backend": "mlx", "files": _MLX_FILES,
                           "sources": [_ms("mlx-community/whisper-large-v3-turbo-4bit"), _hfm("mlx-community/whisper-large-v3-turbo-q4")]},
    "faster-whisper-tiny":     {"backend": "ctranslate2", "files": _CT2_FILES,
                                "sources": [_ms("Systran/faster-whisper-tiny"), _hfm("Systran/faster-whisper-tiny")]},
    "faster-whisper-base":     {"backend": "ctranslate2", "files": _CT2_FILES,
                                "sources": [_ms("Systran/faster-whisper-base"), _hfm("Systran/faster-whisper-base")]},
    "faster-whisper-small":    {"backend": "ctranslate2", "files": _CT2_FILES,
                                "sources": [_ms("Systran/faster-whisper-small"), _hfm("Systran/faster-whisper-small")]},
    "faster-whisper-medium":   {"backend": "ctranslate2", "files": _CT2_FILES,
                                "sources": [_ms("Systran/faster-whisper-medium"), _hfm("Systran/faster-whisper-medium")]},
    "faster-whisper-large-v3": {"backend": "ctranslate2", "files": _CT2_FILES,
                                "sources": [_ms("Systran/faster-whisper-large-v3"), _hfm("Systran/faster-whisper-large-v3")]},
    # large-v3-turbo 没有 Systran 版；这里用 faster-whisper 官方映射的仓库
    # (见 faster_whisper/utils.py 的 _MODELS)，ModelScope 与 HF 镜像都有。
    "faster-whisper-large-v3-turbo": {"backend": "ctranslate2", "files": _CT2_FILES,
                                "sources": [_ms("mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
                                            _hfm("mobiuslabsgmbh/faster-whisper-large-v3-turbo")]},
    # whisper.cpp ggml models (AMD/Intel Vulkan backend; also runs on any CPU)
    "ggml-tiny":      {"backend": "whisper.cpp", "files": _WCPP_FILES["ggml-tiny"], "sources": _WCPP_SOURCES, "skip_tree": True},
    "ggml-base":      {"backend": "whisper.cpp", "files": _WCPP_FILES["ggml-base"], "sources": _WCPP_SOURCES, "skip_tree": True},
    "ggml-small":     {"backend": "whisper.cpp", "files": _WCPP_FILES["ggml-small"], "sources": _WCPP_SOURCES, "skip_tree": True},
    "ggml-large-v3-turbo-q5_0": {"backend": "whisper.cpp", "files": _WCPP_FILES["ggml-large-v3-turbo-q5_0"], "sources": _WCPP_SOURCES, "skip_tree": True},
    # A卡 (AMD/Intel) Windows 专用：DomoticX 预编译 whisper.cpp Vulkan 构建
    # （https://github.com/DomoticX/whisper.cpp-windows-vulkan，仅 win32-x86_64）
    "whispercpp-vulkan-win64": {"backend": "whisper.cpp", "files": ["whisper-cli.exe"],
                                "sources": [("url", "https://github.com/DomoticX/whisper.cpp-windows-vulkan/releases/download/v1.0/whisper.cpp-windows-vulkan.zip")],
                                "url_zip": True},
}

_SKIP_FILES = {"README.md", "configuration.json", ".gitattributes"}

_BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
_state_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}


def _set(key: str, **kw: Any) -> None:
    with _state_lock:
        st = _state.setdefault(key, {"status": "idle", "progress": 0.0, "detail": ""})
        st.update(kw)


def _get(key: str) -> Dict[str, Any]:
    with _state_lock:
        return dict(_state.get(key, {"status": "idle", "progress": 0.0, "detail": ""}))


def _model_dir(key: str) -> Path:
    return settings.models_dir / key


def _existing_model_dir(key: str, repo: str) -> Path | None:
    """Local dir may be named by alias, repo id, or repo basename."""
    for name in (key, repo, Path(repo).name):
        d = settings.models_dir / name
        if d.is_dir():
            return d
    return None


def ffmpeg_status() -> dict:
    ff, fp = bundled("ffmpeg"), bundled("ffprobe")
    return {
        "installed": bool(ff and fp),
        "ffmpeg": str(ff) if ff else None,
        "ffprobe": str(fp) if fp else None,
    }


def downloads_snapshot() -> dict:
    models = []
    for key in CATALOG:
        backend = CATALOG[key]["backend"]
        repo = CATALOG[key]["sources"][0][1]
        d = _existing_model_dir(key, repo)
        has_weights = False
        size = 0
        if d is not None:
            files = [f for f in d.iterdir() if f.is_file()]
            has_weights = any(
                f.suffix in (".npz", ".bin", ".safetensors") or f.name == "whisper-cli.exe"
                for f in files
            )
            size = sum(f.stat().st_size for f in files)
        info = {"key": key, "repo": repo, "backend": backend,
                "downloaded": has_weights, "size_mb": round(size / 1e6, 1)}
        info.update(_get(f"model:{key}"))
        models.append(info)
    ff = ffmpeg_status()
    ff.update(_get("ffmpeg"))
    return {"ffmpeg": ff, "models": models}


# ---------------------------------------------------------------------------
# model downloads (HF files via configured mirror endpoint)
# ---------------------------------------------------------------------------

def _list_source_files(source: tuple, client: httpx.Client) -> list:
    """Return [(path, size)] for a source, or raise. Filter metadata files."""
    source_type, repo = source
    if source_type == "modelscope":
        url = f"https://modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master&Recursive=true"
        r = client.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"modelscope list {r.status_code}")
        data = r.json().get("Data", {}).get("Files", []) or []
        files = [(f["Path"], f.get("Size") or 0) for f in data if f.get("Type") == "blob"]
    else:  # hf-mirror / hf
        endpoint = settings.hf_endpoint.rstrip("/")
        r = client.get(f"{endpoint}/api/models/{repo}/tree/main")
        if r.status_code != 200:
            raise RuntimeError(f"hf list {r.status_code}")
        files = [(f["path"], f.get("size") or 0) for f in r.json()
                 if f.get("type") == "file"]

    files = [(p, s) for p, s in files
             if not p.startswith(".") and p not in _SKIP_FILES]
    if not files:
        raise RuntimeError("empty file list")
    return files


def _source_file_url(source: tuple, path: str) -> str:
    source_type, repo = source
    if source_type == "modelscope":
        from urllib.parse import quote
        return (f"https://modelscope.cn/api/v1/models/{repo}/repo"
                f"?Revision=master&FilePath={quote(path)}")
    endpoint = settings.hf_endpoint.rstrip("/")
    return f"{endpoint}/{repo}/resolve/main/{path}"


def _download_model_sync(key: str, entry: Dict[str, Any]) -> None:
    dest = _model_dir(key)
    dest.mkdir(parents=True, exist_ok=True)
    if entry.get("url_zip"):
        # 单 URL 直下 zip 并解压（whisper.cpp Vulkan 预编译包）
        import zipfile
        url = entry["sources"][0][1]
        with httpx.Client(timeout=300, trust_env=True, follow_redirects=True) as client:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0)) or 1
                done = 0
                zpath = dest / "bundle.zip"
                with open(zpath, "wb") as w:
                    for chunk in r.iter_bytes(1 << 20):
                        w.write(chunk)
                        done += len(chunk)
                        _set(f"model:{key}", progress=round(min(done / total, 1.0), 4),
                             detail=f"{done // 1_000_000}/{total // 1_000_000}MB")
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(dest)
        zpath.unlink()
        for f in dest.iterdir():
            if f.is_file() and os.access(f, os.W_OK):
                f.chmod(0o755)
        _set(f"model:{key}", status="done", progress=1.0, detail="completed")
        return
    files, used = None, None
    if entry.get("skip_tree"):
        # explicit file list (huge repos like ggerganov/whisper.cpp)
        files = [(f, 0) for f in entry.get("files", [])]
        used = entry["sources"][0]
    with httpx.Client(timeout=120, trust_env=True, follow_redirects=True) as client:
        if files is None:
            for source in entry["sources"]:
                try:
                    files = _list_source_files(source, client)
                    used = source
                    break
                except Exception as e:  # noqa: BLE001
                    _set(f"model:{key}", detail=f"source {source[0]} unavailable: {e}")
        if not files or used is None:
            raise RuntimeError("all download sources failed (file list)")

        known = all(s > 0 for _, s in files)
        total = sum(s for _, s in files)
        done_bytes = 0
        done_files = 0
        for path, size in files:
            out = dest / path
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".part")
            url = _source_file_url(used, path)
            try:
                with client.stream("GET", url) as r:
                    if r.status_code == 404:
                        done_files += 1
                        continue
                    r.raise_for_status()
                    with open(tmp, "wb") as w:
                        for chunk in r.iter_bytes(1 << 20):
                            w.write(chunk)
                            done_bytes += len(chunk)
            finally:
                if tmp.exists():
                    tmp.rename(out)
                done_files += 1
            prog = (done_bytes / total) if known and total else (done_files / len(files))
            _set(f"model:{key}", progress=round(min(prog, 1.0), 4),
                 detail=f"[{used[0]}] {path} ({done_files}/{len(files)})")
    _set(f"model:{key}", status="done", progress=1.0, detail=f"completed via {used[0]}")


def _worker(state_key: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        _set(state_key, status="failed", detail=f"{type(e).__name__}: {e}")


def start_model_download(key: str) -> dict:
    if key not in CATALOG:
        raise ValueError(f"unknown model: {key}")
    st = _get(f"model:{key}")
    if st["status"] == "downloading":
        return {"ok": True, "already_running": True}
    _set(f"model:{key}", status="downloading", progress=0.0, detail="starting")
    entry = CATALOG[key]
    threading.Thread(
        target=_worker,
        args=(f"model:{key}", _download_model_sync, key, entry),
        daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# bundled ffmpeg download (current platform only)
# ---------------------------------------------------------------------------

def _download_ffmpeg_sync() -> None:
    bin_dir = _BIN_DIR
    bin_dir.mkdir(parents=True, exist_ok=True)
    tag = plat_tag()        # 归一化平台标签，与 scripts/fetch_ffmpeg.sh 保持一致
    ext = ".exe" if sys.platform == "win32" else ""
    client = httpx.Client(timeout=300, trust_env=True, follow_redirects=True)
    tmp = Path(settings.data_dir) / "ffmpeg_dl"
    tmp.mkdir(parents=True, exist_ok=True)

    def fetch(url: str, dest: Path) -> None:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0)) or 1
            done = 0
            with open(dest, "wb") as w:
                for chunk in r.iter_bytes(1 << 20):
                    w.write(chunk)
                    done += len(chunk)
                    _set("ffmpeg", progress=round(done / total, 4),
                         detail=f"{dest.name} {done // 1_000_000}/{total // 1_000_000}MB")

    def extract(archive: Path, want: str) -> Path:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for n in zf.namelist():
                    if n.endswith("/" + want) or n == want:
                        out = tmp / want
                        out.write_bytes(zf.read(n))
                        return out
        elif archive.suffix == ".xz" or archive.name.endswith(".tar.xz"):
            import tarfile
            with tarfile.open(archive) as tf:
                for n in tf.getnames():
                    if n.endswith("/" + want):
                        tf.extract(n, tmp)
                        return tmp / n
        raise RuntimeError(f"{want} not found in {archive.name}")

    pairs = []
    if sys.platform == "darwin":
        arch = "arm" if platform.machine() == "arm64" else "intel"
        pairs = [("ffmpeg", f"https://www.osxexperts.net/ffmpeg9{arch}.zip"),
                 ("ffprobe", f"https://www.osxexperts.net/ffprobe9{arch}.zip")]
        for i, (name, url) in enumerate(pairs):
            a = tmp / f"dl{i}.zip"
            fetch(url, a)
            got = extract(a, name)
            target = bin_dir / f"{name}-{tag}"
            shutil.copy2(got, target)
            target.chmod(0o755)
    elif sys.platform == "win32":
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        a = tmp / "dl.zip"
        fetch(url, a)
        with zipfile.ZipFile(a) as zf:
            for n, out_name in (("ffmpeg.exe", "ffmpeg-win32-x86_64.exe"),
                                ("ffprobe.exe", "ffprobe-win32-x86_64.exe")):
                for member in zf.namelist():
                    if member.endswith("/" + n):
                        (bin_dir / out_name).write_bytes(zf.read(member))
                        break
    elif sys.platform == "linux":
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
        a = tmp / "dl.tar.xz"
        fetch(url, a)
        with tarfile.open(a) as tf:
            for n, out_name in (("ffmpeg", "ffmpeg-linux-x86_64"),
                                ("ffprobe", "ffprobe-linux-x86_64")):
                for member in tf.getnames():
                    if member.endswith("/" + n):
                        tf.extract(member, tmp)
                        src = tmp / member
                        shutil.copy2(src, bin_dir / out_name)
                        (bin_dir / out_name).chmod(0o755)
                        break
    else:
        raise RuntimeError(f"unsupported platform: {sys.platform}")

    # generic names so third-party libs calling bare "ffmpeg" hit ours
    try:
        (bin_dir / "ffmpeg").symlink_to(f"ffmpeg-{tag}{ext}")
        (bin_dir / "ffprobe").symlink_to(f"ffprobe-{tag}{ext}")
    except (OSError, NotImplementedError):
        shutil.copy2(bin_dir / f"ffmpeg-{tag}{ext}", bin_dir / "ffmpeg")
        shutil.copy2(bin_dir / f"ffprobe-{tag}{ext}", bin_dir / "ffprobe")
    shutil.rmtree(tmp, ignore_errors=True)
    _set("ffmpeg", status="done", progress=1.0, detail="installed")


def start_ffmpeg_download() -> dict:
    st = _get("ffmpeg")
    if st["status"] == "downloading":
        return {"ok": True, "already_running": True}
    _set("ffmpeg", status="downloading", progress=0.0, detail="starting")
    threading.Thread(target=_worker, args=(_download_ffmpeg_sync, "ffmpeg"),
                     daemon=True).start()
    return {"ok": True}
