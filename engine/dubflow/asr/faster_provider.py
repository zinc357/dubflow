from __future__ import annotations

import threading
import wave
from contextlib import closing
from typing import Dict, Optional, Tuple

from .base import ASRError, ASRProvider, BackendInfo, ProgressFn, Segment, Transcript
from ..config import canonical_model_name, resolve_model_dir

DEFAULT_MODEL = "base"

# ctranslate2 的 CUDA 后端**在运行时动态加载** cuBLAS/cuDNN，这带来两个陷阱：
#   1. get_cuda_device_count() > 0 只说明驱动能枚举设备，并不代表 GPU 可推理；
#   2. 真正加载发生在本后端的第一次 encode()，所以模型构造成功也不代表 GPU 可用。
# 因此错误处理必须同时覆盖「构造」和「推理」两处，否则用户只会看到
# "Library cublas64_12.dll is not found or cannot be loaded" 这条没有指向性的英文报错。
_CUDA_HINT = (
    "NVIDIA CUDA 运行库缺失（cuBLAS/cuDNN），无法在 GPU 上推理。推荐用 pip 装，"
    "无需安装 CUDA Toolkit：\n"
    "  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
    "装好后把这两个包的 bin 目录加入 PATH 再启动引擎：\n"
    "  Windows: <venv>\\Lib\\site-packages\\nvidia\\{cublas,cudnn}\\bin\n"
    "  Linux  : <venv>/lib/pythonX.Y/site-packages/nvidia/{cublas,cudnn}/lib\n"
    "如果暂时不想配 GPU，也可以直接走 CPU int8 兜底（不装 CUDA 运行库即可）。"
)


def _raise_if_cuda_libs_missing(exc: BaseException) -> None:
    """把 ctranslate2 的 DLL 加载失败转换成可操作的中文指引。

    不匹配时原样返回，由调用方重新抛出原始异常。
    """
    msg = str(exc).lower()
    if "cublas" in msg or "cudnn" in msg or "cannot be loaded" in msg:
        raise ASRError(_CUDA_HINT) from exc


class FasterWhisperProvider(ASRProvider):
    """CTranslate2 backend: fastest on NVIDIA CUDA; int8 CPU as universal fallback.

    TODO(platform-nvidia):
      - N 卡 (Windows/Linux) 走本后端的 CUDA fp16 路线，代码已实现，
        待在真实 NVIDIA 机器上验证（驱动/cuDNN 依赖打包 + 精度/速度基准）。
      - CPU int8 兜底已可工作（模型经 GUI 模型管理器下载到 models_dir/）。
    """

    name = "faster-whisper"

    def __init__(self, device: str = "cpu", compute_type: str = "int8") -> None:
        super().__init__(BackendInfo(
            name=self.name, device=device,
            detail=f"CTranslate2 ({compute_type})",
        ))
        self.device = device
        self.compute_type = compute_type
        self._models: Dict[Tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def _load(self, model_size: str):
        key = (model_size, self.device)
        with self._lock:
            if key not in self._models:
                try:
                    from faster_whisper import WhisperModel  # type: ignore
                except ImportError as e:
                    raise ASRError(
                        "faster-whisper is not installed. Run: pip install faster-whisper"
                    ) from e
                # 本地目录（GUI 模型管理器下载的）优先于 HF 仓库。
                # 目录名可能是别名（base）也可能是下载器键名（faster-whisper-base），
                # 交给 resolve_model_dir 统一解析，避免「下好了却找不到」。
                local = resolve_model_dir(model_size)
                # 本地没有时，回退名必须是 faster-whisper 认得的名字。
                # GUI 发来的是下载器键名（如 faster-whisper-large-v3-turbo），
                # 原样传给它只会得到一个 ValueError: Invalid model size。
                target = str(local) if local is not None else canonical_model_name(model_size)
                try:
                    self._models[key] = WhisperModel(
                        target, device=self.device, compute_type=self.compute_type,
                    )
                except RuntimeError as e:
                    _raise_if_cuda_libs_missing(e)
                    raise
        return self._models[key]

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
        model_size: Optional[str] = None,
        progress: ProgressFn = None,
    ) -> Transcript:
        size = model_size or DEFAULT_MODEL
        if progress:
            progress(0.05, f"loading model {size} ({self.device})")
        model = self._load(size)

        duration = None
        try:
            with closing(wave.open(audio_path, "rb")) as wf:
                duration = wf.getnframes() / float(wf.getframerate() or 1)
        except Exception:
            pass

        # 推理阶段才是 cuBLAS/cuDNN 真正被加载的时刻，异常必须在这里再兜一次
        try:
            segments_iter, info = model.transcribe(
                str(audio_path), language=language, beam_size=5, vad_filter=True,
            )
            segments = []
            for i, seg in enumerate(segments_iter):
                segments.append(Segment(start=float(seg.start), end=float(seg.end),
                                        text=str(seg.text).strip()))
                if progress and duration:
                    progress(min(seg.end / duration, 1.0), f"segment {i + 1}")
        except RuntimeError as e:
            _raise_if_cuda_libs_missing(e)
            raise
        if progress:
            progress(1.0, f"{len(segments)} segments")
        return Transcript(language=getattr(info, "language", None), segments=segments)
