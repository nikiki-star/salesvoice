"""客户情报标签体系（Schema）。

这是本项目的核心设计资产，也是与现有开源方案的分界线：
市面上的工具输出"会议纪要"，本 Schema 输出"客户画像标签"，
并且**强制每条标签携带原文证据**，从结构上杜绝 LLM 编造。

标签分六类，其中 `landmine`（雷区）是负向情报，是避雷功能的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- 分类定义

CATEGORIES: dict[str, dict] = {
    "fact": {
        "label": "事实",
        "desc": "客观背景信息：行业、公司、职位、籍贯、代际、家庭结构、教育背景、健康客观状况",
        "color": "#4a7c9b",
    },
    "preference": {
        "label": "偏好",
        "desc": "明确表达或反复体现的喜好：饮食口味、酒类茶烟、兴趣爱好、运动、收藏、旅行、品牌、常聊话题",
        "color": "#5b8f5b",
    },
    "landmine": {
        "label": "雷区",
        "desc": (
            "明确表达的反感、忌讳、禁止触碰的事项。包含话题禁忌、人物忌讳、饮食禁忌、"
            "健康禁忌、宗教政治敏感、行为禁忌、称呼禁忌。必须带严重度与触发场景"
        ),
        "color": "#b23b3b",
    },
    "style": {
        "label": "风格",
        "desc": "沟通与决策风格：直爽/含蓄、数据导向/关系导向、快节奏/慢热、理性/感性、决策方式",
        "color": "#8a6d9e",
    },
    "relation": {
        "label": "关系",
        "desc": "关系状态：关系阶段、信任度、关键联系人、跟进节奏偏好、人情往来偏好",
        "color": "#b08d3f",
    },
    "business": {
        "label": "商务",
        "desc": "交易相关信息：需求与痛点、预算、决策链、竞品态度、时间节点、顾虑与异议",
        "color": "#3f7ab0",
    },
}

SEVERITY_LEVELS = {
    "high": "绝对不能踩 —— 一旦触碰，关系可能直接受损",
    "medium": "应当避免 —— 会引起明显不适或尴尬",
    "low": "最好留意 —— 轻微不快，影响有限",
}

# ---------------------------------------------------------------- 标签结构


@dataclass
class Tag:
    """一条客户情报标签。"""

    category: str          # CATEGORIES 的键
    key: str               # 标签键，如 "饮食忌口"、"酒类偏好"、"话题禁忌"
    value: str             # 标签值，具体内容
    evidence: str          # 原文证据（必须是转录文本的子串）
    confidence: float = 0.8  # 0-1 置信度
    speaker: str = ""      # 说话人（客户 / 我方 / 未知）
    severity: str = ""     # 仅 landmine 使用：high / medium / low
    trigger: str = ""      # 仅 landmine 使用：在什么场景下会踩雷
    meeting_id: str = ""   # 来源会面 ID
    evidence_verified: bool = False  # 证据是否在原文中校验通过
    evidence_mode: str = ""  # exact=逐字命中 / fuzzy=近似命中（ASR 有错字）/ 空=未通过

    def to_dict(self) -> dict:
        return asdict(self)


def categories_for_prompt() -> str:
    """生成给 LLM 的分类说明文本。"""
    lines = []
    for cid, meta in CATEGORIES.items():
        lines.append(f"- {cid}（{meta['label']}）：{meta['desc']}")
    return "\n".join(lines)


def severity_for_prompt() -> str:
    return "\n".join(f"  * {k}：{v}" for k, v in SEVERITY_LEVELS.items())


# ---------------------------------------------------------------- 抽取指令

EXTRACTION_SYSTEM_PROMPT = """你是一名资深中文商务情报分析师，专门为销售人员做客户情报提炼。

你的任务：从一段销售会面的对话转录中，提炼出**可长期复用的客户情报标签**。

最重要的原则（违反即为失败）：
1. **绝不允许编造**。每一条标签都必须附上对话中的**原文片段**作为证据。
   如果对话里没有明确依据，就不要输出这条标签。宁可少，不可假。
2. evidence 字段必须是转录文本中**逐字出现**的连续片段（8-60 字为宜），
   可以跨越标点，但不得改写、不得拼接不连续的句子。
3. 特别注意 **landmine（雷区）** 类：客户表达的反感、忌讳、"不喜欢"、
   "别"、"最烦"、"受不了"、"忌讳"等内容。这类信息对销售最有价值，务必捕捉。
   负面情绪往往以委婉方式表达（如"这个就算了"、"我不太方便"），要能识别。
4. 区分说话人。只有**客户**说的内容才能作为客户情报；我方说的话不算，
   除非是客户的明确回应。speaker 字段填"客户"或"我方"。
5. 常识推断不等于情报。客户说"我是广东人"可以推出偏好清淡饮食吗？不可以——
   除非他自己说了。

分类与严重度定义：
{categories}

landmine 的 severity 取值：
{severity}
"""

EXTRACTION_USER_PROMPT = """以下是会面 #{meeting_id} 的对话转录（{client_name} 与销售人员）：

<转录开始>
{transcript}
<转录结束>

请提炼客户情报标签，以 JSON 输出，格式：

{{
  "items": [
    {{
      "category": "preference",
      "key": "饮食口味",
      "value": "偏好清淡，喜欢粤菜",
      "evidence": "我平时吃得比较清淡，粤菜多一些",
      "confidence": 0.9,
      "speaker": "客户",
      "severity": "",
      "trigger": ""
    }}
  ]
}}

字段说明：
- category：fact / preference / landmine / style / relation / business 之一
- key：简短标签键（4-8 字），同类信息用同一个 key，便于聚合
- value：标签值，一句话说清
- evidence：原文逐字片段，必填，不得为空
- confidence：0-1 的置信度。客户明确直说给 0.9+，委婉暗示给 0.5-0.7
- severity：仅 landmine 填写 high/medium/low，其他类别留空
- trigger：仅 landmine 填写，说明"在什么场景下会踩雷"

再强调一次：evidence 必须能在上面的转录中原样找到。找不到就不要输出这条。
只输出 JSON，不要任何其他文字。"""


def build_prompts(transcript: str, client_name: str = "客户", meeting_id: str = "M001") -> tuple[str, str]:
    """返回 (system_prompt, user_prompt)。"""
    system = EXTRACTION_SYSTEM_PROMPT.format(
        categories=categories_for_prompt(),
        severity=severity_for_prompt(),
    )
    user = EXTRACTION_USER_PROMPT.format(
        meeting_id=meeting_id,
        client_name=client_name,
        transcript=transcript,
    )
    return system, user
