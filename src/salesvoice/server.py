"""极简本地 HTTP 后端（仅用 Python 标准库，零额外依赖）。

前端 `web/index.html` 是单文件看板，通过这里的 JSON API 读写中台数据。
这样既保持了"HTML 界面"的轻量，又具备"数据可写回、可累积"的能力。

启动：  python -m salesvoice.server         (默认 http://127.0.0.1:8777)
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config
from .store import Store

DEFAULT_PORT = 8777
_INGEST_LOCK = threading.Lock()


# ---------------------------------------------------------------- 业务动作


def ingest_meeting(store: Store, payload: dict) -> dict:
    """一次完整入库：转录文本 → 标签抽取 → 中台。

    payload: {client_name, met_on, transcript, company?, title?, industry?,
              location?, attendees?, summary?, client_id?}
    """
    from .extract import extract_tags, summarize_meeting

    client_name = (payload.get("client_name") or "").strip()
    transcript = (payload.get("transcript") or "").strip()
    if not client_name:
        raise ValueError("缺少 client_name")
    if not transcript:
        raise ValueError("缺少 transcript（转录文本）")

    met_on = payload.get("met_on") or datetime.now().strftime("%Y-%m-%d")

    with _INGEST_LOCK:
        cid = store.upsert_client(
            name=client_name,
            company=payload.get("company", ""),
            title=payload.get("title", ""),
            industry=payload.get("industry", ""),
            client_id=payload.get("client_id"),
        )

        summary = payload.get("summary")
        if not summary and payload.get("auto_summary", True):
            s = summarize_meeting(transcript, client_name)
            summary = s.get("summary", "") if isinstance(s, dict) else str(s)

        mid = store.add_meeting(
            cid, met_on, transcript=transcript,
            summary=summary or "",
            location=payload.get("location", ""),
            attendees=payload.get("attendees", ""),
            audio_path=payload.get("audio_path", ""),
        )

        tags = extract_tags(transcript, client_name=client_name, meeting_id=mid,
                            engine=payload.get("extract_engine", "default"))
        n = store.add_tags(cid, tags, meeting_id=mid)

    return {
        "ok": True,
        "client_id": cid,
        "meeting_id": mid,
        "tags_written": n,
        "landmines": sum(1 for t in tags if t.category == "landmine"),
        "evidence_verified": sum(1 for t in tags if t.evidence_verified),
        "tags": [t.to_dict() for t in tags],
    }


def client_advice(store: Store, client_id: str, advice_type: str) -> dict:
    from .suggest import generate_advice

    profile = store.profile(client_id)
    return generate_advice(profile, advice_type)


def landmine_check(store: Store, client_id: str, scenario: str) -> dict:
    from .suggest import quick_landmine_check

    return quick_landmine_check(store.profile(client_id), scenario)


# ---------------------------------------------------------------- Notion 同步

_NOTION_SYNC: dict = {"running": False, "started_at": "", "finished_at": "",
                      "report": None, "error": ""}
_NOTION_LOCK = threading.Lock()


def notion_status(store: Store) -> dict:
    return {
        "has_key": config.has_notion_key(),
        "source": config.NOTION_SOURCE,
        "source_set": bool(config.NOTION_SOURCE),
        "writeback": config.NOTION_WRITEBACK,
        "running": _NOTION_SYNC["running"],
        "started_at": _NOTION_SYNC["started_at"],
        "finished_at": _NOTION_SYNC["finished_at"],
        "error": _NOTION_SYNC["error"],
        "last_report": _NOTION_SYNC["report"],
        "ledger": store.notion_sync_list(10),
    }


def start_notion_sync(store: Store, limit: int | None = None, dry_run: bool = False) -> dict:
    """在后台线程里跑一次 Notion 同步。

    同步会下载音频 + 本机转写 + 多次 LLM 调用，动辄几分钟，
    所以 HTTP 请求只负责「启动」，进度由 GET /api/notion 反映。
    """
    with _NOTION_LOCK:
        if _NOTION_SYNC["running"]:
            return {"ok": False, "error": "已有同步任务在跑，请等它结束"}
        if not config.has_notion_key():
            return {"ok": False,
                    "error": "未配置 Notion 集成 token：把 NOTION_API_KEY 写进 ~/.hermes/.env"}
        if not config.NOTION_SOURCE:
            return {"ok": False,
                    "error": "未设置投放区：设置 SALESVOICE_NOTION_SOURCE 为那个数据库/页面"}
        _NOTION_SYNC.update({"running": True, "error": "", "report": None,
                             "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "finished_at": ""})

    def _run() -> None:
        from .notion_sync import NotionApiSource, sync_notion

        try:
            report = sync_notion(store, source=NotionApiSource(), limit=limit,
                                 dry_run=dry_run, verbose=True)
            _NOTION_SYNC["report"] = report
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _NOTION_SYNC["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _NOTION_SYNC["running"] = False
            _NOTION_SYNC["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=_run, daemon=True, name="notion-sync").start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "SalesVoice/0.1"
    store: Store  # 由 serve() 注入

    # ---- 工具

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # ---- 路由

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)

            if p == "/api/health":
                from . import __version__
                return self._json({"ok": True, "version": __version__,
                                   "llm": config.LLM_BASE_URL, "model": config.LLM_MODEL,
                                   "has_key": config.has_api_key()})

            if p == "/api/clients":
                return self._json({"ok": True, "clients": self.store.list_clients()})

            m = re.fullmatch(r"/api/clients/([\w]+)", p)
            if m:
                return self._json({"ok": True, **self.store.profile(m.group(1))})

            m = re.fullmatch(r"/api/clients/([\w]+)/advice", p)
            if m:
                atype = (q.get("type") or ["brief"])[0]
                return self._json({"ok": True, **client_advice(self.store, m.group(1), atype)})

            m = re.fullmatch(r"/api/clients/([\w]+)/tags", p)
            if m:
                status = (q.get("status") or ["active"])[0]
                return self._json({"ok": True, "tags": self.store.get_tags(m.group(1), status)})

            if p == "/api/search":
                return self._json({"ok": True, "results": self.store.search((q.get("q") or [""])[0])})

            if p == "/api/taxonomy":
                from .schema import CATEGORIES
                from .suggest import ADVICE_TYPES
                return self._json({"ok": True, "categories": CATEGORIES, "advice_types": ADVICE_TYPES})

            if p == "/api/notion":
                return self._json({"ok": True, **notion_status(self.store)})

            if p.startswith("/api/"):
                return self._err("未知接口", 404)

            return self._static(p)

        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._err(f"服务器错误: {exc}", 500)

    def do_POST(self):  # noqa: N802
        try:
            u = urlparse(self.path)
            p = u.path

            if p == "/api/ingest":
                return self._json(ingest_meeting(self.store, self._body()))

            m = re.fullmatch(r"/api/clients/([\w]+)/meetings", p)
            if m:
                b = self._body()
                b["client_id"] = m.group(1)
                return self._json(ingest_meeting(self.store, b))

            m = re.fullmatch(r"/api/clients/([\w]+)/check", p)
            if m:
                b = self._body()
                return self._json({"ok": True,
                                   **landmine_check(self.store, m.group(1), b.get("scenario", ""))})

            if p == "/api/transcribe":
                from .transcribe import transcribe_payload
                return self._json(transcribe_payload(self._body()))

            if p == "/api/notion/sync":
                b = self._body()
                return self._json(start_notion_sync(self.store, limit=b.get("limit"),
                                                    dry_run=bool(b.get("dry_run"))))

            return self._err("未知接口", 404)

        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._err(f"{type(exc).__name__}: {exc}", 500)

    def do_PATCH(self):  # noqa: N802
        try:
            b = self._body()
            m = re.fullmatch(r"/api/tags/([\w]+)", urlparse(self.path).path)
            if m:
                ok = self.store.set_tag_status(m.group(1), b.get("status", "active"))
                return self._json({"ok": ok})
            return self._err("未知接口", 404)
        except Exception as exc:  # noqa: BLE001
            self._err(str(exc), 500)

    def do_DELETE(self):  # noqa: N802
        try:
            m = re.fullmatch(r"/api/tags/([\w]+)", urlparse(self.path).path)
            if m:
                return self._json({"ok": self.store.delete_tag(m.group(1))})
            return self._err("未知接口", 404)
        except Exception as exc:  # noqa: BLE001
            self._err(str(exc), 500)

    # ---- 静态文件

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        if not str(target).startswith(str(config.WEB_DIR.resolve())) or not target.is_file():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("404 Not Found".encode())
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    Handler.store = Store()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"客户语音情报中台已启动 → http://{host}:{port}")
    print(f"  数据: {config.DB_PATH}")
    print(f"  LLM : {config.LLM_BASE_URL} / {config.LLM_MODEL}  (凭据: {'已配置' if config.has_api_key() else '缺失'})")
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    import sys

    serve(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)
