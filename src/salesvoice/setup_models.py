"""模型下载脚本：python -m salesvoice.setup_models

下载本机离线语音识别所需的模型（全部为 MIT / Apache 许可的开源模型）：
  - SenseVoice（阿里通义，MIT）：中文/粤语/英语/日语/韩语 ASR，自带情感与音频事件
  - Silero VAD（MIT）：语音活动检测，用于长音频自动分段

约 1.0 GB。下载后音频转写完全在本机完成，客户录音不出设备。
"""

from __future__ import annotations

import bz2
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
MODELS = Path(__file__).resolve().parents[2] / "models"

SENSEVOICE_ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
SENSEVOICE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
VAD_FILE = "silero_vad.onnx"


def _progress(done: int, total: int) -> None:
    if total <= 0:
        sys.stdout.write(f"\r  已下载 {done/1048576:.1f} MB")
    else:
        pct = done / total * 100
        bar = "█" * int(pct / 3) + "·" * (34 - int(pct / 3))
        sys.stdout.write(f"\r  [{bar}] {pct:5.1f}%  {done/1048576:.1f}/{total/1048576:.1f} MB")
    sys.stdout.flush()


def download(url: str, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 0:
        print(f"  已存在，跳过：{dst.name}")
        return dst
    req = Request(url, headers={"User-Agent": "salesvoice-setup"})
    with urlopen(req, timeout=120) as r:  # noqa: S310
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dst, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                _progress(done, total)
    print()
    return dst


def main() -> int:
    MODELS.mkdir(parents=True, exist_ok=True)
    print(f"模型目录：{MODELS}\n")

    # 1) SenseVoice
    target_dir = MODELS / SENSEVOICE_DIR
    if (target_dir / "model.int8.onnx").is_file():
        print("✓ SenseVoice 已就绪，跳过")
    else:
        print("[1/2] 下载 SenseVoice 语音识别模型（约 1.0 GB，含 int8 与 fp32 两个版本）")
        archive = download(f"{BASE}/{SENSEVOICE_ARCHIVE}", MODELS / SENSEVOICE_ARCHIVE)

        print("  解压中…")
        if shutil.which("tar"):
            subprocess.run(["tar", "xjf", str(archive), "-C", str(MODELS)], check=True)
        else:
            with tarfile.open(archive, "r:bz2") as tf:
                tf.extractall(MODELS)  # noqa: S202
        archive.unlink(missing_ok=True)

        # 若解压结果目录名不同，修正命名
        if not target_dir.is_dir():
            cands = [p for p in MODELS.iterdir() if p.is_dir() and "sense" in p.name.lower()]
            if cands:
                cands[0].rename(target_dir)
        print(f"  ✓ {target_dir.name}")

        # 删掉 fp32 版本省空间（int8 精度足够且快 2-3 倍）
        fp32 = target_dir / "model.onnx"
        if fp32.is_file() and (target_dir / "model.int8.onnx").is_file():
            size = fp32.stat().st_size / 1048576
            fp32.unlink()
            print(f"  ✓ 已删除 fp32 版本，释放 {size:.0f} MB（保留 int8）")

    # 2) VAD
    print("\n[2/2] 下载 Silero VAD 分段模型（约 2 MB）")
    download(f"{BASE}/{VAD_FILE}", MODELS / VAD_FILE)
    print(f"  ✓ {VAD_FILE}")

    total = sum(f.stat().st_size for f in MODELS.rglob("*") if f.is_file())
    print(f"\n全部就绪。模型总占用 {total/1048576:.0f} MB")
    print("验证：  PYTHONPATH=src .venv/bin/python -c \"from salesvoice.transcribe import transcribe_sensevoice; print('ok')\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
