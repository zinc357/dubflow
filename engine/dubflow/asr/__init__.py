from __future__ import annotations

import logging
import platform
import sys
from typing import Optional

from .base import ASRProvider, ASRError

log = logging.getLogger(__name__)


_mlx_probe: Optional[bool] = None


def _mlx_available() -> bool:
    """Probe MLX in a child process: importing mlx can HARD-ABORT the process
    (native NSException) on machines without enumerable Metal devices (CI,
    restricted sandboxes). A subprocess crash then just means 'unavailable'
    instead of taking the whole engine down."""
    global _mlx_probe
    if _mlx_probe is None:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            _mlx_probe = False
        elif getattr(sys, "frozen", False):
            # PyInstaller 包内：依赖已在打包时收集，直接导入即可。
            # 不能走子进程探测 —— sys.executable 此时是引擎自身。
            try:
                import mlx.core  # noqa: F401
                _mlx_probe = True
            except Exception:  # pragma: no cover
                _mlx_probe = False
                log.warning("mlx import failed inside frozen app")
        else:
            # 开发/源码环境：import mlx 可能在无 GPU 的受限环境硬崩溃
            # （原生 NSException，try/except 接不住），用子进程隔离探测。
            try:
                import subprocess
                r = subprocess.run(
                    [sys.executable, "-c", "import mlx.core"],
                    capture_output=True, timeout=60,
                )
                _mlx_probe = r.returncode == 0
            except Exception:  # pragma: no cover
                _mlx_probe = False
            if not _mlx_probe:
                log.warning("mlx probe failed - Metal backend disabled")
    return _mlx_probe


def _cuda_device_count() -> int:
    try:
        import ctranslate2  # type: ignore
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def _cuda_available() -> bool:
    try:
        import ctranslate2  # type: ignore
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _whisper_cpp_available() -> bool:
    from .cpp_provider import binary_path
    return binary_path() is not None


def _faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def describe_backend() -> dict:
    """Report which backend would be selected without importing heavy models."""
    if _is_apple_silicon():
        if _mlx_available():
            return {"name": "mlx-whisper", "device": "metal", "detail": "Apple Silicon GPU via MLX"}
        return {"name": "none", "device": "none",
                "detail": "macOS requires Metal GPU backend (pip install mlx-whisper)"}
    if _cuda_available():
        n = _cuda_device_count()
        return {"name": "faster-whisper", "device": "cuda",
                "detail": f"NVIDIA GPU via CTranslate2 ({n} device(s))"}
    if _whisper_cpp_available():
        return {"name": "whisper.cpp", "device": "vulkan",
                "detail": "AMD/Intel GPU via whisper.cpp Vulkan (auto CPU fallback)"}
    if _faster_whisper_available():
        return {"name": "faster-whisper", "device": "cpu", "detail": "CPU int8 fallback (non-mac)"}
    return {"name": "none", "device": "none", "detail": "no ASR backend installed"}


# ---------------------------------------------------------------------------
# TODO(backend-roadmap) 三平台显卡 -> Whisper 后端映射（自动探测 + 用户可覆盖）
#   [x] MacBook / Apple Silicon    : mlx-whisper (Metal GPU)  -> mlx_provider.py    已实现
#   [ ] NVIDIA (Windows/Linux)     : faster-whisper (CUDA fp16) -> faster_provider.py 代码就绪, TODO: 真机验证+cuDNN打包
#   [ ] AMD / Intel (Windows/Linux): whisper.cpp (Vulkan)      -> cpp_provider.py    TODO: MVP2 实现
#   [x] 兜底 (Windows/Linux 无 N 卡): faster-whisper CPU int8
# ---------------------------------------------------------------------------


def select_provider(model_size: Optional[str] = None,
                    preferred: Optional[str] = None) -> ASRProvider:
    """preferred: auto | mlx-whisper | faster-whisper | whisper.cpp.
    An explicit (non-auto) provider is required to be available, else error."""
    """Auto-select the fastest available backend for this machine.

    Policy: macOS (Apple Silicon) is Metal-GPU-only - every Mac has Metal, so
    there is deliberately NO CPU fallback there. Windows/Linux use CUDA when an
    NVIDIA GPU is present, CPU int8 otherwise (Vulkan/whisper.cpp planned)."""
    if preferred in ("mlx-whisper", "faster-whisper", "whisper.cpp"):
        if preferred == "mlx-whisper" and _is_apple_silicon() and _mlx_available():
            from .mlx_provider import MLXWhisperProvider
            return MLXWhisperProvider()
        if preferred == "faster-whisper" and _faster_whisper_available():
            from .faster_provider import FasterWhisperProvider
            dev, ct = ("cuda", "float16") if _cuda_available() else ("cpu", "int8")
            return FasterWhisperProvider(device=dev, compute_type=ct)
        if preferred == "whisper.cpp" and _whisper_cpp_available():
            from .cpp_provider import WhisperCppProvider
            return WhisperCppProvider(backend="vulkan")
        raise ASRError(f"指定的识别后端 {preferred} 在当前机器不可用")
    if _is_apple_silicon():
        if _mlx_available():
            from .mlx_provider import MLXWhisperProvider
            return MLXWhisperProvider()
        raise ASRError(
            "macOS requires the Metal GPU backend (mlx-whisper) but it is not available. "
            "Install it with: pip install mlx-whisper"
        )
    if _cuda_available():
        from .faster_provider import FasterWhisperProvider
        return FasterWhisperProvider(device="cuda", compute_type="float16")
    # TODO(platform-amd): Vulkan 路线未在真实 A 卡上验证（Windows/Linux 均可尝试，
    # whisper.cpp Vulkan 构建在无 Vulkan 设备时自动回退 CPU，可安全尝试）
    if _whisper_cpp_available():
        from .cpp_provider import WhisperCppProvider
        return WhisperCppProvider(backend="vulkan")
    if _faster_whisper_available():
        from .faster_provider import FasterWhisperProvider
        return FasterWhisperProvider(device="cpu", compute_type="int8")
    raise ASRError(
        "No ASR backend available. Install mlx-whisper (macOS) or faster-whisper (Windows/Linux)."
    )
