from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .asr import select_provider
from .asr.base import Segment, Transcript
from .config import settings
from .ffmpeg_tools import burn_subtitles, extract_audio, probe, wav_duration
from .schemas import JobCreate
from .subtitles import to_ass, to_srt
from .translator import build_translator

log = logging.getLogger(__name__)

STEPS = ["probe", "extract_audio", "asr", "translate", "export"]


class _Cancelled(Exception):
    """工作线程内的取消信号。

    asyncio.CancelledError 不能跨线程抛出，所以线程侧统一用这个异常，
    回到事件循环后再翻译成 CancelledError。
    """


@dataclass
class Step:
    status: str = "pending"   # pending | running | done | failed | skipped
    progress: float = 0.0
    detail: str = ""


@dataclass
class Job:
    id: str
    video_path: str
    source_language: Optional[str]
    target_language: str
    asr_options: Dict[str, Any]
    translation_options: Dict[str, Any]
    export_options: Dict[str, Any] = field(default_factory=dict)
    status: str = "queued"    # queued | running | done | failed | cancelled
    steps: Dict[str, Step] = field(default_factory=lambda: {s: Step() for s in STEPS})
    error: Optional[str] = None
    artifacts: Dict[str, str] = field(default_factory=dict)
    backend: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    transcript: Optional[Transcript] = None
    translations: Optional[List[str]] = None
    cancel_requested: bool = False

    def out(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "video_path": self.video_path,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "steps": {k: {"status": v.status, "progress": v.progress, "detail": v.detail}
                      for k, v in self.steps.items()},
            "error": self.error,
            "artifacts": self.artifacts,
            "backend": self.backend,
            "created_at": self.created_at,
            # 回显导出选择：工作台据此把「重新导出」的默认值设成该任务原本的设定，
            # 避免用户没勾却被默默落盘的困惑。
            "export_options": self.export_options,
        }


