"""langextract 接入探测：确认能否用 DeepSeek 这类 OpenAI 兼容端点驱动。

langextract (google, Apache-2.0) 的核心价值是 **source grounding**：
抽取结果自带 char_interval（字符区间）与 alignment_status，可直接回溯原文，
这正是"销售情报不能编造"需要的机制。本脚本验证它能否跑在我们的 LLM 通道上。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langextract.providers import router  # noqa: E402

from salesvoice import config  # noqa: E402

print("=== 已注册的 provider ===")
try:
    provs = router.list_providers()
    print(provs)
except Exception as exc:  # noqa: BLE001
    print("list_providers 失败:", exc)

print(f"\n=== 目标端点 ===\nbase_url={config.LLM_BASE_URL}\nmodel={config.LLM_MODEL}")

TEXT = (
    "【销售】王总，喝茶还是喝水？\n"
    "【客户】这是我珍藏的普洱，我一天不喝这个就不舒服。\n"
    "【客户】我痛风好几年了，医生说酒一口都不能碰，谁要是硬劝我，我直接就翻脸。\n"
    "【客户】吃饭我不吃辣，清淡点就行，最好是粤菜。"
)

import langextract as lx  # noqa: E402
from langextract import data as lxdata  # noqa: E402
from langextract import factory  # noqa: E402

examples = [
    lxdata.ExampleData(
        text="【客户】我平时喜欢喝点红酒，周末爱去打球。",
        extractions=[
            lxdata.Extraction(extraction_class="preference", extraction_text="喜欢喝点红酒"),
            lxdata.Extraction(extraction_class="preference", extraction_text="周末爱去打球"),
        ],
    )
]

PROMPT = (
    "从销售与客户的对话中抽取客户情报。"
    "preference 表示客户的喜好，landmine 表示客户明确反感或忌讳的事。"
    "每条抽取必须是原文中的连续片段，不要改写。"
)

cfg = factory.ModelConfig(
    model_id=config.LLM_MODEL,
    provider="openai",
    provider_kwargs={"base_url": config.LLM_BASE_URL, "api_key": config.get_api_key()},
)

print(f"\n{'='*72}\n用 langextract 对本机转录文本做溯源抽取\n{'='*72}")
# use_schema_constraints=False：DeepSeek 只支持 response_format=json_object，
# 不支持 langextract 默认下发的 json_schema 约束，必须关掉并用 examples 引导格式。
result = lx.extract(
    text_or_documents=TEXT,
    prompt_description=PROMPT,
    examples=examples,
    config=cfg,
    use_schema_constraints=False,
    show_progress=False,
)

docs = result if isinstance(result, list) else [result]
for doc in docs:
    exts = getattr(doc, "extractions", None) or []
    print(f"✓ 抽取到 {len(exts)} 条：")
    for e in exts:
        ci = getattr(e, "char_interval", None)
        span = f"原文[{ci.start_pos}:{ci.end_pos}]" if ci else "无区间"
        align = getattr(e, "alignment_status", "")
        # 用区间回原文切片，验证溯源是否真的对得上
        snippet = TEXT[ci.start_pos:ci.end_pos] if ci else ""
        ok = "✓" if snippet == e.extraction_text else ("≈" if snippet else "✗")
        print(f"  {ok} [{e.extraction_class}] {e.extraction_text!r}  {span} align={align}")
        if snippet:
            print(f"       回切原文: {snippet!r}")

