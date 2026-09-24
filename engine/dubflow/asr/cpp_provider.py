"""A卡 (AMD) / Intel 显卡方案：whisper.cpp Vulkan 后端。

TODO(MVP2-verify) 三平台后端路线：
  - MacBook (M 系列):   mlx-whisper (Metal)         -> mlx_provider.py    [已实现+已验证]
  - NVIDIA (Win/Linux):  faster-whisper (CUDA fp16) -> faster_provider.py [代码就绪, 待真机验证]
  - AMD/Intel (Win/Linux): whisper.cpp (Vulkan)      -> cpp_provider.py    [本文件, 已实现, 待 A 卡真机验证]

实现方式：调用捆绑/系统安装的 whisper.cpp CLI（whisper-cli），JSON 输出归一化为
Transcript。Vulkan 构建在无 Vulkan 设备时会自动回退 CPU，因此在任意
Windows/Linux 机器上均可安全尝试。
"""
from __future__ import annotations

import json
import re
from collections import Counter, deque
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .base import ASRError, ASRProvider, BackendInfo, ProgressFn, Segment, Transcript
from ..config import settings

DEFAULT_MODEL = "ggml-tiny"


def binary_path() -> Optional[Path]:
    """Bundled bin/whisper-cli-<platform> first, then system whisper-cli."""
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    plat = f"{sys.platform}-{machine}"
    ext = ".exe" if sys.platform == "win32" else ""
    if getattr(sys, "frozen", False):
        bin_dir = Path(sys.executable).resolve().parent
    else:
        bin_dir = Path(__file__).resolve().parents[3] / "bin"
    data_bin = Path(settings.models_dir).parent / "bin" / f"whisper-cli-{plat}{ext}"
    if data_bin.is_file():
        return str(data_bin)
    dl_dir = settings.models_dir / "whispercpp-vulkan-win64"
    if (dl_dir / "whisper-cli.exe").is_file():
        return str(dl_dir / "whisper-cli.exe")
    cand = bin_dir / f"whisper-cli-{plat}{ext}"
    if cand.is_file():
        return str(cand)
    return None
    which = shutil.which("whisper-cli")
    return Path(which) if which else None


_HALLUCINATION_MIN_COUNT = 10


def _norm_text(t: str) -> str:
    """Strip whitespace/punctuation so near-identical repeats compare equal."""
    return re.sub(r"[\s、。！？!?.,，．·・「」『』()（）\\-]", "", t or "")


def _filter_hallucinations(segments):
    """Whisper loops on music/BGM, emitting the same phrase for minutes.

    1) collapse consecutive identical texts (keep the first)
    2) drop texts repeated globally >= _HALLUCINATION_MIN_COUNT times —
       real dialogue almost never repeats verbatim that often
    """
    if not segments:
        return segments
    collapsed = []
    for s in segments:
        if collapsed and _norm_text(s.text) and _norm_text(s.text) == _norm_text(collapsed[-1].text):
            continue
        collapsed.append(s)
    counts = Counter(_norm_text(s.text) for s in collapsed if _norm_text(s.text))
    threshold = max(_HALLUCINATION_MIN_COUNT, int(len(collapsed) * 0.02))
    hallucinated = {t for t, c in counts.items() if c >= threshold}
    if hallucinated:
        collapsed = [s for s in collapsed if _norm_text(s.text) not in hallucinated]
    return collapsed


def _collapse_repeats(segments):
    """Merge runs of consecutive identical texts into one segment.

    Only touches back-to-back duplicates (whisper phrase-loop artifacts);
    scattered repetitions (real shouts appearing throughout the film) are
    preserved verbatim.
    """
    out = []
    for s in segments:
        if out and _norm_text(s.text) and _norm_text(s.text) == _norm_text(out[-1].text):
            out[-1].end = max(out[-1].end, s.end)  # extend coverage
            continue
        out.append(s)
    return out


