"""Notion → 情报中台 同步管道。

把 Notion 当**录音采集前端**：销售在手机/电脑上录完，音频或 Notion AI 生成的文字稿
留在某个数据库（或页面）里；本模块定时拉取新条目 → 本机转写（音频不出机器）→
证据回验式抽取 → 入库 → 看板呈现。

设计要点：

1. **自动识别内容形态**：页面里是文字稿就用文字稿（不跑 ASR），是音频就下载后本机转写。
   Notion AI 的会议记录与「手工上传录音」两种习惯都能用。
2. **幂等**：按内容哈希（转录文本 + 音频链接 + 元信息）判断，没变就不重复抽取、不重复入库。
   台账落在 store.notion_sync。
3. **可离线测试**：真正的 Notion 调用被收敛到 NotionApiSource 背后，
   测试用 FixtureSource + 注入的 extract_fn 跑完整链路，不需要网络也不需要 token。
4. **反写可选**：默认只读。开启后勾「已同步」并在页面里放一条带 `[salesvoice]` 标记的
   回执（再次同步是更新同一条，不会堆叠），标记本身会被排除在转录之外，避免自我喂养。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# ---------------------------------------------------------------- 常量

#: 逻辑字段 → 可能的 Notion 属性名（按顺序匹配，先精确后包含）
PROP_ALIASES: dict[str, list[str]] = {
    "client": ["客户名称", "客户", "客户姓名", "姓名", "联系人", "Name", "Title"],
    "company": ["公司", "客户公司", "单位", "Company", "组织"],
    "title": ["职位", "职务", "Title", "Job"],
    "industry": ["行业", "Industry", "领域"],
    "met_on": ["日期", "会面日期", "时间", "Date", "见面日期"],
    "location": ["地点", "会面地点", "场所", "Location"],
    "attendees": ["参与人", "参会人", "出席人", "人员", "Attendees"],
    "transcript": ["转录", "文字稿", "录音文字", "对话原文", "谈话记录",
                   "Transcript", "Transcription", "会议记录"],
    "synced": ["已同步", "已同步到salesvoice", "同步状态", "Synced"],
}

#: 子页面标题命中这些词时，把它的内容当转录文本（Notion AI 会议记录常见把文字稿放子页）
TRANSCRIPT_PAGE_HINTS = ("转录", "文字稿", "transcript", "文字记录", "原文")

#: 回执标记：带这个标记的块不算转录内容，也用它定位已存在的回执
WRITEBACK_MARKER = "[salesvoice]"

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".amr", ".wma",
              ".mp4", ".webm", ".m4v", ".aiff", ".caf"}
AUDIO_MIME_HINTS = ("audio/", "video/mp4", "video/webm")

#: 转录短于这个长度就认为「没内容」，去尝试音频
MIN_TRANSCRIPT_CHARS = 30


def _env_aliases() -> dict[str, list[str]]:
    """允许用 SALESVOICE_NOTION_PROPS 覆盖字段映射，例如：
    {"client": ["客户名"], "transcript": ["笔记"]}"""
    raw = os.environ.get("SALESVOICE_NOTION_PROPS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list) and v}


def aliases() -> dict[str, list[str]]:
    merged = {k: list(v) for k, v in PROP_ALIASES.items()}
    for k, v in _env_aliases().items():
        merged[k] = list(v) + merged.get(k, [])
    return merged


# ---------------------------------------------------------------- 数据结构


@dataclass
class AudioRef:
    url: str
    name: str = ""


@dataclass
class MeetingDraft:
    page_id: str
    title: str = ""
    client_name: str = ""
    company: str = ""
    job_title: str = ""
    industry: str = ""
    met_on: str = ""
    location: str = ""
    attendees: str = ""
    transcript: str = ""
    audios: list[AudioRef] = field(default_factory=list)
    synced_prop: str = ""            # 「已同步」属性名（存在才反写）
    writeback_block_id: str = ""     # 已存在的回执块 ID（用于更新而非追加）

    @property
    def has_content(self) -> bool:
        return len(self.transcript.strip()) >= MIN_TRANSCRIPT_CHARS or bool(self.audios)


# ---------------------------------------------------------------- 属性解析


def rich_text(value: list | None) -> str:
    if not value:
        return ""
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                   for part in value if isinstance(part, dict)).strip()


def prop_text(prop: dict | None) -> str:
    """把一个 Notion 属性拍平成人可读字符串（覆盖本项目会用到的类型）。"""
    if not prop:
        return ""
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return rich_text(v)
    if t == "select":
        return (v or {}).get("name", "")
    if t == "multi_select":
        return "、".join(x.get("name", "") for x in (v or []))
    if t == "status":
        return (v or {}).get("name", "")
    if t == "date":
        return (v or {}).get("start", "")
    if t == "people":
        return "、".join(p.get("name", "") for p in (v or []))
    if t == "checkbox":
        return "true" if v else ""
    if t == "number":
        return "" if v is None else str(v)
    if t == "url":
        return v or ""
    if t == "formula":
        return str((v or {}).get("string") or (v or {}).get("number") or "")
    if t == "files":
        return " ".join(f.get("name", "") for f in (v or []))
    return ""


def find_prop(props: dict, keys: list[str]) -> tuple[str, dict]:
    """按别名找属性：先全名精确（忽略大小写），再退化为包含匹配。"""
    lower = {k.lower(): k for k in props}
    for want in keys:
        if want.lower() in lower:
            return lower[want.lower()], props[lower[want.lower()]]
    for want in keys:
        for k_low, k in lower.items():
            if want.lower() in k_low:
                return k, props[k]
    return "", {}


def date_only(value: str) -> str:
    """'2026-09-20T14:03:00.000+08:00' → '2026-09-20'。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value or "")
    return m.group(1) if m else ""


