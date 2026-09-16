"""客户语音情报中台 —— 从录音到客户画像的销售情报系统。

复用底座：
  - SenseVoice / FunASR   中文语音识别（CPU 友好，含情感与音频事件）
  - pyannote / whisperX   说话人分离（区分客户与销售）
  - google/langextract    证据溯源式结构化抽取（本模块做了业务化落地）
  - Meetily               采集端整机参考（MIT）
"""

__version__ = "0.1.0"