class WhisperCppProvider(ASRProvider):
    name = "whisper.cpp"

    def __init__(self, backend: str = "vulkan") -> None:
        super().__init__(BackendInfo(
            name=self.name, device=backend,
            detail="whisper.cpp CLI (Vulkan/CUDA/Metal build; auto CPU fallback)",
        ))

    def _binary(self) -> str:
        p = binary_path()
        if p:
            return str(p)
        raise ASRError(
            "whisper.cpp CLI 未找到。请通过「模型与依赖」面板或 "
            "scripts/fetch_ffmpeg.sh 的 whisper.cpp 段落下载对应平台二进制，"
            "或安装 whisper-cpp 后确保 whisper-cli 在 PATH 中。"
        )

    def _model_file(self, model_size: Optional[str]) -> str:
        name = model_size or DEFAULT_MODEL
        d = settings.models_dir / name
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix == ".bin":
                    return str(f)
        raise ASRError(
            f"whisper.cpp 模型 {name} 未下载。请在 GUI「模型与依赖」面板下载。"
        )

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
        model_size: Optional[str] = None,
        progress: ProgressFn = None,
    ) -> Transcript:
        binary = self._binary()
        model = self._model_file(model_size)
        if progress:
            progress(0.05, f"whisper.cpp {Path(model).name}")

        out_dir = Path(tempfile.mkdtemp(prefix="dubflow-wcpp-"))
        out_prefix = out_dir / "out"
        model_path = Path(model)
        with open(model_path, "rb") as f:
            magic = f.read(4)
        if magic not in (b"ggml", b"lmgg"):  # lmgg = 同一 magic 的小端字节序
            raise ASRError(
                f"{model_path.name} 不是 whisper.cpp 的 ggml 模型"
                "（可能是 CTranslate2 格式——那是 faster-whisper 专用格式）。"
                "A 卡 Vulkan 请在「模型与依赖」面板下载 ggml-* 模型。"
            )

        # standalone quick language detection for a transparent, reusable hint
        detected = None
        if not language:
            try:
                if progress:
                    progress(0.03, "detecting language")
                dproc = subprocess.run(
                    [binary, "-m", model, "-f", str(audio_path), "-dl"],
                    capture_output=True, text=True, timeout=600,
                )
                for dline in (dproc.stdout or "").splitlines():
                    if "detected language" in dline:
                        tail = dline.split("=")[-1].strip()
                        detected = tail.split()[0] if tail else None
            except Exception:
                detected = None
        lang = language or detected or "auto"

        cmd = [
            binary, "-m", model, "-f", str(audio_path),
            "-l", lang,
            "-pp",  # print progress to stderr (progress = NN%)
            "-nf",  # no temperature fallback (WhisperJAV: temperature=[0.0])
            "-sns",  # suppress non-speech tokens
            "-mc", "0",  # no context carry-over (WhisperJAV: condition_on_previous_text=False)
            "-nth", "0.54",  # no-speech gate (WhisperJAV conservative preset)
            "-lpt", "-1.00",  # logprob threshold (WhisperJAV uniform)
            "-bs", "2",  # beam size (WhisperJAV: 95% of gain at half compute)
            "-bo", "2",  # best-of (WhisperJAV conservative)
            "-oj", "-of", str(out_prefix),
        ]
        # music/BGM-heavy tracks cause repetition hallucinations; skip
        # non-speech segments via silero VAD when the model is present
        vad_model = settings.models_dir / "ggml-silero-v5.1.2.bin"
        if vad_model.is_file():
            # 0.5 is the proven value: high-energy BGM in this content type
            # floods the decoder at lower thresholds and triggers phrase loops
            cmd += ["--vad", "-vm", str(vad_model), "-vt", "0.5"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as e:
            raise ASRError(f"whisper.cpp failed to start: {e}")
        assert proc.stderr is not None
        err_tail: deque = deque(maxlen=8)
        for line in proc.stderr:
            line = line.strip()
            if line:
                err_tail.append(line)
            # whisper.cpp -pp prints "... progress =  42%" on stderr
            if "progress =" in line and progress:
                try:
                    pct = int(line.split("progress =")[1].strip().rstrip("%").strip())
                    progress(0.05 + (pct / 100.0) * 0.85, f"识别中 {pct}%")
                except (ValueError, IndexError):
                    pass
        proc.wait()
        if proc.returncode != 0:
            raise ASRError(
                f"whisper.cpp failed ({proc.returncode}): {chr(10).join(err_tail)}"
            )
        if progress:
            progress(0.9, "parsing output")

        json_path = Path(str(out_prefix) + ".json")
        if not json_path.is_file():
            raise ASRError("whisper.cpp did not produce JSON output")
        # whisper-cli occasionally emits an invalid byte mid-JSON; replace
        # instead of crashing the whole transcription at the final step.
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        # whisper.cpp JSON: offsets are in milliseconds
        segments = [
            Segment(
                start=float(item["offsets"]["from"]) / 1000.0,
                end=float(item["offsets"]["to"]) / 1000.0,
                text=str(item.get("text", "")).replace("\ufffd", "").strip(),
            )
            for item in data.get("transcription", [])
        ]
        shutil.rmtree(out_dir, ignore_errors=True)
        if progress:
            progress(1.0, f"{len(segments)} segments ({lang})")
        segments = _collapse_repeats(segments)
        return Transcript(language=lang if lang != "auto" else None, segments=segments)
