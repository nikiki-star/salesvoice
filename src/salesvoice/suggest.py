"""AI 建议引擎。

把客户画像转成**可执行的行动建议**。这是开源生态完全空白的一层：
现有工具止步于"把对话总结成文字"，而销售真正需要的是
"下次见面我该怎么做、这顿饭我该注意什么"。

设计约束：所有建议必须挂靠到具体标签，并在输出中标注依据，
避免 LLM 给出放之四海皆准的正确废话。
"""

from __future__ import annotations

import json
import re

from . import config
from .extract import _chat, _parse_json  # noqa: PLC2701  复用 LLM 通道与 JSON 解析
from .schema import CATEGORIES

# ---------------------------------------------------------------- 建议类型

ADVICE_TYPES = {
    "brief": {
        "label": "会前简报",
        "desc": "下一次拜访前读这份：聊什么、避什么、怎么开场、带什么不带什么",
    },
    "banquet": {
        "label": "宴请建议",
        "desc": "饭局方案：点菜方向、酒水处理、座位与话题黑名单",
    },
    "followup": {
        "label": "跟进策略",
        "desc": "本次见面后的推进建议：动作、节奏、切入角度",
    },
    "gift": {
        "label": "礼赠建议",
        "desc": "合适的礼物方向与明确的送礼禁区",
    },
}

_BASE_RULES = """你在为一名中国销售人员做客户关系参谋。

铁律：
1. **只依据下方的客户情报标签作答**。标签里没有的信息，一律不要编造，
   也不要用"一般来说""通常商务场合"这类通用套话填充。
2. 每条建议后面用括号标注依据的标签键，例如（依据：饮酒禁忌）。
   如果某条建议找不到标签支撑，就不要写。
3. **雷区（landmine）类标签优先级最高**，必须在"避雷"部分逐一覆盖，
   并明确说明踩雷的具体场景。
4. 语气务实、直接、可执行，不要客套话和免责声明。
5. 用中文，Markdown 输出，篇幅控制在 400 字以内。
"""

_BASE_TMPL = """{rules}

## 客户画像
客户：{name}（{company} {title}）
{profile_text}

## 历史会面
{meetings_text}

## 任务
请生成【{advice_label}】：{advice_desc}

输出结构：
- 先用一句话总结这个客户当前状态
- 然后分点给建议，每点必须标注依据标签
- 最后单列一段「⚠️ 避雷清单」，逐条列出绝对不能触碰的事项及场景
"""


def _profile_text(profile: dict) -> str:
    lines = []
    for cid, meta in CATEGORIES.items():
        items = profile["categories"].get(cid, {}).get("items", [])
        if not items:
            continue
        lines.append(f"【{meta['label']}】")
        for t in items:
            sev = f"（严重度 {t['severity']}）" if t.get("severity") else ""
            trig = f" 触发场景：{t['trigger_when']}" if t.get("trigger_when") else ""
            lines.append(f"  - {t['key']}：{t['value']}{sev}{trig}")
    return "\n".join(lines) if lines else "（暂无情报标签）"


def _meetings_text(meetings: list[dict]) -> str:
    if not meetings:
        return "（无会面记录）"
    out = []
    for m in meetings[:5]:
        s = (m.get("summary") or "").strip()
        out.append(f"- {m.get('met_on', '?')} @ {m.get('location') or '未记录'}：{s[:150] or '（无摘要）'}")
    return "\n".join(out)


def generate_advice(profile: dict, advice_type: str = "brief") -> dict:
    """基于客户画像生成建议。返回 {type, label, markdown, based_on}。"""
    if advice_type not in ADVICE_TYPES:
        raise ValueError(f"未知建议类型: {advice_type}（可选 {list(ADVICE_TYPES)}）")

    meta = ADVICE_TYPES[advice_type]
    client = profile["client"]
    system = "你是资深中国商务关系顾问，只依据给定情报作答，绝不编造。"
    prompt = _BASE_TMPL.format(
        rules=_BASE_RULES,
        name=client.get("name", ""),
        company=client.get("company", "") or "未知",
        title=client.get("title", "") or "",
        profile_text=_profile_text(profile),
        meetings_text=_meetings_text(profile.get("meetings", [])),
        advice_label=meta["label"],
        advice_desc=meta["desc"],
    )

    try:
        text = _chat(system, prompt, timeout=180, json_mode=False)
    except Exception as exc:  # noqa: BLE001
        return {"type": advice_type, "label": meta["label"],
                "markdown": f"（建议生成失败：{exc}）", "based_on": []}

    # 提取建议中引用的标签键，用于前端高亮"建议→证据"链路
    cited = re.findall(r"依据[:：]\s*([^）\)\s]+)", text)
    return {
        "type": advice_type,
        "label": meta["label"],
        "markdown": text.strip(),
        "based_on": sorted(set(cited)),
    }


def quick_landmine_check(profile: dict, scenario_text: str) -> dict:
    """场景预检：'我打算这次带一瓶茅台过去' → 是否踩雷。"""
    landmines = [t for t in profile["categories"].get("landmine", {}).get("items", [])]
    if not landmines:
        return {"risk": "unknown", "markdown": "该客户暂无雷区记录。", "hits": []}

    lm_text = "\n".join(
        f"- {t['key']}（{t['severity']}）：{t['value']}｜触发场景：{t['trigger_when'] or '未记录'}"
        for t in landmines
    )
    system = "你是商务风险审查员。只依据给定雷区清单判断，不做泛化推测。"
    prompt = (
        f"## 该客户的雷区清单\n{lm_text}\n\n"
        f"## 我计划做的事\n{scenario_text}\n\n"
        "请判断这个计划是否触碰雷区。输出 JSON：\n"
        '{"risk": "high|medium|low|none", "hits": [{"key":"命中的雷区键","reason":"为什么命中",'
        '"advice":"应该怎么改"}], "verdict": "一句话结论"}\n'
        "如果没有命中任何雷区，hits 为空数组，risk 为 none。"
    )
    try:
        data = _parse_json(_chat(system, prompt, timeout=120))
    except Exception as exc:  # noqa: BLE001
        return {"risk": "unknown", "markdown": f"（预检失败：{exc}）", "hits": []}
    return {
        "risk": data.get("risk", "unknown"),
        "verdict": data.get("verdict", ""),
        "hits": data.get("hits", []),
    }