def page_has_audio_property(props: dict) -> list[AudioRef]:
    out: list[AudioRef] = []
    for prop in props.values():
        if prop.get("type") != "files":
            continue
        for f in prop.get("files") or []:
            url = (f.get("file") or {}).get("url") or (f.get("external") or {}).get("url") or ""
            name = f.get("name", "")
            if url and _looks_audio(name or url):
                out.append(AudioRef(url=url, name=name or _name_from_url(url)))
    return out


def _name_from_url(url: str) -> str:
    return Path(url.split("?")[0]).name


def _looks_audio(name_or_url: str) -> bool:
    low = (name_or_url or "").lower()
    return Path(low.split("?")[0]).suffix in AUDIO_EXTS or any(m in low for m in AUDIO_MIME_HINTS)


# ---------------------------------------------------------------- 块解析


def block_text(block: dict) -> str:
    t = block.get("type", "")
    payload = block.get(t) or {}
    if t in ("child_page", "child_database"):
        return ""
    if t == "code":
        return rich_text(payload.get("rich_text"))
    if t == "table_row":
        return " | ".join(rich_text(c) for c in payload.get("cells", []))
    return rich_text(payload.get("rich_text") or payload.get("caption"))


def is_writeback_block(block: dict) -> bool:
    return WRITEBACK_MARKER in block_text(block)


def iter_blocks(blocks: list[dict]) -> list[dict]:
    """把嵌套块拍平（Notion 的子块在 children 字段里）。"""
    out: list[dict] = []
    for b in blocks:
        out.append(b)
        kids = (b.get(b.get("type", "")) or {}).get("children")
        if kids:
            out.extend(iter_blocks(kids))
    return out


def transcript_from_blocks(blocks: list[dict]) -> str:
    """把块文本拼成转录。跳过回执块（否则会把上次同步写进去的摘要当成客户原话）。"""
    lines: list[str] = []
    for b in iter_blocks(blocks):
        if b.get("type") in ("child_page", "child_database", "image", "file", "audio"):
            continue
        if is_writeback_block(b):
            continue
        txt = block_text(b)
        if txt:
            lines.append(txt)
    return "\n".join(lines).strip()


def audios_from_blocks(blocks: list[dict]) -> list[AudioRef]:
    out: list[AudioRef] = []
    for b in iter_blocks(blocks):
        t = b.get("type")
        if t not in ("audio", "file", "video"):
            continue
        payload = b.get(t) or {}
        url = (payload.get("file") or {}).get("url") or (payload.get("external") or {}).get("url") or ""
        name = payload.get("name") or _name_from_url(url)
        if url and _looks_audio(name or url):
            out.append(AudioRef(url=url, name=name))
    return out


def find_writeback_block_id(blocks: list[dict]) -> str:
    for b in iter_blocks(blocks):
        if is_writeback_block(b):
            return b.get("id", "")
    return ""


# ---------------------------------------------------------------- 组装草稿


