# 用 Notion 当录音前端

目标：**在 Notion 里录（或让 Notion AI 记），情报自动落到 SalesVoice 看板上。**

```
手机/电脑录音  ──►  Notion 数据库「客户会面录音」
                        │  ① 定时/手动同步（看板按钮或 CLI）
                        ▼
        SalesVoice：文字稿直接抽；只有音频就本机转写（音频不出机器）
                        │  ② 证据回验式抽取
                        ▼
              客户情报中台（SQLite）→ 看板：画像 / 雷区 / 建议
```

## 一、Notion 侧准备（3 步，约 3 分钟）

1. **建内部集成**
   打开 <https://notion.so/my-integrations> → 新建集成（Internal）→ 复制 token
   （`ntn_` 或 `secret_` 开头）。能力至少勾 **Read content**；要反写再勾
   **Update content** / **Insert content**。

2. **把 token 交给 SalesVoice（不要贴在聊天里）**

   ```bash
   # 追加到 ~/.hermes/.env（SalesVoice 会自动读取；文件权限建议 chmod 600）
   echo 'NOTION_API_KEY=ntn_xxxxxxxx' >> ~/.hermes/.env
   ```

3. **把「投放区」共享给集成**
   在 Notion 打开那个数据库/页面 → 右上角 `…` → **连接 / Connect to** → 选你的集成。
   ⚠️ 不共享就一定 404，哪怕 token 完全正确 —— 这是最常见的一个坑。

## 二、没有现成数据库？一条命令建好结构

```bash
salesvoice notion-init --parent "<你在 Notion 里新建的页面 URL>"
# 输出 data_source_id，然后：
export SALESVOICE_NOTION_SOURCE=<data_source_id>
```

它会建出这个结构的数据库（属性名可被 `SALESVOICE_NOTION_PROPS` 覆盖）：

| 属性 | 类型 | 说明 |
|---|---|---|
| 客户名称 | title | 页面标题。写「王总 2026-09-16 会面」也行，日期与「会面/记录」等后缀会被自动剥掉 |
| 公司 / 参与人 / 地点 | rich_text | 元信息 |
| 日期 | date | 会面日期；空则取页面创建时间 |
| 转录 | rich_text | **文字稿**。Notion AI 会议记录的文字稿贴这里（或放正文/子页面，见下） |
| 录音 | files | **音频附件**。只有音频没有文字稿时，本机转写 |
| 已同步 | checkbox | 开启反写时由 SalesVoice 勾上 |

**文字稿的三种放法它都认**（按优先级）：
`转录` 属性 → 页面正文 → 标题含「转录 / 文字稿 / transcript」的子页面。

## 三、跑同步

```bash
# 先看会做什么（不写库、不改 Notion、不调 LLM）
salesvoice notion-sync --dry-run

# 正式同步
salesvoice notion-sync

# 勾「已同步」+ 把这次提取到的雷区和统计回贴到 Notion 页面
salesvoice notion-sync --writeback

# 只处理某一条 / 只处理 5 条 / 内容没变也强制重跑
salesvoice notion-sync --page <页面ID> --limit 5 --force

# 看看集成到底能看见哪些库，挑投放区
salesvoice notion-sources
```

看板上也有按钮：左侧栏 **⟳ 从 Notion 同步**。同步在后台线程跑（转写 + 多次 LLM
调用动辄几分钟），界面不阻塞，跑完自动刷新客户列表；按钮下方的脚注显示上次同步时间与结果。

## 四、它怎么决定「用文字稿还是跑转写」

| 页面内容 | 行为 |
|---|---|
| 转录属性/正文/子页面里有 ≥30 字的文字稿 | 直接用，**不跑 ASR** |
| 没有文字稿，但有音频（`录音` 属性或 `audio`/`file` 块） | 下载到 `data/audio/` → **本机 SenseVoice 转写**（音频不出机器），转录前会标注 `[转录自音频 文件名]` |
| 两者都没有 | 记为「跳过：既没有文字稿也没有可用的音频」，不写库 |

## 五、幂等与去重

每个页面按 **内容哈希**（转录 + 音频链接 + 客户/日期/参与人/地点）记账，落在
`notion_sync` 表里：

- 内容没变 → 跳过（不会重复烧 LLM、不会重复写标签）
- 页面里补了音频、改了文字稿 → 哈希变了，自动重跑
- 想强制重跑 → `--force`

## 六、可配置项

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `NOTION_API_KEY` | 无 | 集成 token（也接受 `SALESVOICE_NOTION_API_KEY`） |
| `SALESVOICE_NOTION_SOURCE` | 无 | 投放区：数据库 / data source / 页面 ID 或 URL |
| `SALESVOICE_NOTION_WRITEBACK` | `0` | 是否反写（`1` 开启；CLI 的 `--writeback` 可临时覆盖） |
| `SALESVOICE_NOTION_PROPS` | 无 | 覆盖字段映射，JSON，例如 `{"client":["客户名"],"transcript":["笔记"]}` |
| `SALESVOICE_NOTION_VERSION` | `2025-09-03` | Notion API 版本 |
| `SALESVOICE_DATA_DIR` | `<repo>/data` | 数据目录重定向（试跑/演示用，避免混进真实客户库） |

## 七、踩过的坑（照着避）

- **404 十有八九是没共享**：token 对、ID 对，但页面没「连接」给集成 → 404。
  本项目的错误信息会把这条提示直接打出来。
- **`data_source_id` ≠ `database_id`**：2025-09-03 版 API 里数据库被拆成两层，
  查询要用 data source ID。`notion-init` 会把两个都打出来。
- **URL 里的 slug 会污染 ID 解析**：页面 URL 常是 `My-DB-<32位id>`，而 `D`/`B`
  本身就是十六进制字符，直接搜「连续 32 位十六进制」会把 slug 尾巴吃进 ID。
  解析器只在最后一段路径里认 ID —— 这是被单测钉住的行为。
- **反写要排除在转录之外**：回执块带 `[salesvoice]` 标记，解析转录时跳过。
  否则第二次同步会把上次写的摘要注意成「客户原话」喂给模型，自我喂养、越滚越歪。
- **Notion 托管文件是限时签名 URL**：拿到就尽快下载，别缓存 URL 以后再下。
- **频率限制约 3 请求/秒**：页面多了就把同步当批处理跑，别开并发。
- **同步是长任务**：走看板按钮时是后台线程；CLI 里跑也无所谓。反写失败不会让入库结果作废
  （只在报告里标出来）。

## 八、离线验证（没有 token 也能确认管道没坏）

```bash
# tests/test_notion_sync.py 用 fixture + 注入的假抽取器，全程离线
.venv/bin/python -m pytest tests/test_notion_sync.py -q
```

真要跑一遍完整链路（含本机转写与真实 LLM 抽取、但不碰 Notion）：自己造两页 fixture 即可 ——
一个页面把文字稿放在 `转录` 属性里，另一个只挂音频（音频块 url 写 `file:<目录内文件名>`）。
然后：

```bash
SALESVOICE_DATA_DIR=/tmp/sv-demo PYTHONPATH=src .venv/bin/python -m salesvoice.cli \
  notion-sync --fixture <fixture目录> --writeback --json
```

`FixtureSource` 认这三类文件：`<名字>.page.json`（页面）、`<页面id前8位>.blocks.json`（块）、
`<页面id前8位>*.child.md`（子页面文字稿）。
