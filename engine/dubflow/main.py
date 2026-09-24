from __future__ import annotations

import asyncio
import logging
import uuid
import os
import string
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import UploadFile, File
from pydantic import BaseModel

import threading
import time

from . import __version__
from .asr import describe_backend
from .config import mask_secret, settings, update_user_settings
from .downloads import downloads_snapshot, start_ffmpeg_download, start_model_download
from .jobs import JobManager
from .schemas import JobCreate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="DubFlow Engine", version=__version__)


@app.on_event("startup")
async def _restore_jobs() -> None:
    n = manager.restore_from_disk()
    if n:
        logging.info("restored %d job(s) from disk", n)

# GUI dev (vite :5173) and Tauri webview origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "tauri://localhost",
        "http://tauri.localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = JobManager()


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    if html_path.is_file():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>DubFlow Engine</h1><p>API running. Use GUI at :5173</p>")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "backend": describe_backend(),
        "data_dir": str(settings.data_dir),
    }


class LlmSettings(BaseModel):
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


class MicrosoftSettings(BaseModel):
    key: Optional[str] = None
    region: Optional[str] = None


class SettingsUpdate(BaseModel):
    llm: Optional[LlmSettings] = None
    microsoft: Optional[MicrosoftSettings] = None


def _settings_payload() -> dict:
    return {
        "llm": {
            "base_url": settings.translate_base_url,
            "model": settings.translate_model,
            "api_key_set": bool(settings.translate_api_key),
            "api_key_hint": mask_secret(settings.translate_api_key),
        },
        "microsoft": {
            "region": settings.msft_translator_region,
            "key_set": bool(settings.msft_translator_key),
            "key_hint": mask_secret(settings.msft_translator_key),
        },
    }


@app.get("/settings")
async def get_settings() -> dict:
    """返回 GUI 可安全展示的翻译配置。

    密钥只回掩码（如 "sk-…1a2b"），**绝不返回明文**：前端把密钥框留空即表示
    「沿用已保存的值」，由 translator.py 里的 `t_opts.get("api_key") or settings....`
    兜底。这样浏览器页面任何时刻都拿不到完整密钥。
    """
    return _settings_payload()


@app.put("/settings")
async def put_settings(body: SettingsUpdate) -> dict:
    """持久化配置到 ~/.dubflow/settings.json。

    字段为 None 表示前端没动它（保持原样），空字符串表示清除。
    传入空字符串时密钥会回落到环境变量基线，而不是变成空字符串。
    """
    updates: dict = {}
    if body.llm is not None:
        updates["translate_base_url"] = body.llm.base_url
        updates["translate_model"] = body.llm.model
        updates["translate_api_key"] = body.llm.api_key
    if body.microsoft is not None:
        updates["msft_translator_key"] = body.microsoft.key
        updates["msft_translator_region"] = body.microsoft.region
    update_user_settings(updates)
    return _settings_payload()


