"""本机语音转写验证：音频 → SenseVoice → 文本 → 关键词命中。

用 macOS `say` 合成的中文语音做输入，验证：
  1. 本机推理链路是否真的能跑通（无需网络、无需 torch）
  2. 中文识别质量是否足以支撑情报抽取（关键词是否能识别出来）
  3. 速度是否可用（CPU 上的实时率）
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salesvoice import config  # noqa: E402
from salesvoice.transcribe import audio_duration, transcribe  # noqa: E402

WAV = Path(__file__).parent / "asr_sample.wav"

# 原始台词（与 say 生成音频时用的文本一致）
GROUND_TRUTH = (
    "我痛风好几年了，医生说酒一口都不能碰。谁要是硬劝我，我直接就翻脸。"
    "吃饭我不吃辣，清淡点就行，最好是粤菜。"
    "我这人没什么别的爱好，就爱这口普洱，再就是钓钓鱼。"
)
KEYWORDS = ["痛风", "酒", "劝", "翻脸", "辣", "清淡", "粤菜", "普洱", "钓鱼"]

if not WAV.is_file():
    print(f"✗ 缺少测试音频 {WAV}\n  请先运行：say -v Tingting -o asr_sample.aiff \"...\"")
    sys.exit(1)

print("=" * 72)
print(f"本机转写测试  引擎=sensevoice  CPU 线程={config.__dict__.get('_x', 'auto')}")
print(f"输入: {WAV.name}  时长 {audio_duration(WAV):.1f} 秒")
print("=" * 72)

t0 = time.time()
out = transcribe(WAV, engine="sensevoice")
elapsed = time.time() - t0

text = out["text"]
print(f"\n识别结果（{len(out['segments'])} 段）：\n")
print(f"  {text}")

print(f"\n参考原文：\n  {GROUND_TRUTH}")

print("\n关键词命中：")
hit = 0
for kw in KEYWORDS:
    ok = kw in text
    hit += ok
    print(f"  {'✓' if ok else '✗'} {kw}")
print(f"\n命中率: {hit}/{len(KEYWORDS)} = {hit/len(KEYWORDS)*100:.0f}%")

dur = out.get("duration") or audio_duration(WAV)
print(f"\n耗时 {elapsed:.1f} 秒 / 音频 {dur:.1f} 秒 → 实时率 {dur/elapsed if elapsed else 0:.1f}x")
print(f"（本次引擎: {out['engine']}）")

# 落盘
dump = config.TRANSCRIPT_DIR / "asr_sample.transcript.txt"
dump.write_text(text, encoding="utf-8")
print(f"\n转录已保存: {dump}")
