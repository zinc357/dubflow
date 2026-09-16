from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# 用户配置持久化（GUI 保存的翻译配置）
#
# 取值优先级：**文件 > 环境变量 > 内置默认**。
# 理由：环境变量面向脚本化/无界面场景做兜底，而 GUI 是用户显式配置的入口，
# 用户刚保存的值不应该被一个旧的环境变量盖掉。
# 文件放在 data_dir（默认 ~/.dubflow/settings.json），缺失或损坏时静默回落，
# 绝不让配置问题导致引擎起不来。
# ---------------------------------------------------------------------------
USER_SETTINGS_FILENAME = "settings.json"

# 允许被 GUI 持久化的字段（同时是 Settings 的属性名）
USER_SETTING_KEYS = frozenset({
    "translate_base_url",
    "translate_api_key",
    "translate_model",
    "msft_translator_key",
    "msft_translator_region",
})

# 环境变量给出的基线值，"清除配置" 时回落到这里
_ENV_BASELINE: Dict[str, str] = {}


def user_settings_path(data_dir: Optional[Path] = None) -> Path:
    base = data_dir if data_dir is not None else settings.data_dir
    return base / USER_SETTINGS_FILENAME


def read_user_settings(data_dir: Optional[Path] = None) -> Dict[str, str]:
    """读取 settings.json；任何异常都当作「没有配置」处理。"""
    try:
        raw = json.loads(user_settings_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in USER_SETTING_KEYS and isinstance(v, str)}


def write_user_settings(values: Dict[str, str], data_dir: Optional[Path] = None) -> None:
    """整体覆盖写入，先写临时文件再原子替换，避免中途失败留下半个文件。"""
    path = user_settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def update_user_settings(updates: Dict[str, Optional[str]]) -> Dict[str, str]:
    """按字段更新并立即生效。

    updates 的语义：
      None → 保持原样（前端没动这个字段）
      ""   → 清除（回落到环境变量基线）
      其它 → 覆盖
    """
    current = read_user_settings()
    changed: Dict[str, str] = {}
    for key, value in updates.items():
        if key not in USER_SETTING_KEYS:
            continue
        if value is None:
            continue
        if value == "":
            current.pop(key, None)
            setattr(settings, key, _ENV_BASELINE.get(key, ""))
        else:
            current[key] = value
            setattr(settings, key, value)
            changed[key] = value
    write_user_settings(current)
    return current


def mask_secret(value: str) -> str:
    """生成可安全展示的提示串，如 "sk-…1a2b"。绝不返回完整明文。"""
    if not value:
        return ""
    tail = value[-4:]
    head = value[:3] if len(value) > 7 else ""
    return f"{head}…{tail}" if head else f"…{tail}"


@dataclass
class Settings:
    host: str
    port: int
    data_dir: Path
    models_dir: Path
    hf_endpoint: str
    translate_base_url: str
    translate_api_key: str
    translate_model: str
    msft_translator_key: str
    msft_translator_region: str

    @classmethod
    def load(cls) -> "Settings":
        default_data = Path.home() / ".dubflow"
        data_dir = Path(_env("DUBFLOW_DATA_DIR", str(default_data))).expanduser()
        inst = cls(
            host=_env("DUBFLOW_HOST", "127.0.0.1"),
            port=int(_env("DUBFLOW_PORT", "8741")),
            data_dir=data_dir,
            models_dir=Path(_env("DUBFLOW_MODELS_DIR", str(Path.home() / ".dubflow" / "models"))).expanduser(),
            hf_endpoint=_env("HF_ENDPOINT", "https://hf-mirror.com"),
            translate_base_url=_env("DUBFLOW_TRANSLATE_BASE_URL", "https://api.openai.com/v1"),
            translate_api_key=_env("DUBFLOW_TRANSLATE_API_KEY", ""),
            translate_model=_env("DUBFLOW_TRANSLATE_MODEL", "gpt-4o-mini"),
            msft_translator_key=_env("DUBFLOW_MSFT_TRANSLATOR_KEY", ""),
            msft_translator_region=_env("DUBFLOW_MSFT_TRANSLATOR_REGION", "global"),
        )
        # 记下环境变量基线，"清除配置" 时回落到它
        _ENV_BASELINE.update({key: getattr(inst, key) for key in USER_SETTING_KEYS})
        # GUI 保存过的配置优先级更高
        for key, value in read_user_settings(data_dir).items():
            setattr(inst, key, value)
        return inst


