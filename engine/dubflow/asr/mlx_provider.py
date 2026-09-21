from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import settings

from .base import ASRError, ASRProvider, BackendInfo, ProgressFn, Transcript, Segment

DEFAULT_MODEL = "large-v3-turbo"

# values may be overridden by passing any HF repo id directly (e.g. "mlx-community/whisper-large-v3-8bit")
MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base",
    "small": "mlx-community/whisper-small",
    "base": "mlx-community/whisper-base",
    "small": "mlx-community/whisper-small",
    "medium": "mlx-community/whisper-medium-mlxfp16",
    "large-v3": "mlx-community/whisper-large-v3-4bit",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo-q4": "mlx-community/whisper-large-v3-turbo-q4",
}


def resolve_repo(model_size: Optional[str]) -> str:
    """Resolve to a local dir (if pre-downloaded) or an HF repo id."""
    name = model_size or DEFAULT_MODEL
    if name.startswith("hf:"):
        return name[3:]
    mapped = MODEL_MAP.get(name, name)
    candidates = [name, mapped, Path(mapped).name]
    for c in candidates:
        local = settings.models_dir / c
        if local.is_dir():
            return str(local)
    return mapped


class MLXWhisperProvider(ASRProvider):
    """Apple Silicon GPU (Metal) via Apple's MLX framework. macOS arm64 only."""

    name = "mlx-whisper"

    def __init__(self) -> None:
        super().__init__(BackendInfo(
            name=self.name, device="metal",
            detail="Apple Silicon GPU (unified memory) via MLX",
        ))

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
        model_size: Optional[str] = None,
        progress: ProgressFn = None,
    ) -> Transcript:
        try:
            import mlx_whisper  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ASRError(
                "mlx-whisper is not installed. Run: pip install mlx-whisper"
            ) from e

        repo = resolve_repo(model_size)
        if progress:
            progress(0.05, f"loading model {repo}")
        result = mlx_whisper.transcribe(
            str(Path(audio_path)),
            path_or_hf_repo=repo,
            language=language,
            word_timestamps=False,
            fp16=True,
        )
        segments = [
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip())
            for s in result.get("segments", [])
        ]
        if progress:
            progress(1.0, f"{len(segments)} segments")
        return Transcript(language=result.get("language"), segments=segments)