def build_draft(page: dict, blocks: list[dict] | None = None,
                child_texts: list[str] | None = None) -> MeetingDraft:
    """把一个 Notion 页面（+ 它的块）解析成入库草稿。"""
    props = page.get("properties") or {}
    a = aliases()
    blocks = blocks or []

    def value(key: str) -> str:
        _, prop = find_prop(props, a.get(key, []))
        return prop_text(prop)

    title = ""
    for prop in props.values():
        if prop.get("type") == "title":
            title = prop_text(prop)
            break

    client = value("client") or title
    draft = MeetingDraft(
        page_id=page.get("id", ""),
        title=title,
        client_name=_clean_client_name(client, value("met_on")),
        company=value("company"),
        job_title=value("title") if "title" not in a.get("client", []) else "",
        industry=value("industry"),
        met_on=date_only(value("met_on")) or date_only(page.get("created_time", "")),
        location=value("location"),
        attendees=value("attendees"),
    )

    synced_name, _ = find_prop(props, a.get("synced", []))
    draft.synced_prop = synced_name

    # 转录优先级：属性里的文字稿 > 页面正文 > 子页面（Notion AI 常把文字稿放子页）
    prop_transcript = value("transcript")
    body = transcript_from_blocks(blocks)
    child = "\n\n".join(t for t in (child_texts or []) if t).strip()
    for candidate in (prop_transcript, body, child):
        if len(candidate.strip()) >= MIN_TRANSCRIPT_CHARS:
            draft.transcript = candidate.strip()
            break
    if not draft.transcript:
        draft.transcript = (prop_transcript or body or child).strip()

    draft.audios = page_has_audio_property(props) + audios_from_blocks(blocks)
    draft.writeback_block_id = find_writeback_block_id(blocks)
    return draft


_TITLE_SUFFIX_RE = re.compile(
    r"(微信语音留言|语音留言|微信留言|语音|留言|电话沟通|电话|通话|"
    r"现场拜访|上门拜访|拜访|回访|初访|复访|现场|线上面谈|面谈|会谈|洽谈|沟通|"
    r"会议|评审|技术交流|交流|会面|会议记录|记录|纪要|笔记|"
    r"meeting|call|notes?|visit)+\s*$",
    re.IGNORECASE)


def _clean_client_name(raw: str, met_on: str = "") -> str:
    """从页面标题里剥掉日期与常见后缀，得到客户名。

    Notion 里的页面标题通常是「客户名 + 日期 + 事件」或「日期 + 客户名 + 事件」，
    例如「周工 2026-09-19 现场拜访」「2026-08-28 微信语音留言」。
    关键是**同一客户的不同会面必须归到同一个名字**，否则跨会面累积就断了 ——
    所以日期与其后的描述性后缀都要剥干净。
    """
    name = (raw or "").strip()
    if not name:
        return ""

    m = re.search(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}[-/月]\d{1,2}日?", name)
    if m:
        before = name[:m.start()].strip(" -—_·|")
        if before:                      # 日期之前有内容 → 那就是客户名
            name = before
        else:                           # 日期打头 → 去掉日期看后面
            name = name[m.end():]

    prev = None
    while prev != name:                 # 后缀可能叠加（"微信语音留言"、"现场拜访记录"）
        prev = name
        name = _TITLE_SUFFIX_RE.sub("", name).strip(" -—_·|")
    name = re.sub(r"\s+", " ", name).strip()
    return name or (raw or "").strip()


