"""客户情报中台：SQLite 存储层。

与所有现有开源方案的根本差异：这里是**客户级、跨会面累积**的记忆，
而不是"一次会议的纪要"。同一个客户每次见面的情报都会叠加到他的画像上，
新信息可以取代旧信息（supersede），形成可演进的关系档案。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .schema import CATEGORIES, Tag

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clients (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    company     TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    industry    TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id          TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    met_on      TEXT NOT NULL,          -- 会面日期 YYYY-MM-DD
    location    TEXT DEFAULT '',
    attendees   TEXT DEFAULT '',
    audio_path  TEXT DEFAULT '',
    transcript  TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id            TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    meeting_id    TEXT REFERENCES meetings(id) ON DELETE SET NULL,
    category      TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    evidence      TEXT DEFAULT '',
    confidence    REAL DEFAULT 0.8,
    speaker       TEXT DEFAULT '',
    severity      TEXT DEFAULT '',
    trigger_when  TEXT DEFAULT '',
    evidence_ok   INTEGER DEFAULT 0,
    evidence_mode TEXT DEFAULT '',          -- exact / fuzzy / ''
    status        TEXT DEFAULT 'active',   -- active | superseded | dismissed
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_client   ON tags(client_id, status);
CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(client_id, category);
CREATE INDEX IF NOT EXISTS idx_meet_client   ON meetings(client_id, met_on);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    """中台存储。所有写操作都落盘到单个 SQLite 文件。

    线程安全：HTTP 服务器是多线程的，因此这里给每个线程分配独立连接
    （配合 WAL 日志模式，读写可并发），而不是共享一个 connection。
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的数据库连接（懒创建）。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.db_path), timeout=30.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            try:
                c.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
            self._local.conn = c
        return c

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        # 轻量迁移：给早期版本创建的库补上新增列
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tags)")}
        if "evidence_mode" not in cols:
            self.conn.execute("ALTER TABLE tags ADD COLUMN evidence_mode TEXT DEFAULT ''")
        self.conn.commit()

    # ------------------------------------------------------------ 客户

    def upsert_client(self, name: str, company: str = "", title: str = "",
                      industry: str = "", note: str = "", client_id: str | None = None) -> str:
        cur = self.conn.cursor()
        if client_id:
            row = cur.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
        else:
            row = cur.execute("SELECT id FROM clients WHERE name=?", (name,)).fetchone()
        if row:
            cid = row["id"]
            cur.execute(
                "UPDATE clients SET name=?, company=COALESCE(NULLIF(?,''),company), "
                "title=COALESCE(NULLIF(?,''),title), industry=COALESCE(NULLIF(?,''),industry), "
                "note=COALESCE(NULLIF(?,''),note), updated_at=? WHERE id=?",
                (name, company, title, industry, note, _now(), cid),
            )
        else:
            cid = client_id or _new_id("cli")
            cur.execute(
                "INSERT INTO clients (id,name,company,title,industry,note,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, name, company, title, industry, note, _now(), _now()),
            )
        self.conn.commit()
        return cid

    def list_clients(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT c.*,
                 (SELECT COUNT(*) FROM meetings m WHERE m.client_id=c.id) AS meeting_count,
                 (SELECT COUNT(*) FROM tags t WHERE t.client_id=c.id AND t.status='active') AS tag_count,
                 (SELECT COUNT(*) FROM tags t WHERE t.client_id=c.id AND t.status='active'
                    AND t.category='landmine') AS landmine_count,
                 (SELECT MAX(met_on) FROM meetings m WHERE m.client_id=c.id) AS last_met
               FROM clients c ORDER BY c.updated_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 会面

    def add_meeting(self, client_id: str, met_on: str, transcript: str = "",
                    summary: str = "", location: str = "", attendees: str = "",
                    audio_path: str = "") -> str:
        mid = _new_id("mtg")
        self.conn.execute(
            "INSERT INTO meetings (id,client_id,met_on,location,attendees,audio_path,"
            "transcript,summary,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, client_id, met_on, location, attendees, audio_path, transcript,
             summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False), _now()),
        )
        self.conn.execute("UPDATE clients SET updated_at=? WHERE id=?", (_now(), client_id))
        self.conn.commit()
        return mid

    def get_meetings(self, client_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM meetings WHERE client_id=? ORDER BY met_on DESC", (client_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 标签

    def add_tags(self, client_id: str, tags: list[Tag], meeting_id: str | None = None,
                 supersede_same_key: bool = True) -> int:
        """写入标签。同 key 的旧标签默认标记为 superseded（信息迭代）。"""
        cur = self.conn.cursor()
        n = 0
        for t in tags:
            if supersede_same_key:
                cur.execute(
                    "UPDATE tags SET status='superseded' WHERE client_id=? AND key=? "
                    "AND status='active' AND value<>?",
                    (client_id, t.key, t.value),
                )
            cur.execute(
                "INSERT INTO tags (id,client_id,meeting_id,category,key,value,evidence,"
                "confidence,speaker,severity,trigger_when,evidence_ok,evidence_mode,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("tag"), client_id, meeting_id or t.meeting_id or None,
                 t.category, t.key, t.value, t.evidence, t.confidence, t.speaker,
                 t.severity, t.trigger, 1 if t.evidence_verified else 0,
                 getattr(t, "evidence_mode", "") or "", "active", _now()),
            )
            n += 1
        cur.execute("UPDATE clients SET updated_at=? WHERE id=?", (_now(), client_id))
        self.conn.commit()
        return n

    def get_tags(self, client_id: str, status: str = "active") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tags WHERE client_id=? AND status=? ORDER BY category, confidence DESC",
            (client_id, status),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_tag_status(self, tag_id: str, status: str) -> bool:
        cur = self.conn.execute("UPDATE tags SET status=? WHERE id=?", (status, tag_id))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_tag(self, tag_id: str) -> bool:
        """硬删除 —— 用于合规场景下移除敏感条目。"""
        cur = self.conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 画像聚合

    def profile(self, client_id: str) -> dict:
        """客户完整画像：基本信息 + 按类别分组的活跃标签 + 会面时间线。"""
        c = self.conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not c:
            raise KeyError(f"客户不存在: {client_id}")
        tags = self.get_tags(client_id, "active")
        grouped: dict[str, list] = {cid: [] for cid in CATEGORIES}
        for t in tags:
            grouped.setdefault(t["category"], []).append(t)
        return {
            "client": dict(c),
            "categories": {
                cid: {"label": CATEGORIES.get(cid, {}).get("label", cid),
                      "color": CATEGORIES.get(cid, {}).get("color", "#888"),
                      "items": items}
                for cid, items in grouped.items()
            },
            "meetings": self.get_meetings(client_id),
            "stats": {
                "tags": len(tags),
                "landmines": sum(1 for t in tags if t["category"] == "landmine"),
                "high_severity": sum(1 for t in tags if t["severity"] == "high"),
                "verified_rate": round(
                    sum(1 for t in tags if t["evidence_ok"]) / len(tags), 3) if tags else 0.0,
                "fuzzy": sum(1 for t in tags if (t.get("evidence_mode") or "") == "fuzzy"),
                "unverified": sum(1 for t in tags if not t["evidence_ok"]),
            },
            "superseded": self.get_tags(client_id, "superseded"),
        }

    def search(self, query: str) -> list[dict]:
        """跨客户全文检索标签与转录（'谁提过白酒' 这类问题）。"""
        q = f"%{query}%"
        rows = self.conn.execute(
            """SELECT t.*, c.name AS client_name FROM tags t JOIN clients c ON c.id=t.client_id
               WHERE t.status='active' AND (t.key LIKE ? OR t.value LIKE ? OR t.evidence LIKE ?)
               ORDER BY t.confidence DESC LIMIT 50""",
            (q, q, q),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
