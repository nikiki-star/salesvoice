"""端到端测试：转录文本 → 客户标签（含证据回验）。"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salesvoice import config, extract  # noqa: E402
from salesvoice.schema import CATEGORIES  # noqa: E402

transcript = (Path(__file__).parent / "sample_transcript.txt").read_text(encoding="utf-8")

print("=" * 72)
print(f"LLM 后端: {config.LLM_BASE_URL}  模型: {config.LLM_MODEL}")
print(f"凭据可用: {config.has_api_key()}")
print("=" * 72)

tags = extract.extract_tags(transcript, client_name="王总", meeting_id="M001")

print()
print("=" * 72)
print("抽取结果（按类别）")
print("=" * 72)

by_cat: dict[str, list] = {}
for t in tags:
    by_cat.setdefault(t.category, []).append(t)

for cid, meta in CATEGORIES.items():
    items = by_cat.get(cid, [])
    if not items:
        continue
    print(f"\n【{meta['label']}】{len(items)} 条")
    for t in items:
        mark = "✓" if t.evidence_verified else "✗未回验"
        sev = f" [{t.severity}]" if t.severity else ""
        print(f"  {mark} {t.key}: {t.value}  (信心 {t.confidence}{sev})")
        print(f"      证据: {t.evidence[:60]}")
        if t.trigger:
            print(f"      触发: {t.trigger}")

out = config.DATA_DIR / "test_extract_result.json"
out.write_text(json.dumps([t.to_dict() for t in tags], ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n结果已保存: {out}")

verified = sum(1 for t in tags if t.evidence_verified)
print(f"\n统计: {len(tags)} 条标签，证据回验通过 {verified} 条 "
      f"({verified / len(tags) * 100:.0f}%)" if tags else "无标签")
