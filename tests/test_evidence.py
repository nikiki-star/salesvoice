"""证据回验（evidence grounding）单元测试。

这是本项目防幻觉的核心机制，必须覆盖三类边界：
  1. 逐字命中   —— 正常情况
  2. 近似命中   —— 语音识别有错字（真实场景必然出现）
  3. 应当拒绝   —— LLM 编造、与原文无关、空证据

运行：  PYTHONPATH=src .venv/bin/python tests/test_evidence.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salesvoice.extract import normalize, verify_evidence  # noqa: E402

# 真实转录（来自本机 SenseVoice 识别 TTS 音频的结果，含真实识别误差）
TRANSCRIPT = (
    "痛风好几年了。\n医生说酒一口都不能碰，谁要是硬劝我。\n我直接就翻脸。\n"
    "吃饭我不吃啦。\n清淡点就行。\n好试粤菜，我这人没什么别的爱好。\n"
    "就爱这口普洱。\n再就是钓钓鱼。"
)

CASES = [
    # (说明, 证据, 期望通过, 期望模式)
    ("逐字命中", "医生说酒一口都不能碰", True, "exact"),
    ("跨标点命中", "谁要是硬劝我。我直接就翻脸", True, "exact"),
    ("全角标点差异仍算命中", "痛风好几年了。", True, "exact"),
    ("ASR 错字：不吃辣→不吃啦", "吃饭我不吃辣。清淡点就行", True, "fuzzy"),
    ("ASR 错字 + 长证据", "吃饭我不吃辣。清淡点就行。好试粤菜，我这人没什么别的爱好",
     True, "fuzzy"),
    ("短证据单字差异", "就爱这口普洱", True, "exact"),
    ("LLM 编造：原文根本没提茅台", "客户说他最喜欢喝茅台", False, ""),
    ("LLM 编造：无中生有的结论", "客户明确表示下个月签约", False, ""),
    ("与原文用字重合度极低", "他打算把公司搬到新加坡", False, ""),
    ("空证据", "", False, ""),
    ("过短证据（<4 字）", "痛风", False, ""),
]


def main() -> int:
    print("=" * 76)
    print("证据回验单元测试")
    print("=" * 76)

    passed = failed = 0
    for case in CASES:
        desc = case[0]
        good, ok, mode = check(case)
        passed, failed = (passed + 1, failed) if good else (passed, failed + 1)
        mark = "✓" if good else "✗"
        print(f"{mark} {desc}")
        print(f"    证据: {case[1][:44] or '(空)'}")
        print(f"    结果: ok={ok} mode={mode!r}   期望: ok={case[2]} mode={case[3]!r}")
        if not good:
            print(f"    ← 断言失败")

    # 归一化自检
    print("\n归一化自检：")
    for raw in ("医生说酒一口都不能碰，", " 医生说酒一口都不能碰。 "):
        print(f"  {raw!r} → {normalize(raw)!r}")

    print("\n" + "=" * 76)
    print(f"通过 {passed} / {passed + failed}")
    print("=" * 76)
    return 0 if failed == 0 else 1


def check(case) -> tuple[bool, bool, str]:
    """跑一条用例，返回 (是否符合期望, ok, mode)。"""
    _, evidence, want_ok, want_mode = case
    ok, mode = verify_evidence(evidence, TRANSCRIPT)
    return (ok == want_ok and mode == want_mode), ok, mode


def test_evidence_case(case) -> None:
    """pytest 入口（参数化见下）。"""
    good, ok, mode = check(case)
    assert good, f"{case[0]}：ok={ok} mode={mode!r}，期望 ok={case[2]} mode={case[3]!r}"


try:  # 未装 pytest 时脚本模式照常可用
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

if pytest is not None:
    test_evidence_case = pytest.mark.parametrize(
        "case", CASES, ids=[c[0] for c in CASES])(test_evidence_case)


if __name__ == "__main__":
    sys.exit(main())
