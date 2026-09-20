"""项目配置与凭据加载。

设计原则：凭据只从环境或 Hermes 的 .env 读取，永不写入代码或日志。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 可用 SALESVOICE_DATA_DIR 把数据目录重定向（演示 / 离线 fixture 试跑时用，
# 避免把实验数据混进真实的客户库）
DATA_DIR = Path(os.environ.get("SALESVOICE_DATA_DIR") or (PROJECT_ROOT / "data"))
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


# ---------------------------------------------------------------- Notion

# 与 LLM 凭据同样的策略：显式环境变量 > Hermes .env，永不硬编码、永不打印。
NOTION_VERSION = os.environ.get("SALESVOICE_NOTION_VERSION", "2025-09-03")

# 投放区：作为「录音/会面记录」的 Notion 数据库（或页面）ID / URL。
# 留空时 CLI 会列出集成可见的候选让你挑。
NOTION_SOURCE = os.environ.get("SALESVOICE_NOTION_SOURCE", "").strip()

# 反写回 Notion（勾「已同步」+ 回贴画像摘要）。默认关闭，只读更安全。
NOTION_WRITEBACK = os.environ.get("SALESVOICE_NOTION_WRITEBACK", "0").strip().lower() \
    not in ("0", "false", "no", "off", "")


def get_notion_key() -> str:
    """Notion 集成 token：环境变量 > ~/.hermes/.env。"""
    for name in ("SALESVOICE_NOTION_API_KEY", "NOTION_API_KEY"):
        val = os.environ.get(name)
        if val:
            return val
        val = _load_key_from_hermes_env(name)
        if val:
            return val
    raise RuntimeError(
        "未找到 Notion 集成 token。请创建内部集成（notion.so/my-integrations）后设置 "
        "NOTION_API_KEY（可写进 ~/.hermes/.env），并把目标数据库「连接」给该集成。"
    )


def has_notion_key() -> bool:
    try:
        get_notion_key()
        return True
    except RuntimeError:
        return False
