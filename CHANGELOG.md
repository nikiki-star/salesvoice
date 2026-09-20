# 更新日志

本文件记录对外可见的变更。版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-09-20

首个公开版本。

### 新增

- **本机离线转写**：sherpa-onnx + SenseVoice（纯 ONNX，无需 torch），Silero VAD 自动切段，
  无 GPU 的 Intel Mac 上实测 **5.8x 实时**（17.8s 音频 3.1s 转完）
- **证据回验式抽取**：每条标签必须引用客户原话，原文按 NFKC 归一化后做精确子串回验，
  失败则模糊回退（全部匹配块总长占比 ≥ 0.78，容忍 ASR 错字）；
  两条都不过 → 置信度 ×0.5 并标记待复核。防幻觉机制是本项目的核心
- **负面情报优先**：雷区（禁忌/厌恶）带 `severity` 与 `trigger`（在什么场景踩雷），
  识别「这个就算了」「我不太方便」这类委婉表达，而不只是总结「谈了什么」
- **跨会面累积中台**：SQLite + WAL + 每线程独立连接，按客户聚合标签、雷区与历史会面
- **单文件看板** `web/index.html`：零构建、零 CDN，直接由后端托管
- **AI 建议与雷区预检**：会前简报、宴请建议、任意场景的踩雷预检（AI 建议关闭 json mode，
  因为 DeepSeek 的 json_object 会拒绝输出 Markdown）
- **可选第二抽取引擎**：google/langextract 作为交叉验证的第二意见（实测其中文
  `char_interval` 锚定率随文本长度退化：89 字 7/7 → 544 字 8/11）
- **说话人标记约定**：无 diarization 时要求转录文稿带 `【客户】`/`【销售】` 标记，
  并明确「只有客户说的才算情报」

### 工程

- MIT 许可，LICENSE 末尾附第三方组件许可说明，并声明不分发模型权重与二进制
- `pyproject.toml` 元数据（license / classifiers / urls / console script）与可选依赖
  `extract` / `diarize` / `dev`
- 运行时数据与模型权重不入库：`.gitignore` 用目录级规则拦住 `data/`、`models/`、`.venv/`
- **CI**：Python 3.10 / 3.11 / 3.12 上跑证据回验与 pytest；独立 job 校验 Linux 依赖安装、
  全部模块可导入、真起 server 打 HTTP 的冒烟测试
- 测试分层：单测与冒烟测试可被 pytest 收集，需模型/凭据的人工验收脚本由
  `tests/conftest.py` 显式排除

[0.1.0]: https://github.com/nikiki-star/salesvoice/releases/tag/v0.1.0
