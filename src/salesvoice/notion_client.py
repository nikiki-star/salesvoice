"""Notion API 客户端（仅标准库，零新增依赖）。

只做 salesvoice 需要的事：查数据源、读页面与块、下载音频、创建投放区数据库、反写同步状态。
token 只从 config 读，**不打印、不写日志**。

用法：
    c = NotionClient()                      # token 取自环境变量 / ~/.hermes/.env
    c.check()                               # 1 次 /users/me 验证凭据
    for page in c.iter_pages(source_id):    # 自动分页
        ...

错误处理：所有失败都抛 NotionError，且把 Notion 的 message 原样带出来，
并按状态码补一句人话提示（401 凭据不对、403 没权限、404 页面没共享给集成 —— 这是 90% 的坑）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from . import config

API_BASE = "https://api.notion.com/v1"
_NOTION_HOSTS = ("notion.so", "notion.site", "app.notion.com", "www.notion.so")

_HINT = {
    401: "凭据无效：确认 NOTION_API_KEY 是「内部集成」的 token（ntn_/secret_ 开头）且未失效。",
    403: "集成缺少权限：在 Notion 里把该页面/数据库「连接」给这个集成，或给集成开更多能力。",
    404: "Notion 返回 404：通常是**这个页面/数据库没有共享给集成**（页面右上角 … → 连接 → 选集成），"
         "或 ID 抄错了（注意 data source ID 与 database ID 不是同一个）。",
    429: "触发频率限制（约 3 请求/秒）：稍后重试，或调大同步间隔。",
}


class NotionError(RuntimeError):
    def __init__(self, status: int, message: str, path: str = ""):
        self.status = status
        self.message = message
        hint = _HINT.get(status, "")
        super().__init__(f"Notion API {status} {path}: {message}" + (f"\n  提示：{hint}" if hint else ""))


# ---------------------------------------------------------------- ID 解析


def extract_id(value: str) -> str:
    """从 URL / 带连字符 UUID / 32 位裸 ID 里取出规范化 ID。

    支持：
      https://www.notion.so/ws/My-DB-1f2a3b4c5d6e7f8091a2b3c4d5e6f708?v=...
      https://www.notion.so/1f2a3b4c5d6e7f8091a2b3c4d5e6f708
      1f2a3b4c-5d6e-7f80-91a2-b3c4d5e6f708
      1f2a3b4c5d6e7f8091a2b3c4d5e6f708

    注意 slug 陷阱：页面 URL 常是 `My-DB-<id>`，而 `D`/`B` 本身就是十六进制字符，
    直接搜「连续 32 位十六进制」会把 slug 的尾巴吃进 ID 里。
    所以只在**最后一段路径**里、且要求 32 位之后不再是字母数字，才认。
    """
    if not value:
        return ""
    v = value.strip()

    # 1) 纯 UUID（带连字符）
    m = re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v)
    if m:
        return v.lower()

    # 2) 去掉 query/fragment，只看最后一段路径
    seg = v.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
    compact = seg.replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", compact):
        return _dash(compact.lower())
    m = re.search(r"([0-9a-fA-F]{32})(?![0-9a-zA-Z])", compact)
    if m:
        return _dash(m.group(1).lower())

    # 3) 兜底：整串里找带连字符的 UUID
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v)
    if m:
        return m.group(0).lower()
    return v


def _dash(hex32: str) -> str:
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:]}"


def looks_like_url(value: str) -> bool:
    return any(h in (value or "") for h in _NOTION_HOSTS)


def rich_text_paragraph(text: str, limit: int = 1900) -> list[dict]:
    """把文本切成 Notion rich_text 片段（单个 rich_text 元素上限 2000 字符）。

    按行边界切，避免把句子劈开；返回可直接塞进 callout/paragraph 的数组。
    """
    items: list[dict] = []
    buf = ""
    for line in (text or "").split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            items.append({"type": "text", "text": {"content": buf}})
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        items.append({"type": "text", "text": {"content": buf}})
    return items or [{"type": "text", "text": {"content": ""}}]


# ---------------------------------------------------------------- 客户端


class NotionClient:
    def __init__(self, token: str | None = None, version: str | None = None,
                 timeout: int = 60):
        self.token = token or config.get_notion_key()
        self.version = version or config.NOTION_VERSION
        self.timeout = timeout

    # ---- 底层

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None) -> dict:
        url = f"{API_BASE}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Notion-Version", self.version)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(raw).get("message", raw)
            except json.JSONDecodeError:
                msg = raw[:300]
            raise NotionError(e.code, msg, path) from None

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body=body)

    def patch(self, path: str, body: dict) -> dict:
        return self._request("PATCH", path, body=body)

    def check(self) -> dict:
        """验证凭据：返回集成自身信息（bot 用户）。"""
        me = self.get("users/me")
        return {"name": me.get("name", ""), "type": me.get("type", ""), "id": me.get("id", "")}

    # ---- 搜索与数据源

    def search(self, query: str = "", object_type: str = "", page_size: int = 100) -> list[dict]:
        body: dict = {"page_size": page_size}
        if query:
            body["query"] = query
        if object_type in ("page", "data_source"):
            body["filter"] = {"property": "object", "value": object_type}
        out, cursor = [], None
        while True:
            if cursor:
                body["start_cursor"] = cursor
            res = self.post("search", body)
            out.extend(res.get("results", []))
            if not res.get("has_more"):
                return out
            cursor = res.get("next_cursor")

    def source_kind(self, source_id: str) -> str:
        """判断投放区是数据库（data source）还是普通页面。"""
        try:
            self.get(f"data_sources/{source_id}")
            return "data_source"
        except NotionError as e:
            if e.status != 404:
                raise
        try:
            self.get(f"databases/{source_id}")
            return "database"
        except NotionError as e:
            if e.status != 404:
                raise
        return "page"

    def data_source_properties(self, data_source_id: str) -> dict:
        try:
            return self.get(f"data_sources/{data_source_id}").get("properties", {})
        except NotionError as e:
            if e.status != 404:
                raise
        return self.get(f"databases/{data_source_id}").get("properties", {})

    def query_data_source(self, data_source_id: str, body: dict | None = None,
                          page_size: int = 100) -> Iterator[dict]:
        """查询数据库里的页面（自动分页）。"""
        req = dict(body or {})
        req["page_size"] = page_size
        cursor = None
        while True:
            if cursor:
                req["start_cursor"] = cursor
            try:
                res = self.post(f"data_sources/{data_source_id}/query", req)
            except NotionError as e:
                if e.status != 404:
                    raise
                res = self.post(f"databases/{data_source_id}/query", req)
            for r in res.get("results", []):
                yield r
            if not res.get("has_more"):
                return
            cursor = res.get("next_cursor")

    def list_child_pages(self, page_id: str) -> Iterator[dict]:
        """页面型投放区：把它下面的子页面当成一次「会面记录」。"""
        page = self.get(f"pages/{page_id}")
        for block in self.iter_children(page_id):
            if block.get("type") == "child_page":
                yield self.get(f"pages/{block['id']}")
        _ = page  # 保留父页面自身信息，便于调用方记录

    # ---- 页面与块

    def get_page(self, page_id: str) -> dict:
        return self.get(f"pages/{page_id}")

    def iter_children(self, block_id: str, page_size: int = 100) -> Iterator[dict]:
        cursor = None
        while True:
            params = {"page_size": page_size}
            if cursor:
                params["start_cursor"] = cursor
            res = self.get(f"blocks/{block_id}/children", **params)
            for b in res.get("results", []):
                yield b
            if not res.get("has_more"):
                return
            cursor = res.get("next_cursor")

    def update_block(self, block_id: str, payload: dict) -> dict:
        return self.patch(f"blocks/{block_id}", payload)

    def append_children(self, block_id: str, children: list[dict]) -> dict:
        out = {}
        for i in range(0, len(children), 100):    # API 单次上限 100 个块
            out = self.patch(f"blocks/{block_id}/children", {"children": children[i:i + 100]})
        return out

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self.patch(f"pages/{page_id}", {"properties": properties})

    # ---- 音频下载

    def download(self, url: str, dest) -> str:
        """下载（Notion 托管文件的 URL 是限时签名链接，拿到就尽快下）。"""
        from pathlib import Path

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "salesvoice/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        return str(dest)

    # ---- 一键建投放区

    def create_meeting_database(self, parent_page_id: str, title: str = "客户会面录音"):
        """在指定页面下建一个结构匹配 salesvoice 的数据库。

        返回 (database_id, data_source_id, properties)。
        注意：2025-09-03 版 API 里「数据库」被拆成 database + data source，
        先试 data_sources 端点，404 再退回 databases（不同工作区/版本会有差异）。
        """
        props = {
            "客户名称": {"title": {}},
            "公司": {"rich_text": {}},
            "日期": {"date": {}},
            "参与人": {"rich_text": {}},
            "地点": {"rich_text": {}},
            "转录": {"rich_text": {}},
            "录音": {"files": {}},
            "已同步": {"checkbox": {}},
        }
        payload = {"parent": {"page_id": extract_id(parent_page_id)},
                   "title": [{"text": {"content": title}}],
                   "properties": props}
        try:
            res = self.post("data_sources", payload)
        except NotionError as e:
            if e.status != 404:
                raise
            res = self.post("databases", payload)
        ds_id = res.get("data_source_id") or res.get("id", "")
        db_id = res.get("id", "")
        return db_id, ds_id, props
