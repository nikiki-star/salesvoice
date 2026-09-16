"""语音转写层（可插拔引擎）。

支持三种引擎，通过参数或环境变量选择，便于在"隐私合规"和"速度"之间取舍：

  sensevoice  本机离线。sherpa-onnx + SenseVoice（阿里通义，MIT）。
              非自回归架构，CPU 上比 Whisper 快一个数量级，
              中文/粤语识别质量优于 Whisper，且模型自带情感与音频事件能力。
              音频**不出本机** —— 客户对话合规风险最低的方案。

  cloud       调用任意 OpenAI 兼容的 /audio/transcriptions 端点
              （OpenAI、硅基流动、阿里百炼等）。速度最快，但音频会上传。

  manual      不转写，直接使用已有的转录文稿（例如手机自带转录、或人工整理）。
              线下见面场景最省事的起步方式。

说话人分离（区分"客户说的"和"我说的"）由 diarization 提供，
在无法分离时，建议在转录文稿里手工标注说话人，格式：
    【客户】……
    【销售】……
本模块会自动解析这种标记。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

# ---------------------------------------------------------------- 模型路径

MODELS_DIR = config.PROJECT_ROOT / "models"
SENSEVOICE_DIR = MODELS_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSEVOICE_MODEL = SENSEVOICE_DIR / "model.int8.onnx"
SENSEVOICE_TOKENS = SENSEVOICE_DIR / "tokens.txt"
VAD_MODEL = MODELS_DIR / "silero_vad.onnx"

DEFAULT_ENGINE = os.environ.get("SALESVOICE_ASR_ENGINE", "sensevoice")


class TranscribeError(RuntimeError):
    pass


# ---------------------------------------------------------------- 音频准备


def to_wav16k(src: str | Path, dst: str | Path | None = None) -> Path:
    """用 ffmpeg 统一转成 16kHz 单声道 wav（ASR 标准输入）。"""
    src = Path(src)
    if not src.is_file():
        raise TranscribeError(f"音频文件不存在: {src}")
    if shutil.which("ffmpeg") is None:
        raise TranscribeError("未找到 ffmpeg，请先安装（brew install ffmpeg）")

    dst = Path(dst) if dst else Path(tempfile.gettempdir()) / (src.stem + ".16k.wav")
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-loglevel", "error", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TranscribeError(f"ffmpeg 转码失败: {proc.stderr.strip()[:300]}")
    return dst


def audio_duration(path: str | Path) -> float:
    """读取音频时长（秒）。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------- 本机引擎


def transcribe_sensevoice(wav: str | Path, num_threads: int | None = None,
                          use_vad: bool = True, verbose: bool = True) -> dict:
    """本机离线转写：sherpa-onnx + SenseVoice（+ Silero VAD 分段）。"""
    import sherpa_onnx  # 延迟导入，未安装时不影响其他引擎

    if not SENSEVOICE_MODEL.is_file():
        raise TranscribeError(
            f"SenseVoice 模型缺失: {SENSEVOICE_MODEL}\n"
            "请运行 python -m salesvoice.setup_models 下载模型（约 160MB）"
        )

    num_threads = num_threads or min(8, (os.cpu_count() or 4))
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(SENSEVOICE_MODEL),
        tokens=str(SENSEVOICE_TOKENS),
        num_threads=num_threads,
        use_itn=True,          # 逆文本正则化：数字/标点更自然
        language="zh",
        debug=False,
    )

    wav = Path(wav)
    segments: list[dict] = []

    if use_vad and VAD_MODEL.is_file():
        import wave

        import numpy as np

        vad_cfg = sherpa_onnx.VadModelConfig()
        vad_cfg.silero_vad.model = str(VAD_MODEL)
        vad_cfg.silero_vad.min_silence_duration = 0.25
        vad_cfg.silero_vad.min_speech_duration = 0.25
        vad_cfg.silero_vad.window_size = 512
        vad_cfg.sample_rate = 16000
        vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)

        with wave.open(str(wav), "rb") as f:
            sr, n_frames = f.getframerate(), f.getnframes()
            samples = np.frombuffer(f.readframes(n_frames), dtype=np.int16).astype("float32") / 32768.0

        window = 512
        buf = []
        for i in range(0, len(samples), window):
            chunk = samples[i:i + window]
            if len(chunk) < window:
                chunk = np.pad(chunk, (0, window - len(chunk)))
            vad.accept_waveform(chunk)
            while not vad.empty():
                seg = vad.front
                buf.append({"start": seg.start / sr, "samples": np.array(seg.samples, dtype="float32")})
                vad.pop()
        vad.flush()
        while not vad.empty():
            seg = vad.front
            buf.append({"start": seg.start / sr, "samples": np.array(seg.samples, dtype="float32")})
            vad.pop()

        if verbose:
            print(f"[asr] VAD 切出 {len(buf)} 个语音段，开始识别…")
        for i, item in enumerate(buf, 1):
            stream = recognizer.create_stream()
            stream.accept_waveform(16000, item["samples"])
            recognizer.decode_stream(stream)
            text = (stream.result.text or "").strip()
            if text:
                segments.append({
                    "start": round(item["start"], 2),
                    "end": round(item["start"] + len(item["samples"]) / 16000, 2),
                    "text": text,
                })
            if verbose and i % 10 == 0:
                print(f"[asr]   …{i}/{len(buf)}")
    else:
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, _read_wav_samples(wav))
        recognizer.decode_stream(stream)
        segments.append({"start": 0.0, "end": 0.0, "text": (stream.result.text or "").strip()})

    return {
        "engine": "sensevoice",
        "segments": segments,
        "text": "\n".join(s["text"] for s in segments if s["text"]),
        "duration": audio_duration(wav),
    }


