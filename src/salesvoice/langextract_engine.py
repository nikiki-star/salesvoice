"""langextract 抽取引擎（可选）。

google/langextract（Apache-2.0，38.6k★）的核心能力是 **source grounding**：
LLM 输出必须挂靠到原文的字符区间（char_interval + alignment_status），
从而让每条抽取都可回溯。本模块把它的输出映射到本项目的标签体系。

实测结论（务必知悉，这决定了它为什么不是默认引擎）：

    中文对齐会随文本长度明显退化。同一份材料跑两个引擎：

      短对话（89 字）：  7 条，逐字对齐 7，可溯率 100%   ← 与自研打平
      完整转录（544 字）：11 条，逐字对齐 4 / 近似 4 / 未对齐 3，
                        可溯率 73%（自研同场景 100%）

    即文本一长，约 1/4 的标签无法锚定回原文。销售情报最怕"无法回溯"，
    所以默认引擎是 extract.extract_tags（自研中文子串回验），
    本模块作为**交叉验证的第二意见**：两个引擎都抽到且都能溯源的标签可信度最高。

    另外它的 prompt 预检对中文示例会报 "FAILED to align" 警告，不影响运行但噪音较大。

已知兼容性问题（DeepSeek 等 OpenAI 兼容端点）：

    1. langextract 默认下发 response_format=json_schema，DeepSeek 只支持
       json_object，会直接 400 → 必须传 use_schema_constraints=False。
    2. extract() 不接受 provider/base_url 关键字，必须用
       config=factory.ModelConfig(provider="openai", provider_kwargs={...})。
"""

from __future__ import annotations

from . import config
from .schema import CATEGORIES, Tag

# 少样本示例：extraction_text 必须是示例文本的连续片段，否则 langextract
# 的 prompt 预检会对齐失败。
_EXAMPLES = [
    (
        "【客户】我平时喜欢喝点红酒，周末爱去打球。不过别给我送烟，我戒了。",
        [
            {"category": "preference", "text": "喜欢喝点红酒",
             "key": "酒类偏好", "value": "喜欢喝红酒"},
            {"category": "preference", "text": "周末爱去打球",
             "key": "兴趣爱好", "value": "喜欢打球"},
            {"category": "landmine", "text": "别给我送烟，我戒了",
             "key": "礼赠禁忌", "value": "已戒烟，不要送烟",
             "severity": "medium", "trigger": "赠送香烟类礼品时"},
        ],
    ),
    (
        "【客户】我痛风好几年了，医生说酒一口都不能碰。吃饭我爱吃辣。",
        [
            {"category": "fact", "text": "痛风好几年了",
             "key": "健康状况", "value": "患痛风多年"},
            {"category": "landmine", "text": "医生说酒一口都不能碰",
             "key": "饮酒禁忌", "value": "医生禁止饮酒",
             "severity": "high", "trigger": "饭局劝酒、敬酒时"},
        ],
    ),
]

PROMPT = (
    "从销售与客户的对话中抽取客户情报标签。每条抽取必须是原文中的**连续片段**，"
    "禁止改写、拼接或概括。\n\n"
    "extraction_class 取值：" + "、".join(f"{k}（{v['label']}）" for k, v in CATEGORIES.items()) + "\n"
    "其中 landmine 表示客户明确表达的反感、忌讳、禁忌，是最高价值的信息。\n\n"
    "每条抽取需带 attributes：key（4-8 字标签键）、value（一句话值）、"
    "severity（仅 landmine：high/medium/low）、trigger（仅 landmine：什么场景会踩雷）。"
)


def is_available() -> bool:
    try:
        import langextract  # noqa: F401
        return True
    except ImportError:
        return False


def _build_examples():
    from langextract import data as lxdata

    out = []
    for text, items in _EXAMPLES:
        out.append(lxdata.ExampleData(
            text=text,
            extractions=[
                lxdata.Extraction(
                    extraction_class=it["category"],
                    extraction_text=it["text"],
                    attributes={
                        "key": it.get("key", ""),
                        "value": it.get("value", ""),
                        "severity": it.get("severity", ""),
                        "trigger": it.get("trigger", ""),
                    },
                )
                for it in items
            ],
        ))
    return out


def run_langextract(transcript: str, client_name: str = "客户",
                    meeting_id: str = "M001", verbose: bool = True) -> list[Tag]:
    """用 langextract 抽取客户标签，并把它的对齐结果映射为证据可信度。"""
    import langextract as lx
    from langextract import factory

    if verbose:
        print(f"[langextract] 抽取中… 转录 {len(transcript)} 字")

    cfg = factory.ModelConfig(
        model_id=config.LLM_MODEL,
        provider="openai",           # OpenAI 兼容端点（DeepSeek 走这里）
        provider_kwargs={
            "base_url": config.LLM_BASE_URL,
            "api_key": config.get_api_key(),
        },
    )

    result = lx.extract(
        text_or_documents=transcript,
        prompt_description=PROMPT,
        examples=_build_examples(),
        config=cfg,
        use_schema_constraints=False,   # DeepSeek 不支持 json_schema，必须关
        show_progress=False,
    )

    docs = result if isinstance(result, list) else [result]
    tags: list[Tag] = []
    for doc in docs:
        for e in (getattr(doc, "extractions", None) or []):
            cat = (e.extraction_class or "").strip()
            if cat not in CATEGORIES:
                continue
            attrs = getattr(e, "attributes", None) or {}
            span = getattr(e, "char_interval", None)
            status = str(getattr(e, "alignment_status", "") or "")

            # lungoxtract 的 alignment_status 直接告诉我们证据是否真的锚定原文
            verified = "MATCH_EXACT" in status or "MATCH_FUZZY" in status
            mode = "exact" if "MATCH_EXACT" in status else ("fuzzy" if "MATCH_FUZZY" in status else "")

            # 区间回切校验：对齐状态说命中，但切出来的文不对，则降级
            if verified and span:
                snippet = transcript[span.start_pos:span.end_pos]
                if snippet != e.extraction_text:
                    mode = "fuzzy" if mode == "exact" else mode
                    if abs(len(snippet) - len(e.extraction_text)) > max(4, len(e.extraction_text) // 3):
                        verified, mode = False, ""

            severity = str(attrs.get("severity", "") or "").strip().lower()
            if cat != "landmine":
                severity = ""
            elif severity not in ("high", "medium", "low"):
                severity = "medium"

            conf = {"exact": 0.9, "fuzzy": 0.75, "": 0.45}[mode]
            tags.append(Tag(
                category=cat,
                key=str(attrs.get("key", "") or "").strip()[:20] or cat,
                value=str(attrs.get("value", "") or e.extraction_text).strip()[:200],
                evidence=(e.extraction_text or "")[:300],
                confidence=conf,
                speaker="客户",
                severity=severity,
                trigger=str(attrs.get("trigger", "") or "").strip()[:120],
                meeting_id=meeting_id,
                evidence_verified=verified,
                evidence_mode=mode,
            ))

    if verbose:
        ex = sum(1 for t in tags if t.evidence_mode == "exact")
        fz = sum(1 for t in tags if t.evidence_mode == "fuzzy")
        print(f"[langextract] 完成：{len(tags)} 条（对齐精确 {ex}，近似 {fz}，"
              f"未对齐 {len(tags) - ex - fz}）")
    return tags