class EventHub:
    """Fans out job events to websocket subscribers."""

    def __init__(self) -> None:
        self._subs: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        self._subs.get(job_id, set()).discard(q)

    def publish(self, job_id: str, event: dict) -> None:
        for q in list(self._subs.get(job_id, ())):
            try:
                q.put_nowait(event)
            except Exception:
                pass


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self.hub = EventHub()
        self.tasks: Dict[str, asyncio.Task] = {}

    # ---------- public API ----------

    def create(self, req: JobCreate) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            video_path=req.video_path,
            source_language=req.source_language,
            target_language=req.target_language,
            asr_options=req.asr.model_dump(),
            translation_options=req.translation.model_dump(),
            export_options=req.export.model_dump(),
        )
        self._jobs[job.id] = job
        job_dir = self.job_dir(job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        return job

    def start(self, job: Job) -> None:
        self.tasks[job.id] = asyncio.get_running_loop().create_task(self._run(job))

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[dict]:
        return [j.out() for j in sorted(self._jobs.values(), key=lambda j: -j.created_at)]

    def request_cancel(self, job: Job) -> None:
        job.cancel_requested = True

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self.tasks.pop(job_id, None)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    def clear_failed(self) -> int:
        dead = [j for j in self._jobs.values()
                if j.status in ("failed", "cancelled")]
        for j in dead:
            self.delete(j.id)
        return len(dead)

    def job_dir(self, job_id: str) -> Path:
        return settings.data_dir / "jobs" / job_id

    # ---------- pipeline ----------

    def _snap(self, job: Job, event_type: str = "progress") -> dict:
        return {"type": event_type, "job": job.out()}

    def _pub_threadsafe(self, loop: asyncio.AbstractEventLoop, job: Job) -> None:
        try:
            loop.call_soon_threadsafe(self.hub.publish, job.id, self._snap(job))
        except RuntimeError:
            pass

    def _persist(self, job: Job) -> None:
        state = job.out()
        state["transcript"] = job.transcript.to_dict() if job.transcript else None
        state["translations"] = job.translations
        state["asr_options"] = job.asr_options
        state["translation_options"] = job.translation_options
        state["export_options"] = job.export_options
        path = self.job_dir(job.id) / "job.json"
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    async def _set_step(self, job: Job, loop, name: str, status: Optional[str] = None,
                        progress: Optional[float] = None, detail: Optional[str] = None) -> None:
        step = job.steps[name]
        if status:
            step.status = status
        if progress is not None:
            step.progress = progress
        if detail is not None:
            step.detail = detail
        self.hub.publish(job.id, self._snap(job))
        self._persist(job)

    def _check_cancel(self, job: Job) -> None:
        if job.cancel_requested:
            raise asyncio.CancelledError

    @staticmethod
    def _raise_if_cancelled(job: Job) -> None:
        """供工作线程（ASR / 翻译）调用，让「停止」能及时生效。

        ASR 与翻译跑在线程里，事件循环的取消信号传不进去，只能靠它们自己的
        进度回调主动检查；否则点了停止也要等整段推理跑完才会响应。
        """
        if job.cancel_requested:
            raise _Cancelled()

    async def _run(self, job: Job) -> None:
        loop = asyncio.get_running_loop()
        job.status = "running"
        self.hub.publish(job.id, self._snap(job))
        job_dir = self.job_dir(job.id)
        wav_path = job_dir / "audio.wav"

        try:
            # 1. probe
            await self._set_step(job, loop, "probe", "running")
            self._check_cancel(job)
            info = await probe(job.video_path)
            fmt = info.get("format", {})
            await self._set_step(job, loop, "probe", "done",
                                 detail=f"{float(fmt.get('duration', 0)):.1f}s")

            # 2. extract audio
            await self._set_step(job, loop, "extract_audio", "running")
            self._check_cancel(job)
            await extract_audio(job.video_path, str(wav_path))
            await self._set_step(job, loop, "extract_audio", "done",
                                 detail=wav_path.name)

            # 3. ASR (GPU when available; runs in worker thread)
            await self._set_step(job, loop, "asr", "running", 0.02)
            provider = select_provider(job.asr_options.get("model"),
                                 job.asr_options.get("provider"))
            job.backend = provider.info.to_dict()
            self._persist(job)
            self.hub.publish(job.id, self._snap(job))

            def asr_progress(p: float, detail: str) -> None:
                self._raise_if_cancelled(job)      # 段间响应「停止」
                job.steps["asr"].progress = p
                job.steps["asr"].detail = detail
                self._pub_threadsafe(loop, job)

            self._check_cancel(job)
            try:
                transcript = await asyncio.to_thread(
                    provider.transcribe, str(wav_path),
                    job.source_language, job.asr_options.get("model"), asr_progress,
                )
            except _Cancelled:
                raise asyncio.CancelledError
            job.transcript = transcript
            tpath = job_dir / "transcript.json"
            tpath.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2))
            job.artifacts["transcript"] = str(tpath)
            await self._set_step(job, loop, "asr", "done",
                                 detail=f"{len(transcript.segments)} segments "
                                        f"[{provider.info.name}/{provider.info.device}]")

            # 4. translate (optional)
            texts: Optional[List[str]] = None
            t_opts = job.translation_options
            if t_opts.get("enabled"):
                await self._set_step(job, loop, "translate", "running")
                self._check_cancel(job)
                translator = build_translator(
                    t_opts, job.target_language, job.source_language,
                )

                def tr_progress(p: float, detail: str) -> None:
                    self._raise_if_cancelled(job)      # 批次间响应「停止」
                    job.steps["translate"].progress = p
                    job.steps["translate"].detail = detail
                    self._pub_threadsafe(loop, job)

                try:
                    texts = await asyncio.to_thread(
                        translator.translate_transcript, transcript, tr_progress)
                except _Cancelled:
                    raise asyncio.CancelledError
                job.translations = texts
                await self._set_step(job, loop, "translate", "done")
            else:
                await self._set_step(job, loop, "translate", "skipped")

            # 5. export subtitles (shared with editor re-export)
            await self._do_export(job, loop)

            job.status = "done"
            self.hub.publish(job.id, self._snap(job, "done"))
            self._persist(job)

        except asyncio.CancelledError:
            job.status = "cancelled"
            self.hub.publish(job.id, self._snap(job))
            self._persist(job)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            job.status = "failed"
            job.error = f"{type(e).__name__}: {e}"
            # 把当时还在 running 的步骤一并标成 failed。
            # 否则它会永远停在 running，界面上同时出现「任务失败」和
            # 「语音识别·running 5%」两个互相矛盾的状态。
            for step in job.steps.values():
                if step.status == "running":
                    step.status = "failed"
                    step.detail = job.error
            self.hub.publish(job.id, self._snap(job, "failed"))
            self._persist(job)

    # ---------- editor: mutations + re-export ----------

    async def _do_export(self, job: Job, loop: asyncio.AbstractEventLoop) -> None:
        """Generate SRT artifacts + deliver (save to video folder / burn video)."""
        job_dir = self.job_dir(job.id)
        await self._set_step(job, loop, "export", "running", 0.0, "writing srt")
        transcript = job.transcript
        texts = job.translations
        src_srt = job_dir / "source.srt"
        src_srt.write_text(to_srt(transcript.segments), encoding="utf-8")
        job.artifacts["source_srt"] = str(src_srt)
        variant = "source"
        if texts:
            dst_srt = job_dir / f"{job.target_language}.srt"
            dst_srt.write_text(_srt_target(transcript, texts), encoding="utf-8")
            bi_srt = job_dir / "bilingual.srt"
            bi_srt.write_text(to_srt(transcript.segments, second_lines=texts), encoding="utf-8")
            job.artifacts["target_srt"] = str(dst_srt)
            job.artifacts["bilingual_srt"] = str(bi_srt)
            variant = job.export_options.get("variant", "bilingual")

        files = {"source": src_srt,
                 "target": job.artifacts.get("target_srt"),
                 "bilingual": job.artifacts.get("bilingual_srt")}
        srt_file = Path(files.get(variant) or src_srt)
        e_opts = job.export_options
        save = e_opts.get("save_to_video_folder", True)
        embed = e_opts.get("embed_video", False)
        parts = []
        if save or embed:
            # 交付目录：用户指定优先，未指定则落到原视频所在目录
            custom_dir = (e_opts.get("output_dir") or "").strip().strip('"')
            if custom_dir:
                video_dir = Path(custom_dir).expanduser()
                if not video_dir.is_dir():
                    raise ValueError(f"输出目录不存在或不可用: {video_dir}")
            else:
                video_dir = Path(job.video_path).parent
            stem = Path(job.video_path).stem
            src_lang = transcript.language or job.source_language or "src"
            names = {"source": f"{stem}.{src_lang}.srt",
                     "target": f"{stem}.{job.target_language}.srt",
                     "bilingual": f"{stem}.bilingual.{job.target_language}.srt"}

            # 「保存字幕文件」与「烧录硬字幕」是两件独立的事，不能共用一个判断：
            # 取消勾选前者时就不该再往交付目录写 .srt —— 烧录读的是任务目录里
            # 那份 srt，不需要额外的交付副本。
            if save:
                dest = video_dir / names[variant]
                shutil.copyfile(srt_file, dest)
                job.artifacts["delivered_srt"] = str(dest)
                parts.append(f"saved {dest.name}")

            if embed:
                out_ext = Path(job.video_path).suffix or ".mp4"
                out_video = video_dir / f"{stem}.{variant}.hardsub{out_ext}"
                duration = wav_duration(job_dir / "audio.wav")

                def burn_progress(pr: float, d: str) -> None:
                    self._check_cancel(job)   # 烧录过程也响应「停止」
                    job.steps["export"].progress = pr
                    job.steps["export"].detail = d
                    self._pub_threadsafe(loop, job)

                await burn_subtitles(job.video_path, str(srt_file),
                                     str(out_video), duration=duration,
                                     workdir=str(job_dir), progress=burn_progress)
                job.artifacts["embedded_video"] = str(out_video)
                parts.append(out_video.name)
        await self._set_step(job, loop, "export", "done", 1.0, " + ".join(parts))

    def _regen_srts(self, job: Job) -> None:
        """Refresh in-project SRT artifacts after an edit (no delivery)."""
        job_dir = self.job_dir(job.id)
        transcript = job.transcript
        (job_dir / "source.srt").write_text(to_srt(transcript.segments), encoding="utf-8")
        if job.translations:
            (job_dir / f"{job.target_language}.srt").write_text(
                _srt_target(transcript, job.translations), encoding="utf-8")
            (job_dir / "bilingual.srt").write_text(
                to_srt(transcript.segments, second_lines=job.translations), encoding="utf-8")
        self._persist(job)

    def update_transcript(self, job: Job, segments: List[Dict[str, Any]],
                          translations: Optional[List[str]]) -> None:
        segs = [Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]))
                for s in segments]
        if translations is not None and len(translations) != len(segs):
            raise ValueError("translations count mismatch")
        lang = job.transcript.language if job.transcript else None
        job.transcript = Transcript(language=lang, segments=segs)
        if translations is not None:
            job.translations = [str(t) for t in translations]
        elif job.translations:
            job.translations = job.translations[:len(segs)]
        self._regen_srts(job)

    def merge_next(self, job: Job, index: int) -> None:
        segs = job.transcript.segments
        if index < 0 or index + 1 >= len(segs):
            raise ValueError("no next segment to merge")
        a, b = segs[index], segs[index + 1]
        a.end = b.end
        sep = " " if (a.text.isascii() and b.text.isascii()) else ""
        a.text = (a.text + sep + b.text).strip()
        if job.translations:
            t1, t2 = job.translations[index], job.translations[index + 1]
            sep_t = " " if (t1.isascii() and t2.isascii()) else ""
            job.translations[index] = (t1 + sep_t + t2).strip()
            del job.translations[index + 1]
        del segs[index + 1]
        self._regen_srts(job)

    def split_segment(self, job: Job, index: int, at_time: Optional[float]) -> None:
        segs = job.transcript.segments
        seg = segs[index]
        if seg.end - seg.start < 0.3:
            raise ValueError("segment too short to split")
        mid = (seg.start + seg.end) / 2.0
        t = min(max(at_time if at_time is not None else mid, seg.start + 0.05), seg.end - 0.05)
        ratio = min(max((t - seg.start) / max(seg.end - seg.start, 1e-6), 0.05), 0.95)

        def split_text(text: str) -> tuple:
            if "\n" in text:
                parts = text.split("\n", 1)
                return parts[0].strip(), parts[1].strip()
            if " " in text:
                positions = [i for i, ch in enumerate(text) if ch == " "]
                pos = min(positions, key=lambda i: abs(i / len(text) - ratio))
                return text[:pos].strip(), text[pos:].strip()
            cut = int(len(text) * ratio)
            return text[:cut].strip(), text[cut:].strip()

        t1, t2 = split_text(seg.text)
        new_seg = Segment(start=t, end=seg.end, text=t2)
        seg.end = t
        seg.text = t1
        segs.insert(index + 1, new_seg)
        if job.translations:
            u1, u2 = split_text(job.translations[index])
            job.translations[index] = u1
            job.translations.insert(index + 1, u2)
        self._regen_srts(job)

    def delete_segment(self, job: Job, index: int) -> None:
        segs = job.transcript.segments
        if index < 0 or index >= len(segs):
            raise ValueError("index out of range")
        del segs[index]
        if job.translations and index < len(job.translations):
            del job.translations[index]
        self._regen_srts(job)

    def start_export(self, job: Job) -> None:
        job.steps["export"].status = "running"
        self.tasks[job.id] = asyncio.get_running_loop().create_task(self._export_task(job))

    async def _export_task(self, job: Job) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self._do_export(job, loop)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("re-export failed")
            await self._set_step(job, loop, "export", "failed", detail=f"{type(e).__name__}: {e}")

    def restore_from_disk(self) -> int:
        """Rebuild jobs from job.json after an engine restart."""
        count = 0
        base = settings.data_dir / "jobs"
        if not base.exists():
            return 0
        for jf in sorted(base.glob("*/job.json")):
            try:
                state = json.loads(jf.read_text())
            except Exception:
                continue
            jid = state.get("id")
            if not jid or jid in self._jobs:
                continue
            job = Job(
                id=jid,
                video_path=state.get("video_path", ""),
                source_language=state.get("source_language"),
                target_language=state.get("target_language", "zh"),
                asr_options=state.get("asr_options", {}),
                translation_options=state.get("translation_options", {}),
                export_options=state.get("export_options", {}),
                status=state.get("status", "failed"),
                error=state.get("error"),
                artifacts=state.get("artifacts", {}),
                backend=state.get("backend", {}),
                created_at=state.get("created_at", time.time()),
            )
            if state.get("status") in ("queued", "running"):
                job.status = "failed"
                job.error = "engine restarted during job"
            for name, s in state.get("steps", {}).items():
                if name in job.steps:
                    job.steps[name] = Step(status=s.get("status", "pending"),
                                           progress=s.get("progress", 0.0),
                                           detail=s.get("detail", ""))
            tr = state.get("transcript")
            if tr and tr.get("segments"):
                job.transcript = Transcript(
                    language=tr.get("language"),
                    segments=[Segment(start=float(s["start"]), end=float(s["end"]),
                                      text=str(s.get("text", "")))
                              for s in tr["segments"]])
            job.translations = state.get("translations")
            # backfill translations for legacy jobs: parse target SRT artifact
            if not job.translations and job.artifacts.get("target_srt"):
                tsp = Path(job.artifacts["target_srt"])
                if tsp.is_file():
                    texts = _parse_srt_texts(tsp.read_text(encoding="utf-8"))
                    if len(texts) == len(job.transcript.segments) if job.transcript else False:
                        job.translations = texts
            self._jobs[jid] = job
            count += 1
        return count