@app.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    """接收前端上传的视频文件，保存到 uploads/ 目录，返回服务端路径。"""
    upload_dir = Path(settings.data_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 保留原始文件名（输出文件名基于此）；重名时加后缀
    stem = Path(file.filename).stem or "video"
    ext = Path(file.filename).suffix or ".mp4"
    dest = upload_dir / f"{stem}{ext}"
    if dest.exists():
        dest = upload_dir / f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return {"path": str(dest), "size_mb": round(dest.stat().st_size / 1e6, 1)}


@app.post("/jobs")
async def create_job(req: JobCreate) -> dict:
    from pathlib import Path
    if not Path(req.video_path).is_file():
        raise HTTPException(status_code=400, detail=f"video not found: {req.video_path}")
    if not req.translation.enabled and req.export.variant != "source":
        raise HTTPException(status_code=400, detail="该字幕类型需要开启翻译")
    job = manager.create(req)
    manager.start(job)
    return job.out()


@app.get("/jobs")
async def list_jobs() -> dict:
    return {"jobs": manager.list()}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job.out()


@app.get("/jobs/{job_id}/transcript")
async def get_transcript(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.transcript:
        raise HTTPException(status_code=409, detail="transcript not ready")
    data = job.transcript.to_dict()
    data["translations"] = job.translations
    data["target_language"] = job.target_language
    return data


class TranscriptUpdate(BaseModel):
    segments: list
    translations: Optional[list] = None


@app.put("/jobs/{job_id}/transcript")
async def put_transcript(job_id: str, body: TranscriptUpdate) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.transcript:
        raise HTTPException(status_code=409, detail="transcript not ready")
    try:
        manager.update_transcript(job, body.segments, body.translations)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "segments": len(job.transcript.segments)}


@app.post("/jobs/{job_id}/segments/{index}/merge_next")
async def merge_segment(job_id: str, index: int) -> dict:
    job = manager.get(job_id)
    if not job or not job.transcript:
        raise HTTPException(status_code=404, detail="job/transcript not found")
    try:
        manager.merge_next(job, index)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/jobs/{job_id}/segments/{index}/split")
async def split_segment(job_id: str, index: int, body: dict = None) -> dict:
    job = manager.get(job_id)
    if not job or not job.transcript:
        raise HTTPException(status_code=404, detail="job/transcript not found")
    body = body or {}
    try:
        manager.split_segment(job, index, body.get("at_time"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/jobs/{job_id}/segments/{index}/delete")
async def delete_segment(job_id: str, index: int) -> dict:
    job = manager.get(job_id)
    if not job or not job.transcript:
        raise HTTPException(status_code=404, detail="job/transcript not found")
    try:
        manager.delete_segment(job, index)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


class ExportOverrides(BaseModel):
    variant: Optional[str] = None
    save_to_video_folder: Optional[bool] = None
    embed_video: Optional[bool] = None
    output_dir: Optional[str] = None


@app.get("/jobs/{job_id}/audio")
async def get_job_audio(job_id: str):
    """Extracted audio for the GUI waveform timeline."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    path = manager.job_dir(job_id) / "audio.wav"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="audio not extracted yet")
    return FileResponse(str(path), media_type="audio/wav")


@app.post("/jobs/{job_id}/export")
async def reexport(job_id: str, body: ExportOverrides = None) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.transcript:
        raise HTTPException(status_code=409, detail="transcript not ready")
    if job.steps["export"].status == "running":
        raise HTTPException(status_code=409, detail="export already running")
    body = body or ExportOverrides()
    if body.variant:
        if body.variant != "source" and not job.translations:
            raise HTTPException(status_code=400, detail="该字幕类型需要译文")
        job.export_options["variant"] = body.variant
    if body.save_to_video_folder is not None:
        job.export_options["save_to_video_folder"] = body.save_to_video_folder
    if body.embed_video is not None:
        job.export_options["embed_video"] = body.embed_video
    if body.output_dir is not None:
        job.export_options["output_dir"] = body.output_dir
    manager.start_export(job)
    return {"ok": True, "job": job.out()}


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    manager.request_cancel(job)
    return {"ok": True}


@app.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "running":
        raise HTTPException(status_code=409, detail="仅运行中的任务可暂停")
    job.paused = True
    return {"ok": True, "paused": True}


@app.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    job.paused = False
    return {"ok": True, "paused": False}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("queued", "running"):
        raise HTTPException(status_code=409, detail="任务进行中，无法删除（可先取消）")
    manager.delete(job_id)
    return {"ok": True}


@app.post("/jobs/clear-failed")
async def clear_failed_jobs() -> dict:
    return {"ok": True, "removed": manager.clear_failed()}


@app.get("/fs/dirs")
async def list_dirs(path: str = "") -> dict:
    """列出目录下的子目录，供 GUI 的「输出目录」选择器使用。

    浏览器的安全策略不允许网页读取本地绝对路径，所以枚举能力放在引擎侧：
    路径全部由服务端产生，前端只负责点选。引擎默认只监听 127.0.0.1。

    path 留空时以用户主目录作为起点。
    """
    raw = (path or "").strip().strip('"')
    if raw:
        base = Path(raw).expanduser()
        if not base.is_dir():
            raise HTTPException(status_code=400, detail=f"不是有效目录: {raw}")
    else:
        base = Path.home()
    base = base.resolve()

    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"没有权限读取: {base}")

    dirs = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            writable = os.access(child, os.W_OK)
        except OSError:
            continue
        dirs.append({"name": child.name, "path": str(child), "writable": writable})

    parent = base.parent if base.parent != base else None
    drives = []
    if sys.platform == "win32":
        drives = [f"{c}:\\" for c in string.ascii_uppercase if Path(f"{c}:\\").exists()]

    return {
        "path": str(base),
        "parent": str(parent) if parent else None,
        "dirs": dirs,
        "drives": drives,
        "is_writable": os.access(base, os.W_OK),
    }


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".ts"}


@app.get("/fs/browse")
async def browse_fs(path: str = "", kind: str = "video") -> dict:
    """列出目录下的子目录与视频文件，供 GUI 的「选择视频文件」使用。

    与 /fs/dirs 相同的安全模型：枚举在引擎侧完成，路径由服务端产生。
    kind=video 时附带视频扩展名的文件列表。
    """
    raw = (path or "").strip().strip('"')
    if raw:
        base = Path(raw).expanduser()
        if not base.is_dir():
            raise HTTPException(status_code=400, detail=f"不是有效目录: {raw}")
    else:
        base = Path.home()
    base = base.resolve()

    dirs = []
    files = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"没有权限读取: {base}")

    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                dirs.append({"name": child.name, "path": str(child)})
            elif kind == "video" and child.suffix.lower() in VIDEO_EXTS:
                files.append({"name": child.name, "path": str(child),
                              "size_mb": round(child.stat().st_size / 1e6, 1)})
        except OSError:
            continue

    parent = base.parent if base.parent != base else None
    drives = []
    if sys.platform == "win32":
        drives = [f"{c}:{os.sep}" for c in string.ascii_uppercase if Path(f"{c}:{os.sep}").exists()]

    return {
        "path": str(base),
        "parent": str(parent) if parent else None,
        "dirs": dirs,
        "files": files,
        "drives": drives,
    }


@app.post("/fs/reveal")
async def reveal_file(body: dict):
    """在系统文件管理器中显示指定文件（macOS Finder / Windows 资源管理器 / Linux xdg-open）。"""
    import subprocess
    path = body.get("path", "")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])
    return {"ok": True}


@app.get("/downloads")
async def downloads() -> dict:
    return downloads_snapshot()


@app.post("/downloads/models/{key}")
async def download_model(key: str) -> dict:
    try:
        return start_model_download(key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/downloads/ffmpeg")
async def download_ffmpeg() -> dict:
    return start_ffmpeg_download()


@app.websocket("/ws/jobs/{job_id}")
async def job_events(ws: WebSocket, job_id: str) -> None:
    job = manager.get(job_id)
    if not job:
        await ws.close(code=4404)
        return
    await ws.accept()
    q = manager.hub.subscribe(job_id)
    try:
        await ws.send_json({"type": "snapshot", "job": job.out()})
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                await ws.send_json(event)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.hub.unsubscribe(job_id, q)


def _parent_watchdog() -> None:
    """POSIX: 父进程（GUI 壳）退出后引擎自动退出。

    PyInstaller onefile 的引导父进程被杀时，实际引擎子进程会成为孤儿继续
    运行；轮询 getppid 变化来兜底。Windows 走 taskkill /T 树杀，不需要此线程。
    """
    if sys.platform == "win32":
        return
    initial = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != initial:
            os._exit(0)


def main() -> None:
    threading.Thread(target=_parent_watchdog, daemon=True).start()
    uvicorn.run("dubflow.main:app", host=settings.host, port=settings.port,
                log_level="info")


if __name__ == "__main__":
    main()