def content_hash(draft: MeetingDraft) -> str:
    """内容指纹：转录 + 音频链接 + 关键元信息，任一变化即视为新内容。"""
    h = hashlib.sha1()
    for part in (draft.transcript, draft.met_on, draft.client_name, draft.company,
                 draft.attendees, draft.location,
                 "|".join(sorted(a.url for a in draft.audios))):
        h.update((part or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- 数据源抽象


class PageBundle:
    """一个页面 + 它需要参与解析的全部内容。"""

    def __init__(self, page: dict, blocks: list[dict], child_texts: list[str] | None = None):
        self.page = page
        self.blocks = blocks
        self.child_texts = child_texts or []


class NotionApiSource:
    """真实 Notion 数据源（数据库或页面）。"""

    def __init__(self, client=None, source_id: str | None = None):
        from .notion_client import NotionClient, extract_id

        self.client = client or NotionClient()
        self.source_id = extract_id(source_id or config.NOTION_SOURCE)
        if not self.source_id:
            raise RuntimeError(
                "未配置投放区。请设置 SALESVOICE_NOTION_SOURCE 为那个数据库/页面的 ID 或 URL，"
                "或用 --list-sources 从可见的候选里挑。")
        self.kind = self.client.source_kind(self.source_id)

    def describe(self) -> str:
        return f"Notion {self.kind} {self.source_id}"

    def list_pages(self, limit: int | None = None) -> list[dict]:
        out: list[dict] = []
        if self.kind == "page":
            for p in self.client.list_child_pages(self.source_id):
                out.append(p)
                if limit and len(out) >= limit:
                    break
            return out
        for p in self.client.query_data_source(self.source_id):
            out.append(p)
            if limit and len(out) >= limit:
                break
        return out

    def load_bundle(self, page: dict) -> PageBundle:
        page_id = page.get("id", "")
        blocks = list(self.client.iter_children(page_id))
        child_texts: list[str] = []
        for b in iter_blocks(blocks):
            if b.get("type") != "child_page":
                continue
            title = ((b.get("child_page") or {}).get("title") or "").lower()
            if any(h in title for h in TRANSCRIPT_PAGE_HINTS):
                child_blocks = list(self.client.iter_children(b["id"]))
                child_texts.append(transcript_from_blocks(child_blocks))
        return PageBundle(page, blocks, child_texts)

    def download_audio(self, ref: AudioRef, dest: Path) -> str:
        return self.client.download(ref.url, dest)

    def writeback(self, draft: MeetingDraft, payload: dict) -> str:
        """勾「已同步」+ 更新/新增一条回执。返回做了什么（用于报告）。"""
        from .notion_client import rich_text_paragraph

        did = []
        if draft.synced_prop:
            prop_type = payload.get("_synced_prop_type", "checkbox")
            body = {"checkbox": True} if prop_type == "checkbox" else {"select": {"name": "已同步"}}
            self.client.update_page(draft.page_id, {draft.synced_prop: body})
            did.append(f"勾选「{draft.synced_prop}」")

        callout = {
            "type": "callout",
            "callout": {
                "rich_text": rich_text_paragraph(payload.get("text", "")),
                "icon": {"type": "emoji", "emoji": "🔁"},
                "color": "blue_background",
            },
        }
        if draft.writeback_block_id:
            self.client.update_block(draft.writeback_block_id, callout)
            did.append("更新回执")
        else:
            self.client.append_children(draft.page_id, [callout])
            did.append("写入回执")
        return "，".join(did)


class FixtureSource:
    """离线数据源：目录里放 <名字>.page.json / <名字>.blocks.json / 音频文件。

    用于在没有 Notion 凭据时跑通并验证整条链路（含回归测试）。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not self.root.is_dir():
            raise RuntimeError(f"fixture 目录不存在: {self.root}")
        self.writebacks: list[dict] = []

    def describe(self) -> str:
        return f"Fixture {self.root}"

    def list_pages(self, limit: int | None = None) -> list[dict]:
        pages = []
        for f in sorted(self.root.glob("*.page.json")):
            pages.append(json.loads(f.read_text(encoding="utf-8")))
            if limit and len(pages) >= limit:
                break
        return pages

    def _blocks_file(self, page_id: str) -> Path | None:
        for f in sorted(self.root.glob("*.blocks.json")):
            if f.name.startswith(page_id[:8]):
                return f
        return None

    def load_bundle(self, page: dict) -> PageBundle:
        path = self._blocks_file(page.get("id", ""))
        blocks = json.loads(path.read_text(encoding="utf-8")) if path else []
        # fixture 也支持「子页面 = 转录」：同名前缀 .child.md 直接当文字稿
        child_texts = []
        for f in sorted(self.root.glob(f"{page.get('id','')[:8]}*.child.md")):
            child_texts.append(f.read_text(encoding="utf-8"))
        return PageBundle(page, blocks, child_texts)

    def download_audio(self, ref: AudioRef, dest: Path) -> str:
        # fixture 里 url 写成 file:相对路径 或直接本地文件名
        src = self.root / ref.url.replace("file:", "")
        if not src.is_file():
            raise RuntimeError(f"fixture 音频不存在: {src}")
        dest.write_bytes(src.read_bytes())
        return str(dest)

    def writeback(self, draft: MeetingDraft, payload: dict) -> str:
        self.writebacks.append({"page_id": draft.page_id, "text": payload.get("text", ""),
                                "synced_prop": draft.synced_prop})
        return "fixture 记录反写"


# ---------------------------------------------------------------- 同步主流程


def _default_extract(transcript: str, client_name: str, meeting_id: str):
    from .extract import extract_tags

    return extract_tags(transcript, client_name=client_name, meeting_id=meeting_id)


def _default_transcribe(audio_path: str) -> str:
    from .transcribe import transcribe

    out = transcribe(audio_path, verbose=True)
    return out.get("text", "")


def _default_ingest(store, payload: dict) -> dict:
    from .server import ingest_meeting

    return ingest_meeting(store, payload)


def writeback_text(draft: MeetingDraft, report: dict) -> str:
    stats = (f"客户「{draft.client_name}」· 本次写入 {report.get('tags_written', 0)} 条标签"
             f"（雷区 {report.get('landmines', 0)} 条，证据回验通过 "
             f"{report.get('evidence_verified', 0)} 条）")
    lines = [f"{WRITEBACK_MARKER} 已同步到 SalesVoice · {stats}"]
    top = report.get("top_landmines") or []
    if top:
        lines.append("重点雷区：" + "；".join(top))
    lines.append(f"看板：http://127.0.0.1:{os.environ.get('SALESVOICE_PORT', '8777')}")
    return "\n".join(lines)


def sync_notion(store, source=None, dry_run: bool = False, limit: int | None = None,
                page_id: str | None = None, writeback: bool | None = None,
                force: bool = False, extract_fn=None, transcribe_fn=None,
                ingest_fn=None, verbose: bool = True) -> dict:
    """扫描投放区 → 转写/抽取 → 入库。返回报告（可直接 JSON 序列化）。"""
    extract_fn = extract_fn or _default_extract
    transcribe_fn = transcribe_fn or _default_transcribe
    ingest_fn = ingest_fn or _default_ingest
    writeback = config.NOTION_WRITEBACK if writeback is None else writeback
    source = source or NotionApiSource()

    report: dict = {"ok": True, "source": source.describe(), "dry_run": dry_run,
                    "scanned": 0, "ingested": 0, "skipped": 0, "failed": 0, "results": []}

    pages = source.list_pages(limit=None)
    if page_id:
        want = page_id.replace("-", "").lower()
        pages = [p for p in pages if p.get("id", "").replace("-", "").lower() == want] or pages
    if limit:
        pages = pages[:limit]

    for page in pages:
        report["scanned"] += 1
        res: dict = {"page_id": page.get("id", ""), "status": ""}
        try:
            bundle = source.load_bundle(page)
            draft = build_draft(bundle.page, bundle.blocks, bundle.child_texts)
            res["title"] = draft.title or draft.client_name
            digest = content_hash(draft)

            prev = store.notion_sync_get(draft.page_id)
            if prev and prev.get("content_hash") == digest and not force:
                res["status"] = "skipped"
                res["detail"] = "内容未变，已同步过"
                report["skipped"] += 1
                report["results"].append(res)
                if verbose:
                    print(f"  · 跳过（未变化）：{res['title']}")
                continue

            # 决定转录来源：文字稿优先，否则下载音频本机转写
            transcript = draft.transcript
            audio_used = ""
            if len(transcript.strip()) < MIN_TRANSCRIPT_CHARS and draft.audios:
                ref = draft.audios[0]
                dest = config.AUDIO_DIR / f"notion-{draft.page_id[:8]}-{ref.name or 'audio'}"
                if verbose:
                    print(f"  · 本机转写音频：{ref.name or ref.url[:60]}")
                if dry_run:
                    audio_used = ref.name or "(音频)"
                    transcript = "（dry-run：未下载转写）"
                else:
                    path = source.download_audio(ref, dest)
                    transcript = transcribe_fn(path)
                    audio_used = path
                    dest_note = f"[转录自音频 {Path(path).name}]\n"
                    transcript = dest_note + transcript

            if len(transcript.strip()) < MIN_TRANSCRIPT_CHARS and not transcript.startswith("（dry-run"):
                res["status"] = "skipped"
                res["detail"] = "既没有文字稿也没有可用的音频"
                report["skipped"] += 1
                report["results"].append(res)
                if verbose:
                    print(f"  · 跳过（无内容）：{res['title']}")
                continue

            res.update({"status": "ingested", "chars": len(transcript),
                        "audio": audio_used, "content_hash": digest,
                        "client_name": draft.client_name, "met_on": draft.met_on})

            if dry_run:
                res["status"] = "would-ingest"
                report["ingested"] += 1
                report["results"].append(res)
                if verbose:
                    print(f"  · 待入库：{draft.client_name} / {draft.met_on} / {len(transcript)} 字"
                          f"（音频 {len(draft.audios)} 个）")
                continue

            payload = {
                "client_name": draft.client_name or "未命名客户",
                "company": draft.company, "title": draft.job_title,
                "industry": draft.industry, "met_on": draft.met_on or None,
                "location": draft.location, "attendees": draft.attendees,
                "transcript": transcript, "audio_path": audio_used,
            }
            result = ingest_fn(store, payload)
            tags = result.get("tags") or []
            top = [f"{t['key']}：{t['value'][:24]}" for t in tags
                   if t.get("category") == "landmine" and t.get("severity") in ("high", "medium")]
            res.update({"client_id": result.get("client_id", ""),
                        "meeting_id": result.get("meeting_id", ""),
                        "tags_written": result.get("tags_written", 0),
                        "landmines": result.get("landmines", 0),
                        "evidence_verified": result.get("evidence_verified", 0)})

            store.notion_sync_upsert(
                page_id=draft.page_id, client_id=res.get("client_id", ""),
                meeting_id=res.get("meeting_id", ""), title=res.get("title", ""),
                content_hash=digest, status="ingested",
                message=f"{res.get('tags_written', 0)} 条标签")

            if writeback:
                try:
                    res["writeback"] = source.writeback(draft, {
                        "text": writeback_text(draft, {**result, "top_landmines": top[:3]}),
                        "_synced_prop_type": "checkbox"})
                except Exception as exc:  # noqa: BLE001  反写失败不应让入库结果作废
                    res["writeback"] = f"失败：{exc}"

            report["ingested"] += 1
            report["results"].append(res)
            if verbose:
                print(f"  ✓ 入库：{draft.client_name} / {draft.met_on} → "
                      f"{res.get('tags_written', 0)} 条标签（雷区 {res.get('landmines', 0)}，"
                      f"回验 {res.get('evidence_verified', 0)}）"
                      + (f" · 反写：{res.get('writeback')}" if res.get("writeback") else ""))

        except Exception as exc:  # noqa: BLE001  单页失败不中断整批
            res["status"] = "failed"
            res["error"] = f"{type(exc).__name__}: {exc}"
            report["failed"] += 1
            report["results"].append(res)
            if verbose:
                print(f"  ✗ 失败：{res.get('title') or res['page_id']} → {res['error']}")
            try:
                store.notion_sync_upsert(page_id=res["page_id"], title=res.get("title", ""),
                                         status="failed", message=res["error"])
            except Exception:  # noqa: BLE001
                pass

    report["ok"] = report["failed"] == 0
    return report


def list_sources(limit: int = 30, verbose: bool = True) -> list[dict]:
    """列出集成可见的投放区候选（数据库优先），供人工挑选。"""
    from .notion_client import NotionClient

    client = NotionClient()
    out: list[dict] = []
    for obj in client.search("", page_size=100):
        if obj.get("object") == "data_source":
            title = "".join(t.get("plain_text", "") for t in obj.get("title", []))
            out.append({"kind": "data_source", "id": obj.get("id", ""),
                        "title": title or "(未命名数据库)", "parent": "database"})
        elif obj.get("object") == "database":
            title = "".join(t.get("plain_text", "") for t in obj.get("title", []))
            out.append({"kind": "database", "id": obj.get("id", ""),
                        "title": title or "(未命名数据库)", "parent": "workspace"})
        elif obj.get("object") == "page":
            props = obj.get("properties") or {}
            title = ""
            for p in props.values():
                if p.get("type") == "title":
                    title = rich_text(p.get("title"))
                    break
            out.append({"kind": "page", "id": obj.get("id", ""),
                        "title": title or "(无标题页面)"})
    out = out[:limit]
    if verbose:
        print(f"集成可见的候选投放区（{len(out)} 个）：")
        for i, o in enumerate(out, 1):
            print(f"  {i:2d}. [{o['kind']:11s}] {o['title']}")
            print(f"      id: {o['id']}")
        print("\n选定后设置：export SALESVOICE_NOTION_SOURCE=<id 或页面 URL>")
    return out