def _parse_srt_texts(srt_content: str) -> List[str]:
    """Extract cue texts (joining multi-line cues with \n) from SRT content."""
    texts: List[str] = []
    block: List[str] = []
    for raw in srt_content.splitlines() + [""]:
        line = raw.strip()
        if not line:
            if block:
                text_lines = [l for l in block if not l.isdigit() and "-->" not in l]
                texts.append("\n".join(text_lines).strip())
                block = []
            continue
        block.append(line)
    return [t for t in texts if t]


def _srt_target(transcript: Transcript, texts: List[str]) -> str:
    """SRT containing only translated text (same timing)."""
    from .asr.base import Segment
    from .subtitles import to_ass, to_srt
    segs = [Segment(start=s.start, end=s.end, text=t)
            for s, t in zip(transcript.segments, texts)]
    return to_srt(segs)


_ISO639_2 = {"zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor", "de": "deu",
             "fr": "fra", "es": "spa", "ru": "rus", "pt": "por"}


def _iso639_2(code: str) -> str:
    return _ISO639_2.get((code or "").lower()[:2], "und")


def _ass_target(transcript: Transcript, texts: List[str]) -> str:
    from .asr.base import Segment
    from .subtitles import to_ass
    segs = [Segment(start=s.start, end=s.end, text=t)
            for s, t in zip(transcript.segments, texts)]
    return to_ass(segs)
