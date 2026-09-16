"""双引擎对比：自研证据回验 vs google/langextract。

同一段真实对话（本机 SenseVoice 转写的 TTS 录音），分别用两个引擎抽取，
对比证据溯源质量。用于回答"到底该用哪个引擎"。

运行：  PYTHONPATH=src .venv/bin/python tests/compare_engines.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salesvoice import extract  # noqa: E402

TRANSCRIPT = (
    "【客户】痛风好几年了。医生说酒一口都不能碰，谁要是硬劝我。我直接就翻脸。\n"
    "【客户】吃饭我不吃啦。清淡点就行。好试粤菜，我这人没什么别的爱好。\n"
    "【客户】就爱这口普洱。再就是钓钓鱼。"
)

# 人工核对基线：这段对话里客观存在的情报
GROUND_TRUTH = [
    "痛风（事实）",
    "禁酒/反感劝酒（雷区，高危）",
    "不吃辣（偏好）",
    "清淡/粤菜（偏好）",
    "爱普洱（偏好）",
    "爱钓鱼（偏好）",
]


def stats(tags, label):
    exact = [t for t in tags if t.evidence_mode == "exact"]
    fuzzy = [t for t in tags if t.evidence_mode == "fuzzy"]
    bad = [t for t in tags if not t.evidence_verified]
    land = [t for t in tags if t.category == "landmine"]
    # 独立再核验一次：不看引擎自己的判断，用本项目的中文回验函数重算
    recheck = sum(1 for t in tags if extract.verify_evidence(t.evidence, TRANSCRIPT)[0])
    print(f"\n【{label}】")
    print(f"  标签数        {len(tags)}")
    print(f"  逐字对齐      {len(exact)}")
    print(f"  近似对齐      {len(fuzzy)}")
    print(f"  未对齐        {len(bad)}")
    print(f"  雷区捕获      {len(land)}")
    print(f"  独立复核通过  {recheck}/{len(tags)}"
          f" = {recheck / len(tags) * 100:.0f}%" if tags else "  独立复核      无标签")
    for t in tags:
        mode = t.evidence_mode or "未通过"
        flag = {"exact": "✓", "fuzzy": "≈"}.get(t.evidence_mode, "✗")
        sev = f" [{t.severity}]" if t.severity else ""
        print(f"    {flag} {mode:<6} [{t.category:<10}] {t.key}: {t.value[:34]}{sev}")
    return {"n": len(tags), "exact": len(exact), "fuzzy": len(fuzzy),
            "bad": len(bad), "landmine": len(land), "recheck": recheck}


print("=" * 76)
print("输入转录（含 ASR 真实错字：不吃辣→不吃啦、最好→好试）")
print("=" * 76)
print(TRANSCRIPT)
print("\n人工核对基线（这段对话客观存在的情报）：")
for g in GROUND_TRUTH:
    print(f"  · {g}")

a_tags = extract.extract_tags(TRANSCRIPT, client_name="李总", meeting_id="CMP1",
                              engine="default")
a = stats(a_tags, "引擎 A：自研（中文子串回验）")

b = None
try:
    b_tags = extract.extract_tags(TRANSCRIPT, client_name="李总", meeting_id="CMP1",
                                  engine="langextract")
    b = stats(b_tags, "引擎 B：google/langextract")
except Exception as exc:  # noqa: BLE001
    print(f"\n【引擎 B：langextract】运行失败: {type(exc).__name__}: {str(exc)[:200]}")

print("\n" + "=" * 76)
print("对比结论")
print("=" * 76)
if b:
    print(f"{'指标':<14}{'自研':>8}{'langextract':>14}")
    for k, name in [("n", "标签数"), ("exact", "逐字对齐"), ("fuzzy", "近似对齐"),
                    ("bad", "未对齐"), ("landmine", "雷区捕获"), ("recheck", "独立复核通过")]:
        print(f"{name:<14}{a[k]:>8}{b[k]:>14}")
    rate_a = a["recheck"] / a["n"] if a["n"] else 0
    rate_b = b["recheck"] / b["n"] if b["n"] else 0
    print(f"\n证据可溯率：自研 {rate_a*100:.0f}%  vs  langextract {rate_b*100:.0f}%")
    if abs(rate_a - rate_b) < 0.01:
        print("→ 证据可溯率打平。取舍看别的维度：")
        print(f"   粒度：langextract 抽出 {b['n']} 条 vs 自研 {a['n']} 条")
        print(f"   雷区捕获：langextract {b['landmine']} 条 vs 自研 {a['landmine']} 条")
        print("   依赖：自研零额外依赖；langextract 需装包且需 use_schema_constraints=False")
    else:
        print(f"→ 证据可溯率更高的是：{'自研' if rate_a > rate_b else 'langextract'}")
