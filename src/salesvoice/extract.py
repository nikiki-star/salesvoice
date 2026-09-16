"""情报抽取引擎：对话转录 → 结构化客户标签。

核心机制是 **证据回验（evidence grounding）**：
LLM 输出的每一条标签都必须附带原文片段，我们把它回掷到转录文本里做子串校验，
校验不通过的标签会被标记为 `evidence_verified=False` 并在置信度上打折，
从工程上压制 LLM 幻觉 —— 这正是 google/langextract 的核心思想，
本项目在销售情报场景下做了落地实现。
"""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher

from . import config
from .schema import CATEGORIES, Tag, build_prompts

# ---------------------------------------------------------------- 文本归一化

_PUNCT_MAP = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "（": "(", "）": ")", "《": "<",
    "》": ">", "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "－": "-", "～": "~", "　": " ",
}


def normalize(text: str) -> str:
    """归一化：全角转半角、去所有空白与标点差异，用于宽松子串匹配。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _PUNCT_MAP.items():
        text = text.replace(src, dst)
    text = re.sub(r"[\s]+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    return text.lower()


def verify_evidence(evidence: str, transcript: str,
                    fuzzy_threshold: float = 0.78) -> tuple[bool, str]:
    """校验证据片段是否真的出自转录原文。

    返回 (是否通过, 模式)：
      ("exact") 逐字命中 —— 最可信
      ("fuzzy") 近似命中 —— 语音识别有错字（如"不吃辣"识别成"不吃啦"）时的回退，
                以最长公共子串占证据长度的比例判定
      ("")      未通过 —— 幻觉嫌疑，调用方应当降分并提示复核
    """
    ev = normalize(evidence)
    tx = normalize(transcript)
    if not ev or len(ev) < 4 or not tx:
        return False, ""

    if ev in tx:
        return True, "exact"

    # 模糊回退。长文本下 SequenceMatcher 代价高，先做长度与字符集粗筛。
    if len(tx) > 200_000 or len(ev) > 2_000:
        return False, ""
    share = len(set(ev) & set(tx)) / max(1, len(set(ev)))
    if share < 0.5:            # 用字重合度过低，不可能出自原文
        return False, ""

    sm = SequenceMatcher(None, ev, tx, autojunk=False)
    # 用全部匹配块的总长度占比，而非最长单块 —— 这样"中间夹一个错字"
    # （吃饭我不吃辣 vs 吃饭我不吃啦）也能识别为同源。
    # SequenceMatcher 的匹配块要求顺序一致，随机文本无法累积高占比。
    matched = sum(b.size for b in sm.get_matching_blocks())
    if len(ev) <= 6:
        ok = matched >= max(3, len(ev) - 1)      # 短证据容错空间小
    else:
        ok = matched / len(ev) >= fuzzy_threshold
    if ok:
        return True, "fuzzy"
    return False, ""


# ---------------------------------------------------------------- LLM 调用


def _chat(system: str, user: str, timeout: int = 180, json_mode: bool = True) -> str:
    """调用 LLM。

    json_mode=True 时启用 JSON 输出约束 —— 注意 DeepSeek/OpenAI 要求此时
    prompt 中必须出现 "json" 字样，否则会报 400；要输出 Markdown 时请传 False。
    """
    from openai import OpenAI

    client = OpenAI(api_key=config.get_api_key(), base_url=config.LLM_BASE_URL, timeout=timeout)
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------- 主流程


def extract_tags(
    transcript: str,
    client_name: str = "客户",
    meeting_id: str = "M001",
    verbose: bool = True,
    engine: str = "default",
) -> list[Tag]:
    """从转录文本抽取客户标签，并做证据回验。

    engine:
      "default"     自研引擎 —— 中文子串回验（exact/fuzzy），默认
      "langextract" google/langextract 引擎 —— 依赖其 char_interval 对齐，
                     中文场景对齐质量不稳定，建议作为交叉验证的第二意见
    """
    if engine == "langextract":
        from .langextract_engine import run_langextract

        return run_langextract(transcript, client_name, meeting_id, verbose)

    system, user = build_prompts(transcript, client_name, meeting_id)
    if verbose:
        print(f"[extract] 调用 LLM 抽取中… 转录长度 {len(transcript)} 字")

    data = _parse_json(_chat(system, user))
    raw_items = data.get("items") or []
    if verbose:
        print(f"[extract] LLM 返回 {len(raw_items)} 条原始标签，开始证据回验…")

    tags: list[Tag] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category", "")).strip()
        if cat not in CATEGORIES:
            cat = "fact" if not cat else cat
        if cat not in CATEGORIES:
            continue

        evidence = str(it.get("evidence", "")).strip()
        verified, ev_mode = verify_evidence(evidence, transcript)

        try:
            conf = float(it.get("confidence", 0.8))
        except (TypeError, ValueError):
            conf = 0.8
        conf = max(0.0, min(1.0, conf))
        if ev_mode == "fuzzy":
            conf *= 0.92          # 近似命中：识别有误差，轻度打折
        elif not verified:
            conf *= 0.5           # 证据对不上原文：幻觉嫌疑，重罚

        severity = str(it.get("severity", "") or "").strip().lower()
        if cat != "landmine" or severity not in ("high", "medium", "low"):
            severity = "" if cat != "landmine" else (severity or "medium")

        tags.append(
            Tag(
                category=cat,
                key=str(it.get("key", "")).strip()[:20],
                value=str(it.get("value", "")).strip()[:200],
                evidence=evidence[:300],
                confidence=round(conf, 2),
                speaker=str(it.get("speaker", "")).strip()[:10],
                severity=severity,
                trigger=str(it.get("trigger", "")).strip()[:120],
                meeting_id=meeting_id,
                evidence_verified=verified,
                evidence_mode=ev_mode,
            )
        )

    if verbose:
        exact = sum(1 for t in tags if t.evidence_mode == "exact")
        fuzzy = sum(1 for t in tags if t.evidence_mode == "fuzzy")
        land = sum(1 for t in tags if t.category == "landmine")
        print(f"[extract] 完成：{len(tags)} 条标签（逐字回验 {exact} 条，"
              f"近似回验 {fuzzy} 条，雷区 {land} 条）")
        failed = [t for t in tags if not t.evidence_verified]
        if failed:
            print("[extract] 以下标签证据未通过回验（已降分），建议人工复核：")
            for t in failed[:5]:
                print(f"          - {t.key}: {t.value[:40]} | 证据: {t.evidence[:40]}")
    return tags


def summarize_meeting(transcript: str, client_name: str = "客户") -> dict:
    """生成会面摘要（这是各开源方案的主战场，本项目作为附加能力保留）。"""
    system = (
        "你是中文商务会谈记录员。只依据给定转录作答，不要编造。"
        "输出 JSON：{\"summary\": \"...\", \"topics\": [\"...\"], \"next_actions\": [\"...\"]}"
    )
    user = f"以下是 {client_name} 的会面转录：\n\n{transcript}\n\n请输出 JSON 摘要。"
    try:
        return _parse_json(_chat(system, user, timeout=120))
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"（摘要生成失败：{exc}）", "topics": [], "next_actions": []}