settings = Settings.load()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.models_dir.mkdir(parents=True, exist_ok=True)

# Prefer project-bundled ffmpeg/ffprobe (bin/) everywhere in the engine process,
# including third-party libs that shell out to bare "ffmpeg" (e.g. mlx_whisper).
_BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
os.environ["PATH"] = f"{_BIN_DIR}{os.pathsep}" + os.environ.get("PATH", "")
# huggingface_hub reads this at import time; keep CN-friendly default,
# override with HF_ENDPOINT=https://huggingface.co if you prefer.
os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)


# ---------------------------------------------------------------------------
# 本地模型目录解析
#
# 同一个模型在工程里有两种命名，而且两个方向都会出现：
#   * 下载器（downloads.CATALOG）落盘时用键名，例如 "faster-whisper-base"，
#     它同时也是 models_dir 下的目录名，GUI 的 ctranslate2 下拉框也发这个名字；
#   * 而 faster-whisper 自己只认短名 "base" / "large-v3-turbo"。
# 早期实现只做了「短名 → 补前缀」这一个方向，于是下载器下好的模型（目录名带前缀）
# 引擎反而找不到，最后把 "faster-whisper-base" 原样交给 WhisperModel()，
# 直接抛 ValueError: Invalid model size。现在两个方向都认。
# ---------------------------------------------------------------------------
_MODEL_DIR_ALIASES: Dict[str, str] = {
    "tiny": "faster-whisper-tiny",
    "base": "faster-whisper-base",
    "small": "faster-whisper-small",
    "medium": "faster-whisper-medium",
    "large-v3": "faster-whisper-large-v3",
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
    "turbo": "faster-whisper-large-v3-turbo",
}

# 下载器键名前缀（CATALOG 的键 = models_dir 下的目录名）
_CT2_MODEL_PREFIX = "faster-whisper-"


def canonical_model_name(model_size: str) -> str:
    """把下载器键名归一成 faster-whisper 认得的模型名。

    本地找不到模型目录时，这个名字会被交给 WhisperModel() 由它去 HF 拉取。
    而 WhisperModel 的白名单里是 "base" / "large-v3-turbo" 这类短名，
    传 "faster-whisper-base" 会直接抛 ValueError，所以必须剥掉前缀。
    """
    if model_size.startswith(_CT2_MODEL_PREFIX):
        stripped = model_size[len(_CT2_MODEL_PREFIX):]
        if stripped:
            return stripped
    return model_size


# 反向索引：下载器键名 -> 指向它的所有短名
# 例：faster-whisper-large-v3-turbo -> ["large-v3-turbo", "turbo"]
# faster-whisper 把 turbo 和 large-v3-turbo 视为同一模型，目录名可能落在任一侧，
# 所以解析时要顺着这层关系一起展开，否则 "turbo" 会漏掉名为 large-v3-turbo 的目录。
_SHORT_OF: Dict[str, List[str]] = {}
for _short_name, _prefixed_name in _MODEL_DIR_ALIASES.items():
    _SHORT_OF.setdefault(_prefixed_name, []).append(_short_name)


def _model_dir_candidates(model_size: str) -> List[str]:
    """枚举该模型在 models_dir 下所有可能的目录名（去重并保持顺序）。"""
    names: List[str] = []

    def add(name: str) -> bool:
        if name and name not in names:
            names.append(name)
            return True
        return False

    add(model_size)
    short = canonical_model_name(model_size)
    if short == model_size:
        # 输入是短名 → 顺带试下载器键名
        add(f"{_CT2_MODEL_PREFIX}{model_size}")
    else:
        # 输入是下载器键名 → 顺带试剥掉前缀的短名
        add(short)

    # 别名组内互相展开（收敛性由候选集有限保证）
    changed = True
    while changed:
        changed = False
        for name in list(names):
            for linked in (_MODEL_DIR_ALIASES.get(name), *_SHORT_OF.get(name, [])):
                if add(linked):
                    changed = True

    return names


def resolve_model_dir(model_size: str) -> Optional[Path]:
    """返回本地已就绪的模型目录（要求内含 model.bin），没有则返回 None。"""
    # 也允许直接传目录路径，方便手工指定模型
    direct = Path(model_size).expanduser()
    if (direct.is_absolute() or os.sep in model_size or "/" in model_size) \
            and (direct / "model.bin").is_file():
        return direct

    for name in _model_dir_candidates(model_size):
        candidate = settings.models_dir / name
        if (candidate / "model.bin").is_file():
            return candidate
    return None
