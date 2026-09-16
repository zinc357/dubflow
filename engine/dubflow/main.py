from __future__ import annotations

import asyncio
import logging
import os
import string
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


def main() -> None:
    uvicorn.run("dubflow.main:app", host=settings.host, port=settings.port,
                log_level="info")


if __name__ == "__main__":
    main()
