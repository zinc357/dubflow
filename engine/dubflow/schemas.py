from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel


class ASROptions(BaseModel):
    provider: str = "auto"          # auto | mlx-whisper | faster-whisper | whisper.cpp
    device: Optional[str] = None    # auto | gpu | cpu（新 GUI 使用）
    size: Optional[str] = None      # tiny | base | small | medium | large-v3 | large-v3-turbo
    model: Optional[str] = None     # 旧客户端显式模型键（兼容保留）


class TranslationOptions(BaseModel):
    enabled: bool = False
    provider: str = "llm"           # llm | google | microsoft
    base_url: Optional[str] = None  # llm only; default from settings (env)
    api_key: Optional[str] = None   # llm or microsoft(azure key)
    region: Optional[str] = None    # microsoft azure region, e.g. global
    model: Optional[str] = None     # llm only


class ExportOptions(BaseModel):
    variant: str = "bilingual"      # source | target | bilingual  (三选一)
    save_to_video_folder: bool = True   # 交付方式一：字幕存到输出目录
    embed_video: bool = False           # 交付方式二：同时嵌入字幕生成新视频
    output_dir: Optional[str] = None    # 输出目录；留空则用原视频所在目录


class JobCreate(BaseModel):
    video_path: str
    source_language: Optional[str] = None   # None = auto detect
    target_language: str = "zh"
    asr: ASROptions = ASROptions()
    translation: TranslationOptions = TranslationOptions()
    export: ExportOptions = ExportOptions()
