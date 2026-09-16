"""项目配置与凭据加载。

设计原则：凭据只从环境或 Hermes 的 .env 读取，永不写入代码或日志。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
AUDIO_DIR = DATA_DIR / "audio"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
DB_PATH = DATA_DIR / "salesvoice.db"
WEB_DIR = PROJECT_ROOT / "web"

for _d in (DATA_DIR, AUDIO_DIR, TRANSCRIPT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- LLM

# 默认走 DeepSeek（Hermes 已配置的后端），可通过环境变量覆盖为任意
# OpenAI 兼容端点，便于切换到本地 Ollama。
LLM_BASE_URL = os.environ.get("SALESVOICE_LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("SALESVOICE_LLM_MODEL", "deepseek-chat")

_HERMES_ENV = Path.home() / ".hermes" / ".env"


def _load_key_from_hermes_env(name: str) -> str | None:
    """从 ~/.hermes/.env 读取密钥，不打印、不落盘。"""
    if not _HERMES_ENV.exists():
        return None
    try:
        for line in _HERMES_ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip("'\"")
    except OSError:
        return None
    return None


def get_api_key() -> str:
    """按优先级取凭据：显式环境变量 > Hermes .env。"""
    for name in ("SALESVOICE_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(name)
        if val:
            return val
        val = _load_key_from_hermes_env(name)
        if val:
            return val
    raise RuntimeError(
        "未找到 LLM 凭据。请设置环境变量 SALESVOICE_LLM_API_KEY，"
        "或确认 ~/.hermes/.env 中存在 DEEPSEEK_API_KEY。"
    )


def has_api_key() -> bool:
    try:
        get_api_key()
        return True
    except RuntimeError:
        return False