def _read_wav_samples(wav: Path):
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype("float32") / 32768.0


# ---------------------------------------------------------------- 云端引擎


def transcribe_cloud(wav: str | Path, model: str | None = None,
                     language: str = "zh", verbose: bool = True) -> dict:
    """调用 OpenAI 兼容的音频转写端点。音频会上传 —— 注意合规。"""
    from openai import OpenAI

    base = os.environ.get("SALESVOICE_ASR_BASE_URL", config.LLM_BASE_URL)
    key = os.environ.get("SALESVOICE_ASR_API_KEY") or config.get_api_key()
    model = model or os.environ.get("SALESVOICE_ASR_MODEL", "whisper-1")

    if verbose:
        print(f"[asr] 云端转写 {Path(wav).name} → {base} / {model}")
    client = OpenAI(api_key=key, base_url=base, timeout=600)
    with open(wav, "rb") as fh:
        resp = client.audio.transcriptions.create(
            model=model, file=fh, language=language,
            response_format="verbose_json" if "whisper" in model else "json",
        )
    segs = []
    for s in (getattr(resp, "segments", None) or []):
        segs.append({"start": getattr(s, "start", 0.0), "end": getattr(s, "end", 0.0),
                     "text": (getattr(s, "text", "") or "").strip()})
    text = getattr(resp, "text", "") or ""
    if not segs and text:
        segs = [{"start": 0.0, "end": 0.0, "text": text}]
    return {"engine": f"cloud:{model}", "segments": segs, "text": text,
            "duration": audio_duration(wav)}


# ---------------------------------------------------------------- 统一入口


def transcribe(audio_path: str | Path, engine: str | None = None,
               language: str = "zh", num_threads: int | None = None,
               verbose: bool = True) -> dict:
    """转写音频文件。返回 {engine, segments, text, duration, wav}。"""
    engine = (engine or DEFAULT_ENGINE).lower()
    if engine == "manual":
        raise TranscribeError("manual 引擎不处理音频文件，请直接提交转录文本")

    wav = to_wav16k(audio_path)
    if engine == "sensevoice":
        out = transcribe_sensevoice(wav, num_threads=num_threads, verbose=verbose)
    elif engine in ("cloud", "openai", "whisper"):
        out = transcribe_cloud(wav, language=language, verbose=verbose)
    else:
        raise TranscribeError(f"未知引擎: {engine}（可选 sensevoice / cloud / manual）")
    out["wav"] = str(wav)
    return out


# ---------------------------------------------------------------- 说话人标记

_SPEAKER_RE = re.compile(r"[【\[]\s*([^】\]]{1,12})\s*[】\]]\s*")


def parse_speaker_markers(text: str) -> list[dict]:
    """解析 '【客户】……' 形式的说话人标记，返回 [{speaker, text}]。

    若没有标记，整个文本视为一段，speaker 为空 —— 此时抽取层会依赖
    LLM 从对话内容推断归属，准确率会下降，建议手工补标。
    """
    if not _SPEAKER_RE.search(text):
        return [{"speaker": "", "text": text.strip()}]

    turns: list[dict] = []
    matches = list(_SPEAKER_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            turns.append({"speaker": m.group(1).strip(), "text": body})
    return turns


def segments_to_marked_text(segments: list[dict], speaker: str = "") -> str:
    """把识别分段拼成带说话人标记的转录文本。"""
    lines = []
    for s in segments:
        t = (s.get("text") or "").strip()
        if t:
            lines.append(f"【{speaker}】{t}" if speaker else t)
    return "\n".join(lines)


# ---------------------------------------------------------------- 服务端入口


def transcribe_payload(payload: dict) -> dict:
    """供 HTTP 接口调用：{audio_path 或 audio_base64, engine, language}。"""
    engine = payload.get("engine") or DEFAULT_ENGINE
    audio_path = payload.get("audio_path")

    if payload.get("audio_base64"):
        import base64
        import time

        raw = payload["audio_base64"].split(",")[-1]
        dst = config.AUDIO_DIR / f"upload_{int(time.time())}.bin"
        dst.write_bytes(base64.b64decode(raw))
        audio_path = str(dst)

    if not audio_path:
        raise TranscribeError("缺少 audio_path 或 audio_base64")

    out = transcribe(audio_path, engine=engine, language=payload.get("language", "zh"))
    speaker = payload.get("speaker", "客户")
    return {
        "ok": True,
        "engine": out["engine"],
        "duration": out.get("duration", 0),
        "segments": out["segments"],
        "text": out["text"],
        "marked_text": segments_to_marked_text(out["segments"], speaker),
    }
